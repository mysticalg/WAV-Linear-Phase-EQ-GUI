#!/usr/bin/env python3
"""WAV batch filter GUI with optional drag-and-drop.

Features:
- Batch WAV processing from files/folders
- Linear-phase FIR EQ (high-pass + low-pass)
- Optional pitch retuning by semitones, cents, millicents, or A reference frequency
- Optional tape-emulated saturation
- Optional single-band or multiband compression + final limiter
- Optional texture layer (pink / room / vinyl)
- Optional mild dynamic humanization
- Optional random channel-offset "stem" effect
- Preview playback and visual analysis for the rendered output
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
import tempfile
import threading
import tkinter as tk
import webbrowser
import getpass
import sys
from collections import deque
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from tkinter import filedialog, messagebox, simpledialog, ttk
from typing import List, Sequence, Tuple
import wave

from license_manager import LicenseError, LicenseState, activate_license_code, get_installation_id, get_license_storage_path, load_saved_license

try:
    import winsound

    HAS_WINSOUND = True
except ImportError:
    winsound = None
    HAS_WINSOUND = False

try:
    import pygame

    HAS_PYGAME = True
except ImportError:
    pygame = None
    HAS_PYGAME = False

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
DEFAULT_TAPE_DRIVE_PERCENT = 18.0
DEFAULT_TAPE_MIX_PERCENT = 100.0
DEFAULT_ALIAS_INTERPOLATION = "spline"
DEFAULT_ALIAS_QUALITY = "high"
DEFAULT_OUTPUT_SAMPLE_RATE = "source"
DEFAULT_OUTPUT_BIT_DEPTH = "source"
DEFAULT_OUTPUT_ALIAS_INTERPOLATION = "spline"
DEFAULT_OUTPUT_ALIAS_QUALITY = "high"
DEFAULT_STEREO_WIDTH_PERCENT = 100.0
DEFAULT_PITCH_MODE = "semitones"
DEFAULT_PITCH_SEMITONES = 0.0
DEFAULT_PITCH_CENTS = 0.0
DEFAULT_PITCH_MILLICENTS = 0.0
DEFAULT_PITCH_SOURCE_A_HZ = 440.0
DEFAULT_PITCH_TARGET_A_HZ = 440.0
DEFAULT_COMP_THRESHOLD_DB = -18.0
DEFAULT_COMP_RATIO = 3.0
DEFAULT_COMP_ATTACK_MS = 12.0
DEFAULT_COMP_RELEASE_MS = 140.0
DEFAULT_COMP_MAKEUP_DB = 0.0
DEFAULT_COMP_MODE = "single-band"
DEFAULT_LIMITER_CEILING_DB = -0.8
DEFAULT_LIMITER_LOOKAHEAD_MS = 2.5
DEFAULT_LIMITER_RELEASE_MS = 80.0
MULTIBAND_LOW_MID_HZ = 180.0
MULTIBAND_MID_HIGH_HZ = 3500.0
MULTIBAND_SPLIT_TAPS = 127
TAPE_RESAMPLE_TAPS_PER_PHASE = 28
PREVIEW_EXCERPT_SECONDS = 12.0
PREVIEW_ANALYSIS_SECONDS = 12.0
PREVIEW_MIXER_BUFFER = 512
PREVIEW_UI_POLL_MS = 20
PREVIEW_IDLE_UI_POLL_MS = 100
PREVIEW_STREAM_PUMP_MS = 20
PREVIEW_REFRESH_DEBOUNCE_MS = 350
PARALLEL_MIN_CHUNK_SECONDS = 6.0
PARALLEL_MAX_CHUNK_SECONDS = 20.0
PARALLEL_TARGET_CHUNKS_PER_WORKER = 3
PARALLEL_CHUNK_PAD_MS = 1200.0
MIN_PARALLEL_FILE_SECONDS = 18.0
DEFAULT_WINDOW_SIZE = "980x760"
APP_TITLE = "WAV Linear-Phase EQ + FX Tool"
SETTINGS_DIR_NAME = "WAVLinearPhaseEQ"
SETTINGS_FILE_NAME = "settings.json"
PREMIUM_PRICE_LABEL = "GBP 29.99"
LICENSE_SERVER_URL_ENV = "WAV_EQ_LICENSE_SERVER_URL"
DEFAULT_LICENSE_SERVER_URL = "https://wavequnlock.promptshieldapp.co.uk"
OUTPUT_SAMPLE_RATE_OPTIONS = ("source", "48000", "88200", "96000", "176400", "192000")
OUTPUT_BIT_DEPTH_OPTIONS = ("source", "16", "24", "32")
PITCH_MODE_OPTIONS = ("semitones", "cents", "millicents", "frequency")
PITCH_PRESET_LABELS = (
    "Custom",
    "Bach (Baroque) 415 Hz",
    "Modern 440 Hz",
    "Beethoven (Classical) 455 Hz",
)
PITCH_REFERENCE_PRESETS = {
    "Bach (Baroque) 415 Hz": ("Bach (Baroque)", 415.0, "Mellow, varied", "Well-Temperament / Meantone"),
    "Modern 440 Hz": ("Modern", 440.0, "Uniform", "Equal Temperament"),
    "Beethoven (Classical) 455 Hz": ("Beethoven (Classical)", 455.0, "Aggressive, bright", "Well-Temperament (Unequal)"),
}
PITCH_SHIFT_LIMIT_SEMITONES = 24.0
PITCH_STFT_SIZE = 2048
PITCH_HOP_SIZE = PITCH_STFT_SIZE // 4


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
        flat = unsigned.reshape(-1)
        packed = np.empty((flat.size, 3), dtype=np.uint8)
        packed[:, 0] = flat & 0xFF
        packed[:, 1] = (flat >> 8) & 0xFF
        packed[:, 2] = (flat >> 16) & 0xFF
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


def _bit_depth_scale(source_width: int, target_width: int) -> float:
    if source_width == target_width:
        return 1.0
    return _max_sample_value(target_width) / _max_sample_value(source_width)


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _db_to_linear(value_db: float) -> float:
    return 10 ** (value_db / 20.0)


def _linear_to_db(value: float, floor: float = -180.0) -> float:
    if value <= 1e-12:
        return floor
    return max(floor, 20.0 * math.log10(value))


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
    linear_db = _db_to_linear(level_db)
    mix = max(0.0, min(100.0, mix_percent)) / 100.0
    scale = max_amp * linear_db * mix
    dry_gain = _texture_amount_to_dry_gain(mix_percent)

    out = [frame[:] for frame in samples]
    for i in range(length):
        add = mono[i] * env[i] * scale
        for ch in range(len(out[i])):
            out[i][ch] = (out[i][ch] * dry_gain) + add
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
    linear_db = _db_to_linear(level_db)
    mix = max(0.0, min(100.0, mix_percent)) / 100.0
    scale = max_amp * linear_db * mix
    dry_gain = _texture_amount_to_dry_gain(mix_percent)
    return (samples * dry_gain) + (mono * env * scale)[:, np.newaxis]


def _texture_amount_to_level_db(amount_percent: float) -> float:
    amount = _clamp(amount_percent, 0.0, 100.0)
    if amount <= 60.0:
        return -42.0 + (amount * 0.4)
    return -18.0 + ((amount - 60.0) * 0.45)


def _texture_amount_to_dry_gain(amount_percent: float) -> float:
    amount = _clamp(amount_percent, 0.0, 100.0)
    if amount <= 70.0:
        return 1.0
    takeover = (amount - 70.0) / 30.0
    return max(0.0, 1.0 - (takeover ** 1.35))


def _alias_quality_profile(quality: str) -> Tuple[int, int]:
    quality_key = quality.strip().lower()
    if quality_key == "standard":
        return 2, 12
    if quality_key == "perfect":
        return 8, 48
    return 4, TAPE_RESAMPLE_TAPS_PER_PHASE


def _design_resample_filter(up: int, down: int, taps_per_phase: int = TAPE_RESAMPLE_TAPS_PER_PHASE) -> List[float]:
    max_rate = max(up, down)
    taps = max(31, taps_per_phase * max_rate * 2 + 1)
    if taps % 2 == 0:
        taps += 1
    cutoff = 1.0 / max_rate
    center = taps // 2
    coeffs: List[float] = []
    for n in range(taps):
        k = n - center
        ideal = up * cutoff * _sinc(cutoff * k)
        window = 0.42 - 0.5 * math.cos((2.0 * math.pi * n) / (taps - 1)) + 0.08 * math.cos((4.0 * math.pi * n) / (taps - 1))
        coeffs.append(ideal * window)
    total = sum(coeffs)
    if abs(total) < 1e-12:
        return coeffs
    scale = up / total
    return [c * scale for c in coeffs]


def _design_resample_filter_numpy(up: int, down: int, taps_per_phase: int = TAPE_RESAMPLE_TAPS_PER_PHASE):
    if not HAS_NUMPY:
        raise RuntimeError("NumPy is not available.")
    max_rate = max(up, down)
    taps = max(31, taps_per_phase * max_rate * 2 + 1)
    if taps % 2 == 0:
        taps += 1
    cutoff = 1.0 / max_rate
    center = taps // 2
    idx = np.arange(taps, dtype=np.float64) - float(center)
    coeffs = up * cutoff * np.sinc(cutoff * idx)
    coeffs *= np.blackman(taps)
    total = float(np.sum(coeffs))
    if abs(total) >= 1e-12:
        coeffs *= up / total
    return coeffs


def _pad_edge_1d(signal: List[float], pad: int) -> List[float]:
    if pad <= 0 or not signal:
        return signal[:]
    return [signal[0]] * pad + signal + [signal[-1]] * pad


def _catmull_rom_sample(signal: Sequence[float], position: float) -> float:
    if not signal:
        return 0.0
    idx = int(math.floor(position))
    frac = position - idx
    i0 = max(0, min(len(signal) - 1, idx - 1))
    i1 = max(0, min(len(signal) - 1, idx))
    i2 = max(0, min(len(signal) - 1, idx + 1))
    i3 = max(0, min(len(signal) - 1, idx + 2))
    p0 = signal[i0]
    p1 = signal[i1]
    p2 = signal[i2]
    p3 = signal[i3]
    return 0.5 * (
        (2.0 * p1)
        + (-p0 + p2) * frac
        + (2.0 * p0 - 5.0 * p1 + 4.0 * p2 - p3) * frac * frac
        + (-p0 + 3.0 * p1 - 3.0 * p2 + p3) * frac * frac * frac
    )


def _interpolate_oversample_1d(signal: List[float], factor: int, interpolation: str) -> List[float]:
    if not signal or factor <= 1:
        return signal[:]
    method = interpolation.strip().lower()
    out = [0.0] * (len(signal) * factor)
    last_index = max(1, len(signal) - 1)
    for idx in range(len(out)):
        position = idx / float(factor)
        position = min(position, float(last_index))
        base = int(math.floor(position))
        frac = position - base
        if method == "linear":
            next_idx = min(len(signal) - 1, base + 1)
            out[idx] = signal[base] + (signal[next_idx] - signal[base]) * frac
        else:
            out[idx] = _catmull_rom_sample(signal, position)
    return out


def _resample_poly_1d(signal: List[float], up: int, down: int, coeffs: List[float]) -> List[float]:
    if not signal:
        return []
    pad = min(len(signal), len(coeffs))
    padded = _pad_edge_1d(signal, pad)
    upsampled = [0.0] * (len(padded) * up)
    for idx, sample in enumerate(padded):
        upsampled[idx * up] = sample
    filtered = _apply_fir(upsampled, coeffs)
    if down > 1:
        filtered = filtered[::down]
    start = (pad * up) // down
    target_len = (len(signal) * up) // down
    return filtered[start : start + target_len]


def _one_pass_lowpass(signal: List[float], cutoff_hz: float, sample_rate: int) -> List[float]:
    if not signal:
        return []
    cutoff = _clamp(cutoff_hz, 5.0, sample_rate * 0.45)
    alpha = 1.0 - math.exp((-2.0 * math.pi * cutoff) / sample_rate)
    out = [0.0] * len(signal)
    y = signal[0]
    for i, sample in enumerate(signal):
        y += alpha * (sample - y)
        out[i] = y
    return out


def _dc_block(signal: List[float], pole: float = 0.995) -> List[float]:
    out = [0.0] * len(signal)
    prev_x = 0.0
    prev_y = 0.0
    for i, sample in enumerate(signal):
        y = sample - prev_x + pole * prev_y
        out[i] = y
        prev_x = sample
        prev_y = y
    return out


def _tape_nonlinearity(value: float, drive: float, bias: float) -> float:
    offset = math.tanh(drive * bias)
    pos = math.tanh(drive * (1.0 + bias)) - offset
    neg = math.tanh(drive * (-1.0 + bias)) - offset
    norm = max(abs(pos), abs(neg), 1e-9)
    return (math.tanh(drive * (value + bias)) - offset) / norm


def apply_tape_saturation(
    samples: List[List[float]],
    sample_width: int,
    sample_rate: int,
    drive_percent: float,
    mix_percent: float,
    interpolation: str,
    quality: str,
) -> List[List[float]]:
    if drive_percent <= 0 or mix_percent <= 0 or not samples:
        return samples

    max_amp = _max_sample_value(sample_width)
    wet_mix = _clamp(mix_percent, 0.0, 100.0) / 100.0
    drive_amount = _clamp(drive_percent, 0.0, 100.0) / 100.0
    drive = 1.0 + 6.0 * drive_amount
    bias = 0.012 + 0.02 * drive_amount
    makeup = 1.0 / (1.0 + 0.6 * drive_amount)
    oversample_factor, taps_per_phase = _alias_quality_profile(quality)
    oversampled_rate = sample_rate * oversample_factor
    coeffs_down = _design_resample_filter(1, oversample_factor, taps_per_phase)

    channels = len(samples[0])
    channel_data: List[List[float]] = [[frame[ch] / max_amp for frame in samples] for ch in range(channels)]
    out_channels: List[List[float]] = []
    for signal in channel_data:
        oversampled = _interpolate_oversample_1d(signal, oversample_factor, interpolation)
        body = _one_pass_lowpass(oversampled, 2_400.0, oversampled_rate)
        bass = _one_pass_lowpass(oversampled, 85.0, oversampled_rate)
        pre = [
            x + (x - b) * (0.18 + 0.22 * drive_amount) + lf * (0.03 + 0.05 * drive_amount)
            for x, b, lf in zip(oversampled, body, bass)
        ]
        saturated = [_tape_nonlinearity(value, drive, bias) for value in pre]
        softened = _one_pass_lowpass(saturated, 11_000.0 + (1.0 - drive_amount) * 5_000.0, oversampled_rate)
        wet = _dc_block(
            [((0.62 * sat) + (0.38 * soft)) * makeup for sat, soft in zip(saturated, softened)]
        )
        downsampled = _resample_poly_1d(wet, 1, oversample_factor, coeffs_down)
        mixed = [
            dry * (1.0 - wet_mix) + wet_sample * wet_mix
            for dry, wet_sample in zip(signal, downsampled)
        ]
        out_channels.append(mixed)

    frame_count = len(samples)
    out: List[List[float]] = [[0.0] * channels for _ in range(frame_count)]
    for i in range(frame_count):
        for ch in range(channels):
            out[i][ch] = out_channels[ch][i] * max_amp
    return out


def _resample_poly_numpy(samples, up: int, down: int, coeffs):
    if not HAS_NUMPY:
        raise RuntimeError("NumPy is not available.")
    if samples.size == 0:
        return samples
    pad = min(samples.shape[0], len(coeffs))
    padded = np.pad(samples, ((pad, pad), (0, 0)), mode="edge")
    upsampled = np.zeros((padded.shape[0] * up, padded.shape[1]), dtype=np.float64)
    upsampled[::up] = padded
    filtered = np.empty_like(upsampled)
    for ch in range(upsampled.shape[1]):
        filtered[:, ch] = _apply_fir_same_numpy(upsampled[:, ch], coeffs)
    if down > 1:
        filtered = filtered[::down]
    start = (pad * up) // down
    target_len = (samples.shape[0] * up) // down
    return filtered[start : start + target_len]


def _catmull_rom_numpy_channel(signal, positions):
    if signal.size == 0:
        return np.empty(0, dtype=np.float64)
    base = np.floor(positions).astype(np.int64)
    frac = positions - base
    i0 = np.clip(base - 1, 0, signal.size - 1)
    i1 = np.clip(base, 0, signal.size - 1)
    i2 = np.clip(base + 1, 0, signal.size - 1)
    i3 = np.clip(base + 2, 0, signal.size - 1)
    p0 = signal[i0]
    p1 = signal[i1]
    p2 = signal[i2]
    p3 = signal[i3]
    return 0.5 * (
        (2.0 * p1)
        + (-p0 + p2) * frac
        + (2.0 * p0 - 5.0 * p1 + 4.0 * p2 - p3) * frac * frac
        + (-p0 + 3.0 * p1 - 3.0 * p2 + p3) * frac * frac * frac
    )


def _interpolate_oversample_numpy(samples, factor: int, interpolation: str):
    if not HAS_NUMPY:
        raise RuntimeError("NumPy is not available.")
    if samples.size == 0 or factor <= 1:
        return samples
    target_len = samples.shape[0] * factor
    positions = np.arange(target_len, dtype=np.float64) / float(factor)
    positions = np.clip(positions, 0.0, float(max(0, samples.shape[0] - 1)))
    out = np.empty((target_len, samples.shape[1]), dtype=np.float64)
    if interpolation.strip().lower() == "linear":
        xp = np.arange(samples.shape[0], dtype=np.float64)
        for ch in range(samples.shape[1]):
            out[:, ch] = np.interp(positions, xp, samples[:, ch])
    else:
        for ch in range(samples.shape[1]):
            out[:, ch] = _catmull_rom_numpy_channel(samples[:, ch], positions)
    return out


def _resample_to_length_numpy(samples, target_len: int, interpolation: str = "spline"):
    if not HAS_NUMPY:
        raise RuntimeError("NumPy is not available.")
    if samples.size == 0:
        return np.empty((0, samples.shape[1] if samples.ndim == 2 else 1), dtype=np.float64)
    target_len = max(1, int(target_len))
    if samples.shape[0] == target_len:
        return samples.astype(np.float64, copy=True)
    if samples.shape[0] == 1:
        return np.repeat(samples.astype(np.float64, copy=True), target_len, axis=0)
    positions = np.linspace(0.0, float(samples.shape[0] - 1), target_len, dtype=np.float64)
    out = np.empty((target_len, samples.shape[1]), dtype=np.float64)
    if interpolation.strip().lower() == "linear":
        xp = np.arange(samples.shape[0], dtype=np.float64)
        for ch in range(samples.shape[1]):
            out[:, ch] = np.interp(positions, xp, samples[:, ch])
    else:
        for ch in range(samples.shape[1]):
            out[:, ch] = _catmull_rom_numpy_channel(samples[:, ch], positions)
    return out


def _stft_channel_numpy(signal, n_fft: int, hop_length: int):
    if not HAS_NUMPY:
        raise RuntimeError("NumPy is not available.")
    if signal.size == 0:
        return np.empty((n_fft // 2 + 1, 0), dtype=np.complex128)
    pad = n_fft // 2
    padded = np.pad(signal.astype(np.float64, copy=False), (pad, pad), mode="constant")
    if padded.size < n_fft:
        padded = np.pad(padded, (0, n_fft - padded.size), mode="constant")
    frame_count = max(1, 1 + ((padded.size - n_fft) // hop_length))
    window = np.hanning(n_fft)
    stft = np.empty((n_fft // 2 + 1, frame_count), dtype=np.complex128)
    for frame_idx in range(frame_count):
        start = frame_idx * hop_length
        frame = padded[start : start + n_fft]
        if frame.size < n_fft:
            frame = np.pad(frame, (0, n_fft - frame.size), mode="constant")
        stft[:, frame_idx] = np.fft.rfft(frame * window)
    return stft


def _istft_channel_numpy(stft, n_fft: int, hop_length: int, target_len: int):
    if not HAS_NUMPY:
        raise RuntimeError("NumPy is not available.")
    if stft.size == 0:
        return np.zeros(max(0, target_len), dtype=np.float64)
    frame_count = stft.shape[1]
    window = np.hanning(n_fft)
    total_len = n_fft + hop_length * max(0, frame_count - 1)
    out = np.zeros(total_len, dtype=np.float64)
    window_sum = np.zeros(total_len, dtype=np.float64)
    for frame_idx in range(frame_count):
        start = frame_idx * hop_length
        frame = np.fft.irfft(stft[:, frame_idx], n=n_fft).real
        out[start : start + n_fft] += frame * window
        window_sum[start : start + n_fft] += window * window
    nonzero = window_sum > 1e-9
    out[nonzero] /= window_sum[nonzero]
    pad = n_fft // 2
    if out.size > (pad * 2):
        out = out[pad:-pad]
    if out.size < target_len:
        out = np.pad(out, (0, target_len - out.size), mode="constant")
    return out[:target_len]


def _phase_vocoder_numpy(stft, rate: float, hop_length: int, n_fft: int):
    if not HAS_NUMPY:
        raise RuntimeError("NumPy is not available.")
    if stft.size == 0 or stft.shape[1] < 2:
        return stft.copy()
    time_steps = np.arange(0.0, float(stft.shape[1] - 1), rate, dtype=np.float64)
    if time_steps.size == 0:
        return stft[:, :1].copy()
    out = np.empty((stft.shape[0], time_steps.size), dtype=np.complex128)
    phase_acc = np.angle(stft[:, 0])
    phase_advance = np.arange(stft.shape[0], dtype=np.float64) * ((2.0 * math.pi * hop_length) / float(n_fft))
    out[:, 0] = stft[:, 0]
    for out_idx, step in enumerate(time_steps[1:], start=1):
        left = int(math.floor(step))
        right = min(left + 1, stft.shape[1] - 1)
        frac = step - left
        left_col = stft[:, left]
        right_col = stft[:, right]
        mag = ((1.0 - frac) * np.abs(left_col)) + (frac * np.abs(right_col))
        delta = np.angle(right_col) - np.angle(left_col) - phase_advance
        delta -= (2.0 * math.pi) * np.round(delta / (2.0 * math.pi))
        phase_acc += phase_advance + delta
        out[:, out_idx] = mag * np.exp(1j * phase_acc)
    return out


def apply_pitch_shift_numpy(samples, sample_width: int, sample_rate: int, shift_semitones: float):
    if not HAS_NUMPY:
        raise RuntimeError("Pitch shifting requires NumPy.")
    if samples.size == 0 or abs(shift_semitones) < 1e-9:
        return samples.copy()
    ratio = 2.0 ** (shift_semitones / 12.0)
    stretch_rate = 1.0 / ratio
    max_amp = _max_sample_value(sample_width)
    normalized = samples.astype(np.float64, copy=False) / max_amp
    out = np.empty_like(normalized)
    n_fft = PITCH_STFT_SIZE if sample_rate >= 32000 else 1024
    hop_length = max(256, n_fft // 4)
    for ch in range(normalized.shape[1]):
        spectrum = _stft_channel_numpy(normalized[:, ch], n_fft, hop_length)
        stretched = _phase_vocoder_numpy(spectrum, stretch_rate, hop_length, n_fft)
        time_stretched = _istft_channel_numpy(stretched, n_fft, hop_length, max(1, int(round(normalized.shape[0] / stretch_rate))))
        shifted = _resample_to_length_numpy(time_stretched[:, np.newaxis], normalized.shape[0], "spline")[:, 0]
        out[:, ch] = shifted
    return np.clip(out * max_amp, -max_amp, max_amp)


def _one_pass_lowpass_numpy(samples, cutoff_hz: float, sample_rate: int):
    if not HAS_NUMPY:
        raise RuntimeError("NumPy is not available.")
    if samples.size == 0:
        return samples
    cutoff = _clamp(cutoff_hz, 5.0, sample_rate * 0.45)
    alpha = 1.0 - math.exp((-2.0 * math.pi * cutoff) / sample_rate)
    out = np.empty_like(samples)
    y = samples[0].copy()
    out[0] = y
    for idx in range(1, samples.shape[0]):
        y = y + alpha * (samples[idx] - y)
        out[idx] = y
    return out


def _dc_block_numpy(samples, pole: float = 0.995):
    if not HAS_NUMPY:
        raise RuntimeError("NumPy is not available.")
    if samples.size == 0:
        return samples
    out = np.empty_like(samples)
    prev_x = np.zeros(samples.shape[1], dtype=np.float64)
    prev_y = np.zeros(samples.shape[1], dtype=np.float64)
    for idx in range(samples.shape[0]):
        current = samples[idx]
        y = current - prev_x + pole * prev_y
        out[idx] = y
        prev_x = current
        prev_y = y
    return out


def apply_tape_saturation_numpy(
    samples,
    sample_width: int,
    sample_rate: int,
    drive_percent: float,
    mix_percent: float,
    interpolation: str,
    quality: str,
):
    if not HAS_NUMPY:
        raise RuntimeError("NumPy is not available.")
    if drive_percent <= 0 or mix_percent <= 0 or samples.size == 0:
        return samples

    max_amp = _max_sample_value(sample_width)
    normalized = samples / max_amp
    wet_mix = _clamp(mix_percent, 0.0, 100.0) / 100.0
    drive_amount = _clamp(drive_percent, 0.0, 100.0) / 100.0
    drive = 1.0 + 6.0 * drive_amount
    bias = 0.012 + 0.02 * drive_amount
    makeup = 1.0 / (1.0 + 0.6 * drive_amount)
    oversample_factor, taps_per_phase = _alias_quality_profile(quality)
    oversampled_rate = sample_rate * oversample_factor
    coeffs_down = _design_resample_filter_numpy(1, oversample_factor, taps_per_phase)

    oversampled = _interpolate_oversample_numpy(normalized, oversample_factor, interpolation)
    body = _one_pass_lowpass_numpy(oversampled, 2_400.0, oversampled_rate)
    bass = _one_pass_lowpass_numpy(oversampled, 85.0, oversampled_rate)
    pre = oversampled + (oversampled - body) * (0.18 + 0.22 * drive_amount) + bass * (0.03 + 0.05 * drive_amount)

    offset = math.tanh(drive * bias)
    pos = math.tanh(drive * (1.0 + bias)) - offset
    neg = math.tanh(drive * (-1.0 + bias)) - offset
    norm = max(abs(pos), abs(neg), 1e-9)
    saturated = (np.tanh(drive * (pre + bias)) - offset) / norm

    softened = _one_pass_lowpass_numpy(saturated, 11_000.0 + (1.0 - drive_amount) * 5_000.0, oversampled_rate)
    wet = _dc_block_numpy(((0.62 * saturated) + (0.38 * softened)) * makeup)
    downsampled = _resample_poly_numpy(wet, 1, oversample_factor, coeffs_down)
    mixed = normalized * (1.0 - wet_mix) + downsampled * wet_mix
    return mixed * max_amp


def _compressor_gain_db(level_db: float, threshold_db: float, ratio: float, knee_db: float = 6.0) -> float:
    if ratio <= 1.0:
        return 0.0
    half_knee = knee_db * 0.5
    delta = level_db - threshold_db
    slope = (1.0 / ratio) - 1.0
    if knee_db > 0.0 and -half_knee < delta < half_knee:
        x = delta + half_knee
        return slope * (x * x) / (2.0 * knee_db)
    if delta <= -half_knee:
        return 0.0
    return slope * delta


def _valve_warmth_sample(value: float) -> float:
    return (_tape_nonlinearity(value, 1.35, 0.018) * 0.28) + (value * 0.72)


def apply_valve_warmth(samples: List[List[float]], sample_width: int) -> List[List[float]]:
    if not samples:
        return samples
    max_amp = _max_sample_value(sample_width)
    out = [frame[:] for frame in samples]
    for frame in out:
        for ch in range(len(frame)):
            frame[ch] = _valve_warmth_sample(frame[ch] / max_amp) * max_amp
    return out


def apply_valve_warmth_numpy(samples, sample_width: int):
    if not HAS_NUMPY:
        raise RuntimeError("NumPy is not available.")
    if samples.size == 0:
        return samples
    max_amp = _max_sample_value(sample_width)
    normalized = samples / max_amp
    dry = normalized
    wet = np.tanh(1.35 * (normalized + 0.018)) - math.tanh(1.35 * 0.018)
    pos = math.tanh(1.35 * (1.0 + 0.018)) - math.tanh(1.35 * 0.018)
    neg = math.tanh(1.35 * (-1.0 + 0.018)) - math.tanh(1.35 * 0.018)
    norm = max(abs(pos), abs(neg), 1e-9)
    return (((wet / norm) * 0.28) + dry * 0.72) * max_amp


def apply_compressor(
    samples: List[List[float]],
    sample_width: int,
    sample_rate: int,
    threshold_db: float,
    ratio: float,
    attack_ms: float,
    release_ms: float,
    makeup_db: float,
    valve_warmth: bool,
) -> List[List[float]]:
    if not samples or ratio <= 1.0:
        return samples

    max_amp = _max_sample_value(sample_width)
    attack_ms = max(0.1, attack_ms)
    release_ms = max(1.0, release_ms)
    attack_coeff = math.exp(-1.0 / (attack_ms * 0.001 * sample_rate))
    release_coeff = math.exp(-1.0 / (release_ms * 0.001 * sample_rate))
    makeup_gain = _db_to_linear(makeup_db)
    current_gr_db = 0.0

    out = [frame[:] for frame in samples]
    for i, frame in enumerate(out):
        rms = math.sqrt(sum((sample / max_amp) ** 2 for sample in frame) / len(frame))
        target_gr_db = _compressor_gain_db(_linear_to_db(rms), threshold_db, ratio)
        coeff = attack_coeff if target_gr_db < current_gr_db else release_coeff
        current_gr_db = coeff * current_gr_db + (1.0 - coeff) * target_gr_db
        gain = _db_to_linear(current_gr_db) * makeup_gain
        for ch in range(len(frame)):
            frame[ch] = (frame[ch] / max_amp) * gain * max_amp
    if valve_warmth:
        return apply_valve_warmth(out, sample_width)
    return out


def apply_compressor_numpy(
    samples,
    sample_width: int,
    sample_rate: int,
    threshold_db: float,
    ratio: float,
    attack_ms: float,
    release_ms: float,
    makeup_db: float,
    valve_warmth: bool,
):
    if not HAS_NUMPY:
        raise RuntimeError("NumPy is not available.")
    if samples.size == 0 or ratio <= 1.0:
        return samples

    max_amp = _max_sample_value(sample_width)
    normalized = samples / max_amp
    attack_ms = max(0.1, attack_ms)
    release_ms = max(1.0, release_ms)
    attack_coeff = math.exp(-1.0 / (attack_ms * 0.001 * sample_rate))
    release_coeff = math.exp(-1.0 / (release_ms * 0.001 * sample_rate))
    makeup_gain = _db_to_linear(makeup_db)
    detector = np.sqrt(np.mean(np.square(normalized), axis=1))
    gains = np.empty(normalized.shape[0], dtype=np.float64)
    current_gr_db = 0.0
    for idx, level in enumerate(detector):
        target_gr_db = _compressor_gain_db(_linear_to_db(float(level)), threshold_db, ratio)
        coeff = attack_coeff if target_gr_db < current_gr_db else release_coeff
        current_gr_db = coeff * current_gr_db + (1.0 - coeff) * target_gr_db
        gains[idx] = _db_to_linear(current_gr_db) * makeup_gain
    out = normalized * gains[:, np.newaxis]
    if valve_warmth:
        return apply_valve_warmth_numpy(out * max_amp, sample_width)
    return out * max_amp


def _split_multiband(samples: List[List[float]], sample_rate: int, taps: int = MULTIBAND_SPLIT_TAPS):
    low = apply_linear_phase_eq(samples, sample_rate, 0.0, MULTIBAND_LOW_MID_HZ, taps)
    high = apply_linear_phase_eq(samples, sample_rate, MULTIBAND_MID_HIGH_HZ, sample_rate / 2.0 - 1.0, taps)
    mid = apply_linear_phase_eq(samples, sample_rate, MULTIBAND_LOW_MID_HZ, MULTIBAND_MID_HIGH_HZ, taps)
    return low, mid, high


def _split_multiband_numpy(samples, sample_rate: int):
    if not HAS_NUMPY:
        raise RuntimeError("NumPy is not available.")
    low = _apply_spectral_band_filter_numpy(samples, sample_rate, 0.0, MULTIBAND_LOW_MID_HZ)
    mid = _apply_spectral_band_filter_numpy(samples, sample_rate, MULTIBAND_LOW_MID_HZ, MULTIBAND_MID_HIGH_HZ)
    high = _apply_spectral_band_filter_numpy(samples, sample_rate, MULTIBAND_MID_HIGH_HZ, sample_rate / 2.0 - 1.0)
    return low, mid, high


def apply_multiband_compressor(
    samples: List[List[float]],
    sample_width: int,
    sample_rate: int,
    threshold_db: float,
    ratio: float,
    attack_ms: float,
    release_ms: float,
    makeup_db: float,
    valve_warmth: bool,
) -> List[List[float]]:
    if not samples or ratio <= 1.0:
        return samples
    low, mid, high = _split_multiband(samples, sample_rate)
    low = apply_compressor(low, sample_width, sample_rate, threshold_db, ratio, attack_ms, release_ms, makeup_db, False)
    mid = apply_compressor(mid, sample_width, sample_rate, threshold_db, ratio, attack_ms, release_ms, makeup_db, False)
    high = apply_compressor(high, sample_width, sample_rate, threshold_db, ratio, attack_ms, release_ms, makeup_db, False)
    out = [frame[:] for frame in samples]
    for i in range(len(out)):
        for ch in range(len(out[i])):
            out[i][ch] = low[i][ch] + mid[i][ch] + high[i][ch]
    if valve_warmth:
        return apply_valve_warmth(out, sample_width)
    return out


def apply_multiband_compressor_numpy(
    samples,
    sample_width: int,
    sample_rate: int,
    threshold_db: float,
    ratio: float,
    attack_ms: float,
    release_ms: float,
    makeup_db: float,
    valve_warmth: bool,
):
    if not HAS_NUMPY:
        raise RuntimeError("NumPy is not available.")
    if samples.size == 0 or ratio <= 1.0:
        return samples
    low, mid, high = _split_multiband_numpy(samples, sample_rate)
    low = apply_compressor_numpy(low, sample_width, sample_rate, threshold_db, ratio, attack_ms, release_ms, makeup_db, False)
    mid = apply_compressor_numpy(mid, sample_width, sample_rate, threshold_db, ratio, attack_ms, release_ms, makeup_db, False)
    high = apply_compressor_numpy(high, sample_width, sample_rate, threshold_db, ratio, attack_ms, release_ms, makeup_db, False)
    out = low + mid + high
    if valve_warmth:
        return apply_valve_warmth_numpy(out, sample_width)
    return out


def _future_peak_window(values: List[float], lookahead: int) -> List[float]:
    if not values:
        return []
    peaks = [0.0] * len(values)
    dq: deque[Tuple[int, float]] = deque()
    for idx in range(len(values) - 1, -1, -1):
        end = idx + lookahead
        while dq and dq[0][0] > end:
            dq.popleft()
        current = values[idx]
        while dq and dq[-1][1] <= current:
            dq.pop()
        dq.append((idx, current))
        peaks[idx] = dq[0][1]
    return peaks


def apply_limiter(
    samples: List[List[float]],
    sample_width: int,
    sample_rate: int,
    ceiling_db: float,
    lookahead_ms: float,
    release_ms: float,
) -> List[List[float]]:
    if not samples:
        return samples
    max_amp = _max_sample_value(sample_width)
    ceiling = _db_to_linear(min(0.0, ceiling_db))
    lookahead = max(1, int(sample_rate * max(0.1, lookahead_ms) / 1000.0))
    release_coeff = math.exp(-1.0 / (max(1.0, release_ms) * 0.001 * sample_rate))
    linked_peaks = [max(abs(sample / max_amp) for sample in frame) for frame in samples]
    future_peaks = _future_peak_window(linked_peaks, lookahead)

    current_gain = 1.0
    out = [frame[:] for frame in samples]
    for i, frame in enumerate(out):
        desired = 1.0
        if future_peaks[i] > ceiling:
            desired = ceiling / future_peaks[i]
        current_gain = min(current_gain, desired)
        if desired >= current_gain:
            current_gain = release_coeff * current_gain + (1.0 - release_coeff) * desired
        for ch in range(len(frame)):
            value = (frame[ch] / max_amp) * current_gain
            frame[ch] = _clamp(value, -ceiling, ceiling) * max_amp
    return out


def apply_limiter_numpy(samples, sample_width: int, sample_rate: int, ceiling_db: float, lookahead_ms: float, release_ms: float):
    if not HAS_NUMPY:
        raise RuntimeError("NumPy is not available.")
    if samples.size == 0:
        return samples
    max_amp = _max_sample_value(sample_width)
    normalized = samples / max_amp
    ceiling = _db_to_linear(min(0.0, ceiling_db))
    lookahead = max(1, int(sample_rate * max(0.1, lookahead_ms) / 1000.0))
    release_coeff = math.exp(-1.0 / (max(1.0, release_ms) * 0.001 * sample_rate))
    linked_peaks = np.max(np.abs(normalized), axis=1)
    future_peaks = np.array(_future_peak_window(linked_peaks.tolist(), lookahead), dtype=np.float64)
    gains = np.empty(normalized.shape[0], dtype=np.float64)
    current_gain = 1.0
    for idx, future_peak in enumerate(future_peaks):
        desired = 1.0 if future_peak <= ceiling else ceiling / future_peak
        current_gain = min(current_gain, desired)
        if desired >= current_gain:
            current_gain = release_coeff * current_gain + (1.0 - release_coeff) * desired
        gains[idx] = current_gain
    limited = np.clip(normalized * gains[:, np.newaxis], -ceiling, ceiling)
    return limited * max_amp


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


def _read_wav_analysis_data(path: pathlib.Path):
    with wave.open(str(path), "rb") as wf:
        channels = wf.getnchannels()
        sample_width = wf.getsampwidth()
        sample_rate = wf.getframerate()
        frame_count = wf.getnframes()
        if wf.getcomptype() != "NONE":
            raise WavReadError("Compressed WAV is not supported.")
        frames = wf.readframes(frame_count)

    max_amp = _max_sample_value(sample_width)
    duration_s = frame_count / float(sample_rate) if sample_rate > 0 else 0.0
    if HAS_NUMPY:
        samples = _bytes_to_numpy_samples(frames, sample_width, channels) / max_amp
        mono = np.mean(samples, axis=1) if channels > 1 else samples[:, 0]
        peak = float(np.max(np.abs(samples))) if samples.size else 0.0
        return sample_rate, duration_s, mono.astype(np.float64), peak

    raw = _bytes_to_samples(frames, sample_width, channels)
    mono = [sum(frame) / (len(frame) * max_amp) for frame in raw]
    peak = max((max(abs(sample) / max_amp for sample in frame) for frame in raw), default=0.0)
    return sample_rate, duration_s, mono, peak


def _build_waveform_summary(mono, bins: int = 720):
    if HAS_NUMPY and isinstance(mono, np.ndarray):
        if mono.size == 0:
            return []
        edges = np.linspace(0, mono.size, bins + 1, dtype=int)
        summary: List[Tuple[float, float]] = []
        for start, end in zip(edges[:-1], edges[1:]):
            if end <= start:
                sample = float(mono[min(start, mono.size - 1)])
                summary.append((sample, sample))
            else:
                segment = mono[start:end]
                summary.append((float(np.min(segment)), float(np.max(segment))))
        return summary

    if not mono:
        return []
    edges = [int((idx * len(mono)) / bins) for idx in range(bins + 1)]
    summary = []
    for start, end in zip(edges[:-1], edges[1:]):
        if end <= start:
            sample = mono[min(start, len(mono) - 1)]
            summary.append((sample, sample))
        else:
            segment = mono[start:end]
            summary.append((min(segment), max(segment)))
    return summary


def _build_spectrum_summary(mono, sample_rate: int, points: int = 512):
    if not HAS_NUMPY or not isinstance(mono, np.ndarray) or mono.size == 0:
        return []
    segment_len = min(max(2048, 1 << int(math.log2(max(2048, min(mono.size, 16384))))), mono.size)
    start = max(0, (mono.size - segment_len) // 2)
    segment = mono[start : start + segment_len]
    window = np.hanning(segment_len)
    spectrum = np.abs(np.fft.rfft(segment * window))
    freqs = np.fft.rfftfreq(segment_len, d=1.0 / sample_rate)
    max_freq = max(100.0, min(sample_rate / 2.0, 20_000.0))
    target_freqs = np.geomspace(20.0, max_freq, points)
    mags_db = 20.0 * np.log10(spectrum + 1e-9)
    interp = np.interp(target_freqs, freqs, mags_db)
    return list(zip(target_freqs.tolist(), interp.tolist()))


def _build_spectrogram_summary(mono, sample_rate: int, width: int = 360, height: int = 96):
    if not HAS_NUMPY or not isinstance(mono, np.ndarray) or mono.size == 0:
        return None
    n_fft = min(2048, max(512, 1 << int(math.log2(max(512, min(mono.size, 2048))))))
    if mono.size < n_fft:
        padded = np.zeros(n_fft, dtype=np.float64)
        padded[: mono.size] = mono
        mono = padded
    hop = max(1, (mono.size - n_fft) // max(1, width - 1))
    window = np.hanning(n_fft)
    freqs = np.fft.rfftfreq(n_fft, d=1.0 / sample_rate)
    max_freq = max(100.0, min(sample_rate / 2.0, 20_000.0))
    target_freqs = np.geomspace(20.0, max_freq, height)
    band_indices = np.clip(np.searchsorted(freqs, target_freqs), 0, freqs.size - 1)

    image = np.zeros((height, width), dtype=np.uint8)
    for x in range(width):
        start = min(x * hop, max(0, mono.size - n_fft))
        frame = mono[start : start + n_fft]
        spectrum = np.abs(np.fft.rfft(frame * window))
        mags_db = 20.0 * np.log10(spectrum + 1e-9)
        selected = mags_db[band_indices]
        normalized = np.clip((selected + 96.0) * (255.0 / 96.0), 0.0, 255.0).astype(np.uint8)
        image[:, x] = normalized[::-1]
    return image


def build_preview_analysis(path: pathlib.Path) -> dict[str, object]:
    sample_rate, duration_s, mono, peak = _read_wav_analysis_data(path)
    return {
        "path": str(path),
        "sample_rate": sample_rate,
        "duration_s": duration_s,
        "peak_dbfs": _linear_to_db(peak),
        "waveform": _build_waveform_summary(mono),
        "spectrum": _build_spectrum_summary(mono, sample_rate),
        "spectrogram": _build_spectrogram_summary(mono, sample_rate),
    }


def build_preview_analysis_from_frames(
    frames: bytes,
    channels: int,
    sample_width: int,
    sample_rate: int,
    path: str = "",
) -> dict[str, object]:
    max_amp = _max_sample_value(sample_width)
    frame_count = len(frames) // max(1, channels * sample_width)
    duration_s = frame_count / float(sample_rate) if sample_rate > 0 else 0.0
    if HAS_NUMPY:
        samples = _bytes_to_numpy_samples(frames, sample_width, channels) / max_amp
        mono = np.mean(samples, axis=1) if channels > 1 else samples[:, 0]
        peak = float(np.max(np.abs(samples))) if samples.size else 0.0
    else:
        raw = _bytes_to_samples(frames, sample_width, channels)
        mono = [sum(frame) / (len(frame) * max_amp) for frame in raw]
        peak = max((max(abs(sample) / max_amp for sample in frame) for frame in raw), default=0.0)
    return {
        "path": path,
        "sample_rate": sample_rate,
        "duration_s": duration_s,
        "peak_dbfs": _linear_to_db(peak),
        "waveform": _build_waveform_summary(mono),
        "spectrum": _build_spectrum_summary(mono, sample_rate),
        "spectrogram": _build_spectrogram_summary(mono, sample_rate),
    }


def _slice_frame_bytes(data: bytes, channels: int, sample_width: int, start_frame: int, frame_count: int) -> bytes:
    bytes_per_frame = channels * sample_width
    start = max(0, start_frame) * bytes_per_frame
    end = start + max(0, frame_count) * bytes_per_frame
    return data[start:end]


def _convert_frames_to_preview_pcm16(data: bytes, channels: int, sample_width: int) -> bytes:
    if sample_width == 2:
        return data
    if HAS_NUMPY:
        samples = _bytes_to_numpy_samples(data, sample_width, channels)
        scale = 32767.0 / _max_sample_value(sample_width)
        converted = np.clip(np.rint(samples * scale), -32768, 32767).astype("<i2")
        return converted.tobytes()

    scale = 32767.0 / _max_sample_value(sample_width)
    converted_samples = _bytes_to_samples(data, sample_width, channels)
    for frame in converted_samples:
        for idx, sample in enumerate(frame):
            frame[idx] = sample * scale
    return _samples_to_bytes(converted_samples, 2)


def _crossfade_preview_pcm16(outgoing_tail: bytes, incoming_head: bytes, channels: int) -> bytes:
    if not outgoing_tail or not incoming_head:
        return incoming_head or outgoing_tail
    bytes_per_frame = max(1, channels * 2)
    frame_count = min(len(outgoing_tail), len(incoming_head)) // bytes_per_frame
    if frame_count <= 0:
        return incoming_head
    byte_count = frame_count * bytes_per_frame
    tail = outgoing_tail[:byte_count]
    head = incoming_head[:byte_count]

    if HAS_NUMPY:
        tail_samples = np.frombuffer(tail, dtype="<i2").astype(np.float64).reshape(frame_count, channels)
        head_samples = np.frombuffer(head, dtype="<i2").astype(np.float64).reshape(frame_count, channels)
        fade_in = np.linspace(0.0, 1.0, frame_count, endpoint=True, dtype=np.float64)[:, np.newaxis]
        fade_out = 1.0 - fade_in
        mixed = np.clip(np.rint((tail_samples * fade_out) + (head_samples * fade_in)), -32768, 32767).astype("<i2")
        return mixed.tobytes()

    tail_samples = _bytes_to_samples(tail, 2, channels)
    head_samples = _bytes_to_samples(head, 2, channels)
    out: List[List[float]] = [[0.0] * channels for _ in range(frame_count)]
    for idx in range(frame_count):
        fade_in = idx / max(1, frame_count - 1)
        fade_out = 1.0 - fade_in
        for ch in range(channels):
            out[idx][ch] = (tail_samples[idx][ch] * fade_out) + (head_samples[idx][ch] * fade_in)
    return _samples_to_bytes(out, 2)


def _resample_quality_cutoff_ratio(quality: str) -> float:
    quality_key = quality.strip().lower()
    if quality_key == "standard":
        return 0.94
    if quality_key == "perfect":
        return 0.84
    return 0.89


def _apply_resample_correction(samples: List[List[float]], sample_rate: int, cutoff_hz: float, quality: str) -> List[List[float]]:
    if not samples:
        return samples
    quality_key = quality.strip().lower()
    cutoff = _clamp(cutoff_hz, 20.0, sample_rate * 0.49)
    if quality_key == "standard":
        channels = len(samples[0])
        out = [[0.0] * channels for _ in range(len(samples))]
        for ch in range(channels):
            filtered = _one_pass_lowpass([frame[ch] for frame in samples], cutoff, sample_rate)
            for idx, value in enumerate(filtered):
                out[idx][ch] = value
        return out
    if quality_key == "high":
        channels = len(samples[0])
        out = [[0.0] * channels for _ in range(len(samples))]
        for ch in range(channels):
            filtered = _one_pass_lowpass([frame[ch] for frame in samples], cutoff, sample_rate)
            filtered = _one_pass_lowpass(filtered, cutoff, sample_rate)
            for idx, value in enumerate(filtered):
                out[idx][ch] = value
        return out
    return apply_linear_phase_eq(samples, sample_rate, 0.0, cutoff, 127)


def _apply_resample_correction_numpy(samples, sample_rate: int, cutoff_hz: float, quality: str):
    if not HAS_NUMPY:
        raise RuntimeError("NumPy is not available.")
    if samples.size == 0:
        return samples
    quality_key = quality.strip().lower()
    cutoff = _clamp(cutoff_hz, 20.0, sample_rate * 0.49)
    if quality_key == "standard":
        return _one_pass_lowpass_numpy(samples, cutoff, sample_rate)
    if quality_key == "high":
        return _one_pass_lowpass_numpy(_one_pass_lowpass_numpy(samples, cutoff, sample_rate), cutoff, sample_rate)
    return _apply_spectral_band_filter_numpy(samples, sample_rate, 0.0, cutoff)


def _resample_channel(signal: List[float], source_rate: int, target_rate: int, interpolation: str) -> List[float]:
    if not signal or source_rate <= 0 or target_rate <= 0 or source_rate == target_rate:
        return signal[:]
    target_len = max(1, int(round(len(signal) * (target_rate / float(source_rate)))))
    out = [0.0] * target_len
    last_pos = max(0.0, float(len(signal) - 1))
    method = interpolation.strip().lower()
    for idx in range(target_len):
        position = min(last_pos, idx * (source_rate / float(target_rate)))
        base = int(math.floor(position))
        frac = position - base
        if method == "linear":
            next_idx = min(len(signal) - 1, base + 1)
            out[idx] = signal[base] + ((signal[next_idx] - signal[base]) * frac)
        else:
            out[idx] = _catmull_rom_sample(signal, position)
    return out


def _resample_audio(samples: List[List[float]], source_rate: int, target_rate: int, interpolation: str, quality: str) -> List[List[float]]:
    if not samples or source_rate <= 0 or target_rate <= 0 or source_rate == target_rate:
        return [frame[:] for frame in samples]
    working = [frame[:] for frame in samples]
    shared_cutoff = min(source_rate, target_rate) * 0.5 * _resample_quality_cutoff_ratio(quality)
    if target_rate < source_rate:
        working = _apply_resample_correction(working, source_rate, shared_cutoff, quality)
    channels = len(working[0])
    channel_data = [[frame[ch] for frame in working] for ch in range(channels)]
    resampled_channels = [_resample_channel(sig, source_rate, target_rate, interpolation) for sig in channel_data]
    target_len = len(resampled_channels[0]) if resampled_channels else 0
    out = [[0.0] * channels for _ in range(target_len)]
    for idx in range(target_len):
        for ch in range(channels):
            out[idx][ch] = resampled_channels[ch][idx]
    if target_rate > source_rate:
        smoothing_cutoff = source_rate * 0.5 * _resample_quality_cutoff_ratio(quality)
        out = _apply_resample_correction(out, target_rate, smoothing_cutoff, quality)
    return out


def _resample_audio_numpy(samples, source_rate: int, target_rate: int, interpolation: str, quality: str):
    if not HAS_NUMPY:
        raise RuntimeError("NumPy is not available.")
    if samples.size == 0 or source_rate <= 0 or target_rate <= 0 or source_rate == target_rate:
        return samples.copy()
    working = samples.astype(np.float64, copy=True)
    shared_cutoff = min(source_rate, target_rate) * 0.5 * _resample_quality_cutoff_ratio(quality)
    if target_rate < source_rate:
        working = _apply_resample_correction_numpy(working, source_rate, shared_cutoff, quality)
    target_len = max(1, int(round(working.shape[0] * (target_rate / float(source_rate)))))
    positions = np.arange(target_len, dtype=np.float64) * (source_rate / float(target_rate))
    positions = np.clip(positions, 0.0, float(max(0, working.shape[0] - 1)))
    out = np.empty((target_len, working.shape[1]), dtype=np.float64)
    if interpolation.strip().lower() == "linear":
        xp = np.arange(working.shape[0], dtype=np.float64)
        for ch in range(working.shape[1]):
            out[:, ch] = np.interp(positions, xp, working[:, ch])
    else:
        for ch in range(working.shape[1]):
            out[:, ch] = _catmull_rom_numpy_channel(working[:, ch], positions)
    if target_rate > source_rate:
        smoothing_cutoff = source_rate * 0.5 * _resample_quality_cutoff_ratio(quality)
        out = _apply_resample_correction_numpy(out, target_rate, smoothing_cutoff, quality)
    return out


def convert_output_format(
    frames: bytes,
    channels: int,
    sample_width: int,
    sample_rate: int,
    target_sample_rate: int | None,
    target_sample_width: int | None,
    interpolation: str,
    quality: str,
) -> tuple[bytes, int, int]:
    output_rate = target_sample_rate or sample_rate
    output_width = target_sample_width or sample_width
    if output_rate == sample_rate and output_width == sample_width:
        return frames, sample_width, sample_rate

    if HAS_NUMPY:
        samples = _bytes_to_numpy_samples(frames, sample_width, channels)
        if output_rate != sample_rate:
            samples = _resample_audio_numpy(samples, sample_rate, output_rate, interpolation, quality)
        if output_width != sample_width:
            samples = samples * _bit_depth_scale(sample_width, output_width)
        return _numpy_samples_to_bytes(samples, output_width), output_width, output_rate

    samples = _bytes_to_samples(frames, sample_width, channels)
    if output_rate != sample_rate:
        samples = _resample_audio(samples, sample_rate, output_rate, interpolation, quality)
    if output_width != sample_width:
        scale = _bit_depth_scale(sample_width, output_width)
        scaled_samples = [frame[:] for frame in samples]
        for frame in scaled_samples:
            for idx, sample in enumerate(frame):
                frame[idx] = sample * scale
        samples = scaled_samples
    return _samples_to_bytes(samples, output_width), output_width, output_rate


def apply_stereo_enhance(samples: List[List[float]], width_percent: float) -> List[List[float]]:
    if not samples or len(samples[0]) < 2:
        return samples
    width = _clamp(width_percent, 0.0, 200.0) / 100.0
    if abs(width - 1.0) < 1e-9:
        return samples
    out = [frame[:] for frame in samples]
    for frame in out:
        left = frame[0]
        right = frame[1]
        mid = (left + right) * 0.5
        side = (left - right) * 0.5 * width
        frame[0] = mid + side
        frame[1] = mid - side
    return out


def apply_stereo_enhance_numpy(samples, width_percent: float):
    if not HAS_NUMPY:
        raise RuntimeError("NumPy is not available.")
    if samples.size == 0 or samples.shape[1] < 2:
        return samples
    width = _clamp(width_percent, 0.0, 200.0) / 100.0
    if abs(width - 1.0) < 1e-9:
        return samples
    out = samples.copy()
    mid = (samples[:, 0] + samples[:, 1]) * 0.5
    side = (samples[:, 0] - samples[:, 1]) * 0.5 * width
    out[:, 0] = mid + side
    out[:, 1] = mid - side
    return out


def process_wav_data_numpy(
    frames: bytes,
    channels: int,
    sample_width: int,
    sample_rate: int,
    high_pass_hz: float,
    low_pass_hz: float,
    pitch_enabled: bool,
    pitch_shift_semitones: float,
    tape_enabled: bool,
    tape_drive_percent: float,
    tape_mix_percent: float,
    tape_alias_interpolation: str,
    tape_alias_quality: str,
    compressor_enabled: bool,
    compressor_mode: str,
    compressor_threshold_db: float,
    compressor_ratio: float,
    compressor_attack_ms: float,
    compressor_release_ms: float,
    compressor_makeup_db: float,
    compressor_valve_warmth: bool,
    limiter_enabled: bool,
    limiter_ceiling_db: float,
    limiter_lookahead_ms: float,
    limiter_release_ms: float,
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
    stereo_enabled: bool,
    stereo_width_percent: float,
    eq_taps: int,
) -> bytes:
    samples = _bytes_to_numpy_samples(frames, sample_width, channels)
    samples = apply_linear_phase_eq_numpy(samples, sample_rate, high_pass_hz, low_pass_hz, eq_taps)
    if pitch_enabled:
        samples = apply_pitch_shift_numpy(samples, sample_width, sample_rate, pitch_shift_semitones)
    if tape_enabled:
        samples = apply_tape_saturation_numpy(
            samples,
            sample_width,
            sample_rate,
            tape_drive_percent,
            tape_mix_percent,
            tape_alias_interpolation,
            tape_alias_quality,
        )
    if compressor_enabled:
        if compressor_mode == "multiband":
            samples = apply_multiband_compressor_numpy(
                samples,
                sample_width,
                sample_rate,
                compressor_threshold_db,
                compressor_ratio,
                compressor_attack_ms,
                compressor_release_ms,
                compressor_makeup_db,
                compressor_valve_warmth,
            )
        else:
            samples = apply_compressor_numpy(
                samples,
                sample_width,
                sample_rate,
                compressor_threshold_db,
                compressor_ratio,
                compressor_attack_ms,
                compressor_release_ms,
                compressor_makeup_db,
                compressor_valve_warmth,
            )
    if offset_enabled:
        samples, _ = apply_random_stem_offsets_numpy(samples, sample_rate, offset_max_ms)
    samples = add_texture_numpy(samples, sample_rate, sample_width, texture_type, texture_mix_percent, texture_level_db, texture_fade_ms)
    if humanize_enabled:
        samples = apply_dynamic_humanize_numpy(samples, humanize_min_db, humanize_max_db, humanize_section_ms, sample_rate)
    if stereo_enabled:
        samples = apply_stereo_enhance_numpy(samples, stereo_width_percent)
    if limiter_enabled:
        samples = apply_limiter_numpy(
            samples, sample_width, sample_rate, limiter_ceiling_db, limiter_lookahead_ms, limiter_release_ms
        )
    return _numpy_samples_to_bytes(samples, sample_width)


def process_wav_frames(
    frames: bytes,
    channels: int,
    sample_width: int,
    sample_rate: int,
    high_pass_hz: float,
    low_pass_hz: float,
    pitch_enabled: bool,
    pitch_shift_semitones: float,
    tape_enabled: bool,
    tape_drive_percent: float,
    tape_mix_percent: float,
    tape_alias_interpolation: str,
    tape_alias_quality: str,
    compressor_enabled: bool,
    compressor_mode: str,
    compressor_threshold_db: float,
    compressor_ratio: float,
    compressor_attack_ms: float,
    compressor_release_ms: float,
    compressor_makeup_db: float,
    compressor_valve_warmth: bool,
    limiter_enabled: bool,
    limiter_ceiling_db: float,
    limiter_lookahead_ms: float,
    limiter_release_ms: float,
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
    stereo_enabled: bool,
    stereo_width_percent: float,
    eq_taps: int,
) -> bytes:
    if HAS_NUMPY:
        return process_wav_data_numpy(
            frames,
            channels,
            sample_width,
            sample_rate,
            high_pass_hz,
            low_pass_hz,
            pitch_enabled,
            pitch_shift_semitones,
            tape_enabled,
            tape_drive_percent,
            tape_mix_percent,
            tape_alias_interpolation,
            tape_alias_quality,
            compressor_enabled,
            compressor_mode,
            compressor_threshold_db,
            compressor_ratio,
            compressor_attack_ms,
            compressor_release_ms,
            compressor_makeup_db,
            compressor_valve_warmth,
            limiter_enabled,
            limiter_ceiling_db,
            limiter_lookahead_ms,
            limiter_release_ms,
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
            stereo_enabled,
            stereo_width_percent,
            eq_taps,
        )

    samples = _bytes_to_samples(frames, sample_width, channels)
    samples = apply_linear_phase_eq(samples, sample_rate, high_pass_hz, low_pass_hz, eq_taps)
    if pitch_enabled:
        raise RuntimeError("Pitch shifting requires NumPy.")
    if tape_enabled:
        samples = apply_tape_saturation(
            samples,
            sample_width,
            sample_rate,
            tape_drive_percent,
            tape_mix_percent,
            tape_alias_interpolation,
            tape_alias_quality,
        )
    if compressor_enabled:
        if compressor_mode == "multiband":
            samples = apply_multiband_compressor(
                samples,
                sample_width,
                sample_rate,
                compressor_threshold_db,
                compressor_ratio,
                compressor_attack_ms,
                compressor_release_ms,
                compressor_makeup_db,
                compressor_valve_warmth,
            )
        else:
            samples = apply_compressor(
                samples,
                sample_width,
                sample_rate,
                compressor_threshold_db,
                compressor_ratio,
                compressor_attack_ms,
                compressor_release_ms,
                compressor_makeup_db,
                compressor_valve_warmth,
            )

    if offset_enabled:
        samples, _ = apply_random_stem_offsets(samples, sample_rate, offset_max_ms)

    samples = add_texture(samples, sample_rate, sample_width, texture_type, texture_mix_percent, texture_level_db, texture_fade_ms)

    if humanize_enabled:
        samples = apply_dynamic_humanize(samples, humanize_min_db, humanize_max_db, humanize_section_ms, sample_rate)
    if stereo_enabled:
        samples = apply_stereo_enhance(samples, stereo_width_percent)
    if limiter_enabled:
        samples = apply_limiter(samples, sample_width, sample_rate, limiter_ceiling_db, limiter_lookahead_ms, limiter_release_ms)
    return _samples_to_bytes(samples, sample_width)


def finalize_processed_output(
    source_path: pathlib.Path,
    output_directory: pathlib.Path | None,
    produced_by: str,
    data_bytes: bytes,
    channels: int,
    sample_width: int,
    sample_rate: int,
    high_pass_hz: float,
    low_pass_hz: float,
    pitch_enabled: bool,
    pitch_shift_semitones: float,
    tape_enabled: bool,
    tape_drive_percent: float,
    tape_mix_percent: float,
    tape_alias_interpolation: str,
    tape_alias_quality: str,
    compressor_enabled: bool,
    compressor_mode: str,
    compressor_threshold_db: float,
    compressor_ratio: float,
    compressor_attack_ms: float,
    compressor_release_ms: float,
    compressor_makeup_db: float,
    compressor_valve_warmth: bool,
    limiter_enabled: bool,
    limiter_ceiling_db: float,
    limiter_lookahead_ms: float,
    limiter_release_ms: float,
    texture_type: str,
    texture_mix_percent: float,
    humanize_enabled: bool,
    offset_enabled: bool,
    offset_max_ms: float,
    stereo_enabled: bool,
    stereo_width_percent: float,
    output_sample_rate: int | None,
    output_sample_width: int | None,
    output_alias_interpolation: str,
    output_alias_quality: str,
) -> pathlib.Path:
    data_bytes, final_sample_width, final_sample_rate = convert_output_format(
        data_bytes,
        channels,
        sample_width,
        sample_rate,
        output_sample_rate,
        output_sample_width,
        output_alias_interpolation,
        output_alias_quality,
    )

    hp_label = int(round(high_pass_hz))
    lp_label = int(round(low_pass_hz))
    suffix_parts = [f"hp{hp_label}", f"lp{lp_label}"]
    if pitch_enabled and abs(pitch_shift_semitones) >= 1e-4:
        pitch_cents = int(round(pitch_shift_semitones * 100.0))
        suffix_parts.append(f"pitch{'p' if pitch_cents >= 0 else 'm'}{abs(pitch_cents)}c")
    if tape_enabled:
        suffix_parts.append(f"tape{int(round(tape_drive_percent))}d{int(round(tape_mix_percent))}m")
        suffix_parts.append(tape_alias_interpolation[:3].lower())
        suffix_parts.append(tape_alias_quality[:1].lower())
    if compressor_enabled:
        suffix_parts.append(f"comp{int(round(abs(compressor_threshold_db)))}t{int(round(compressor_ratio))}r")
        if compressor_mode == "multiband":
            suffix_parts.append("mb")
        if compressor_valve_warmth:
            suffix_parts.append("valve")
    if texture_type != "none":
        suffix_parts.append(f"{texture_type}{int(round(texture_mix_percent))}pct")
    if humanize_enabled:
        suffix_parts.append("human")
    if offset_enabled:
        suffix_parts.append(f"off{int(round(offset_max_ms))}ms")
    if limiter_enabled:
        suffix_parts.append(f"lim{abs(limiter_ceiling_db):.1f}".replace(".", "p"))
    if stereo_enabled:
        suffix_parts.append(f"st{int(round(stereo_width_percent))}w")
    if output_sample_rate is not None:
        suffix_parts.append(f"sr{str(output_sample_rate / 1000.0).replace('.', 'p')}k")
        suffix_parts.append(output_alias_interpolation[:3].lower())
        suffix_parts.append(f"oq{output_alias_quality[:1].lower()}")
    if output_sample_width is not None:
        suffix_parts.append(f"{output_sample_width * 8}b")

    output_path = build_output_path(source_path, output_directory, suffix_parts)
    write_pcm_wav_bytes(output_path, data_bytes, channels, final_sample_width, final_sample_rate, produced_by)
    return output_path


def _task_supports_chunk_parallel(task: Tuple[object, ...]) -> bool:
    pitch_enabled = bool(task[5])
    tape_enabled = bool(task[7])
    compressor_enabled = bool(task[12])
    limiter_enabled = bool(task[20])
    texture_type = str(task[24]).strip().lower()
    humanize_enabled = bool(task[28])
    offset_enabled = bool(task[32])
    return (
        not pitch_enabled
        and
        not tape_enabled
        and not compressor_enabled
        and not limiter_enabled
        and texture_type == "none"
        and not humanize_enabled
        and not offset_enabled
    )


def _choose_parallel_chunk_frames(total_frames: int, sample_rate: int, workers: int) -> int:
    if sample_rate <= 0:
        return 1
    worker_count = max(1, workers)
    target_chunks = max(2, worker_count * PARALLEL_TARGET_CHUNKS_PER_WORKER)
    duration_seconds = total_frames / float(sample_rate)
    target_seconds = duration_seconds / float(target_chunks)
    chunk_seconds = _clamp(target_seconds, PARALLEL_MIN_CHUNK_SECONDS, PARALLEL_MAX_CHUNK_SECONDS)
    return max(1, int(sample_rate * chunk_seconds))


def process_wav_chunk_task(args: Tuple[object, ...]) -> Tuple[int, bytes, int, int, int]:
    chunk_index = int(args[0])
    source_path = pathlib.Path(str(args[1]))
    start_frame = int(args[2])
    chunk_frame_count = int(args[3])
    pad_frames = int(args[4])
    high_pass_hz = float(args[5])
    low_pass_hz = float(args[6])
    effect_args = args[7:-1]
    eq_taps = int(args[-1])

    with wave.open(str(source_path), "rb") as wf:
        channels = wf.getnchannels()
        sample_width = wf.getsampwidth()
        sample_rate = wf.getframerate()
        total_frames = wf.getnframes()
        if wf.getcomptype() != "NONE":
            raise WavReadError("Compressed WAV is not supported.")
        render_start = max(0, start_frame - pad_frames)
        render_end = min(total_frames, start_frame + chunk_frame_count + pad_frames)
        wf.setpos(render_start)
        frames = wf.readframes(render_end - render_start)

    processed = process_wav_frames(
        frames,
        channels,
        sample_width,
        sample_rate,
        high_pass_hz,
        low_pass_hz,
        effect_args[0],
        effect_args[1],
        effect_args[2],
        effect_args[3],
        effect_args[4],
        effect_args[5],
        effect_args[6],
        effect_args[7],
        effect_args[8],
        effect_args[9],
        effect_args[10],
        effect_args[11],
        effect_args[12],
        effect_args[13],
        effect_args[14],
        effect_args[15],
        effect_args[16],
        effect_args[17],
        effect_args[18],
        effect_args[19],
        effect_args[20],
        effect_args[21],
        effect_args[22],
        effect_args[23],
        effect_args[24],
        effect_args[25],
        effect_args[26],
        effect_args[27],
        effect_args[28],
        effect_args[29],
        eq_taps,
    )
    trimmed = _slice_frame_bytes(processed, channels, sample_width, start_frame - render_start, chunk_frame_count)
    return chunk_index, trimmed, channels, sample_width, sample_rate


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
    if sys.platform == "darwin":
        base_dir = pathlib.Path.home() / "Library" / "Application Support"
    elif os.name == "nt":
        appdata = os.getenv("APPDATA")
        base_dir = pathlib.Path(appdata) if appdata else pathlib.Path.home() / "AppData" / "Roaming"
    else:
        base_dir = pathlib.Path.home() / ".config"
    return base_dir / SETTINGS_DIR_NAME / SETTINGS_FILE_NAME


def get_default_user_name() -> str:
    for env_name in ("USERNAME", "USER"):
        value = os.getenv(env_name, "").strip()
        if value:
            return value
    try:
        return getpass.getuser().strip()
    except Exception:
        return ""


def process_wav_file(
    path: pathlib.Path,
    output_directory: pathlib.Path | None,
    produced_by: str,
    high_pass_hz: float,
    low_pass_hz: float,
    pitch_enabled: bool,
    pitch_shift_semitones: float,
    tape_enabled: bool,
    tape_drive_percent: float,
    tape_mix_percent: float,
    tape_alias_interpolation: str,
    tape_alias_quality: str,
    compressor_enabled: bool,
    compressor_mode: str,
    compressor_threshold_db: float,
    compressor_ratio: float,
    compressor_attack_ms: float,
    compressor_release_ms: float,
    compressor_makeup_db: float,
    compressor_valve_warmth: bool,
    limiter_enabled: bool,
    limiter_ceiling_db: float,
    limiter_lookahead_ms: float,
    limiter_release_ms: float,
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
    stereo_enabled: bool,
    stereo_width_percent: float,
    eq_taps: int,
    output_sample_rate: int | None,
    output_sample_width: int | None,
    output_alias_interpolation: str,
    output_alias_quality: str,
) -> pathlib.Path:
    with wave.open(str(path), "rb") as wf:
        channels = wf.getnchannels()
        sample_width = wf.getsampwidth()
        sample_rate = wf.getframerate()
        nframes = wf.getnframes()
        if wf.getcomptype() != "NONE":
            raise WavReadError("Compressed WAV is not supported.")
        frames = wf.readframes(nframes)

    data_bytes = process_wav_frames(
        frames,
        channels,
        sample_width,
        sample_rate,
        high_pass_hz,
        low_pass_hz,
        pitch_enabled,
        pitch_shift_semitones,
        tape_enabled,
        tape_drive_percent,
        tape_mix_percent,
        tape_alias_interpolation,
        tape_alias_quality,
        compressor_enabled,
        compressor_mode,
        compressor_threshold_db,
        compressor_ratio,
        compressor_attack_ms,
        compressor_release_ms,
        compressor_makeup_db,
        compressor_valve_warmth,
        limiter_enabled,
        limiter_ceiling_db,
        limiter_lookahead_ms,
        limiter_release_ms,
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
        stereo_enabled,
        stereo_width_percent,
        eq_taps,
    )

    return finalize_processed_output(
        path,
        output_directory,
        produced_by,
        data_bytes,
        channels,
        sample_width,
        sample_rate,
        high_pass_hz,
        low_pass_hz,
        pitch_enabled,
        pitch_shift_semitones,
        tape_enabled,
        tape_drive_percent,
        tape_mix_percent,
        tape_alias_interpolation,
        tape_alias_quality,
        compressor_enabled,
        compressor_mode,
        compressor_threshold_db,
        compressor_ratio,
        compressor_attack_ms,
        compressor_release_ms,
        compressor_makeup_db,
        compressor_valve_warmth,
        limiter_enabled,
        limiter_ceiling_db,
        limiter_lookahead_ms,
        limiter_release_ms,
        texture_type,
        texture_mix_percent,
        humanize_enabled,
        offset_enabled,
        offset_max_ms,
        stereo_enabled,
        stereo_width_percent,
        output_sample_rate,
        output_sample_width,
        output_alias_interpolation,
        output_alias_quality,
    )


def process_wav_file_task(args: Tuple[object, ...]) -> str:
    output_directory = pathlib.Path(args[1]).resolve() if args[1] else None
    output_sample_rate = int(args[37]) or None
    output_sample_width = int(args[38]) or None
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
        args[16],
        args[17],
        args[18],
        args[19],
        args[20],
        args[21],
        args[22],
        args[23],
        args[24],
        args[25],
        args[26],
        args[27],
        args[28],
        args[29],
        args[30],
        args[31],
        args[32],
        args[33],
        args[34],
        args[35],
        args[36],
        output_sample_rate,
        output_sample_width,
        args[39],
        args[40],
    )
    return str(output)


class WavFilterApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(APP_TITLE)
        self.root.geometry(DEFAULT_WINDOW_SIZE)
        self.settings_path = get_settings_path()
        self.license_path = get_license_storage_path(self.settings_path)
        self.installation_id = get_installation_id()
        self.license_state = load_saved_license(self.license_path, self.installation_id)
        self._settings_load_error: str | None = None
        self.ui_queue: queue.Queue[tuple[str, object]] = queue.Queue()
        self.process_thread: threading.Thread | None = None
        self.preview_thread: threading.Thread | None = None
        self.is_processing = False
        self.is_preview_rendering = False
        self._ui_poll_scheduled = False
        self.control_widgets: List[tk.Widget] = []
        self.cancel_requested = threading.Event()
        self.progress_var = tk.DoubleVar(value=0.0)
        self.progress_text_var = tk.StringVar(value="Idle")
        self.preview_info_var = tk.StringVar(value="No preview rendered")
        self.peak_meter_var = tk.StringVar(value="Peak: -- dBFS")
        self.license_status_var = tk.StringVar(value=self.license_state.status)
        self.installation_id_var = tk.StringVar(value=self.installation_id)

        self.selected_files: List[pathlib.Path] = []
        self.preview_path: pathlib.Path | None = None
        self.current_analysis: dict[str, object] | None = None
        self.spectrogram_image: tk.PhotoImage | None = None
        self._fx_visual_trace_ids: List[tuple[tk.Variable, str]] = []
        self._pygame_ready = False
        self._preview_channel = None
        self._preview_pending_sounds: deque[object] = deque()
        self._preview_sound_refs: deque[object] = deque()
        self._preview_stream_pump_scheduled = False
        self._preview_is_streaming = False
        self._preview_session_id = 0
        self._preview_stop_event: threading.Event | None = None
        self._preview_refresh_after_id: str | None = None
        self._preview_refresh_pending = False

        self.high_pass_var = tk.StringVar(value=str(int(DEFAULT_HIGH_PASS_HZ)))
        self.low_pass_var = tk.StringVar(value=str(int(DEFAULT_LOW_PASS_HZ)))
        self.eq_taps_var = tk.StringVar(value=str(DEFAULT_EQ_TAPS))
        self.worker_count_var = tk.StringVar(value=str(max(1, min(4, (os.cpu_count() or 1)))))
        self.output_folder_var = tk.StringVar(value="")
        self.produced_by_var = tk.StringVar(value=get_default_user_name())
        self.output_sample_rate_var = tk.StringVar(value=DEFAULT_OUTPUT_SAMPLE_RATE)
        self.output_bit_depth_var = tk.StringVar(value=DEFAULT_OUTPUT_BIT_DEPTH)
        self.output_alias_interpolation_var = tk.StringVar(value=DEFAULT_OUTPUT_ALIAS_INTERPOLATION)
        self.output_alias_quality_var = tk.StringVar(value=DEFAULT_OUTPUT_ALIAS_QUALITY)
        self.pitch_enabled_var = tk.BooleanVar(value=False)
        self.pitch_mode_var = tk.StringVar(value=DEFAULT_PITCH_MODE)
        self.pitch_semitones_var = tk.StringVar(value=str(DEFAULT_PITCH_SEMITONES))
        self.pitch_cents_var = tk.StringVar(value=str(DEFAULT_PITCH_CENTS))
        self.pitch_millicents_var = tk.StringVar(value=str(DEFAULT_PITCH_MILLICENTS))
        self.pitch_source_a_var = tk.StringVar(value=str(DEFAULT_PITCH_SOURCE_A_HZ))
        self.pitch_target_a_var = tk.StringVar(value=str(DEFAULT_PITCH_TARGET_A_HZ))
        self.pitch_preset_var = tk.StringVar(value=PITCH_PRESET_LABELS[0])
        self.pitch_summary_var = tk.StringVar(value="0.00 st  |  0.0 cents  |  A440.0 -> A440.0")

        self.tape_enabled_var = tk.BooleanVar(value=False)
        self.tape_drive_var = tk.StringVar(value=str(int(DEFAULT_TAPE_DRIVE_PERCENT)))
        self.tape_mix_var = tk.StringVar(value=str(int(DEFAULT_TAPE_MIX_PERCENT)))
        self.tape_alias_interpolation_var = tk.StringVar(value=DEFAULT_ALIAS_INTERPOLATION)
        self.tape_alias_quality_var = tk.StringVar(value=DEFAULT_ALIAS_QUALITY)

        self.compressor_enabled_var = tk.BooleanVar(value=False)
        self.compressor_mode_var = tk.StringVar(value=DEFAULT_COMP_MODE)
        self.compressor_threshold_var = tk.StringVar(value=str(DEFAULT_COMP_THRESHOLD_DB))
        self.compressor_ratio_var = tk.StringVar(value=str(DEFAULT_COMP_RATIO))
        self.compressor_attack_var = tk.StringVar(value=str(DEFAULT_COMP_ATTACK_MS))
        self.compressor_release_var = tk.StringVar(value=str(DEFAULT_COMP_RELEASE_MS))
        self.compressor_makeup_var = tk.StringVar(value=str(DEFAULT_COMP_MAKEUP_DB))
        self.compressor_valve_var = tk.BooleanVar(value=False)

        self.limiter_enabled_var = tk.BooleanVar(value=True)
        self.limiter_ceiling_var = tk.StringVar(value=str(DEFAULT_LIMITER_CEILING_DB))
        self.limiter_lookahead_var = tk.StringVar(value=str(DEFAULT_LIMITER_LOOKAHEAD_MS))
        self.limiter_release_var = tk.StringVar(value=str(DEFAULT_LIMITER_RELEASE_MS))

        self.texture_type_var = tk.StringVar(value="none")
        self.texture_mix_var = tk.StringVar(value="0")
        self.texture_level_db_var = tk.StringVar(value="-35")
        self.texture_fade_ms_var = tk.StringVar(value="250")

        self.stereo_enabled_var = tk.BooleanVar(value=False)
        self.stereo_width_var = tk.StringVar(value=str(int(DEFAULT_STEREO_WIDTH_PERCENT)))

        self.humanize_enabled_var = tk.BooleanVar(value=False)
        self.humanize_min_db_var = tk.StringVar(value="-2.0")
        self.humanize_max_db_var = tk.StringVar(value="-0.5")
        self.humanize_section_ms_var = tk.StringVar(value="900")

        self.offset_enabled_var = tk.BooleanVar(value=False)
        self.offset_max_ms_var = tk.StringVar(value="100")

        self._load_settings()
        self._build_ui()
        self._refresh_license_ui()
        self._setup_fx_visual_traces()
        self._refresh_fx_visuals()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    def _build_ui(self) -> None:
        license_frame = tk.LabelFrame(self.root, text="Premium Unlock")
        license_frame.pack(fill="x", padx=12, pady=(8, 4))
        tk.Label(
            license_frame,
            text=f"Premium FX unlock: {PREMIUM_PRICE_LABEL} one-time. Humanize, Pitch, Tape, Compressor, Texture, Stereo, and Offsets stay locked until activated.",
            anchor="w",
        ).grid(row=0, column=0, columnspan=4, sticky="w", padx=6, pady=(6, 2))
        tk.Label(license_frame, textvariable=self.license_status_var, anchor="w", fg="#0b6e4f").grid(
            row=1, column=0, columnspan=4, sticky="w", padx=6, pady=(0, 4)
        )
        tk.Label(license_frame, text="Install ID:").grid(row=2, column=0, sticky="w", padx=6, pady=(0, 6))
        tk.Entry(license_frame, textvariable=self.installation_id_var, width=32, state="readonly").grid(row=2, column=1, sticky="w", padx=6, pady=(0, 6))
        tk.Button(license_frame, text="Copy ID", command=self.copy_installation_id).grid(row=2, column=2, padx=6, pady=(0, 6))
        tk.Button(license_frame, text="Buy Unlock", command=self.buy_unlock).grid(row=2, column=3, padx=6, pady=(0, 6))
        tk.Button(license_frame, text="Enter Code", command=self.enter_unlock_code).grid(row=2, column=4, padx=6, pady=(0, 6))
        license_frame.grid_columnconfigure(1, weight=1)

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
        tk.Label(output, text="Output rate:").grid(row=2, column=0, sticky="w", padx=6, pady=(6, 6))
        self._track_control(
            ttk.Combobox(output, textvariable=self.output_sample_rate_var, values=list(OUTPUT_SAMPLE_RATE_OPTIONS), width=10, state="readonly")
        ).grid(row=2, column=1, sticky="w", padx=6, pady=(6, 6))
        tk.Label(output, text="Bit depth:").grid(row=2, column=2, sticky="w", padx=6, pady=(6, 6))
        self._track_control(
            ttk.Combobox(output, textvariable=self.output_bit_depth_var, values=list(OUTPUT_BIT_DEPTH_OPTIONS), width=8, state="readonly")
        ).grid(row=2, column=3, sticky="w", padx=6, pady=(6, 6))
        tk.Label(output, text="Upscaler:").grid(row=2, column=4, sticky="w", padx=6, pady=(6, 6))
        self._track_control(
            ttk.Combobox(
                output,
                textvariable=self.output_alias_interpolation_var,
                values=["linear", "spline"],
                width=10,
                state="readonly",
            )
        ).grid(row=2, column=5, sticky="w", padx=6, pady=(6, 6))
        tk.Label(output, text="Correction:").grid(row=2, column=6, sticky="w", padx=6, pady=(6, 6))
        self._track_control(
            ttk.Combobox(
                output,
                textvariable=self.output_alias_quality_var,
                values=["standard", "high", "perfect"],
                width=10,
                state="readonly",
            )
        ).grid(row=2, column=7, sticky="w", padx=6, pady=(6, 6))
        tk.Label(output, text="Final export upscaler with smoothing and alias correction.").grid(row=3, column=1, columnspan=7, sticky="w", padx=6)
        output.grid_columnconfigure(1, weight=1)

        fx_notebook = ttk.Notebook(self.root)
        fx_notebook.pack(fill="x", padx=12, pady=4)
        self.fx_notebook = fx_notebook

        texture = tk.Frame(fx_notebook, padx=10, pady=10)
        pitch = tk.Frame(fx_notebook, padx=10, pady=10)
        tape = tk.Frame(fx_notebook, padx=10, pady=10)
        compressor = tk.Frame(fx_notebook, padx=10, pady=10)
        limiter = tk.Frame(fx_notebook, padx=10, pady=10)
        stereo = tk.Frame(fx_notebook, padx=10, pady=10)
        human = tk.Frame(fx_notebook, padx=10, pady=10)
        offsets = tk.Frame(fx_notebook, padx=10, pady=10)
        fx_notebook.add(texture, text="Texture")
        fx_notebook.add(pitch, text="Pitch")
        fx_notebook.add(tape, text="Tape")
        fx_notebook.add(compressor, text="Compressor")
        fx_notebook.add(limiter, text="Limiter")
        fx_notebook.add(stereo, text="Stereo")
        fx_notebook.add(human, text="Humanize")
        fx_notebook.add(offsets, text="Offsets")
        self.premium_tab_indexes = [0, 1, 2, 3, 5, 6, 7]

        self._track_control(
            ttk.Combobox(texture, textvariable=self.texture_type_var, values=["none", "pink", "room", "vinyl"], width=10, state="readonly")
        ).grid(row=0, column=0, padx=6, pady=6)
        tk.Label(texture, text="Amount %:").grid(row=0, column=1, sticky="w")
        self._track_control(tk.Entry(texture, textvariable=self.texture_mix_var, width=8)).grid(row=0, column=2, padx=6)
        tk.Label(texture, text="Fade ms:").grid(row=0, column=3, sticky="w")
        self._track_control(tk.Entry(texture, textvariable=self.texture_fade_ms_var, width=8)).grid(row=0, column=4, padx=6)
        tk.Label(texture, text="Low values stay subtle; high values can progressively take over the preview/export.").grid(row=0, column=5, sticky="w", padx=(8, 0))
        self.texture_fx_canvas = tk.Canvas(texture, height=110, bg="#0d1116", highlightthickness=0)
        self.texture_fx_canvas.grid(row=1, column=0, columnspan=6, sticky="we", padx=6, pady=(8, 0))
        texture.grid_columnconfigure(5, weight=1)

        self._track_control(tk.Checkbutton(pitch, text="Enable", variable=self.pitch_enabled_var)).grid(row=0, column=0, padx=6, pady=6)
        tk.Label(pitch, text="Mode:").grid(row=0, column=1, sticky="w")
        self._track_control(
            ttk.Combobox(
                pitch,
                textvariable=self.pitch_mode_var,
                values=list(PITCH_MODE_OPTIONS),
                width=12,
                state="readonly",
            )
        ).grid(row=0, column=2, padx=6)
        tk.Label(pitch, text="Semitones:").grid(row=0, column=3, sticky="w")
        self._track_control(tk.Entry(pitch, textvariable=self.pitch_semitones_var, width=8)).grid(row=0, column=4, padx=6)
        tk.Label(pitch, text="Cents:").grid(row=0, column=5, sticky="w")
        self._track_control(tk.Entry(pitch, textvariable=self.pitch_cents_var, width=8)).grid(row=0, column=6, padx=6)
        tk.Label(pitch, text="Millicents:").grid(row=0, column=7, sticky="w")
        self._track_control(tk.Entry(pitch, textvariable=self.pitch_millicents_var, width=10)).grid(row=0, column=8, padx=6)
        tk.Label(pitch, text="Only the selected mode drives the rendered shift.").grid(row=0, column=9, sticky="w", padx=(8, 0))

        tk.Label(pitch, text="Source A (Hz):").grid(row=1, column=1, sticky="w")
        self._track_control(tk.Entry(pitch, textvariable=self.pitch_source_a_var, width=8)).grid(row=1, column=2, padx=6, pady=(0, 6))
        tk.Label(pitch, text="Target A (Hz):").grid(row=1, column=3, sticky="w")
        self._track_control(tk.Entry(pitch, textvariable=self.pitch_target_a_var, width=8)).grid(row=1, column=4, padx=6, pady=(0, 6))
        tk.Label(pitch, text="Preset:").grid(row=1, column=5, sticky="w")
        self._track_control(
            ttk.Combobox(
                pitch,
                textvariable=self.pitch_preset_var,
                values=list(PITCH_PRESET_LABELS),
                width=26,
                state="readonly",
            )
        ).grid(row=1, column=6, columnspan=2, sticky="w", padx=6, pady=(0, 6))
        self._track_control(tk.Button(pitch, text="Apply Preset", command=self.apply_pitch_preset)).grid(row=1, column=8, padx=6, pady=(0, 6))
        tk.Label(
            pitch,
            text="Bach ≈415 Hz mellow, Beethoven ≈455 Hz brighter, Modern 440 Hz equal temperament.",
        ).grid(row=1, column=9, sticky="w", padx=(8, 0), pady=(0, 6))
        tk.Label(pitch, textvariable=self.pitch_summary_var, anchor="w").grid(row=2, column=1, columnspan=9, sticky="w", padx=6, pady=(0, 4))
        self.pitch_fx_canvas = tk.Canvas(pitch, height=110, bg="#0d1116", highlightthickness=0)
        self.pitch_fx_canvas.grid(row=3, column=0, columnspan=10, sticky="we", padx=6, pady=(8, 0))
        pitch.grid_columnconfigure(9, weight=1)

        self._track_control(tk.Checkbutton(tape, text="Enable", variable=self.tape_enabled_var)).grid(row=0, column=0, padx=6, pady=6)
        tk.Label(tape, text="Drive % (10-25 subtle):").grid(row=0, column=1, sticky="w")
        self._track_control(tk.Entry(tape, textvariable=self.tape_drive_var, width=8)).grid(row=0, column=2, padx=6)
        tk.Label(tape, text="Wet mix %:").grid(row=0, column=3, sticky="w")
        self._track_control(tk.Entry(tape, textvariable=self.tape_mix_var, width=8)).grid(row=0, column=4, padx=6)
        tk.Label(tape, text="Upsampling:").grid(row=0, column=5, sticky="w")
        self._track_control(
            ttk.Combobox(
                tape,
                textvariable=self.tape_alias_interpolation_var,
                values=["linear", "spline"],
                width=10,
                state="readonly",
            )
        ).grid(row=0, column=6, padx=6)
        tk.Label(tape, text="Downsampling:").grid(row=0, column=7, sticky="w")
        self._track_control(
            ttk.Combobox(
                tape,
                textvariable=self.tape_alias_quality_var,
                values=["standard", "high", "perfect"],
                width=10,
                state="readonly",
            )
        ).grid(row=0, column=8, padx=6)
        tk.Label(tape, text="Interpolation kernel plus anti-alias quality for the saturation stage.").grid(row=1, column=1, columnspan=8, sticky="w", padx=(0, 0))
        self.tape_fx_canvas = tk.Canvas(tape, height=110, bg="#0d1116", highlightthickness=0)
        self.tape_fx_canvas.grid(row=2, column=0, columnspan=9, sticky="we", padx=6, pady=(8, 0))
        tape.grid_columnconfigure(8, weight=1)

        self._track_control(tk.Checkbutton(compressor, text="Enable", variable=self.compressor_enabled_var)).grid(row=0, column=0, padx=6, pady=6)
        tk.Label(compressor, text="Mode:").grid(row=0, column=1, sticky="w")
        self._track_control(
            ttk.Combobox(
                compressor,
                textvariable=self.compressor_mode_var,
                values=["single-band", "multiband"],
                width=12,
                state="readonly",
            )
        ).grid(row=0, column=2, padx=6)
        tk.Label(compressor, text="Threshold dB:").grid(row=0, column=3, sticky="w")
        self._track_control(tk.Entry(compressor, textvariable=self.compressor_threshold_var, width=8)).grid(row=0, column=4, padx=6)
        tk.Label(compressor, text="Ratio:").grid(row=0, column=5, sticky="w")
        self._track_control(tk.Entry(compressor, textvariable=self.compressor_ratio_var, width=8)).grid(row=0, column=6, padx=6)
        tk.Label(compressor, text="Attack ms:").grid(row=0, column=7, sticky="w")
        self._track_control(tk.Entry(compressor, textvariable=self.compressor_attack_var, width=8)).grid(row=0, column=8, padx=6)
        tk.Label(compressor, text="Release ms:").grid(row=1, column=1, sticky="w")
        self._track_control(tk.Entry(compressor, textvariable=self.compressor_release_var, width=8)).grid(row=1, column=2, padx=6, pady=(0, 6))
        tk.Label(compressor, text="Makeup dB:").grid(row=1, column=3, sticky="w")
        self._track_control(tk.Entry(compressor, textvariable=self.compressor_makeup_var, width=8)).grid(row=1, column=4, padx=6, pady=(0, 6))
        self._track_control(tk.Checkbutton(compressor, text="Valve warmth", variable=self.compressor_valve_var)).grid(row=1, column=5, columnspan=2, padx=6, pady=(0, 6), sticky="w")
        tk.Label(compressor, text="Single-band or fixed 3-band multiband compression with optional valve color.").grid(row=1, column=7, columnspan=2, sticky="w", padx=(8, 0))
        self.compressor_fx_canvas = tk.Canvas(compressor, height=120, bg="#0d1116", highlightthickness=0)
        self.compressor_fx_canvas.grid(row=2, column=0, columnspan=9, sticky="we", padx=6, pady=(8, 0))
        compressor.grid_columnconfigure(8, weight=1)

        self._track_control(tk.Checkbutton(limiter, text="Enable final limiter", variable=self.limiter_enabled_var)).grid(row=0, column=0, padx=6, pady=6)
        tk.Label(limiter, text="Ceiling dBFS:").grid(row=0, column=1, sticky="w")
        self._track_control(tk.Entry(limiter, textvariable=self.limiter_ceiling_var, width=8)).grid(row=0, column=2, padx=6)
        tk.Label(limiter, text="Lookahead ms:").grid(row=0, column=3, sticky="w")
        self._track_control(tk.Entry(limiter, textvariable=self.limiter_lookahead_var, width=8)).grid(row=0, column=4, padx=6)
        tk.Label(limiter, text="Release ms:").grid(row=0, column=5, sticky="w")
        self._track_control(tk.Entry(limiter, textvariable=self.limiter_release_var, width=8)).grid(row=0, column=6, padx=6)
        tk.Label(limiter, text="Final lookahead peak limiter to catch overs after the rest of the chain.").grid(row=0, column=7, sticky="w", padx=(8, 0))
        self.limiter_fx_canvas = tk.Canvas(limiter, height=110, bg="#0d1116", highlightthickness=0)
        self.limiter_fx_canvas.grid(row=1, column=0, columnspan=8, sticky="we", padx=6, pady=(8, 0))
        limiter.grid_columnconfigure(7, weight=1)

        self._track_control(tk.Checkbutton(stereo, text="Enable", variable=self.stereo_enabled_var)).grid(row=0, column=0, padx=6, pady=6)
        tk.Label(stereo, text="Width %:").grid(row=0, column=1, sticky="w")
        self._track_control(tk.Entry(stereo, textvariable=self.stereo_width_var, width=8)).grid(row=0, column=2, padx=6)
        tk.Label(stereo, text="<100 narrows, 100 keeps the original image, >100 widens and adds separation.").grid(row=0, column=3, sticky="w", padx=(8, 0))
        self.stereo_fx_canvas = tk.Canvas(stereo, height=110, bg="#0d1116", highlightthickness=0)
        self.stereo_fx_canvas.grid(row=1, column=0, columnspan=4, sticky="we", padx=6, pady=(8, 0))
        stereo.grid_columnconfigure(3, weight=1)

        self._track_control(tk.Checkbutton(human, text="Enable", variable=self.humanize_enabled_var)).grid(row=0, column=0, padx=6, pady=6)
        tk.Label(human, text="Min dB:").grid(row=0, column=1, sticky="w")
        self._track_control(tk.Entry(human, textvariable=self.humanize_min_db_var, width=8)).grid(row=0, column=2, padx=6)
        tk.Label(human, text="Max dB:").grid(row=0, column=3, sticky="w")
        self._track_control(tk.Entry(human, textvariable=self.humanize_max_db_var, width=8)).grid(row=0, column=4, padx=6)
        tk.Label(human, text="Section ms:").grid(row=0, column=5, sticky="w")
        self._track_control(tk.Entry(human, textvariable=self.humanize_section_ms_var, width=8)).grid(row=0, column=6, padx=6)
        self.humanize_fx_canvas = tk.Canvas(human, height=110, bg="#0d1116", highlightthickness=0)
        self.humanize_fx_canvas.grid(row=1, column=0, columnspan=7, sticky="we", padx=6, pady=(8, 0))
        human.grid_columnconfigure(6, weight=1)

        self._track_control(tk.Checkbutton(offsets, text="Enable random stem offsets", variable=self.offset_enabled_var)).grid(row=0, column=0, padx=6, pady=6)
        tk.Label(offsets, text="Max offset ms (0-100+):").grid(row=0, column=1, sticky="w")
        self._track_control(tk.Entry(offsets, textvariable=self.offset_max_ms_var, width=8)).grid(row=0, column=2, padx=6)
        self._track_control(tk.Button(offsets, text="Randomizer", command=self.randomize_options)).grid(row=0, column=3, padx=10)
        self.offsets_fx_canvas = tk.Canvas(offsets, height=110, bg="#0d1116", highlightthickness=0)
        self.offsets_fx_canvas.grid(row=1, column=0, columnspan=4, sticky="we", padx=6, pady=(8, 0))
        offsets.grid_columnconfigure(3, weight=1)

        dnd_msg = "Drag/drop WAV files or folders below" if HAS_DND else "Install tkinterdnd2 to enable drag-and-drop"
        self.drop_label = tk.Label(self.root, text=dnd_msg, relief="groove", padx=8, pady=10)
        self.drop_label.pack(fill="x", padx=12, pady=8)

        self.file_list = tk.Listbox(self.root)
        self.file_list.pack(fill="both", expand=True, padx=12, pady=(0, 8))
        self.file_list.bind("<<ListboxSelect>>", self.on_file_selection_change)

        action_frame = tk.Frame(self.root)
        action_frame.pack(fill="x", padx=12, pady=(0, 8))

        self.process_button = self._track_control(tk.Button(action_frame, text="Process WAV(s)", command=self.process_files, height=2))
        self.process_button.pack(side="left", fill="x", expand=True)
        self.preview_button = self._track_control(tk.Button(action_frame, text="Preview Selected", command=self.preview_selected, height=2, width=16))
        self.preview_button.pack(side="left", padx=(8, 0))
        self.stop_preview_button = tk.Button(action_frame, text="Stop Preview", command=self.stop_preview, height=2, state=tk.DISABLED, width=12)
        self.stop_preview_button.pack(side="left", padx=(8, 0))
        self.cancel_button = tk.Button(action_frame, text="Cancel", command=self.cancel_processing, height=2, state=tk.DISABLED, width=12)
        self.cancel_button.pack(side="left", padx=(8, 0))

        progress_frame = tk.Frame(self.root)
        progress_frame.pack(fill="x", padx=12, pady=(0, 6))

        self.progress_bar = ttk.Progressbar(progress_frame, variable=self.progress_var, maximum=100.0, mode="determinate")
        self.progress_bar.pack(fill="x", side="left", expand=True)
        self.progress_label = tk.Label(progress_frame, textvariable=self.progress_text_var, width=16, anchor="e")
        self.progress_label.pack(side="left", padx=(10, 0))

        analysis = tk.LabelFrame(self.root, text="Preview Analysis")
        analysis.pack(fill="both", expand=False, padx=12, pady=(0, 8))

        analysis_header = tk.Frame(analysis)
        analysis_header.pack(fill="x", padx=8, pady=(6, 4))
        tk.Label(analysis_header, textvariable=self.preview_info_var, anchor="w").pack(side="left", fill="x", expand=True)
        self.peak_meter_canvas = tk.Canvas(analysis_header, width=30, height=110, bg="#101317", highlightthickness=1, highlightbackground="#2c343d")
        self.peak_meter_canvas.pack(side="left", padx=(10, 0))
        tk.Label(analysis_header, textvariable=self.peak_meter_var, width=18, anchor="w").pack(side="left", padx=(8, 0))

        self.analysis_notebook = ttk.Notebook(analysis)
        self.analysis_notebook.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.waveform_canvas = tk.Canvas(self.analysis_notebook, height=150, bg="#0d1116", highlightthickness=0)
        self.spectrum_canvas = tk.Canvas(self.analysis_notebook, height=150, bg="#0d1116", highlightthickness=0)
        self.spectrogram_canvas = tk.Canvas(self.analysis_notebook, height=180, bg="#0d1116", highlightthickness=0)
        self.analysis_notebook.add(self.waveform_canvas, text="Waveform")
        self.analysis_notebook.add(self.spectrum_canvas, text="Spectrum")
        self.analysis_notebook.add(self.spectrogram_canvas, text="Spectrogram")
        for canvas in (self.waveform_canvas, self.spectrum_canvas, self.spectrogram_canvas):
            canvas.bind("<Configure>", self.on_analysis_canvas_resize)

        self.status = tk.Label(self.root, text="Ready", anchor="w")
        self.status.pack(fill="x", padx=12, pady=(0, 10))

        if HAS_DND:
            self.drop_label.drop_target_register(DND_FILES)
            self.drop_label.dnd_bind("<<Drop>>", self.on_drop)

        if self._settings_load_error:
            self.set_status(self._settings_load_error)

    def randomize_options(self) -> None:
        self.tape_enabled_var.set(random.choice([True, False]))
        self.tape_drive_var.set(str(random.randint(10, 30)))
        self.tape_mix_var.set(str(random.randint(70, 100)))
        self.tape_alias_interpolation_var.set(random.choice(["linear", "spline"]))
        self.tape_alias_quality_var.set(random.choice(["standard", "high", "perfect"]))

        self.compressor_enabled_var.set(random.choice([True, False]))
        self.compressor_mode_var.set(random.choice(["single-band", "multiband"]))
        self.compressor_threshold_var.set(str(random.randint(-24, -10)))
        self.compressor_ratio_var.set(str(round(random.uniform(2.0, 5.0), 1)))
        self.compressor_attack_var.set(str(round(random.uniform(5.0, 25.0), 1)))
        self.compressor_release_var.set(str(round(random.uniform(60.0, 220.0), 1)))
        self.compressor_makeup_var.set(str(round(random.uniform(0.0, 3.0), 1)))
        self.compressor_valve_var.set(random.choice([True, False]))

        self.limiter_enabled_var.set(True)
        self.limiter_ceiling_var.set(str(round(random.uniform(-1.2, -0.3), 1)))
        self.limiter_lookahead_var.set(str(round(random.uniform(1.5, 4.0), 1)))
        self.limiter_release_var.set(str(round(random.uniform(40.0, 120.0), 1)))

        self.texture_type_var.set(random.choice(["pink", "room", "vinyl"]))
        self.texture_mix_var.set(str(random.randint(15, 60)))
        self.texture_level_db_var.set(str(round(_texture_amount_to_level_db(float(self.texture_mix_var.get())), 1)))
        self.texture_fade_ms_var.set(str(random.randint(120, 500)))

        self.stereo_enabled_var.set(random.choice([True, False]))
        self.stereo_width_var.set(str(random.randint(70, 155)))

        lo = round(random.uniform(-2.0, -1.0), 2)
        hi = round(random.uniform(-1.0, -0.5), 2)
        self.humanize_min_db_var.set(str(min(lo, hi)))
        self.humanize_max_db_var.set(str(max(lo, hi)))
        self.humanize_section_ms_var.set(str(random.randint(400, 1500)))

        self.offset_enabled_var.set(random.choice([True, False]))
        self.offset_max_ms_var.set(str(random.randint(0, 100)))
        self.set_status("Randomized tape/compressor/limiter/texture/stereo/humanize/offset settings.")

    def apply_pitch_preset(self) -> None:
        preset_label = self.pitch_preset_var.get().strip()
        preset = PITCH_REFERENCE_PRESETS.get(preset_label)
        if preset is None:
            self.set_status("Pitch preset cleared. Using custom pitch values.")
            return
        era, target_hz, character, tuning = preset
        self.pitch_mode_var.set("frequency")
        self.pitch_source_a_var.set(f"{DEFAULT_PITCH_SOURCE_A_HZ:.1f}")
        self.pitch_target_a_var.set(f"{target_hz:.1f}")
        self.pitch_enabled_var.set(True)
        self.set_status(f"Pitch preset loaded: {era} A={target_hz:.1f} Hz ({character}; {tuning}).")

    def _get_pitch_shift_details(self, strict: bool = True) -> tuple[bool, str, float, float, float]:
        enabled = bool(self.pitch_enabled_var.get())
        mode = self.pitch_mode_var.get().strip().lower()
        if mode not in set(PITCH_MODE_OPTIONS):
            if strict:
                raise ValueError("Pitch mode must be semitones, cents, millicents, or frequency.")
            mode = DEFAULT_PITCH_MODE

        def parse_value(var: tk.StringVar, label: str, fallback: float) -> float:
            text = var.get().strip()
            if not text:
                return fallback
            try:
                return float(text)
            except ValueError:
                if strict:
                    raise ValueError(f"{label} must be a number.")
                return fallback

        semitones = parse_value(self.pitch_semitones_var, "Pitch semitones", DEFAULT_PITCH_SEMITONES)
        cents = parse_value(self.pitch_cents_var, "Pitch cents", DEFAULT_PITCH_CENTS)
        millicents = parse_value(self.pitch_millicents_var, "Pitch millicents", DEFAULT_PITCH_MILLICENTS)
        source_a = parse_value(self.pitch_source_a_var, "Pitch source A", DEFAULT_PITCH_SOURCE_A_HZ)
        target_a = parse_value(self.pitch_target_a_var, "Pitch target A", DEFAULT_PITCH_TARGET_A_HZ)

        if mode == "semitones":
            shift_semitones = semitones
        elif mode == "cents":
            shift_semitones = cents / 100.0
        elif mode == "millicents":
            shift_semitones = millicents / 10000.0
        else:
            if source_a <= 0 or target_a <= 0:
                if strict:
                    raise ValueError("Pitch source/target A must be > 0 Hz.")
                source_a = max(source_a, 1.0)
                target_a = max(target_a, 1.0)
            shift_semitones = 12.0 * math.log2(target_a / source_a)

        if strict and abs(shift_semitones) > PITCH_SHIFT_LIMIT_SEMITONES:
            raise ValueError(f"Pitch shift must stay within +/-{int(PITCH_SHIFT_LIMIT_SEMITONES)} semitones.")
        return enabled, mode, shift_semitones, source_a, target_a

    def set_status(self, text: str) -> None:
        self.status.config(text=text)
        self.root.update_idletasks()

    def _has_premium_unlock(self) -> bool:
        return self.license_state.unlocked

    def _refresh_license_ui(self) -> None:
        self.license_status_var.set(self.license_state.status)
        if not hasattr(self, "fx_notebook"):
            return
        selected = self.fx_notebook.index("current")
        if not self._has_premium_unlock() and selected in self.premium_tab_indexes:
            self.fx_notebook.select(4)
        for idx in self.premium_tab_indexes:
            self.fx_notebook.tab(idx, state="normal" if self._has_premium_unlock() else "disabled")

    def copy_installation_id(self) -> None:
        self.root.clipboard_clear()
        self.root.clipboard_append(self.installation_id)
        self.set_status("Installation ID copied to clipboard.")

    def buy_unlock(self) -> None:
        server_url = os.getenv(LICENSE_SERVER_URL_ENV, DEFAULT_LICENSE_SERVER_URL).strip().rstrip("/")
        if not server_url:
            messagebox.showinfo(
                "Unlock server not configured",
                f"Set {LICENSE_SERVER_URL_ENV} to your AWS checkout URL before shipping the paid unlock flow.",
            )
            return
        checkout_url = f"{server_url}/buy?install_id={self.installation_id}"
        opened = webbrowser.open(checkout_url)
        if opened:
            self.set_status("Opened premium checkout in your browser.")
        else:
            messagebox.showinfo("Open checkout", f"Open this URL in a browser:\n\n{checkout_url}")

    def enter_unlock_code(self) -> None:
        code = simpledialog.askstring(
            "Enter unlock code",
            "Paste the full premium unlock code from your Stripe purchase page.",
            parent=self.root,
        )
        if not code:
            return
        try:
            self.license_state = activate_license_code(code, self.license_path, self.installation_id)
        except LicenseError as exc:
            messagebox.showerror("Unlock failed", str(exc))
            return
        self._refresh_license_ui()
        self.set_status("Premium features unlocked for this installation.")
        messagebox.showinfo("Unlocked", self.license_state.status)

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
            self.preview_button.configure(state=tk.DISABLED)
        elif not self.is_preview_rendering:
            self.preview_button.configure(state=tk.NORMAL)
        if processing:
            self.status.config(text="Starting background processing...")
            self.progress_var.set(0.0)
            self.progress_text_var.set("0%")

    def _set_preview_render_state(self, rendering: bool) -> None:
        self.is_preview_rendering = rendering
        if rendering:
            self.preview_button.configure(state=tk.DISABLED)
            self.stop_preview_button.configure(state=tk.NORMAL)
        else:
            self.preview_button.configure(state=tk.DISABLED if self.is_processing else tk.NORMAL)
            self.stop_preview_button.configure(state=tk.NORMAL if (self.preview_path or self._preview_is_streaming) else tk.DISABLED)

    def _ensure_preview_mixer(self, sample_rate: int, channels: int) -> bool:
        if not HAS_PYGAME:
            return False
        desired = (sample_rate, -16, channels)
        try:
            current = pygame.mixer.get_init() if self._pygame_ready else None
            if current != desired:
                if self._pygame_ready:
                    pygame.mixer.quit()
                pygame.mixer.init(
                    frequency=sample_rate,
                    size=-16,
                    channels=channels,
                    buffer=PREVIEW_MIXER_BUFFER,
                    allowedchanges=0,
                )
                self._pygame_ready = True
            self._preview_channel = pygame.mixer.Channel(0)
            return True
        except Exception:
            self._pygame_ready = False
            self._preview_channel = None
            return False

    def _schedule_preview_stream_pump(self) -> None:
        if not self._preview_stream_pump_scheduled:
            self._preview_stream_pump_scheduled = True
            self.root.after(PREVIEW_STREAM_PUMP_MS, self._pump_preview_stream)

    def _pump_preview_stream(self) -> None:
        self._preview_stream_pump_scheduled = False
        if not self._preview_is_streaming or not HAS_PYGAME or self._preview_channel is None:
            return
        try:
            if self._preview_pending_sounds:
                if not self._preview_channel.get_busy():
                    sound = self._preview_pending_sounds.popleft()
                    self._preview_channel.play(sound)
                elif self._preview_channel.get_queue() is None:
                    sound = self._preview_pending_sounds.popleft()
                    self._preview_channel.queue(sound)
            while len(self._preview_sound_refs) > 8:
                self._preview_sound_refs.popleft()
            still_active = (
                bool(self._preview_pending_sounds)
                or self._preview_channel.get_busy()
                or self._preview_channel.get_queue() is not None
                or self.is_preview_rendering
                or self.preview_thread is not None
            )
            if still_active:
                self._schedule_preview_stream_pump()
            else:
                self._preview_is_streaming = False
                self.stop_preview_button.configure(state=tk.DISABLED)
                if not self.is_processing:
                    self.preview_button.configure(state=tk.NORMAL)
                self.set_status("Preview finished.")
        except pygame.error:
            self._preview_is_streaming = False
            self.stop_preview_button.configure(state=tk.DISABLED)
            if not self.is_processing:
                self.preview_button.configure(state=tk.NORMAL)
            self.set_status("Preview playback stopped.")

    def _enqueue_preview_chunk(self, pcm16_bytes: bytes, sample_rate: int, channels: int, start_now: bool = False) -> bool:
        if not pcm16_bytes:
            return False
        if not self._ensure_preview_mixer(sample_rate, channels):
            return False
        try:
            sound = pygame.mixer.Sound(buffer=pcm16_bytes)
        except pygame.error:
            return False

        self._preview_sound_refs.append(sound)
        self._preview_is_streaming = True
        if self._preview_channel is None or start_now or not self._preview_channel.get_busy():
            self._preview_channel.play(sound)
        elif self._preview_channel.get_queue() is None:
            self._preview_channel.queue(sound)
        else:
            self._preview_pending_sounds.append(sound)
        self.stop_preview_button.configure(state=tk.NORMAL)
        self._schedule_preview_stream_pump()
        return True

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
            delay_ms = PREVIEW_UI_POLL_MS if (self.is_preview_rendering or self._preview_is_streaming or self.preview_thread is not None) else PREVIEW_IDLE_UI_POLL_MS
            self.root.after(delay_ms, self._drain_ui_queue)

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
            elif event_type == "preview_stream_start":
                data = payload  # type: ignore[assignment]
                if not isinstance(data, dict) or int(data.get("session_id", -1)) != self._preview_session_id:
                    continue
                started = self._enqueue_preview_chunk(
                    data.get("pcm16_bytes", b""),
                    int(data.get("sample_rate", 44100)),
                    int(data.get("channels", 2)),
                    start_now=True,
                )
                if not started:
                    self._set_preview_render_state(False)
                    self.preview_thread = None
                    self._preview_is_streaming = False
                    self.set_status("Preview failed to start audio playback.")
                    messagebox.showerror("Preview failed", "Could not initialize low-latency preview playback.")
                    continue
                self.preview_path = None
                self.current_analysis = data.get("analysis")
                mode_label = str(data.get("mode_label", "streaming"))
                self.preview_info_var.set(
                    f"{data.get('source_name', 'Preview')}  |  {int(data.get('sample_rate', 44100))} Hz  |  {float(data.get('total_duration_s', 0.0)):.1f}s  |  {mode_label}"
                )
                self.set_status(f"Previewing {data.get('source_name', 'selection')}...")
                self.root.after_idle(self._refresh_analysis_views)
            elif event_type == "preview_stream_chunk":
                data = payload  # type: ignore[assignment]
                if not isinstance(data, dict) or int(data.get("session_id", -1)) != self._preview_session_id:
                    continue
                self._enqueue_preview_chunk(
                    data.get("pcm16_bytes", b""),
                    int(data.get("sample_rate", 44100)),
                    int(data.get("channels", 2)),
                )
            elif event_type == "preview_analysis_update":
                data = payload  # type: ignore[assignment]
                if not isinstance(data, dict) or int(data.get("session_id", -1)) != self._preview_session_id:
                    continue
                analysis = data.get("analysis")
                if isinstance(analysis, dict):
                    self.current_analysis = analysis
                    self.root.after_idle(self._refresh_analysis_views)
            elif event_type == "preview_stream_complete":
                data = payload  # type: ignore[assignment]
                if not isinstance(data, dict) or int(data.get("session_id", -1)) != self._preview_session_id:
                    continue
                self.preview_thread = None
                self._set_preview_render_state(False)
                if self._preview_is_streaming:
                    self.set_status(f"Previewing {data.get('source_name', 'selection')}...")
            elif event_type == "preview_ready":
                if isinstance(payload, dict):
                    if int(payload.get("session_id", -1)) != self._preview_session_id:
                        continue
                    preview_path_str = str(payload.get("preview_path", ""))
                    analysis = payload.get("analysis", {})
                else:
                    preview_path_str, analysis = payload  # type: ignore[misc]
                self._set_preview_render_state(False)
                self._activate_preview(pathlib.Path(preview_path_str), analysis)
            elif event_type == "preview_error":
                if isinstance(payload, dict):
                    if int(payload.get("session_id", -1)) != self._preview_session_id:
                        continue
                    error_message = str(payload.get("message", "Preview failed."))
                else:
                    error_message = str(payload)
                self._set_preview_render_state(False)
                self.preview_thread = None
                self.set_status("Preview failed.")
                messagebox.showerror("Preview failed", error_message)

        if self.is_processing or self.is_preview_rendering or self.preview_thread is not None or not self.ui_queue.empty():
            self._schedule_ui_queue_poll()

    def _cleanup_preview_file(self, path: pathlib.Path | None) -> None:
        if path is None:
            return
        try:
            if path.exists():
                path.unlink()
        except OSError:
            pass

    def _get_selected_preview_source(self) -> pathlib.Path | None:
        selection = self.file_list.curselection()
        if selection:
            idx = selection[0]
            if 0 <= idx < len(self.selected_files):
                return self.selected_files[idx]
        return self.selected_files[0] if self.selected_files else None

    def preview_selected(self) -> None:
        if self.is_processing:
            messagebox.showinfo("Batch processing active", "Wait for the current batch to finish before previewing.")
            return
        source = self._get_selected_preview_source()
        if source is None:
            messagebox.showinfo("No files", "Please add at least one file and select it for preview.")
            return

        try:
            hp, lp, eq_taps, _ = self._get_filter_settings()
            _, produced_by = self._get_output_settings()
            output_sample_rate, output_sample_width, output_alias_interpolation, output_alias_quality = self._get_output_format_settings()
            settings = self._get_effect_settings()
        except Exception as exc:
            messagebox.showerror("Invalid settings", str(exc))
            return

        preview_task = (
            str(source),
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
            settings[10],
            settings[11],
            settings[12],
            settings[13],
            settings[14],
            settings[15],
            settings[16],
            settings[17],
            settings[18],
            settings[19],
            settings[20],
            settings[21],
            settings[22],
            settings[23],
            settings[24],
            settings[25],
            settings[26],
            settings[27],
            settings[28],
            settings[29],
            settings[30],
            eq_taps,
            output_sample_rate if output_sample_rate is not None else 0,
            output_sample_width if output_sample_width is not None else 0,
            output_alias_interpolation,
            output_alias_quality,
        )

        self.stop_preview()
        self._preview_session_id += 1
        session_id = self._preview_session_id
        stop_event = threading.Event()
        self._preview_stop_event = stop_event
        self._set_preview_render_state(True)
        self.preview_info_var.set(f"Starting low-latency preview: {source.name}")
        self.set_status(f"Buffering preview for {source.name}...")
        self.preview_thread = threading.Thread(
            target=self._run_preview_job,
            args=(session_id, stop_event, preview_task),
            name="wav-preview",
            daemon=True,
        )
        self.preview_thread.start()
        self._schedule_ui_queue_poll()

    def _run_preview_job(
        self,
        session_id: int,
        stop_event: threading.Event,
        task: Tuple[object, ...],
    ) -> None:
        if HAS_PYGAME:
            self._run_preview_stream_job(session_id, stop_event, task)
            return
        self._run_preview_file_job(session_id, task)

    def _run_preview_file_job(
        self,
        session_id: int,
        task: Tuple[object, ...],
    ) -> None:
        try:
            preview_directory = pathlib.Path(tempfile.gettempdir()) / SETTINGS_DIR_NAME / "preview"
            preview_directory.mkdir(parents=True, exist_ok=True)
            file_task = (task[0], str(preview_directory)) + task[1:]
            preview_path = pathlib.Path(process_wav_file_task(file_task))
            analysis = build_preview_analysis(preview_path)
        except Exception as exc:
            self._post_ui_event("preview_error", {"session_id": session_id, "message": str(exc)})
            return
        self._post_ui_event(
            "preview_ready",
            {
                "session_id": session_id,
                "preview_path": str(preview_path),
                "analysis": analysis,
            },
        )

    def _run_preview_stream_job(
        self,
        session_id: int,
        stop_event: threading.Event,
        task: Tuple[object, ...],
    ) -> None:
        try:
            source = pathlib.Path(str(task[0]))
            high_pass_hz = float(task[2])
            low_pass_hz = float(task[3])
            effect_args = task[4:-5]
            eq_taps = int(task[-5])
            output_sample_rate = int(task[-4]) or None
            output_sample_width = int(task[-3]) or None
            output_alias_interpolation = str(task[-2])
            output_alias_quality = str(task[-1])
            with wave.open(str(source), "rb") as wf:
                channels = wf.getnchannels()
                sample_width = wf.getsampwidth()
                sample_rate = wf.getframerate()
                frame_count = wf.getnframes()
                if wf.getcomptype() != "NONE":
                    raise WavReadError("Compressed WAV is not supported.")
                excerpt_frames = min(frame_count, max(1, int(sample_rate * PREVIEW_EXCERPT_SECONDS)))
                wf.setpos(0)
                raw_frames = wf.readframes(excerpt_frames)
                processed_frames = process_wav_frames(
                    raw_frames,
                    channels,
                    sample_width,
                    sample_rate,
                    high_pass_hz,
                    low_pass_hz,
                    effect_args[0],
                    effect_args[1],
                    effect_args[2],
                    effect_args[3],
                    effect_args[4],
                    effect_args[5],
                    effect_args[6],
                    effect_args[7],
                    effect_args[8],
                    effect_args[9],
                    effect_args[10],
                    effect_args[11],
                    effect_args[12],
                    effect_args[13],
                    effect_args[14],
                    effect_args[15],
                    effect_args[16],
                    effect_args[17],
                    effect_args[18],
                    effect_args[19],
                    effect_args[20],
                    effect_args[21],
                    effect_args[22],
                    effect_args[23],
                    effect_args[24],
                    effect_args[25],
                    effect_args[26],
                    effect_args[27],
                    effect_args[28],
                    effect_args[29],
                    effect_args[30],
                    eq_taps,
                )
                processed_frames, converted_sample_width, converted_sample_rate = convert_output_format(
                    processed_frames,
                    channels,
                    sample_width,
                    sample_rate,
                    output_sample_rate,
                    output_sample_width,
                    output_alias_interpolation,
                    output_alias_quality,
                )
                preview_pcm16 = _convert_frames_to_preview_pcm16(processed_frames, channels, converted_sample_width)
                analysis = build_preview_analysis_from_frames(
                    preview_pcm16,
                    channels,
                    2,
                    converted_sample_rate,
                    path=str(source),
                )
                self._post_ui_event(
                    "preview_stream_start",
                    {
                        "session_id": session_id,
                        "source_name": source.name,
                        "total_duration_s": len(preview_pcm16) / float(max(1, channels * 2 * converted_sample_rate)),
                        "sample_rate": converted_sample_rate,
                        "channels": channels,
                        "pcm16_bytes": preview_pcm16,
                        "analysis": analysis,
                        "mode_label": "preview excerpt",
                    },
                )
        except Exception as exc:
            if not stop_event.is_set():
                self._post_ui_event("preview_error", {"session_id": session_id, "message": str(exc)})
            return

        if stop_event.is_set():
            return
        self._post_ui_event("preview_stream_complete", {"session_id": session_id, "source_name": source.name})

    def _activate_preview(self, preview_path: pathlib.Path, analysis: dict[str, object]) -> None:
        old_path = self.preview_path
        self.preview_path = preview_path
        self.current_analysis = analysis
        self.preview_thread = None
        self.preview_info_var.set(
            f"{preview_path.name}  |  {analysis['sample_rate']} Hz  |  {analysis['duration_s']:.1f}s"
        )
        started = self._start_preview_playback(preview_path)
        self.stop_preview_button.configure(state=tk.NORMAL)
        if started:
            self.set_status(f"Previewing {preview_path.name}")
        else:
            self.set_status(f"Preview rendered, but playback could not start: {preview_path.name}")
        self.root.after_idle(self._refresh_analysis_views)
        self._cleanup_preview_file(old_path)

    def stop_preview(self) -> None:
        was_active = self.is_preview_rendering or self._preview_is_streaming or self.preview_path is not None
        if self._preview_refresh_after_id is not None:
            try:
                self.root.after_cancel(self._preview_refresh_after_id)
            except tk.TclError:
                pass
        self._preview_refresh_after_id = None
        self._preview_refresh_pending = False
        if self._preview_stop_event is not None:
            self._preview_stop_event.set()
        self._preview_stop_event = None
        self._preview_session_id += 1
        self.preview_thread = None
        self.is_preview_rendering = False
        self._preview_pending_sounds.clear()
        self._preview_sound_refs.clear()
        self._preview_is_streaming = False
        old_path = self.preview_path
        self.preview_path = None
        if HAS_PYGAME and self._pygame_ready:
            try:
                if self._preview_channel is not None:
                    self._preview_channel.stop()
                pygame.mixer.music.stop()
            except pygame.error:
                pass
        if HAS_WINSOUND:
            winsound.PlaySound(None, winsound.SND_PURGE)
        self._cleanup_preview_file(old_path)
        self.stop_preview_button.configure(state=tk.DISABLED)
        if not self.is_processing:
            self.preview_button.configure(state=tk.NORMAL)
        if was_active:
            self.set_status("Preview stopped.")

    def _start_preview_playback(self, preview_path: pathlib.Path) -> bool:
        if HAS_PYGAME:
            try:
                if not self._pygame_ready:
                    pygame.mixer.init()
                    self._pygame_ready = True
                pygame.mixer.music.load(str(preview_path))
                pygame.mixer.music.play()
                return True
            except Exception:
                self._pygame_ready = False
        if HAS_WINSOUND:
            try:
                winsound.PlaySound(str(preview_path), winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
                return True
            except RuntimeError:
                return False
        return False

    def on_file_selection_change(self, _event=None) -> None:
        source = self._get_selected_preview_source()
        if source is not None and self.current_analysis is None:
            self.preview_info_var.set(f"Selected: {source.name}")

    def on_analysis_canvas_resize(self, _event=None) -> None:
        if self.current_analysis is not None:
            self._refresh_analysis_views()

    def _setup_fx_visual_traces(self) -> None:
        vars_to_watch: List[tk.Variable] = [
            self.high_pass_var,
            self.low_pass_var,
            self.eq_taps_var,
            self.output_sample_rate_var,
            self.output_bit_depth_var,
            self.output_alias_interpolation_var,
            self.output_alias_quality_var,
            self.texture_type_var,
            self.texture_mix_var,
            self.texture_fade_ms_var,
            self.pitch_enabled_var,
            self.pitch_mode_var,
            self.pitch_semitones_var,
            self.pitch_cents_var,
            self.pitch_millicents_var,
            self.pitch_source_a_var,
            self.pitch_target_a_var,
            self.pitch_preset_var,
            self.stereo_enabled_var,
            self.stereo_width_var,
            self.tape_enabled_var,
            self.tape_drive_var,
            self.tape_mix_var,
            self.tape_alias_interpolation_var,
            self.tape_alias_quality_var,
            self.compressor_enabled_var,
            self.compressor_mode_var,
            self.compressor_threshold_var,
            self.compressor_ratio_var,
            self.compressor_attack_var,
            self.compressor_release_var,
            self.compressor_makeup_var,
            self.compressor_valve_var,
            self.limiter_enabled_var,
            self.limiter_ceiling_var,
            self.limiter_lookahead_var,
            self.limiter_release_var,
            self.humanize_enabled_var,
            self.humanize_min_db_var,
            self.humanize_max_db_var,
            self.humanize_section_ms_var,
            self.offset_enabled_var,
            self.offset_max_ms_var,
        ]
        for var in vars_to_watch:
            trace_id = var.trace_add("write", self._on_preview_parameter_var_changed)
            self._fx_visual_trace_ids.append((var, trace_id))
        for canvas in (
            self.texture_fx_canvas,
            self.pitch_fx_canvas,
            self.tape_fx_canvas,
            self.compressor_fx_canvas,
            self.limiter_fx_canvas,
            self.stereo_fx_canvas,
            self.humanize_fx_canvas,
            self.offsets_fx_canvas,
        ):
            canvas.bind("<Configure>", lambda _event: self._refresh_fx_visuals())

    def _on_preview_parameter_var_changed(self, *_args) -> None:
        self._refresh_fx_visuals()
        self._schedule_preview_refresh()

    def _schedule_preview_refresh(self) -> None:
        if not (self.is_preview_rendering or self._preview_is_streaming or self.preview_path is not None):
            return
        source = self._get_selected_preview_source()
        if source is None:
            return
        try:
            self._get_filter_settings()
            self._get_output_format_settings()
            self._get_effect_settings()
        except Exception:
            return
        self._preview_refresh_pending = True
        if self._preview_refresh_after_id is not None:
            try:
                self.root.after_cancel(self._preview_refresh_after_id)
            except tk.TclError:
                pass
        self._preview_refresh_after_id = self.root.after(PREVIEW_REFRESH_DEBOUNCE_MS, self._restart_preview_with_current_settings)

    def _restart_preview_with_current_settings(self) -> None:
        self._preview_refresh_after_id = None
        if not self._preview_refresh_pending:
            return
        self._preview_refresh_pending = False
        if self.is_processing:
            return
        source = self._get_selected_preview_source()
        if source is None:
            return
        try:
            self._get_filter_settings()
            self._get_output_format_settings()
            self._get_effect_settings()
        except Exception:
            return
        self.preview_selected()

    def _safe_float(self, var: tk.StringVar, fallback: float = 0.0) -> float:
        try:
            return float(var.get().strip())
        except ValueError:
            return fallback

    def _refresh_fx_visuals(self) -> None:
        if not hasattr(self, "texture_fx_canvas"):
            return
        self._draw_texture_fx()
        self._draw_pitch_fx()
        self._draw_tape_fx()
        self._draw_compressor_fx()
        self._draw_limiter_fx()
        self._draw_stereo_fx()
        self._draw_humanize_fx()
        self._draw_offsets_fx()

    def _clear_fx_canvas(self, canvas: tk.Canvas, title: str) -> tuple[int, int]:
        canvas.delete("all")
        width = max(40, canvas.winfo_width())
        height = max(40, canvas.winfo_height())
        canvas.create_rectangle(0, 0, width, height, fill="#0d1116", outline="")
        canvas.create_text(8, 10, text=title, fill="#9fb0c2", anchor="w")
        return width, height

    def _draw_texture_fx(self) -> None:
        width, height = self._clear_fx_canvas(self.texture_fx_canvas, "Texture profile")
        amount = _clamp(self._safe_float(self.texture_mix_var), 0.0, 100.0) / 100.0
        texture_type = self.texture_type_var.get().strip().lower()
        center_y = height * 0.68
        self.texture_fx_canvas.create_line(12, center_y, width - 12, center_y, fill="#22303c")
        points: List[float] = []
        for x in range(16, width - 12):
            t = (x - 16) / max(1, width - 28)
            if texture_type == "pink":
                profile = 1.0 - 0.75 * t
            elif texture_type == "room":
                profile = 0.55 - 0.2 * t + 0.14 * math.sin(t * math.pi * 5.0)
            elif texture_type == "vinyl":
                profile = 0.2 + 0.22 * math.sin(t * math.pi * 28.0) ** 2 + (0.25 * (1.0 - t))
            else:
                profile = 0.0
            y = center_y - (profile * amount * (height * 0.45))
            points.extend((x, y))
        if len(points) >= 4:
            self.texture_fx_canvas.create_line(points, fill="#78dce8", width=2, smooth=True)
        self.texture_fx_canvas.create_text(width - 12, 10, text=f"{texture_type or 'none'}  {int(round(amount * 100))}%", fill="#78dce8", anchor="ne")

    def _draw_pitch_fx(self) -> None:
        width, height = self._clear_fx_canvas(self.pitch_fx_canvas, "Pitch shift")
        enabled, mode, shift_semitones, source_a, target_a = self._get_pitch_shift_details(strict=False)
        cents = shift_semitones * 100.0
        ratio = 2.0 ** (shift_semitones / 12.0)
        self.pitch_summary_var.set(
            f"{shift_semitones:+.4f} st  |  {cents:+.1f} cents  |  ratio {ratio:.5f}  |  A{source_a:.1f} -> A{target_a:.1f}"
        )
        center_y = height * 0.68
        left = 18
        right = width - 18
        self.pitch_fx_canvas.create_line(left, center_y, right, center_y, fill="#22303c")
        self.pitch_fx_canvas.create_line(left, 18, left, height - 18, fill="#22303c")
        limit = max(1.0, PITCH_SHIFT_LIMIT_SEMITONES)
        shift_x = left + ((shift_semitones + limit) / (2.0 * limit)) * max(1.0, right - left)
        shift_x = _clamp(shift_x, left, right)
        self.pitch_fx_canvas.create_line(shift_x, 22, shift_x, height - 22, fill="#a6da95", width=3)
        self.pitch_fx_canvas.create_oval(shift_x - 5, center_y - 5, shift_x + 5, center_y + 5, fill="#a6da95", outline="")
        for mark in (-12, -6, 0, 6, 12):
            x = left + ((mark + limit) / (2.0 * limit)) * max(1.0, right - left)
            self.pitch_fx_canvas.create_line(x, center_y - 6, x, center_y + 6, fill="#44515e")
            self.pitch_fx_canvas.create_text(x, center_y + 16, text=f"{mark:+.0f}", fill="#7b8a99", anchor="n")
        descriptor = f"{'on' if enabled else 'off'}  {mode}  {shift_semitones:+.4f} st"
        if mode == "frequency":
            descriptor += f"  A{source_a:.1f}->{target_a:.1f}"
        self.pitch_fx_canvas.create_text(width - 12, 10, text=descriptor, fill="#a6da95", anchor="ne")

    def _draw_tape_fx(self) -> None:
        width, height = self._clear_fx_canvas(self.tape_fx_canvas, "Tape transfer curve")
        enabled = bool(self.tape_enabled_var.get())
        drive = _clamp(self._safe_float(self.tape_drive_var), 0.0, 100.0) / 100.0
        mix = _clamp(self._safe_float(self.tape_mix_var), 0.0, 100.0) / 100.0
        interpolation = self.tape_alias_interpolation_var.get().strip().lower()
        quality = self.tape_alias_quality_var.get().strip().lower()
        self.tape_fx_canvas.create_line(18, height - 18, width - 18, 18, fill="#22303c")
        self.tape_fx_canvas.create_line(18, height / 2, width - 18, height / 2, fill="#22303c")
        self.tape_fx_canvas.create_line(width / 2, 18, width / 2, height - 18, fill="#22303c")
        points: List[float] = []
        curve_drive = 1.0 + 6.0 * drive
        bias = 0.012 + 0.02 * drive
        for i in range(width - 36):
            x_norm = (i / max(1, width - 37)) * 2.0 - 1.0
            y_norm = _tape_nonlinearity(x_norm, curve_drive, bias) if enabled else x_norm
            x = 18 + i
            y = (height - 18) - ((y_norm + 1.0) * 0.5 * (height - 36))
            points.extend((x, y))
        if len(points) >= 4:
            self.tape_fx_canvas.create_line(points, fill="#ff9e64", width=2, smooth=True)
        self.tape_fx_canvas.create_text(
            width - 12,
            10,
            text=f"{'on' if enabled else 'off'}  drive {int(round(drive * 100))}%  wet {int(round(mix * 100))}%  up {interpolation}  down {quality}",
            fill="#ff9e64",
            anchor="ne",
        )

    def _draw_compressor_fx(self) -> None:
        width, height = self._clear_fx_canvas(self.compressor_fx_canvas, "Compression curve")
        enabled = bool(self.compressor_enabled_var.get())
        threshold = self._safe_float(self.compressor_threshold_var, DEFAULT_COMP_THRESHOLD_DB)
        ratio = max(1.0, self._safe_float(self.compressor_ratio_var, DEFAULT_COMP_RATIO))
        makeup = self._safe_float(self.compressor_makeup_var, DEFAULT_COMP_MAKEUP_DB)
        mode = self.compressor_mode_var.get().strip().lower()
        valve = bool(self.compressor_valve_var.get())
        min_db = -48.0
        max_db = 6.0
        colors = ["#7aa2f7"] if mode != "multiband" else ["#7aa2f7", "#9ece6a", "#f7768e"]
        offsets = [0.0] if mode != "multiband" else [-2.5, 0.0, 2.5]
        for color, offset in zip(colors, offsets):
            points: List[float] = []
            for i in range(width - 36):
                in_db = min_db + (i / max(1, width - 37)) * (max_db - min_db)
                out_db = in_db + _compressor_gain_db(in_db, threshold + offset, ratio) + makeup
                x = 18 + i
                y = (height - 18) - ((_clamp(out_db, min_db, max_db) - min_db) / (max_db - min_db) * (height - 36))
                points.extend((x, y))
            self.compressor_fx_canvas.create_line(points, fill=color, width=2, smooth=True)
        self.compressor_fx_canvas.create_line(18, height - 18, width - 18, 18, fill="#22303c")
        label = f"{mode}  {'on' if enabled else 'off'}  {threshold:.1f} dB  {ratio:.1f}:1"
        if valve:
            label += "  valve"
        self.compressor_fx_canvas.create_text(width - 12, 10, text=label, fill="#c0caf5", anchor="ne")

    def _draw_limiter_fx(self) -> None:
        width, height = self._clear_fx_canvas(self.limiter_fx_canvas, "Limiter response")
        enabled = bool(self.limiter_enabled_var.get())
        ceiling = min(0.0, self._safe_float(self.limiter_ceiling_var, DEFAULT_LIMITER_CEILING_DB))
        lookahead = self._safe_float(self.limiter_lookahead_var, DEFAULT_LIMITER_LOOKAHEAD_MS)
        release = self._safe_float(self.limiter_release_var, DEFAULT_LIMITER_RELEASE_MS)
        min_db = -18.0
        max_db = 3.0
        ceiling_y = (height - 18) - (((ceiling - min_db) / (max_db - min_db)) * (height - 36))
        self.limiter_fx_canvas.create_line(18, ceiling_y, width - 18, ceiling_y, fill="#f7768e", dash=(4, 3))
        points: List[float] = []
        knee_start = ceiling - 3.0
        for i in range(width - 36):
            in_db = min_db + (i / max(1, width - 37)) * (max_db - min_db)
            if not enabled:
                out_db = in_db
            elif in_db <= knee_start:
                out_db = in_db
            else:
                soft = _clamp((in_db - knee_start) / max(0.5, 3.0 + lookahead), 0.0, 1.0)
                out_db = in_db + (ceiling - in_db) * soft
                out_db = min(out_db, ceiling)
            x = 18 + i
            y = (height - 18) - (((_clamp(out_db, min_db, max_db) - min_db) / (max_db - min_db)) * (height - 36))
            points.extend((x, y))
        self.limiter_fx_canvas.create_line(points, fill="#e0af68", width=2, smooth=True)
        self.limiter_fx_canvas.create_text(width - 12, 10, text=f"{'on' if enabled else 'off'}  ceil {ceiling:.1f}  look {lookahead:.1f}ms  rel {release:.0f}ms", fill="#e0af68", anchor="ne")

    def _draw_stereo_fx(self) -> None:
        width, height = self._clear_fx_canvas(self.stereo_fx_canvas, "Stereo width")
        enabled = bool(self.stereo_enabled_var.get())
        width_percent = _clamp(self._safe_float(self.stereo_width_var, DEFAULT_STEREO_WIDTH_PERCENT), 0.0, 200.0)
        center_x = width * 0.5
        center_y = height * 0.56
        image_width = (width - 60) * (width_percent / 200.0)
        image_height = height * 0.48
        color = "#7dcfff" if enabled else "#4b5563"
        self.stereo_fx_canvas.create_line(20, center_y, width - 20, center_y, fill="#22303c")
        self.stereo_fx_canvas.create_line(center_x, 22, center_x, height - 16, fill="#22303c")
        self.stereo_fx_canvas.create_oval(
            center_x - image_width * 0.5,
            center_y - image_height * 0.5,
            center_x + image_width * 0.5,
            center_y + image_height * 0.5,
            outline=color,
            width=2,
        )
        self.stereo_fx_canvas.create_text(width - 12, 10, text=f"{'on' if enabled else 'off'}  width {width_percent:.0f}%", fill=color, anchor="ne")

    def _draw_humanize_fx(self) -> None:
        width, height = self._clear_fx_canvas(self.humanize_fx_canvas, "Humanize envelope")
        enabled = bool(self.humanize_enabled_var.get())
        min_db = self._safe_float(self.humanize_min_db_var, -2.0)
        max_db = self._safe_float(self.humanize_max_db_var, -0.5)
        section_ms = max(50.0, self._safe_float(self.humanize_section_ms_var, 900.0))
        lo = min(min_db, max_db)
        hi = max(min_db, max_db)
        db_span = max(0.5, hi - lo)
        sections = max(3, min(12, int(4000.0 / section_ms) + 3))
        seeds = [lo + db_span * ((math.sin(idx * 1.7) + 1.0) * 0.5) for idx in range(sections + 1)]
        points: List[float] = []
        for i in range(width - 24):
            t = i / max(1, width - 25)
            idx = min(sections - 1, int(t * sections))
            frac = (t * sections) - idx
            value = seeds[idx] + (seeds[idx + 1] - seeds[idx]) * frac
            x = 12 + i
            y = (height - 18) - (((value - lo) / db_span) * (height - 36))
            points.extend((x, y))
        self.humanize_fx_canvas.create_line(points, fill="#9ece6a" if enabled else "#4b5563", width=2, smooth=True)
        self.humanize_fx_canvas.create_text(width - 12, 10, text=f"{'on' if enabled else 'off'}  {lo:.1f}..{hi:.1f} dB  {section_ms:.0f} ms", fill="#9ece6a", anchor="ne")

    def _draw_offsets_fx(self) -> None:
        width, height = self._clear_fx_canvas(self.offsets_fx_canvas, "Stem offset preview")
        enabled = bool(self.offset_enabled_var.get())
        max_offset = max(0.0, self._safe_float(self.offset_max_ms_var, 100.0))
        channels = [("L", 0.2), ("R", 0.65)]
        colors = ["#7dcfff", "#bb9af7"]
        for idx, ((label, y_frac), color) in enumerate(zip(channels, colors)):
            y = 20 + y_frac * (height - 40)
            preview_offset = 0.0 if not enabled else max_offset * (0.35 + idx * 0.4)
            x0 = 24
            x1 = 24 + (preview_offset / max(1.0, max_offset if max_offset > 0 else 1.0)) * (width - 64)
            self.offsets_fx_canvas.create_text(14, y, text=label, fill=color, anchor="w")
            self.offsets_fx_canvas.create_line(x0, y, width - 20, y, fill="#22303c", width=4)
            self.offsets_fx_canvas.create_line(x0, y, x1, y, fill=color, width=6)
            self.offsets_fx_canvas.create_oval(x1 - 4, y - 4, x1 + 4, y + 4, fill=color, outline="")
        self.offsets_fx_canvas.create_text(width - 12, 10, text=f"{'on' if enabled else 'off'}  max {max_offset:.0f} ms", fill="#7dcfff", anchor="ne")

    def _refresh_analysis_views(self) -> None:
        if self.current_analysis is None:
            return
        self._draw_waveform(self.current_analysis.get("waveform", []))
        self._draw_spectrum(self.current_analysis.get("spectrum", []), float(self.current_analysis.get("sample_rate", 44100)))
        self._draw_spectrogram(self.current_analysis.get("spectrogram"))
        self._draw_peak_meter(float(self.current_analysis.get("peak_dbfs", -180.0)))

    def _draw_waveform(self, waveform: Sequence[Tuple[float, float]]) -> None:
        canvas = self.waveform_canvas
        canvas.delete("all")
        width = max(10, canvas.winfo_width())
        height = max(10, canvas.winfo_height())
        canvas.create_line(0, height / 2, width, height / 2, fill="#24303d")
        if not waveform:
            canvas.create_text(width / 2, height / 2, text="Preview to view waveform", fill="#8fa3b8")
            return
        step = len(waveform) / max(1, width)
        for x in range(width):
            idx = min(len(waveform) - 1, int(x * step))
            lo, hi = waveform[idx]
            y1 = height * (0.5 - (hi * 0.46))
            y2 = height * (0.5 - (lo * 0.46))
            canvas.create_line(x, y1, x, y2, fill="#5dd1ff")

    def _draw_spectrum(self, spectrum: Sequence[Tuple[float, float]], sample_rate: float) -> None:
        canvas = self.spectrum_canvas
        canvas.delete("all")
        width = max(10, canvas.winfo_width())
        height = max(10, canvas.winfo_height())
        if not spectrum:
            canvas.create_text(width / 2, height / 2, text="Preview to view spectrum", fill="#8fa3b8")
            return

        freqs = [point[0] for point in spectrum]
        mags = [point[1] for point in spectrum]
        min_db = -96.0
        max_db = 0.0
        points: List[float] = []
        min_freq = 20.0
        max_freq = max(min(sample_rate / 2.0, 20_000.0), min_freq * 1.1)
        log_min = math.log10(min_freq)
        log_span = math.log10(max_freq) - log_min
        for freq, mag in zip(freqs, mags):
            x = ((math.log10(max(freq, min_freq)) - log_min) / log_span) * width
            y = height - ((_clamp(mag, min_db, max_db) - min_db) / (max_db - min_db)) * (height - 8) - 4
            points.extend((x, y))
        canvas.create_line(points, fill="#ffb84d", width=2, smooth=True)
        for freq in (50, 100, 500, 1000, 5000, 10000):
            x = ((math.log10(freq) - log_min) / log_span) * width
            canvas.create_line(x, 0, x, height, fill="#1d2630")
            canvas.create_text(x + 2, height - 10, text=str(freq), fill="#6d7f91", anchor="w")

    def _spectrogram_color(self, value: int) -> str:
        v = _clamp(value / 255.0, 0.0, 1.0)
        if v < 0.25:
            t = v / 0.25
            r, g, b = 10, int(20 + 80 * t), int(40 + 120 * t)
        elif v < 0.5:
            t = (v - 0.25) / 0.25
            r, g, b = int(10 + 80 * t), int(100 + 90 * t), int(160 + 60 * t)
        elif v < 0.75:
            t = (v - 0.5) / 0.25
            r, g, b = int(90 + 120 * t), int(190 + 40 * t), int(220 - 110 * t)
        else:
            t = (v - 0.75) / 0.25
            r, g, b = 210, int(230 - 90 * t), int(110 - 80 * t)
        return f"#{r:02x}{g:02x}{b:02x}"

    def _draw_spectrogram(self, spectrogram) -> None:
        canvas = self.spectrogram_canvas
        canvas.delete("all")
        width = max(10, canvas.winfo_width())
        height = max(10, canvas.winfo_height())
        if spectrogram is None or (HAS_NUMPY and isinstance(spectrogram, np.ndarray) and spectrogram.size == 0):
            canvas.create_text(width / 2, height / 2, text="Preview to view spectrogram", fill="#8fa3b8")
            return
        if not HAS_NUMPY or not isinstance(spectrogram, np.ndarray):
            canvas.create_text(width / 2, height / 2, text="Spectrogram unavailable", fill="#8fa3b8")
            return

        y_idx = np.linspace(0, spectrogram.shape[0] - 1, height, dtype=int)
        x_idx = np.linspace(0, spectrogram.shape[1] - 1, width, dtype=int)
        resized = spectrogram[np.ix_(y_idx, x_idx)]
        img = tk.PhotoImage(width=width, height=height)
        for y in range(height):
            row = " ".join(self._spectrogram_color(int(v)) for v in resized[y])
            img.put("{" + row + "}", to=(0, y))
        self.spectrogram_image = img
        canvas.create_image(0, 0, image=img, anchor="nw")

    def _draw_peak_meter(self, peak_dbfs: float) -> None:
        canvas = self.peak_meter_canvas
        canvas.delete("all")
        width = max(10, canvas.winfo_width())
        height = max(10, canvas.winfo_height())
        canvas.create_rectangle(0, 0, width, height, outline="#2c343d", fill="#101317")
        floor_db = -60.0
        clipped = _clamp(peak_dbfs, floor_db, 0.0)
        fill_height = (clipped - floor_db) / abs(floor_db) * (height - 4)
        y0 = height - 2 - fill_height
        color = "#ff4d5d" if peak_dbfs > -3.0 else "#f4b942" if peak_dbfs > -9.0 else "#35d07f"
        canvas.create_rectangle(2, y0, width - 2, height - 2, outline="", fill=color)
        for mark in (0, -6, -12, -24, -48):
            y = height - 2 - ((_clamp(mark, floor_db, 0.0) - floor_db) / abs(floor_db) * (height - 4))
            canvas.create_line(0, y, width, y, fill="#25303a")
        self.peak_meter_var.set(f"Peak: {peak_dbfs:.1f} dBFS")

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
            "output_sample_rate": self.output_sample_rate_var,
            "output_bit_depth": self.output_bit_depth_var,
            "output_alias_interpolation": self.output_alias_interpolation_var,
            "output_alias_quality": self.output_alias_quality_var,
            "pitch_mode": self.pitch_mode_var,
            "pitch_semitones": self.pitch_semitones_var,
            "pitch_cents": self.pitch_cents_var,
            "pitch_millicents": self.pitch_millicents_var,
            "pitch_source_a_hz": self.pitch_source_a_var,
            "pitch_target_a_hz": self.pitch_target_a_var,
            "pitch_preset": self.pitch_preset_var,
            "stereo_width_percent": self.stereo_width_var,
            "tape_drive_percent": self.tape_drive_var,
            "tape_mix_percent": self.tape_mix_var,
            "tape_alias_interpolation": self.tape_alias_interpolation_var,
            "tape_alias_quality": self.tape_alias_quality_var,
            "compressor_mode": self.compressor_mode_var,
            "compressor_threshold_db": self.compressor_threshold_var,
            "compressor_ratio": self.compressor_ratio_var,
            "compressor_attack_ms": self.compressor_attack_var,
            "compressor_release_ms": self.compressor_release_var,
            "compressor_makeup_db": self.compressor_makeup_var,
            "limiter_ceiling_db": self.limiter_ceiling_var,
            "limiter_lookahead_ms": self.limiter_lookahead_var,
            "limiter_release_ms": self.limiter_release_var,
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
            "pitch_enabled": self.pitch_enabled_var,
            "tape_enabled": self.tape_enabled_var,
            "compressor_enabled": self.compressor_enabled_var,
            "compressor_valve_warmth": self.compressor_valve_var,
            "limiter_enabled": self.limiter_enabled_var,
            "stereo_enabled": self.stereo_enabled_var,
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
        try:
            texture_amount = float(self.texture_mix_var.get().strip() or "0")
        except ValueError:
            texture_amount = 0.0
        return {
            "window_geometry": self.root.geometry(),
            "high_pass_hz": self.high_pass_var.get(),
            "low_pass_hz": self.low_pass_var.get(),
            "eq_taps": self.eq_taps_var.get(),
            "worker_count": self.worker_count_var.get(),
            "output_folder": self.output_folder_var.get(),
            "produced_by": self.produced_by_var.get(),
            "output_sample_rate": self.output_sample_rate_var.get(),
            "output_bit_depth": self.output_bit_depth_var.get(),
            "output_alias_interpolation": self.output_alias_interpolation_var.get(),
            "output_alias_quality": self.output_alias_quality_var.get(),
            "pitch_enabled": bool(self.pitch_enabled_var.get()),
            "pitch_mode": self.pitch_mode_var.get(),
            "pitch_semitones": self.pitch_semitones_var.get(),
            "pitch_cents": self.pitch_cents_var.get(),
            "pitch_millicents": self.pitch_millicents_var.get(),
            "pitch_source_a_hz": self.pitch_source_a_var.get(),
            "pitch_target_a_hz": self.pitch_target_a_var.get(),
            "pitch_preset": self.pitch_preset_var.get(),
            "stereo_enabled": bool(self.stereo_enabled_var.get()),
            "stereo_width_percent": self.stereo_width_var.get(),
            "tape_enabled": bool(self.tape_enabled_var.get()),
            "tape_drive_percent": self.tape_drive_var.get(),
            "tape_mix_percent": self.tape_mix_var.get(),
            "tape_alias_interpolation": self.tape_alias_interpolation_var.get(),
            "tape_alias_quality": self.tape_alias_quality_var.get(),
            "compressor_enabled": bool(self.compressor_enabled_var.get()),
            "compressor_mode": self.compressor_mode_var.get(),
            "compressor_threshold_db": self.compressor_threshold_var.get(),
            "compressor_ratio": self.compressor_ratio_var.get(),
            "compressor_attack_ms": self.compressor_attack_var.get(),
            "compressor_release_ms": self.compressor_release_var.get(),
            "compressor_makeup_db": self.compressor_makeup_var.get(),
            "compressor_valve_warmth": bool(self.compressor_valve_var.get()),
            "limiter_enabled": bool(self.limiter_enabled_var.get()),
            "limiter_ceiling_db": self.limiter_ceiling_var.get(),
            "limiter_lookahead_ms": self.limiter_lookahead_var.get(),
            "limiter_release_ms": self.limiter_release_var.get(),
            "texture_type": self.texture_type_var.get(),
            "texture_mix_percent": self.texture_mix_var.get(),
            "texture_level_db": str(round(_texture_amount_to_level_db(texture_amount), 1)),
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
        self.stop_preview()
        self._cleanup_preview_file(self.preview_path)
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

    def _get_output_format_settings(self) -> Tuple[int | None, int | None, str, str]:
        sample_rate_text = self.output_sample_rate_var.get().strip().lower()
        bit_depth_text = self.output_bit_depth_var.get().strip().lower()
        interpolation = self.output_alias_interpolation_var.get().strip().lower()
        quality = self.output_alias_quality_var.get().strip().lower()

        target_sample_rate = None if sample_rate_text == "source" else int(sample_rate_text)
        target_sample_width = None if bit_depth_text == "source" else int(int(bit_depth_text) / 8)

        if target_sample_rate is not None and target_sample_rate < 8000:
            raise ValueError("Output sample rate must be at least 8000 Hz.")
        if target_sample_width not in {None, 2, 3, 4}:
            raise ValueError("Output bit depth must be source, 16, 24, or 32 bit.")
        if interpolation not in {"linear", "spline"}:
            raise ValueError("Output upscaler must be linear or spline.")
        if quality not in {"standard", "high", "perfect"}:
            raise ValueError("Output correction must be standard, high, or perfect.")
        return target_sample_rate, target_sample_width, interpolation, quality

    def _get_effect_settings(
        self,
    ) -> Tuple[
        bool,
        float,
        bool,
        float,
        float,
        str,
        str,
        bool,
        str,
        float,
        float,
        float,
        float,
        float,
        bool,
        bool,
        float,
        float,
        float,
        str,
        float,
        float,
        float,
        bool,
        float,
        float,
        float,
        bool,
        float,
        bool,
        float,
    ]:
        pitch_enabled, _pitch_mode, pitch_shift_semitones, _source_a, _target_a = self._get_pitch_shift_details(strict=True)

        tape_enabled = bool(self.tape_enabled_var.get())
        tape_drive = float(self.tape_drive_var.get().strip())
        tape_mix = float(self.tape_mix_var.get().strip())
        tape_alias_interpolation = self.tape_alias_interpolation_var.get().strip().lower()
        tape_alias_quality = self.tape_alias_quality_var.get().strip().lower()

        compressor_enabled = bool(self.compressor_enabled_var.get())
        compressor_mode = self.compressor_mode_var.get().strip().lower()
        compressor_threshold = float(self.compressor_threshold_var.get().strip())
        compressor_ratio = float(self.compressor_ratio_var.get().strip())
        compressor_attack = float(self.compressor_attack_var.get().strip())
        compressor_release = float(self.compressor_release_var.get().strip())
        compressor_makeup = float(self.compressor_makeup_var.get().strip())
        compressor_valve = bool(self.compressor_valve_var.get())

        limiter_enabled = bool(self.limiter_enabled_var.get())
        limiter_ceiling = float(self.limiter_ceiling_var.get().strip())
        limiter_lookahead = float(self.limiter_lookahead_var.get().strip())
        limiter_release = float(self.limiter_release_var.get().strip())

        texture_type = self.texture_type_var.get().strip().lower()
        if texture_type not in {"none", "pink", "room", "vinyl"}:
            raise ValueError("Texture must be one of: none, pink, room, vinyl")

        mix = float(self.texture_mix_var.get().strip())
        level_db = _texture_amount_to_level_db(mix)
        self.texture_level_db_var.set(str(round(level_db, 1)))
        fade_ms = float(self.texture_fade_ms_var.get().strip())

        stereo_enabled = bool(self.stereo_enabled_var.get())
        stereo_width = float(self.stereo_width_var.get().strip())

        humanize = bool(self.humanize_enabled_var.get())
        min_db = float(self.humanize_min_db_var.get().strip())
        max_db = float(self.humanize_max_db_var.get().strip())
        section_ms = float(self.humanize_section_ms_var.get().strip())

        offset_enabled = bool(self.offset_enabled_var.get())
        offset_max_ms = float(self.offset_max_ms_var.get().strip())

        if tape_drive < 0 or tape_drive > 100:
            raise ValueError("Tape drive must be 0-100%.")
        if tape_mix < 0 or tape_mix > 100:
            raise ValueError("Tape wet mix must be 0-100%.")
        if tape_alias_interpolation not in {"linear", "spline"}:
            raise ValueError("Tape interpolation must be linear or spline.")
        if tape_alias_quality not in {"standard", "high", "perfect"}:
            raise ValueError("Tape quality must be standard, high, or perfect.")
        if compressor_mode not in {"single-band", "multiband"}:
            raise ValueError("Compressor mode must be single-band or multiband.")
        if compressor_ratio < 1.0:
            raise ValueError("Compressor ratio must be >= 1.0.")
        if compressor_attack < 0.1:
            raise ValueError("Compressor attack must be >= 0.1 ms.")
        if compressor_release < 1.0:
            raise ValueError("Compressor release must be >= 1 ms.")
        if limiter_ceiling > 0:
            raise ValueError("Limiter ceiling must be <= 0 dBFS.")
        if limiter_lookahead < 0.1:
            raise ValueError("Limiter lookahead must be >= 0.1 ms.")
        if limiter_release < 1.0:
            raise ValueError("Limiter release must be >= 1 ms.")
        if mix < 0 or mix > 100:
            raise ValueError("Texture amount must be 0-100%.")
        if stereo_width < 0 or stereo_width > 200:
            raise ValueError("Stereo width must be 0-200%.")
        if fade_ms < 0:
            raise ValueError("Fade ms must be >= 0.")
        if offset_max_ms < 0:
            raise ValueError("Offset max ms must be >= 0.")

        if not self._has_premium_unlock():
            pitch_enabled = False
            pitch_shift_semitones = 0.0
            tape_enabled = False
            tape_drive = 0.0
            tape_mix = 0.0
            compressor_enabled = False
            compressor_mode = "single-band"
            compressor_threshold = DEFAULT_COMP_THRESHOLD_DB
            compressor_ratio = DEFAULT_COMP_RATIO
            compressor_attack = DEFAULT_COMP_ATTACK_MS
            compressor_release = DEFAULT_COMP_RELEASE_MS
            compressor_makeup = DEFAULT_COMP_MAKEUP_DB
            compressor_valve = False
            texture_type = "none"
            mix = 0.0
            level_db = _texture_amount_to_level_db(mix)
            fade_ms = 0.0
            humanize = False
            min_db = 0.0
            max_db = 0.0
            section_ms = 0.0
            offset_enabled = False
            offset_max_ms = 0.0
            stereo_enabled = False
            stereo_width = DEFAULT_STEREO_WIDTH_PERCENT

        return (
            pitch_enabled,
            pitch_shift_semitones,
            tape_enabled,
            tape_drive,
            tape_mix,
            tape_alias_interpolation,
            tape_alias_quality,
            compressor_enabled,
            compressor_mode,
            compressor_threshold,
            compressor_ratio,
            compressor_attack,
            compressor_release,
            compressor_makeup,
            compressor_valve,
            limiter_enabled,
            limiter_ceiling,
            limiter_lookahead,
            limiter_release,
            texture_type,
            mix,
            level_db,
            fade_ms,
            humanize,
            min_db,
            max_db,
            section_ms,
            offset_enabled,
            offset_max_ms,
            stereo_enabled,
            stereo_width,
        )

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
            output_sample_rate, output_sample_width, output_alias_interpolation, output_alias_quality = self._get_output_format_settings()
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
                settings[10],
                settings[11],
                settings[12],
                settings[13],
                settings[14],
                settings[15],
                settings[16],
                settings[17],
                settings[18],
                settings[19],
                settings[20],
                settings[21],
                settings[22],
                settings[23],
                settings[24],
                settings[25],
                settings[26],
                settings[27],
                settings[28],
                settings[29],
                settings[30],
                eq_taps,
                output_sample_rate if output_sample_rate is not None else 0,
                output_sample_width if output_sample_width is not None else 0,
                output_alias_interpolation,
                output_alias_quality,
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

    def _run_processing_job(
        self,
        tasks: List[Tuple[object, ...]],
        workers: int,
    ) -> None:
        ok = 0
        failures: List[str] = []
        total = len(tasks)
        cancelled = False
        try:
            if total == 1 and workers > 1 and _task_supports_chunk_parallel(tasks[0]):
                wav_path = pathlib.Path(tasks[0][0])
                self._post_ui_event("status", f"Processing {wav_path.name} in parallel across {workers} workers...")
                try:
                    self._run_single_file_parallel_job(tasks[0], workers)
                    ok = 1
                except RuntimeError as exc:
                    if str(exc) == "Cancelled":
                        cancelled = True
                    else:
                        failures.append(f"{wav_path.name}: {exc}")
                except Exception as exc:
                    failures.append(f"{wav_path.name}: {exc}")
                self._post_ui_event("progress", (1, total))
            elif total == 1 and workers > 1:
                wav_path = pathlib.Path(tasks[0][0])
                self._post_ui_event(
                    "status",
                    f"Processing {wav_path.name} serially because the current FX chain is not chunk-safe for parallel export.",
                )
                try:
                    process_wav_file_task(tasks[0])
                    ok = 1
                except Exception as exc:
                    failures.append(f"{wav_path.name}: {exc}")
                self._post_ui_event("progress", (1, total))
            elif workers == 1 or total == 1:
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

    def _run_single_file_parallel_job(self, task: Tuple[object, ...], workers: int) -> None:
        source_path = pathlib.Path(task[0])
        output_directory = pathlib.Path(task[1]).resolve() if task[1] else None
        produced_by = str(task[2])
        with wave.open(str(source_path), "rb") as wf:
            sample_rate = wf.getframerate()
            total_frames = wf.getnframes()
            channels = wf.getnchannels()
            sample_width = wf.getsampwidth()
            if wf.getcomptype() != "NONE":
                raise RuntimeError("Compressed WAV is not supported.")

        if sample_rate <= 0 or total_frames <= 0:
            raise RuntimeError("WAV has no audio frames.")
        duration_s = total_frames / float(sample_rate)
        if duration_s < MIN_PARALLEL_FILE_SECONDS:
            process_wav_file_task(task)
            return

        chunk_frames = _choose_parallel_chunk_frames(total_frames, sample_rate, workers)
        pad_frames = int(sample_rate * (PARALLEL_CHUNK_PAD_MS / 1000.0))
        chunk_specs = []
        start_frame = 0
        chunk_index = 0
        while start_frame < total_frames:
            chunk_length = min(chunk_frames, total_frames - start_frame)
            chunk_specs.append((chunk_index, start_frame, chunk_length))
            start_frame += chunk_length
            chunk_index += 1

        if len(chunk_specs) < 2:
            process_wav_file_task(task)
            return

        chunk_tasks = [
            (
                idx,
                str(source_path),
                start,
                length,
                pad_frames,
                task[3],
                task[4],
                *task[5:36],
                task[36],
            )
            for idx, start, length in chunk_specs
        ]

        results: dict[int, bytes] = {}
        with ProcessPoolExecutor(max_workers=min(workers, len(chunk_tasks))) as pool:
            future_map = {pool.submit(process_wav_chunk_task, chunk_task): chunk_task[0] for chunk_task in chunk_tasks}
            completed = 0
            while future_map:
                if self.cancel_requested.is_set():
                    for future in list(future_map):
                        future.cancel()
                    pool.shutdown(wait=False, cancel_futures=True)
                    raise RuntimeError("Cancelled")
                done_set, _ = wait(list(future_map.keys()), timeout=0.1, return_when=FIRST_COMPLETED)
                if not done_set:
                    continue
                for future in done_set:
                    chunk_idx = future_map.pop(future)
                    result_idx, chunk_bytes, chunk_channels, chunk_sample_width, chunk_sample_rate = future.result()
                    if chunk_channels != channels or chunk_sample_width != sample_width or chunk_sample_rate != sample_rate:
                        raise RuntimeError("Chunk processing returned incompatible audio format.")
                    results[result_idx] = chunk_bytes
                    completed += 1
                    self._post_ui_event("status", f"{source_path.name}: completed chunk {completed}/{len(chunk_tasks)}")

        assembled = b"".join(results[idx] for idx in range(len(chunk_tasks)))
        finalize_processed_output(
            source_path,
            output_directory,
            produced_by,
            assembled,
            channels,
            sample_width,
            sample_rate,
            task[3],
            task[4],
            task[5],
            task[6],
            task[7],
            task[8],
            task[9],
            task[10],
            task[11],
            task[12],
            task[13],
            task[14],
            task[15],
            task[16],
            task[17],
            task[18],
            task[19],
            task[20],
            task[21],
            task[22],
            task[23],
            task[24],
            task[25],
            task[28],
            task[32],
            task[33],
            task[34],
            task[35],
            int(task[37]) or None,
            int(task[38]) or None,
            str(task[39]),
            str(task[40]),
        )


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
