#!/usr/bin/env python3
"""WAV batch filter GUI with optional drag-and-drop.

Features:
- Batch WAV processing from files/folders
- Linear-phase FIR EQ (high-pass + low-pass)
- Optional texture layer (pink / room / vinyl)
- Optional mild dynamic humanization
- Optional random channel-offset "stem" effect
"""

from __future__ import annotations

import json
import math
import multiprocessing
import os
import pathlib
import queue
import random
import struct
import threading
import tkinter as tk
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from tkinter import filedialog, messagebox, ttk
from typing import List, Sequence, Tuple
import wave

try:
    import numpy as np

    HAS_NUMPY = True
except ImportError:
    np = None
    HAS_NUMPY = False


try:
    from tkinterdnd2 import DND_FILES, TkinterDnD

    HAS_DND = True
except ImportError:
    DND_FILES = None
    TkinterDnD = None
    HAS_DND = False


DEFAULT_LOW_PASS_HZ = 13_000.0
DEFAULT_HIGH_PASS_HZ = 120.0
DEFAULT_EQ_TAPS = 63
DEFAULT_WINDOW_SIZE = "980x760"
APP_TITLE = "WAV Linear-Phase EQ + Texture Tool"
SETTINGS_DIR_NAME = "WAVLinearPhaseEQ"
SETTINGS_FILE_NAME = "settings.json"


class WavReadError(Exception):
    """Raised when a WAV file cannot be decoded in supported PCM formats."""


def parse_drop_payload(payload: str) -> List[pathlib.Path]:
    paths: List[str] = []
    current = ""
    in_braces = False
    for char in payload.strip():
        if char == "{":
            in_braces = True
            current = ""
            continue
        if char == "}":
            in_braces = False
            if current:
                paths.append(current)
            current = ""
            continue
        if char == " " and not in_braces:
            if current:
                paths.append(current)
                current = ""
            continue
        current += char
    if current:
        paths.append(current)
    return [pathlib.Path(p) for p in paths]


def list_wav_files(paths: Sequence[pathlib.Path]) -> List[pathlib.Path]:
    found: List[pathlib.Path] = []
    for raw_path in paths:
        path = raw_path.expanduser().resolve()
        if path.is_file() and path.suffix.lower() == ".wav":
            found.append(path)
        elif path.is_dir():
            found.extend(sorted(path.rglob("*.wav")))
    out: List[pathlib.Path] = []
    seen = set()
    for wav in found:
        if wav not in seen:
            out.append(wav)
            seen.add(wav)
    return out


def _bytes_to_samples(frames: bytes, sample_width: int, channels: int) -> List[List[float]]:
    if channels <= 0:
        raise WavReadError("WAV has invalid channel count.")
    bytes_per_frame = sample_width * channels
    if bytes_per_frame <= 0 or len(frames) % bytes_per_frame != 0:
        raise WavReadError("Corrupted WAV frame data.")

    frame_count = len(frames) // bytes_per_frame
    samples: List[List[float]] = [[0.0] * channels for _ in range(frame_count)]
    idx = 0

    for frame_idx in range(frame_count):
        for ch in range(channels):
            if sample_width == 1:
                value = frames[idx] - 128
                idx += 1
            elif sample_width == 2:
                value = int.from_bytes(frames[idx : idx + 2], "little", signed=True)
                idx += 2
            elif sample_width == 3:
                b0 = frames[idx]
                b1 = frames[idx + 1]
                b2 = frames[idx + 2]
                idx += 3
                value = b0 | (b1 << 8) | (b2 << 16)
                if value & 0x800000:
                    value -= 0x1000000
            elif sample_width == 4:
                value = int.from_bytes(frames[idx : idx + 4], "little", signed=True)
                idx += 4
            else:
                raise WavReadError(f"Unsupported sample width: {sample_width} bytes")
            samples[frame_idx][ch] = float(value)
    return samples


def _samples_to_bytes(samples: List[List[float]], sample_width: int) -> bytes:
    out = bytearray()
    for frame in samples:
        for raw in frame:
            value = int(round(raw))
            if sample_width == 1:
                value = max(-128, min(127, value))
                out.append(value + 128)
            elif sample_width == 2:
                value = max(-32768, min(32767, value))
                out.extend(value.to_bytes(2, "little", signed=True))
            elif sample_width == 3:
                value = max(-8388608, min(8388607, value))
                if value < 0:
                    value += 1 << 24
                out.extend((value & 0xFFFFFF).to_bytes(3, "little", signed=False))
            elif sample_width == 4:
                value = max(-2147483648, min(2147483647, value))
                out.extend(value.to_bytes(4, "little", signed=True))
            else:
                raise WavReadError(f"Unsupported sample width: {sample_width} bytes")
    return bytes(out)


def _bytes_to_numpy_samples(frames: bytes, sample_width: int, channels: int):
    if not HAS_NUMPY:
        raise RuntimeError("NumPy is not available.")
    if channels <= 0:
        raise WavReadError("WAV has invalid channel count.")

    if sample_width == 1:
        data = np.frombuffer(frames, dtype=np.uint8).astype(np.float64) - 128.0
    elif sample_width == 2:
        data = np.frombuffer(frames, dtype="<i2").astype(np.float64)
    elif sample_width == 3:
        raw = np.frombuffer(frames, dtype=np.uint8)
        if len(raw) % 3 != 0:
            raise WavReadError("Corrupted 24-bit WAV frame data.")
        triplets = raw.reshape(-1, 3).astype(np.int32)
        data = triplets[:, 0] | (triplets[:, 1] << 8) | (triplets[:, 2] << 16)
        data[data & 0x800000 != 0] -= 0x1000000
        data = data.astype(np.float64)
    elif sample_width == 4:
        data = np.frombuffer(frames, dtype="<i4").astype(np.float64)
    else:
        raise WavReadError(f"Unsupported sample width: {sample_width} bytes")

    if data.size % channels != 0:
        raise WavReadError("Corrupted WAV frame data.")
    return data.reshape(-1, channels)


def _numpy_samples_to_bytes(samples, sample_width: int) -> bytes:
    if not HAS_NUMPY:
        raise RuntimeError("NumPy is not available.")

    rounded = np.rint(samples)
    if sample_width == 1:
        clipped = np.clip(rounded, -128, 127).astype(np.int16) + 128
        return clipped.astype(np.uint8).tobytes()
    if sample_width == 2:
        clipped = np.clip(rounded, -32768, 32767).astype("<i2")
        return clipped.tobytes()
    if sample_width == 3:
        clipped = np.clip(rounded, -8388608, 8388607).astype(np.int32)
        unsigned = clipped.copy()
        unsigned[unsigned < 0] += 1 << 24
        packed = np.empty((unsigned.size, 3), dtype=np.uint8)
        packed[:, 0] = unsigned & 0xFF
        packed[:, 1] = (unsigned >> 8) & 0xFF
        packed[:, 2] = (unsigned >> 16) & 0xFF
        return packed.reshape(-1).tobytes()
    if sample_width == 4:
        clipped = np.clip(rounded, -2147483648, 2147483647).astype("<i4")
        return clipped.tobytes()
    raise WavReadError(f"Unsupported sample width: {sample_width} bytes")


def _max_sample_value(sample_width: int) -> float:
    if sample_width == 1:
        return 127.0
    if sample_width == 2:
        return 32767.0
    if sample_width == 3:
        return 8388607.0
    if sample_width == 4:
        return 2147483647.0
    raise WavReadError(f"Unsupported sample width: {sample_width} bytes")


def _sinc(x: float) -> float:
    if abs(x) < 1e-12:
        return 1.0
    return math.sin(math.pi * x) / (math.pi * x)


def _design_lowpass_fir(cutoff_hz: float, sample_rate: int, taps: int) -> List[float]:
    nyquist = sample_rate / 2.0
    if cutoff_hz <= 0:
        return [0.0] * taps
    if cutoff_hz >= nyquist:
        coeff = [0.0] * taps
        coeff[(taps - 1) // 2] = 1.0
        return coeff

    fc = cutoff_hz / sample_rate
    m = (taps - 1) // 2
    coeffs: List[float] = []
    for n in range(taps):
        k = n - m
        ideal = 2.0 * fc * _sinc(2.0 * fc * k)
        window = 0.54 - 0.46 * math.cos((2.0 * math.pi * n) / (taps - 1))
        coeffs.append(ideal * window)
    s = sum(coeffs)
    if abs(s) > 1e-12:
        coeffs = [c / s for c in coeffs]
    return coeffs


def _design_highpass_fir(cutoff_hz: float, sample_rate: int, taps: int) -> List[float]:
    lp = _design_lowpass_fir(cutoff_hz, sample_rate, taps)
    center = (taps - 1) // 2
    hp = [-c for c in lp]
    hp[center] += 1.0
    return hp


def _apply_fir(signal: List[float], coeffs: List[float]) -> List[float]:
    taps = len(coeffs)
    center = (taps - 1) // 2
    n = len(signal)
    out = [0.0] * n
    for i in range(n):
        acc = 0.0
        for t, c in enumerate(coeffs):
            src = i - t + center
            if 0 <= src < n:
                acc += signal[src] * c
        out[i] = acc
    return out


def apply_linear_phase_eq(
    samples: List[List[float]], sample_rate: int, high_pass_hz: float, low_pass_hz: float, taps: int
) -> List[List[float]]:
    if high_pass_hz < 0 or low_pass_hz <= 0:
        raise ValueError("Filter frequencies must be positive and valid.")
    if high_pass_hz >= low_pass_hz:
        raise ValueError("High-pass cutoff must be less than low-pass cutoff.")
    if not samples:
        return samples

    taps = max(15, int(taps))
    if taps % 2 == 0:
        taps += 1
    hp = _design_highpass_fir(high_pass_hz, sample_rate, taps)
    lp = _design_lowpass_fir(low_pass_hz, sample_rate, taps)

    channels = len(samples[0])
    channel_data: List[List[float]] = [[frame[ch] for frame in samples] for ch in range(channels)]
    out_channels: List[List[float]] = []
    for sig in channel_data:
        filtered = _apply_fir(sig, hp)
        filtered = _apply_fir(filtered, lp)
        out_channels.append(filtered)

    frame_count = len(samples)
    out: List[List[float]] = [[0.0] * channels for _ in range(frame_count)]
    for i in range(frame_count):
        for ch in range(channels):
            out[i][ch] = out_channels[ch][i]
    return out


def _fade_envelope(length: int, fade_samples: int) -> List[float]:
    if length <= 0:
        return []
    fade_samples = max(1, min(fade_samples, length // 2 if length >= 2 else 1))
    env = [1.0] * length
    for i in range(fade_samples):
        x = i / fade_samples
        env[i] = x
        env[length - 1 - i] = x
    return env


def _fade_envelope_numpy(length: int, fade_samples: int):
    if not HAS_NUMPY:
        raise RuntimeError("NumPy is not available.")
    if length <= 0:
        return np.empty(0, dtype=np.float64)
    fade_samples = max(1, min(fade_samples, length // 2 if length >= 2 else 1))
    env = np.ones(length, dtype=np.float64)
    ramp = np.arange(fade_samples, dtype=np.float64) / float(fade_samples)
    env[:fade_samples] = ramp
    env[-fade_samples:] = ramp[::-1]
    return env


def _make_pink_noise(length: int) -> List[float]:
    rows = 16
    running = [0.0] * rows
    counter = 0
    out = [0.0] * length
    for i in range(length):
        counter += 1
        c = counter
        row = 0
        while (c & 1) == 0 and row < rows:
            running[row] = random.uniform(-1.0, 1.0)
            c >>= 1
            row += 1
        running[0] = random.uniform(-1.0, 1.0)
        out[i] = sum(running)
    max_abs = max(max(abs(v) for v in out), 1e-9)
    return [v / max_abs for v in out]


def _make_pink_noise_numpy(length: int):
    if not HAS_NUMPY:
        raise RuntimeError("NumPy is not available.")
    if length <= 0:
        return np.empty(0, dtype=np.float64)
    rng = np.random.default_rng()
    white = rng.normal(0.0, 1.0, length)
    spectrum = np.fft.rfft(white)
    freqs = np.arange(spectrum.size, dtype=np.float64)
    freqs[0] = 1.0
    spectrum /= np.sqrt(freqs)
    noise = np.fft.irfft(spectrum, n=length)
    max_abs = max(float(np.max(np.abs(noise))), 1e-9)
    return noise / max_abs


def _make_room_tone(length: int, sample_rate: int) -> List[float]:
    noise = _make_pink_noise(length)
    # Mild LPF + hum components
    hum1 = 50.0
    hum2 = 100.0
    out = [0.0] * length
    y = 0.0
    alpha = 0.04
    for i in range(length):
        t = i / sample_rate
        base = noise[i] * 0.6 + 0.15 * math.sin(2 * math.pi * hum1 * t) + 0.1 * math.sin(2 * math.pi * hum2 * t)
        y = y + alpha * (base - y)
        out[i] = y
    m = max(max(abs(v) for v in out), 1e-9)
    return [v / m for v in out]


def _make_room_tone_numpy(length: int, sample_rate: int):
    if not HAS_NUMPY:
        raise RuntimeError("NumPy is not available.")
    noise = _make_pink_noise_numpy(length)
    t = np.arange(length, dtype=np.float64) / float(sample_rate)
    base = noise * 0.6 + 0.15 * np.sin(2 * np.pi * 50.0 * t) + 0.1 * np.sin(2 * np.pi * 100.0 * t)
    window = max(3, int(sample_rate * 0.002))
    kernel = np.ones(window, dtype=np.float64) / float(window)
    out = np.convolve(base, kernel, mode="same")
    max_abs = max(float(np.max(np.abs(out))), 1e-9)
    return out / max_abs


def _make_vinyl_crackle(length: int) -> List[float]:
    out = [0.0] * length
    for i in range(length):
        out[i] = random.uniform(-0.1, 0.1)
        if random.random() < 0.002:
            spike = random.uniform(0.4, 1.0) * random.choice([-1.0, 1.0])
            for k in range(3):
                j = i + k
                if j < length:
                    out[j] += spike * (0.6 ** k)
    m = max(max(abs(v) for v in out), 1e-9)
    return [v / m for v in out]


def _make_vinyl_crackle_numpy(length: int):
    if not HAS_NUMPY:
        raise RuntimeError("NumPy is not available.")
    rng = np.random.default_rng()
    out = rng.uniform(-0.1, 0.1, length)
    spike_positions = np.flatnonzero(rng.random(length) < 0.002)
    if spike_positions.size:
        spikes = rng.uniform(0.4, 1.0, spike_positions.size) * rng.choice(np.array([-1.0, 1.0]), spike_positions.size)
        for decay, scale in enumerate((1.0, 0.6, 0.36)):
            valid = spike_positions + decay < length
            out[spike_positions[valid] + decay] += spikes[valid] * scale
    max_abs = max(float(np.max(np.abs(out))), 1e-9)
    return out / max_abs


def add_texture(
    samples: List[List[float]],
    sample_rate: int,
    sample_width: int,
    texture_type: str,
    mix_percent: float,
    level_db: float,
    fade_ms: float,
) -> List[List[float]]:
    if texture_type == "none" or mix_percent <= 0:
        return samples

    length = len(samples)
    if length == 0:
        return samples

    if texture_type == "pink":
        mono = _make_pink_noise(length)
    elif texture_type == "room":
        mono = _make_room_tone(length, sample_rate)
    elif texture_type == "vinyl":
        mono = _make_vinyl_crackle(length)
    else:
        return samples

    fade_samples = int(sample_rate * max(0.0, fade_ms) / 1000.0)
    env = _fade_envelope(length, fade_samples)
    max_amp = _max_sample_value(sample_width)
    linear_db = 10 ** (level_db / 20.0)
    mix = max(0.0, min(100.0, mix_percent)) / 100.0
    scale = max_amp * linear_db * mix

    out = [frame[:] for frame in samples]
    for i in range(length):
        add = mono[i] * env[i] * scale
        for ch in range(len(out[i])):
            out[i][ch] += add
    return out


def add_texture_numpy(
    samples,
    sample_rate: int,
    sample_width: int,
    texture_type: str,
    mix_percent: float,
    level_db: float,
    fade_ms: float,
):
    if not HAS_NUMPY:
        raise RuntimeError("NumPy is not available.")
    if texture_type == "none" or mix_percent <= 0 or samples.size == 0:
        return samples

    length = samples.shape[0]
    if texture_type == "pink":
        mono = _make_pink_noise_numpy(length)
    elif texture_type == "room":
        mono = _make_room_tone_numpy(length, sample_rate)
    elif texture_type == "vinyl":
        mono = _make_vinyl_crackle_numpy(length)
    else:
        return samples

    fade_samples = int(sample_rate * max(0.0, fade_ms) / 1000.0)
    env = _fade_envelope_numpy(length, fade_samples)
    max_amp = _max_sample_value(sample_width)
    linear_db = 10 ** (level_db / 20.0)
    mix = max(0.0, min(100.0, mix_percent)) / 100.0
    scale = max_amp * linear_db * mix
    return samples + (mono * env * scale)[:, np.newaxis]


def apply_dynamic_humanize(samples: List[List[float]], min_db: float, max_db: float, section_ms: float, sample_rate: int) -> List[List[float]]:
    if min_db == 0 and max_db == 0:
        return samples
    if not samples:
        return samples

    min_db, max_db = sorted((min_db, max_db))
    section_len = max(128, int(sample_rate * max(50.0, section_ms) / 1000.0))
    frame_count = len(samples)

    gains = [1.0] * frame_count
    pos = 0
    prev_gain = 1.0
    while pos < frame_count:
        end = min(frame_count, pos + section_len)
        target_db = random.uniform(min_db, max_db)
        target_gain = 10 ** (target_db / 20.0)
        span = end - pos
        for i in range(span):
            t = i / max(1, span - 1)
            gains[pos + i] = prev_gain + (target_gain - prev_gain) * t
        prev_gain = target_gain
        pos = end

    out = [frame[:] for frame in samples]
    for i, frame in enumerate(out):
        g = gains[i]
        for ch in range(len(frame)):
            frame[ch] *= g
    return out


def apply_dynamic_humanize_numpy(samples, min_db: float, max_db: float, section_ms: float, sample_rate: int):
    if not HAS_NUMPY:
        raise RuntimeError("NumPy is not available.")
    if min_db == 0 and max_db == 0 or samples.size == 0:
        return samples

    min_db, max_db = sorted((min_db, max_db))
    section_len = max(128, int(sample_rate * max(50.0, section_ms) / 1000.0))
    frame_count = samples.shape[0]
    gains = np.ones(frame_count, dtype=np.float64)
    pos = 0
    prev_gain = 1.0
    while pos < frame_count:
        end = min(frame_count, pos + section_len)
        target_db = random.uniform(min_db, max_db)
        target_gain = 10 ** (target_db / 20.0)
        gains[pos:end] = np.linspace(prev_gain, target_gain, end - pos)
        prev_gain = target_gain
        pos = end
    return samples * gains[:, np.newaxis]


def apply_random_stem_offsets(samples: List[List[float]], sample_rate: int, max_offset_ms: float) -> Tuple[List[List[float]], List[int]]:
    if not samples:
        return samples, []
    channels = len(samples[0])
    max_offset = max(0, int(sample_rate * max_offset_ms / 1000.0))
    offsets = [random.randint(0, max_offset) for _ in range(channels)]

    frame_count = len(samples)
    out = [[0.0] * channels for _ in range(frame_count)]
    for ch in range(channels):
        off = offsets[ch]
        for i in range(frame_count):
            src = i - off
            out[i][ch] = samples[src][ch] if 0 <= src < frame_count else 0.0
    return out, offsets


def apply_random_stem_offsets_numpy(samples, sample_rate: int, max_offset_ms: float):
    if not HAS_NUMPY:
        raise RuntimeError("NumPy is not available.")
    if samples.size == 0:
        return samples, []
    channels = samples.shape[1]
    max_offset = max(0, int(sample_rate * max_offset_ms / 1000.0))
    offsets = [random.randint(0, max_offset) for _ in range(channels)]
    out = np.zeros_like(samples)
    for ch, off in enumerate(offsets):
        if off == 0:
            out[:, ch] = samples[:, ch]
        else:
            out[off:, ch] = samples[:-off, ch]
    return out, offsets


def _apply_fir_same_numpy(signal, coeffs):
    if not HAS_NUMPY:
        raise RuntimeError("NumPy is not available.")
    full = np.convolve(signal, coeffs, mode="full")
    start = (len(coeffs) - 1) // 2
    return full[start : start + signal.shape[0]]


def _apply_spectral_band_filter_numpy(samples, sample_rate: int, high_pass_hz: float, low_pass_hz: float):
    if not HAS_NUMPY:
        raise RuntimeError("NumPy is not available.")
    if samples.size == 0:
        return samples

    frame_count = samples.shape[0]
    pad = min(sample_rate, max(2048, frame_count // 8))
    padded = np.pad(samples, ((pad, pad), (0, 0)), mode="reflect")
    spectrum = np.fft.rfft(padded, axis=0)
    freqs = np.fft.rfftfreq(padded.shape[0], d=1.0 / sample_rate)

    response = np.ones(freqs.shape[0], dtype=np.float64)
    if high_pass_hz > 0:
        response[freqs < high_pass_hz] = 0.0
    nyquist = sample_rate / 2.0
    if low_pass_hz < nyquist:
        response[freqs > low_pass_hz] = 0.0

    filtered = np.fft.irfft(spectrum * response[:, np.newaxis], n=padded.shape[0], axis=0)
    return filtered[pad : pad + frame_count]


def apply_linear_phase_eq_numpy(samples, sample_rate: int, high_pass_hz: float, low_pass_hz: float, taps: int):
    if not HAS_NUMPY:
        raise RuntimeError("NumPy is not available.")
    if high_pass_hz < 0 or low_pass_hz <= 0:
        raise ValueError("Filter frequencies must be positive and valid.")
    if high_pass_hz >= low_pass_hz:
        raise ValueError("High-pass cutoff must be less than low-pass cutoff.")
    if samples.size == 0:
        return samples

    return _apply_spectral_band_filter_numpy(samples, sample_rate, high_pass_hz, low_pass_hz)


def _riff_chunk(chunk_id: bytes, payload: bytes) -> bytes:
    chunk = chunk_id + struct.pack("<I", len(payload)) + payload
    if len(payload) % 2:
        chunk += b"\x00"
    return chunk


def _build_list_info_chunk(produced_by: str) -> bytes:
    producer_name = produced_by.strip()
    if not producer_name:
        return b""
    comment = f"Produced by {producer_name}".encode("utf-8", errors="replace") + b"\x00"
    return _riff_chunk(b"LIST", b"INFO" + _riff_chunk(b"ICMT", comment))


def write_pcm_wav(
    output_path: pathlib.Path,
    samples: List[List[float]],
    channels: int,
    sample_width: int,
    sample_rate: int,
    produced_by: str,
) -> None:
    data_bytes = _samples_to_bytes(samples, sample_width)
    write_pcm_wav_bytes(output_path, data_bytes, channels, sample_width, sample_rate, produced_by)


def write_pcm_wav_bytes(
    output_path: pathlib.Path,
    data_bytes: bytes,
    channels: int,
    sample_width: int,
    sample_rate: int,
    produced_by: str,
) -> None:
    byte_rate = sample_rate * channels * sample_width
    block_align = channels * sample_width
    bits_per_sample = sample_width * 8

    fmt_chunk = _riff_chunk(
        b"fmt ",
        struct.pack("<HHIIHH", 1, channels, sample_rate, byte_rate, block_align, bits_per_sample),
    )
    data_chunk = _riff_chunk(b"data", data_bytes)
    info_chunk = _build_list_info_chunk(produced_by)

    riff_payload = b"WAVE" + fmt_chunk + info_chunk + data_chunk
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("wb") as handle:
        handle.write(b"RIFF")
        handle.write(struct.pack("<I", len(riff_payload)))
        handle.write(riff_payload)


def process_wav_data_numpy(
    frames: bytes,
    channels: int,
    sample_width: int,
    sample_rate: int,
    high_pass_hz: float,
    low_pass_hz: float,
    texture_type: str,
    texture_mix_percent: float,
    texture_level_db: float,
    texture_fade_ms: float,
    humanize_enabled: bool,
    humanize_min_db: float,
    humanize_max_db: float,
    humanize_section_ms: float,
    offset_enabled: bool,
    offset_max_ms: float,
    eq_taps: int,
) -> bytes:
    samples = _bytes_to_numpy_samples(frames, sample_width, channels)
    samples = apply_linear_phase_eq_numpy(samples, sample_rate, high_pass_hz, low_pass_hz, eq_taps)
    if offset_enabled:
        samples, _ = apply_random_stem_offsets_numpy(samples, sample_rate, offset_max_ms)
    samples = add_texture_numpy(samples, sample_rate, sample_width, texture_type, texture_mix_percent, texture_level_db, texture_fade_ms)
    if humanize_enabled:
        samples = apply_dynamic_humanize_numpy(samples, humanize_min_db, humanize_max_db, humanize_section_ms, sample_rate)
    return _numpy_samples_to_bytes(samples, sample_width)


def build_output_path(source_path: pathlib.Path, output_directory: pathlib.Path | None, suffix_parts: Sequence[str]) -> pathlib.Path:
    base_dir = output_directory if output_directory is not None else source_path.parent
    stem = f"{source_path.stem}_{'_'.join(suffix_parts)}"
    candidate = base_dir / f"{stem}{source_path.suffix}"
    counter = 2
    while candidate.exists():
        candidate = base_dir / f"{stem}_{counter}{source_path.suffix}"
        counter += 1
    return candidate


def get_settings_path() -> pathlib.Path:
    appdata = os.getenv("APPDATA")
    base_dir = pathlib.Path(appdata) if appdata else pathlib.Path.home() / ".config"
    return base_dir / SETTINGS_DIR_NAME / SETTINGS_FILE_NAME


def process_wav_file(
    path: pathlib.Path,
    output_directory: pathlib.Path | None,
    produced_by: str,
    high_pass_hz: float,
    low_pass_hz: float,
    texture_type: str,
    texture_mix_percent: float,
    texture_level_db: float,
    texture_fade_ms: float,
    humanize_enabled: bool,
    humanize_min_db: float,
    humanize_max_db: float,
    humanize_section_ms: float,
    offset_enabled: bool,
    offset_max_ms: float,
    eq_taps: int,
) -> pathlib.Path:
    with wave.open(str(path), "rb") as wf:
        channels = wf.getnchannels()
        sample_width = wf.getsampwidth()
        sample_rate = wf.getframerate()
        nframes = wf.getnframes()
        if wf.getcomptype() != "NONE":
            raise WavReadError("Compressed WAV is not supported.")
        frames = wf.readframes(nframes)

    if HAS_NUMPY:
        data_bytes = process_wav_data_numpy(
            frames,
            channels,
            sample_width,
            sample_rate,
            high_pass_hz,
            low_pass_hz,
            texture_type,
            texture_mix_percent,
            texture_level_db,
            texture_fade_ms,
            humanize_enabled,
            humanize_min_db,
            humanize_max_db,
            humanize_section_ms,
            offset_enabled,
            offset_max_ms,
            eq_taps,
        )
    else:
        samples = _bytes_to_samples(frames, sample_width, channels)
        samples = apply_linear_phase_eq(samples, sample_rate, high_pass_hz, low_pass_hz, eq_taps)

        if offset_enabled:
            samples, _ = apply_random_stem_offsets(samples, sample_rate, offset_max_ms)

        samples = add_texture(samples, sample_rate, sample_width, texture_type, texture_mix_percent, texture_level_db, texture_fade_ms)

        if humanize_enabled:
            samples = apply_dynamic_humanize(samples, humanize_min_db, humanize_max_db, humanize_section_ms, sample_rate)
        data_bytes = _samples_to_bytes(samples, sample_width)

    hp_label = int(round(high_pass_hz))
    lp_label = int(round(low_pass_hz))
    suffix_parts = [f"hp{hp_label}", f"lp{lp_label}"]
    if texture_type != "none":
        suffix_parts.append(f"{texture_type}{int(round(texture_mix_percent))}pct")
    if humanize_enabled:
        suffix_parts.append("human")
    if offset_enabled:
        suffix_parts.append(f"off{int(round(offset_max_ms))}ms")
    output_path = build_output_path(path, output_directory, suffix_parts)
    write_pcm_wav_bytes(output_path, data_bytes, channels, sample_width, sample_rate, produced_by)

    return output_path


def process_wav_file_task(
    args: Tuple[str, str, str, float, float, str, float, float, float, bool, float, float, float, bool, float, int]
) -> str:
    output_directory = pathlib.Path(args[1]).resolve() if args[1] else None
    output = process_wav_file(
        pathlib.Path(args[0]),
        output_directory,
        args[2],
        args[3],
        args[4],
        args[5],
        args[6],
        args[7],
        args[8],
        args[9],
        args[10],
        args[11],
        args[12],
        args[13],
        args[14],
        args[15],
    )
    return str(output)


class WavFilterApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(APP_TITLE)
        self.root.geometry(DEFAULT_WINDOW_SIZE)
        self.settings_path = get_settings_path()
        self._settings_load_error: str | None = None
        self.ui_queue: queue.Queue[tuple[str, object]] = queue.Queue()
        self.process_thread: threading.Thread | None = None
        self.is_processing = False
        self._ui_poll_scheduled = False
        self.control_widgets: List[tk.Widget] = []
        self.cancel_requested = threading.Event()
        self.progress_var = tk.DoubleVar(value=0.0)
        self.progress_text_var = tk.StringVar(value="Idle")

        self.selected_files: List[pathlib.Path] = []

        self.high_pass_var = tk.StringVar(value=str(int(DEFAULT_HIGH_PASS_HZ)))
        self.low_pass_var = tk.StringVar(value=str(int(DEFAULT_LOW_PASS_HZ)))
        self.eq_taps_var = tk.StringVar(value=str(DEFAULT_EQ_TAPS))
        self.worker_count_var = tk.StringVar(value=str(max(1, min(4, (os.cpu_count() or 1)))))
        self.output_folder_var = tk.StringVar(value="")
        self.produced_by_var = tk.StringVar(value=os.getenv("USERNAME", "").strip())

        self.texture_type_var = tk.StringVar(value="pink")
        self.texture_mix_var = tk.StringVar(value="7")
        self.texture_level_db_var = tk.StringVar(value="-35")
        self.texture_fade_ms_var = tk.StringVar(value="250")

        self.humanize_enabled_var = tk.BooleanVar(value=True)
        self.humanize_min_db_var = tk.StringVar(value="-2.0")
        self.humanize_max_db_var = tk.StringVar(value="-0.5")
        self.humanize_section_ms_var = tk.StringVar(value="900")

        self.offset_enabled_var = tk.BooleanVar(value=False)
        self.offset_max_ms_var = tk.StringVar(value="100")

        self._load_settings()
        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    def _build_ui(self) -> None:
        top = tk.LabelFrame(self.root, text="Linear-phase EQ")
        top.pack(fill="x", padx=12, pady=8)

        tk.Label(top, text="High-pass (Hz):").grid(row=0, column=0, sticky="w", padx=6, pady=6)
        self._track_control(tk.Entry(top, textvariable=self.high_pass_var, width=12)).grid(row=0, column=1, padx=6)

        tk.Label(top, text="Low-pass (Hz):").grid(row=0, column=2, sticky="w", padx=6)
        self._track_control(tk.Entry(top, textvariable=self.low_pass_var, width=12)).grid(row=0, column=3, padx=6)

        taps_label = "EQ taps (legacy fallback):" if HAS_NUMPY else "EQ taps (quality/speed):"
        tk.Label(top, text=taps_label).grid(row=0, column=4, sticky="w", padx=6)
        self._track_control(tk.Entry(top, textvariable=self.eq_taps_var, width=8)).grid(row=0, column=5, padx=6)

        tk.Label(top, text="Workers:").grid(row=0, column=6, sticky="w", padx=6)
        self._track_control(tk.Entry(top, textvariable=self.worker_count_var, width=6)).grid(row=0, column=7, padx=6)

        self._track_control(tk.Button(top, text="Add WAV Files", command=self.pick_files)).grid(row=0, column=8, padx=(20, 6))
        self._track_control(tk.Button(top, text="Add Folder", command=self.pick_folder)).grid(row=0, column=9, padx=6)
        self._track_control(tk.Button(top, text="Clear", command=self.clear_files)).grid(row=0, column=10, padx=6)

        output = tk.LabelFrame(self.root, text="Output and Metadata")
        output.pack(fill="x", padx=12, pady=4)

        tk.Label(output, text="Output folder:").grid(row=0, column=0, sticky="w", padx=6, pady=6)
        self._track_control(tk.Entry(output, textvariable=self.output_folder_var, width=60)).grid(row=0, column=1, padx=6, sticky="we")
        self._track_control(tk.Button(output, text="Browse", command=self.pick_output_folder)).grid(row=0, column=2, padx=6)
        tk.Label(output, text="Leave blank to export beside the source WAVs.").grid(row=1, column=1, sticky="w", padx=6)

        tk.Label(output, text="Produced by:").grid(row=0, column=3, sticky="w", padx=(20, 6))
        self._track_control(tk.Entry(output, textvariable=self.produced_by_var, width=24)).grid(row=0, column=4, padx=6)
        tk.Label(output, text="Existing WAV metadata is stripped on export.").grid(row=1, column=4, sticky="w", padx=6)
        output.grid_columnconfigure(1, weight=1)

        texture = tk.LabelFrame(self.root, text="Texture Layer (pink / room / vinyl)")
        texture.pack(fill="x", padx=12, pady=4)

        self._track_control(
            ttk.Combobox(texture, textvariable=self.texture_type_var, values=["none", "pink", "room", "vinyl"], width=10, state="readonly")
        ).grid(row=0, column=0, padx=6, pady=6)
        tk.Label(texture, text="Mix % (5-10 subtle):").grid(row=0, column=1, sticky="w")
        self._track_control(tk.Entry(texture, textvariable=self.texture_mix_var, width=8)).grid(row=0, column=2, padx=6)
        tk.Label(texture, text="Level dB (-30 to -40):").grid(row=0, column=3, sticky="w")
        self._track_control(tk.Entry(texture, textvariable=self.texture_level_db_var, width=8)).grid(row=0, column=4, padx=6)
        tk.Label(texture, text="Fade ms:").grid(row=0, column=5, sticky="w")
        self._track_control(tk.Entry(texture, textvariable=self.texture_fade_ms_var, width=8)).grid(row=0, column=6, padx=6)

        human = tk.LabelFrame(self.root, text="Mild Dynamic Variation (Humanize)")
        human.pack(fill="x", padx=12, pady=4)

        self._track_control(tk.Checkbutton(human, text="Enable", variable=self.humanize_enabled_var)).grid(row=0, column=0, padx=6, pady=6)
        tk.Label(human, text="Min dB:").grid(row=0, column=1, sticky="w")
        self._track_control(tk.Entry(human, textvariable=self.humanize_min_db_var, width=8)).grid(row=0, column=2, padx=6)
        tk.Label(human, text="Max dB:").grid(row=0, column=3, sticky="w")
        self._track_control(tk.Entry(human, textvariable=self.humanize_max_db_var, width=8)).grid(row=0, column=4, padx=6)
        tk.Label(human, text="Section ms:").grid(row=0, column=5, sticky="w")
        self._track_control(tk.Entry(human, textvariable=self.humanize_section_ms_var, width=8)).grid(row=0, column=6, padx=6)

        offsets = tk.LabelFrame(self.root, text="Stem Random Offsets")
        offsets.pack(fill="x", padx=12, pady=4)

        self._track_control(tk.Checkbutton(offsets, text="Enable random stem offsets", variable=self.offset_enabled_var)).grid(row=0, column=0, padx=6, pady=6)
        tk.Label(offsets, text="Max offset ms (0-100+):").grid(row=0, column=1, sticky="w")
        self._track_control(tk.Entry(offsets, textvariable=self.offset_max_ms_var, width=8)).grid(row=0, column=2, padx=6)
        self._track_control(tk.Button(offsets, text="Randomizer", command=self.randomize_options)).grid(row=0, column=3, padx=10)

        dnd_msg = "Drag/drop WAV files or folders below" if HAS_DND else "Install tkinterdnd2 to enable drag-and-drop"
        self.drop_label = tk.Label(self.root, text=dnd_msg, relief="groove", padx=8, pady=10)
        self.drop_label.pack(fill="x", padx=12, pady=8)

        self.file_list = tk.Listbox(self.root)
        self.file_list.pack(fill="both", expand=True, padx=12, pady=(0, 8))

        action_frame = tk.Frame(self.root)
        action_frame.pack(fill="x", padx=12, pady=(0, 8))

        self.process_button = self._track_control(tk.Button(action_frame, text="Process WAV(s)", command=self.process_files, height=2))
        self.process_button.pack(side="left", fill="x", expand=True)
        self.cancel_button = tk.Button(action_frame, text="Cancel", command=self.cancel_processing, height=2, state=tk.DISABLED, width=12)
        self.cancel_button.pack(side="left", padx=(8, 0))

        progress_frame = tk.Frame(self.root)
        progress_frame.pack(fill="x", padx=12, pady=(0, 6))

        self.progress_bar = ttk.Progressbar(progress_frame, variable=self.progress_var, maximum=100.0, mode="determinate")
        self.progress_bar.pack(fill="x", side="left", expand=True)
        self.progress_label = tk.Label(progress_frame, textvariable=self.progress_text_var, width=16, anchor="e")
        self.progress_label.pack(side="left", padx=(10, 0))

        self.status = tk.Label(self.root, text="Ready", anchor="w")
        self.status.pack(fill="x", padx=12, pady=(0, 10))

        if HAS_DND:
            self.drop_label.drop_target_register(DND_FILES)
            self.drop_label.dnd_bind("<<Drop>>", self.on_drop)

        if self._settings_load_error:
            self.set_status(self._settings_load_error)

    def randomize_options(self) -> None:
        self.texture_type_var.set(random.choice(["pink", "room", "vinyl"]))
        self.texture_mix_var.set(str(random.randint(5, 10)))
        self.texture_level_db_var.set(str(random.randint(-40, -30)))
        self.texture_fade_ms_var.set(str(random.randint(120, 500)))

        lo = round(random.uniform(-2.0, -1.0), 2)
        hi = round(random.uniform(-1.0, -0.5), 2)
        self.humanize_min_db_var.set(str(min(lo, hi)))
        self.humanize_max_db_var.set(str(max(lo, hi)))
        self.humanize_section_ms_var.set(str(random.randint(400, 1500)))

        self.offset_enabled_var.set(random.choice([True, False]))
        self.offset_max_ms_var.set(str(random.randint(0, 100)))
        self.set_status("Randomized texture/humanize/offset settings.")

    def set_status(self, text: str) -> None:
        self.status.config(text=text)
        self.root.update_idletasks()

    def _track_control(self, widget: tk.Widget) -> tk.Widget:
        self.control_widgets.append(widget)
        return widget

    def _set_processing_state(self, processing: bool) -> None:
        self.is_processing = processing
        state = tk.DISABLED if processing else tk.NORMAL
        for widget in self.control_widgets:
            try:
                if isinstance(widget, ttk.Combobox):
                    widget.configure(state="disabled" if processing else "readonly")
                else:
                    widget.configure(state=state)
            except tk.TclError:
                continue
        self.cancel_button.configure(state=tk.NORMAL if processing else tk.DISABLED)
        if processing:
            self.status.config(text="Starting background processing...")
            self.progress_var.set(0.0)
            self.progress_text_var.set("0%")

    def _set_progress(self, completed: int, total: int) -> None:
        if total <= 0:
            self.progress_var.set(0.0)
            self.progress_text_var.set("Idle")
            return
        percent = (completed / total) * 100.0
        self.progress_var.set(percent)
        self.progress_text_var.set(f"{completed}/{total}")

    def _schedule_ui_queue_poll(self) -> None:
        if not self._ui_poll_scheduled:
            self._ui_poll_scheduled = True
            self.root.after(100, self._drain_ui_queue)

    def _post_ui_event(self, event_type: str, payload: object) -> None:
        self.ui_queue.put((event_type, payload))

    def _drain_ui_queue(self) -> None:
        self._ui_poll_scheduled = False
        while True:
            try:
                event_type, payload = self.ui_queue.get_nowait()
            except queue.Empty:
                break

            if event_type == "status":
                self.set_status(str(payload))
            elif event_type == "progress":
                completed, total = payload  # type: ignore[misc]
                self._set_progress(int(completed), int(total))
            elif event_type == "done":
                ok, total, failures, cancelled = payload  # type: ignore[misc]
                self._set_progress(int(ok if cancelled else total), int(total))
                self._set_processing_state(False)
                self.process_thread = None
                self.cancel_requested.clear()
                summary = (
                    f"Processed {ok}/{total} WAV file(s) before cancellation."
                    if cancelled
                    else f"Processed {ok}/{total} WAV file(s)."
                )
                self.set_status(summary)
                if failures:
                    title = "Cancelled with errors" if cancelled else "Completed with errors"
                    messagebox.showwarning(title, summary + "\n\n" + "\n".join(failures))
                elif cancelled:
                    messagebox.showinfo("Processing cancelled", summary)
                else:
                    messagebox.showinfo("Processing complete", summary)
            elif event_type == "fatal":
                self._set_processing_state(False)
                self.process_thread = None
                self.cancel_requested.clear()
                self.set_status("Processing failed.")
                messagebox.showerror("Processing failed", str(payload))

        if self.is_processing or not self.ui_queue.empty():
            self._schedule_ui_queue_poll()

    def _load_settings(self) -> None:
        if not self.settings_path.exists():
            return

        try:
            data = json.loads(self.settings_path.read_text(encoding="utf-8"))
        except Exception as exc:
            self._settings_load_error = f"Settings file could not be loaded: {exc}"
            return

        if not isinstance(data, dict):
            self._settings_load_error = "Settings file was ignored because it is not valid JSON data."
            return

        geometry = data.get("window_geometry")
        if isinstance(geometry, str) and geometry:
            self.root.geometry(geometry)

        string_fields = {
            "high_pass_hz": self.high_pass_var,
            "low_pass_hz": self.low_pass_var,
            "eq_taps": self.eq_taps_var,
            "worker_count": self.worker_count_var,
            "output_folder": self.output_folder_var,
            "produced_by": self.produced_by_var,
            "texture_type": self.texture_type_var,
            "texture_mix_percent": self.texture_mix_var,
            "texture_level_db": self.texture_level_db_var,
            "texture_fade_ms": self.texture_fade_ms_var,
            "humanize_min_db": self.humanize_min_db_var,
            "humanize_max_db": self.humanize_max_db_var,
            "humanize_section_ms": self.humanize_section_ms_var,
            "offset_max_ms": self.offset_max_ms_var,
        }
        bool_fields = {
            "humanize_enabled": self.humanize_enabled_var,
            "offset_enabled": self.offset_enabled_var,
        }

        for key, var in string_fields.items():
            value = data.get(key)
            if isinstance(value, str):
                var.set(value)
        for key, var in bool_fields.items():
            value = data.get(key)
            if isinstance(value, bool):
                var.set(value)

    def _collect_settings(self) -> dict[str, object]:
        return {
            "window_geometry": self.root.geometry(),
            "high_pass_hz": self.high_pass_var.get(),
            "low_pass_hz": self.low_pass_var.get(),
            "eq_taps": self.eq_taps_var.get(),
            "worker_count": self.worker_count_var.get(),
            "output_folder": self.output_folder_var.get(),
            "produced_by": self.produced_by_var.get(),
            "texture_type": self.texture_type_var.get(),
            "texture_mix_percent": self.texture_mix_var.get(),
            "texture_level_db": self.texture_level_db_var.get(),
            "texture_fade_ms": self.texture_fade_ms_var.get(),
            "humanize_enabled": bool(self.humanize_enabled_var.get()),
            "humanize_min_db": self.humanize_min_db_var.get(),
            "humanize_max_db": self.humanize_max_db_var.get(),
            "humanize_section_ms": self.humanize_section_ms_var.get(),
            "offset_enabled": bool(self.offset_enabled_var.get()),
            "offset_max_ms": self.offset_max_ms_var.get(),
        }

    def save_settings(self) -> None:
        self.settings_path.parent.mkdir(parents=True, exist_ok=True)
        self.settings_path.write_text(json.dumps(self._collect_settings(), indent=2), encoding="utf-8")

    def on_close(self) -> None:
        if self.is_processing:
            messagebox.showinfo("Processing in progress", "Please wait for the current batch to finish before closing the app.")
            return
        try:
            self.save_settings()
        except Exception as exc:
            messagebox.showwarning("Settings not saved", f"Could not save settings:\n{exc}")
        self.root.destroy()

    def add_paths(self, paths: Sequence[pathlib.Path]) -> None:
        new_files = list_wav_files(paths)
        known = set(self.selected_files)
        added = 0
        for wav in new_files:
            if wav not in known:
                self.selected_files.append(wav)
                self.file_list.insert(tk.END, str(wav))
                known.add(wav)
                added += 1
        self.set_status(f"Added {added} WAV file(s). Total: {len(self.selected_files)}")

    def pick_files(self) -> None:
        files = filedialog.askopenfilenames(filetypes=[("WAV files", "*.wav")])
        self.add_paths([pathlib.Path(p) for p in files])

    def pick_folder(self) -> None:
        folder = filedialog.askdirectory()
        if folder:
            self.add_paths([pathlib.Path(folder)])

    def pick_output_folder(self) -> None:
        folder = filedialog.askdirectory()
        if folder:
            self.output_folder_var.set(folder)
            self.set_status(f"Output folder set to: {folder}")

    def clear_files(self) -> None:
        self.selected_files.clear()
        self.file_list.delete(0, tk.END)
        self.set_status("Cleared file list.")

    def cancel_processing(self) -> None:
        if not self.is_processing or self.cancel_requested.is_set():
            return
        self.cancel_requested.set()
        self.cancel_button.configure(state=tk.DISABLED)
        self.set_status("Cancellation requested. Waiting for the active file(s) to stop.")

    def on_drop(self, event: tk.Event) -> None:
        self.add_paths(parse_drop_payload(event.data))

    def _get_filter_settings(self) -> Tuple[float, float, int, int]:
        hp = float(self.high_pass_var.get().strip())
        lp = float(self.low_pass_var.get().strip())
        eq_taps = int(float(self.eq_taps_var.get().strip()))
        workers = int(float(self.worker_count_var.get().strip()))
        if hp < 0:
            raise ValueError("High-pass must be >= 0 Hz.")
        if lp <= 0:
            raise ValueError("Low-pass must be > 0 Hz.")
        if hp >= lp:
            raise ValueError("High-pass must be less than low-pass.")
        if eq_taps < 15:
            raise ValueError("EQ taps must be >= 15 (odd preferred).")
        if workers < 1:
            raise ValueError("Workers must be >= 1.")
        return hp, lp, eq_taps, workers

    def _get_output_settings(self) -> Tuple[pathlib.Path | None, str]:
        output_text = self.output_folder_var.get().strip()
        produced_by = self.produced_by_var.get().strip()

        output_directory: pathlib.Path | None = None
        if output_text:
            output_directory = pathlib.Path(output_text).expanduser().resolve()
            output_directory.mkdir(parents=True, exist_ok=True)
            if not output_directory.is_dir():
                raise ValueError("Output folder must be a directory.")
        return output_directory, produced_by

    def _get_effect_settings(self) -> Tuple[str, float, float, float, bool, float, float, float, bool, float]:
        texture_type = self.texture_type_var.get().strip().lower()
        if texture_type not in {"none", "pink", "room", "vinyl"}:
            raise ValueError("Texture must be one of: none, pink, room, vinyl")

        mix = float(self.texture_mix_var.get().strip())
        level_db = float(self.texture_level_db_var.get().strip())
        fade_ms = float(self.texture_fade_ms_var.get().strip())

        humanize = bool(self.humanize_enabled_var.get())
        min_db = float(self.humanize_min_db_var.get().strip())
        max_db = float(self.humanize_max_db_var.get().strip())
        section_ms = float(self.humanize_section_ms_var.get().strip())

        offset_enabled = bool(self.offset_enabled_var.get())
        offset_max_ms = float(self.offset_max_ms_var.get().strip())

        if mix < 0 or mix > 100:
            raise ValueError("Texture mix must be 0-100%.")
        if fade_ms < 0:
            raise ValueError("Fade ms must be >= 0.")
        if offset_max_ms < 0:
            raise ValueError("Offset max ms must be >= 0.")

        return texture_type, mix, level_db, fade_ms, humanize, min_db, max_db, section_ms, offset_enabled, offset_max_ms

    def process_files(self) -> None:
        if self.is_processing:
            messagebox.showinfo("Already processing", "A batch is already running in the background.")
            return
        if not self.selected_files:
            messagebox.showinfo("No files", "Please add at least one WAV file first.")
            return

        try:
            hp, lp, eq_taps, workers = self._get_filter_settings()
            output_directory, produced_by = self._get_output_settings()
            settings = self._get_effect_settings()
        except Exception as exc:
            messagebox.showerror("Invalid settings", str(exc))
            return

        tasks = [
            (
                str(wav_path),
                str(output_directory) if output_directory is not None else "",
                produced_by,
                hp,
                lp,
                settings[0],
                settings[1],
                settings[2],
                settings[3],
                settings[4],
                settings[5],
                settings[6],
                settings[7],
                settings[8],
                settings[9],
                eq_taps,
            )
            for wav_path in self.selected_files
        ]

        self.cancel_requested.clear()
        self._set_processing_state(True)
        self._set_progress(0, len(tasks))
        self.process_thread = threading.Thread(
            target=self._run_processing_job,
            args=(tasks, workers),
            name="wav-processing",
            daemon=True,
        )
        self.process_thread.start()
        self._schedule_ui_queue_poll()

    def _run_processing_job(self, tasks: List[Tuple[str, str, str, float, float, str, float, float, float, bool, float, float, float, bool, float, int]], workers: int) -> None:
        ok = 0
        failures: List[str] = []
        total = len(tasks)
        cancelled = False
        try:
            if workers == 1 or total == 1:
                for idx, task in enumerate(tasks, start=1):
                    if self.cancel_requested.is_set():
                        cancelled = True
                        break
                    wav_path = pathlib.Path(task[0])
                    self._post_ui_event("status", f"Processing {idx}/{total}: {wav_path.name}")
                    try:
                        process_wav_file_task(task)
                        ok += 1
                    except Exception as exc:
                        failures.append(f"{wav_path.name}: {exc}")
                    self._post_ui_event("progress", (idx, total))
            else:
                self._post_ui_event("status", f"Processing in parallel with {workers} workers...")
                with ProcessPoolExecutor(max_workers=workers) as pool:
                    task_iter = iter(tasks)
                    future_map = {}

                    def submit_next() -> bool:
                        if self.cancel_requested.is_set():
                            return False
                        try:
                            task = next(task_iter)
                        except StopIteration:
                            return False
                        future = pool.submit(process_wav_file_task, task)
                        future_map[future] = pathlib.Path(task[0]).name
                        return True

                    for _ in range(min(workers, total)):
                        if not submit_next():
                            break

                    completed = 0
                    while future_map:
                        if self.cancel_requested.is_set():
                            cancelled = True
                            for future in list(future_map):
                                future.cancel()
                            pool.shutdown(wait=False, cancel_futures=True)
                            break

                        done_set, _ = wait(list(future_map.keys()), timeout=0.1, return_when=FIRST_COMPLETED)
                        if not done_set:
                            continue

                        for fut in done_set:
                            completed += 1
                            name = future_map.pop(fut)
                            self._post_ui_event("status", f"Completed {completed}/{total}: {name}")
                            try:
                                fut.result()
                                ok += 1
                            except Exception as exc:
                                failures.append(f"{name}: {exc}")
                            self._post_ui_event("progress", (completed, total))
                            submit_next()
        except Exception as exc:
            self._post_ui_event("fatal", str(exc))
            return

        self._post_ui_event("done", (ok, total, failures, cancelled))


def build_root() -> tk.Tk:
    if HAS_DND:
        return TkinterDnD.Tk()
    return tk.Tk()


def main() -> None:
    multiprocessing.freeze_support()
    root = build_root()
    WavFilterApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
