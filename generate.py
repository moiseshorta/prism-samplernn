from __future__ import print_function
import argparse
import time
import json
import ast
import numbers

import tensorflow as tf
import numpy as np
import librosa

from samplernn import (SampleRNN, write_wav, quantize, dequantize, unsqueeze)


OUTPUT_DUR = 3 # Duration of generated audio in seconds
SAMPLE_RATE = 22050 # Sample rate of generated audio
NUM_SEQS = 1
SAMPLING_TEMPERATURE = ['0.95']
SEED_OFFSET = 0


def get_arguments():
    def check_positive(value):
        val = int(value)
        if val < 1:
             raise argparse.ArgumentTypeError("%s is not positive" % value)
        return val

    parser = argparse.ArgumentParser(description='PRiSM TensorFlow SampleRNN Generator')
    parser.add_argument('--output_path',                type=str,            required=True,
                                                        help='Path to the generated .wav file')
    parser.add_argument('--checkpoint_path',            type=str,            required=True,
                                                        help='Path to a saved checkpoint for the model')
    parser.add_argument('--config_file',                type=str,            required=True,
                                                        help='Path to the JSON config for the model')
    parser.add_argument('--dur',                        type=check_positive, default=OUTPUT_DUR,
                                                        help='Duration of generated audio')
    parser.add_argument('--num_seqs',                   type=check_positive, default=NUM_SEQS,
                                                        help='Number of audio sequences to generate')
    parser.add_argument('--sample_rate',                type=check_positive, default=SAMPLE_RATE,
                                                        help='Sample rate of the generated audio')
    parser.add_argument('--temperature',                type=str,            default=SAMPLING_TEMPERATURE, nargs='+',
                                                        help='Sampling temperature')
    parser.add_argument('--seed',                       type=str,            help='Path to audio for seeding')
    parser.add_argument('--seed_offset',                type=int,            default=SEED_OFFSET,
                                                        help='Starting offset of the seed audio')
    return parser.parse_args()


# On generation speed: https://github.com/soroushmehr/sampleRNN_ICLR2017/issues/19
# Speed again: https://ambisynth.blogspot.com/2018/09/wavernn.html
# On seeding (sort of): https://github.com/soroushmehr/sampleRNN_ICLR2017/issues/11
# Very interesting article on sampling temperature (including the idea of varying it
# while sampling): https://www.robinsloan.com/expressive-temperature/

def create_inference_model(ckpt_path, num_seqs, config):
    model = SampleRNN(
        batch_size = num_seqs, # Generate sequences in batches
        frame_sizes = config['frame_sizes'],
        seq_len = config['seq_len'],
        q_type = config['q_type'],
        q_levels = config['q_levels'],
        dim = config['dim'],
        rnn_type = config.get('rnn_type'),
        num_rnn_layers = config['num_rnn_layers'],
        emb_size = config['emb_size'],
        skip_conn = config.get('skip_conn'),
        rnn_dropout=config.get('rnn_dropout')
    )
    num_samps = config['seq_len'] + model.big_frame_size
    init_data = np.zeros((model.batch_size, num_samps, 1), dtype='int32')
    model(init_data)
    model.load_weights(ckpt_path).expect_partial()
    return model

def load_seed_audio(path, offset, length):
    (audio, _) = librosa.load(path, sr=None, mono=True)
    assert offset + length <= len(audio), 'Seed offset plus length exceeds audio length'
    chunk = audio[offset : offset + length]
    return chunk.reshape(-1, 1)

NUM_FRAMES_TO_PRINT = 4

def parse_temperature_vector(tvect, total_samps):
    fn = lambda x : int(x * total_samps)
    parsed = list(map(fn, tvect))
    parsed.sort()
    return parsed

def parse_temperature(temperature, num_seqs, total_samps):
    temperature = [ast.literal_eval(t) for t in temperature]
    temperature = [t if isinstance(t, numbers.Number)
                     else [parse_temperature_vector(t[0], total_samps), t[1]]
                     for t in temperature]
    if num_seqs > 1:
        if len(temperature) < num_seqs:
            last_val = temperature[-1]
            while len(temperature) < num_seqs:
                temperature = temperature + [last_val]
        elif len(temperature) > num_seqs:
            temperature = temperature[:num_seqs]
    return temperature

def interpolgen(nsamps, xpoints, ypoints):
    dx = xpoints[-1] / nsamps
    pairs = zip(xpoints, xpoints[1:], ypoints, ypoints[1:])
    for (xi, xj, yi, yj) in pairs:
        xdiff = xj - xi
        ydiff = yj - yi
        xn = xdiff / dx
        dy = ydiff / xn
        cx = xi - dx
        cy = yi - dy
        for _ in range(xi, xi + round(xn)):
            cx = cx + dx
            cy = cy + dy
            yield(cx, cy)

def get_temperature(parsed_temp, nsamps):
    temps = [t if isinstance(t, numbers.Number)
               else interpolgen(nsamps, t[0], t[1])
               for t in parsed_temp]
    for _ in range(0, nsamps):
        '''
        We might have a discrepancy between the value of nsamps (actually
        the number of top tier frames) and the size of temps (due to precision
        inside interpolgen). So we catch StopIteration here. But we need a
        better solution. Same issue with the main loop inside generate() below.
        '''
        try:
            out = [[t] if isinstance(t, numbers.Number)
                    else [next(t)[1]]
                    for t in temps]
            yield(tf.constant(out, dtype=tf.float64))
        except StopIteration:
            break

def generate(path, ckpt_path, config, num_seqs=NUM_SEQS, dur=OUTPUT_DUR, sample_rate=SAMPLE_RATE,
             temperature=SAMPLING_TEMPERATURE, seed=None, seed_offset=None):
    model = create_inference_model(ckpt_path, num_seqs, config)
    q_type = model.q_type
    q_levels = model.q_levels
    q_zero = q_levels // 2
    num_samps = dur * sample_rate
    # Precompute sample sequences, initialised to q_zero.
    samples = []
    init_samples = np.full((model.batch_size, model.big_frame_size, 1), q_zero)
    # Set seed if provided.
    if seed is not None:
        seed_audio = load_seed_audio(seed, seed_offset, model.big_frame_size)
        seed_audio = tf.convert_to_tensor(seed_audio)
        init_samples[:, :model.big_frame_size, :] = quantize(seed_audio, q_type, q_levels)
    init_samples = tf.constant(init_samples, dtype=tf.int32)
    samples.append(init_samples)
    num_frames = num_samps // model.big_frame_size
    parsed_temps = parse_temperature(temperature, num_seqs, dur * sample_rate)
    temperature = get_temperature(parsed_temps, num_frames)
    print_progress_every = NUM_FRAMES_TO_PRINT * model.big_frame_size
    start_time = time.time()
    for i in range(0, num_frames):
        try:
            t = i * model.big_frame_size
            # Generate samples
            frame_samples = model(samples[i], training=False, temperature=next(temperature))
            samples.append(frame_samples)
            # Monitor progress
            if t % print_progress_every == 0:
                end = min(t + print_progress_every, num_samps)
                step_dur = time.time() - start_time
                print(f'Generated samples {t+1} - {end} of {num_samps} (time elapsed: {step_dur:.3f} seconds)')
        except StopIteration:
            break
    samples = tf.concat(samples, axis=1)
    samples = samples[:, model.big_frame_size:, :]
    # Save sequences to disk
    epoch = ckpt_path.split('/model.ckpt-')[-1]
    path = f'{path.split(".wav")[0]}_e={epoch}'
    temperature = [t if isinstance(t, numbers.Number)
                     else t[1]
                     for t in parsed_temps]
    for i in range(model.batch_size):
        seq = np.reshape(samples[i], (-1, 1))[model.big_frame_size :].tolist()
        audio = dequantize(seq, q_type, q_levels)
        file_name = f'{path}({i})' if model.batch_size > 1 else path
        file_name = f'{file_name}_t={temperature[i]}.wav'
        write_wav(file_name, audio, sample_rate)
        print(f'Generated sample output to {file_name}')
    print('Done')


def main():
    args = get_arguments()
    with open(args.config_file, 'r') as config_file:
        config = json.load(config_file)
    generate(args.output_path, args.checkpoint_path, config, args.num_seqs, args.dur,
             args.sample_rate, args.temperature, args.seed, args.seed_offset)


if __name__ == '__main__':
    main()