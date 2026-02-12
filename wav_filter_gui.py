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

import math
import os
import pathlib
import random
import tkinter as tk
from concurrent.futures import ProcessPoolExecutor, as_completed
from tkinter import filedialog, messagebox, ttk
from typing import List, Sequence, Tuple
import wave


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


def process_wav_file(
    path: pathlib.Path,
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

    samples = _bytes_to_samples(frames, sample_width, channels)
    samples = apply_linear_phase_eq(samples, sample_rate, high_pass_hz, low_pass_hz, eq_taps)

    if offset_enabled:
        samples, offsets = apply_random_stem_offsets(samples, sample_rate, offset_max_ms)
    else:
        offsets = []

    samples = add_texture(samples, sample_rate, sample_width, texture_type, texture_mix_percent, texture_level_db, texture_fade_ms)

    if humanize_enabled:
        samples = apply_dynamic_humanize(samples, humanize_min_db, humanize_max_db, humanize_section_ms, sample_rate)

    hp_label = int(round(high_pass_hz))
    lp_label = int(round(low_pass_hz))
    suffix_parts = [f"hp{hp_label}", f"lp{lp_label}"]
    if texture_type != "none":
        suffix_parts.append(f"{texture_type}{int(round(texture_mix_percent))}pct")
    if humanize_enabled:
        suffix_parts.append("human")
    if offset_enabled:
        suffix_parts.append(f"off{int(round(offset_max_ms))}ms")
    output_path = path.with_name(f"{path.stem}_{'_'.join(suffix_parts)}{path.suffix}")

    with wave.open(str(output_path), "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(sample_width)
        wf.setframerate(sample_rate)
        wf.writeframes(_samples_to_bytes(samples, sample_width))

    return output_path


def process_wav_file_task(args: Tuple[str, float, float, str, float, float, float, bool, float, float, float, bool, float, int]) -> str:
    output = process_wav_file(
        pathlib.Path(args[0]),
        args[1],
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
    )
    return str(output)


class WavFilterApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("WAV Linear-Phase EQ + Texture Tool")
        self.root.geometry("920x690")

        self.selected_files: List[pathlib.Path] = []

        self.high_pass_var = tk.StringVar(value=str(int(DEFAULT_HIGH_PASS_HZ)))
        self.low_pass_var = tk.StringVar(value=str(int(DEFAULT_LOW_PASS_HZ)))
        self.eq_taps_var = tk.StringVar(value=str(DEFAULT_EQ_TAPS))
        self.worker_count_var = tk.StringVar(value=str(max(1, min(4, (os.cpu_count() or 1)))))

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

        self._build_ui()

    def _build_ui(self) -> None:
        top = tk.LabelFrame(self.root, text="Linear-phase EQ")
        top.pack(fill="x", padx=12, pady=8)

        tk.Label(top, text="High-pass (Hz):").grid(row=0, column=0, sticky="w", padx=6, pady=6)
        tk.Entry(top, textvariable=self.high_pass_var, width=12).grid(row=0, column=1, padx=6)

        tk.Label(top, text="Low-pass (Hz):").grid(row=0, column=2, sticky="w", padx=6)
        tk.Entry(top, textvariable=self.low_pass_var, width=12).grid(row=0, column=3, padx=6)

        tk.Label(top, text="EQ taps (quality/speed):").grid(row=0, column=4, sticky="w", padx=6)
        tk.Entry(top, textvariable=self.eq_taps_var, width=8).grid(row=0, column=5, padx=6)

        tk.Label(top, text="Workers:").grid(row=0, column=6, sticky="w", padx=6)
        tk.Entry(top, textvariable=self.worker_count_var, width=6).grid(row=0, column=7, padx=6)

        tk.Button(top, text="Add WAV Files", command=self.pick_files).grid(row=0, column=8, padx=(20, 6))
        tk.Button(top, text="Add Folder", command=self.pick_folder).grid(row=0, column=9, padx=6)
        tk.Button(top, text="Clear", command=self.clear_files).grid(row=0, column=10, padx=6)

        texture = tk.LabelFrame(self.root, text="Texture Layer (pink / room / vinyl)")
        texture.pack(fill="x", padx=12, pady=4)

        ttk.Combobox(texture, textvariable=self.texture_type_var, values=["none", "pink", "room", "vinyl"], width=10, state="readonly").grid(row=0, column=0, padx=6, pady=6)
        tk.Label(texture, text="Mix % (5-10 subtle):").grid(row=0, column=1, sticky="w")
        tk.Entry(texture, textvariable=self.texture_mix_var, width=8).grid(row=0, column=2, padx=6)
        tk.Label(texture, text="Level dB (-30 to -40):").grid(row=0, column=3, sticky="w")
        tk.Entry(texture, textvariable=self.texture_level_db_var, width=8).grid(row=0, column=4, padx=6)
        tk.Label(texture, text="Fade ms:").grid(row=0, column=5, sticky="w")
        tk.Entry(texture, textvariable=self.texture_fade_ms_var, width=8).grid(row=0, column=6, padx=6)

        human = tk.LabelFrame(self.root, text="Mild Dynamic Variation (Humanize)")
        human.pack(fill="x", padx=12, pady=4)

        tk.Checkbutton(human, text="Enable", variable=self.humanize_enabled_var).grid(row=0, column=0, padx=6, pady=6)
        tk.Label(human, text="Min dB:").grid(row=0, column=1, sticky="w")
        tk.Entry(human, textvariable=self.humanize_min_db_var, width=8).grid(row=0, column=2, padx=6)
        tk.Label(human, text="Max dB:").grid(row=0, column=3, sticky="w")
        tk.Entry(human, textvariable=self.humanize_max_db_var, width=8).grid(row=0, column=4, padx=6)
        tk.Label(human, text="Section ms:").grid(row=0, column=5, sticky="w")
        tk.Entry(human, textvariable=self.humanize_section_ms_var, width=8).grid(row=0, column=6, padx=6)

        offsets = tk.LabelFrame(self.root, text="Stem Random Offsets")
        offsets.pack(fill="x", padx=12, pady=4)

        tk.Checkbutton(offsets, text="Enable random stem offsets", variable=self.offset_enabled_var).grid(row=0, column=0, padx=6, pady=6)
        tk.Label(offsets, text="Max offset ms (0-100+):").grid(row=0, column=1, sticky="w")
        tk.Entry(offsets, textvariable=self.offset_max_ms_var, width=8).grid(row=0, column=2, padx=6)
        tk.Button(offsets, text="Randomizer", command=self.randomize_options).grid(row=0, column=3, padx=10)

        dnd_msg = "Drag/drop WAV files or folders below" if HAS_DND else "Install tkinterdnd2 to enable drag-and-drop"
        self.drop_label = tk.Label(self.root, text=dnd_msg, relief="groove", padx=8, pady=10)
        self.drop_label.pack(fill="x", padx=12, pady=8)

        self.file_list = tk.Listbox(self.root)
        self.file_list.pack(fill="both", expand=True, padx=12, pady=(0, 8))

        tk.Button(self.root, text="Process WAV(s)", command=self.process_files, height=2).pack(fill="x", padx=12, pady=(0, 8))

        self.status = tk.Label(self.root, text="Ready", anchor="w")
        self.status.pack(fill="x", padx=12, pady=(0, 10))

        if HAS_DND:
            self.drop_label.drop_target_register(DND_FILES)
            self.drop_label.dnd_bind("<<Drop>>", self.on_drop)

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

    def clear_files(self) -> None:
        self.selected_files.clear()
        self.file_list.delete(0, tk.END)
        self.set_status("Cleared file list.")

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
        if not self.selected_files:
            messagebox.showinfo("No files", "Please add at least one WAV file first.")
            return

        try:
            hp, lp, eq_taps, workers = self._get_filter_settings()
            settings = self._get_effect_settings()
        except Exception as exc:
            messagebox.showerror("Invalid settings", str(exc))
            return

        ok = 0
        failures: List[str] = []
        tasks = [
            (
                str(wav_path),
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

        if workers == 1 or len(tasks) == 1:
            for idx, task in enumerate(tasks, start=1):
                wav_path = pathlib.Path(task[0])
                self.set_status(f"Processing {idx}/{len(tasks)}: {wav_path.name}")
                try:
                    process_wav_file_task(task)
                    ok += 1
                except Exception as exc:
                    failures.append(f"{wav_path.name}: {exc}")
        else:
            self.set_status(f"Processing in parallel with {workers} workers...")
            with ProcessPoolExecutor(max_workers=workers) as pool:
                future_map = {pool.submit(process_wav_file_task, task): pathlib.Path(task[0]).name for task in tasks}
                done = 0
                for fut in as_completed(future_map):
                    done += 1
                    name = future_map[fut]
                    self.set_status(f"Completed {done}/{len(tasks)}: {name}")
                    try:
                        fut.result()
                        ok += 1
                    except Exception as exc:
                        failures.append(f"{name}: {exc}")

        summary = f"Processed {ok}/{len(self.selected_files)} WAV file(s)."
        self.set_status(summary)
        if failures:
            messagebox.showwarning("Completed with errors", summary + "\n\n" + "\n".join(failures))
        else:
            messagebox.showinfo("Processing complete", summary)


def build_root() -> tk.Tk:
    if HAS_DND:
        return TkinterDnD.Tk()
    return tk.Tk()


def main() -> None:
    root = build_root()
    WavFilterApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
