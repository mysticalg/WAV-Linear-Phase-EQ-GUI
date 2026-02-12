#!/usr/bin/env python3
"""WAV batch filter GUI with drag-and-drop support.

Features:
- Drag-and-drop files/folders (when tkinterdnd2 is installed)
- File/folder picker fallback
- Batch processing of WAV files
- Applies a high-pass and low-pass FFT filter in sequence
- Saves output as new WAV files with filter settings in filename
"""

from __future__ import annotations

import pathlib
import tkinter as tk
from tkinter import filedialog, messagebox
from typing import List, Sequence, Tuple
import wave

import numpy as np


try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    HAS_DND = True
except ImportError:
    DND_FILES = None
    TkinterDnD = None
    HAS_DND = False


DEFAULT_LOW_PASS_HZ = 13_000.0
DEFAULT_HIGH_PASS_HZ = 120.0


class WavReadError(Exception):
    """Raised when a WAV file cannot be decoded in supported PCM formats."""


def parse_drop_payload(payload: str) -> List[pathlib.Path]:
    """Parse Tk DnD payload with brace-escaped paths."""
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
    # dedupe while preserving order
    unique: List[pathlib.Path] = []
    seen = set()
    for f in found:
        if f not in seen:
            unique.append(f)
            seen.add(f)
    return unique


def _bytes_to_samples(frames: bytes, sample_width: int, channels: int) -> np.ndarray:
    if sample_width == 1:
        arr = np.frombuffer(frames, dtype=np.uint8).astype(np.int16) - 128
        return arr.reshape(-1, channels).astype(np.float64)
    if sample_width == 2:
        arr = np.frombuffer(frames, dtype=np.int16)
        return arr.reshape(-1, channels).astype(np.float64)
    if sample_width == 3:
        raw = np.frombuffer(frames, dtype=np.uint8)
        if len(raw) % 3 != 0:
            raise WavReadError("Corrupted 24-bit WAV data.")
        triples = raw.reshape(-1, 3)
        vals = (
            triples[:, 0].astype(np.int32)
            | (triples[:, 1].astype(np.int32) << 8)
            | (triples[:, 2].astype(np.int32) << 16)
        )
        sign = vals & 0x800000
        vals = vals - (sign << 1)
        return vals.reshape(-1, channels).astype(np.float64)
    if sample_width == 4:
        arr = np.frombuffer(frames, dtype=np.int32)
        return arr.reshape(-1, channels).astype(np.float64)
    raise WavReadError(f"Unsupported sample width: {sample_width} bytes")


def _samples_to_bytes(samples: np.ndarray, sample_width: int) -> bytes:
    if sample_width == 1:
        out = np.clip(np.round(samples + 128), 0, 255).astype(np.uint8)
        return out.tobytes()
    if sample_width == 2:
        out = np.clip(np.round(samples), -32768, 32767).astype(np.int16)
        return out.tobytes()
    if sample_width == 3:
        clipped = np.clip(np.round(samples), -8388608, 8388607).astype(np.int32)
        packed = np.empty((clipped.size, 3), dtype=np.uint8)
        packed[:, 0] = clipped & 0xFF
        packed[:, 1] = (clipped >> 8) & 0xFF
        packed[:, 2] = (clipped >> 16) & 0xFF
        return packed.tobytes()
    if sample_width == 4:
        out = np.clip(np.round(samples), -2147483648, 2147483647).astype(np.int32)
        return out.tobytes()
    raise WavReadError(f"Unsupported sample width: {sample_width} bytes")


def apply_fft_band_filter(samples: np.ndarray, sample_rate: int, high_pass_hz: float, low_pass_hz: float) -> np.ndarray:
    """Apply high-pass then low-pass in frequency domain."""
    if high_pass_hz < 0 or low_pass_hz <= 0:
        raise ValueError("Filter frequencies must be positive and valid.")

    if high_pass_hz >= low_pass_hz:
        raise ValueError("High-pass cutoff must be less than low-pass cutoff.")

    frame_count = samples.shape[0]
    spectrum = np.fft.rfft(samples, axis=0)
    freqs = np.fft.rfftfreq(frame_count, d=1.0 / sample_rate)

    mask = (freqs >= high_pass_hz) & (freqs <= low_pass_hz)
    spectrum[~mask, :] = 0

    filtered = np.fft.irfft(spectrum, n=frame_count, axis=0)
    return filtered


def process_wav_file(path: pathlib.Path, high_pass_hz: float, low_pass_hz: float) -> pathlib.Path:
    with wave.open(str(path), "rb") as wf:
        nchannels = wf.getnchannels()
        sample_width = wf.getsampwidth()
        sample_rate = wf.getframerate()
        nframes = wf.getnframes()
        comptype = wf.getcomptype()
        if comptype != "NONE":
            raise WavReadError("Compressed WAV is not supported.")
        frames = wf.readframes(nframes)

    samples = _bytes_to_samples(frames, sample_width, nchannels)
    filtered = apply_fft_band_filter(samples, sample_rate, high_pass_hz, low_pass_hz)

    hp_label = int(round(high_pass_hz))
    lp_label = int(round(low_pass_hz))
    output_path = path.with_name(f"{path.stem}_hp{hp_label}_lp{lp_label}{path.suffix}")

    with wave.open(str(output_path), "wb") as wf:
        wf.setnchannels(nchannels)
        wf.setsampwidth(sample_width)
        wf.setframerate(sample_rate)
        wf.writeframes(_samples_to_bytes(filtered, sample_width))

    return output_path


class WavFilterApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("WAV HP/LP Batch Filter")
        self.root.geometry("720x500")

        self.selected_files: List[pathlib.Path] = []

        self.high_pass_var = tk.StringVar(value=str(int(DEFAULT_HIGH_PASS_HZ)))
        self.low_pass_var = tk.StringVar(value=str(int(DEFAULT_LOW_PASS_HZ)))

        self._build_ui()

    def _build_ui(self) -> None:
        top = tk.Frame(self.root)
        top.pack(fill="x", padx=12, pady=8)

        tk.Label(top, text="High-pass (Hz):").grid(row=0, column=0, sticky="w")
        tk.Entry(top, textvariable=self.high_pass_var, width=12).grid(row=0, column=1, padx=6)

        tk.Label(top, text="Low-pass (Hz):").grid(row=0, column=2, sticky="w", padx=(18, 0))
        tk.Entry(top, textvariable=self.low_pass_var, width=12).grid(row=0, column=3, padx=6)

        tk.Button(top, text="Add WAV Files", command=self.pick_files).grid(row=0, column=4, padx=(18, 6))
        tk.Button(top, text="Add Folder", command=self.pick_folder).grid(row=0, column=5, padx=6)
        tk.Button(top, text="Clear", command=self.clear_files).grid(row=0, column=6, padx=6)

        dnd_message = "Drag and drop WAV files/folders below" if HAS_DND else "Install tkinterdnd2 for drag-and-drop support"
        self.drop_label = tk.Label(self.root, text=dnd_message, relief="groove", padx=8, pady=10)
        self.drop_label.pack(fill="x", padx=12, pady=8)

        self.file_list = tk.Listbox(self.root, selectmode="extended")
        self.file_list.pack(fill="both", expand=True, padx=12, pady=(0, 8))

        tk.Button(self.root, text="Process Selected WAV(s)", command=self.process_files, height=2).pack(
            fill="x", padx=12, pady=(0, 12)
        )

        self.status = tk.Label(self.root, text="Ready", anchor="w")
        self.status.pack(fill="x", padx=12, pady=(0, 10))

        if HAS_DND:
            self.drop_label.drop_target_register(DND_FILES)
            self.drop_label.dnd_bind("<<Drop>>", self.on_drop)

    def set_status(self, text: str) -> None:
        self.status.config(text=text)
        self.root.update_idletasks()

    def add_paths(self, paths: Sequence[pathlib.Path]) -> None:
        new_files = list_wav_files(paths)
        added = 0
        known = set(self.selected_files)
        for f in new_files:
            if f not in known:
                self.selected_files.append(f)
                self.file_list.insert(tk.END, str(f))
                known.add(f)
                added += 1
        self.set_status(f"Added {added} WAV file(s). Total: {len(self.selected_files)}")

    def pick_files(self) -> None:
        files = filedialog.askopenfilenames(filetypes=[("WAV files", "*.wav")])
        self.add_paths([pathlib.Path(f) for f in files])

    def pick_folder(self) -> None:
        folder = filedialog.askdirectory()
        if folder:
            self.add_paths([pathlib.Path(folder)])

    def clear_files(self) -> None:
        self.selected_files.clear()
        self.file_list.delete(0, tk.END)
        self.set_status("Cleared file list.")

    def on_drop(self, event: tk.Event) -> None:
        dropped = parse_drop_payload(event.data)
        self.add_paths(dropped)

    def _get_filter_settings(self) -> Tuple[float, float]:
        try:
            high_pass = float(self.high_pass_var.get().strip())
            low_pass = float(self.low_pass_var.get().strip())
        except ValueError as exc:
            raise ValueError("Filter values must be numeric.") from exc

        if high_pass < 0:
            raise ValueError("High-pass must be >= 0 Hz.")
        if low_pass <= 0:
            raise ValueError("Low-pass must be > 0 Hz.")
        if high_pass >= low_pass:
            raise ValueError("High-pass must be less than low-pass.")

        return high_pass, low_pass

    def process_files(self) -> None:
        if not self.selected_files:
            messagebox.showinfo("No files", "Please add at least one WAV file first.")
            return

        try:
            high_pass, low_pass = self._get_filter_settings()
        except ValueError as exc:
            messagebox.showerror("Invalid filters", str(exc))
            return

        successes = 0
        failures: List[str] = []

        for idx, wav_path in enumerate(self.selected_files, start=1):
            self.set_status(f"Processing {idx}/{len(self.selected_files)}: {wav_path.name}")
            try:
                process_wav_file(wav_path, high_pass, low_pass)
                successes += 1
            except Exception as exc:  # keep batch processing alive
                failures.append(f"{wav_path.name}: {exc}")

        summary = f"Processed {successes}/{len(self.selected_files)} WAV file(s)."
        if failures:
            summary += f" Failed: {len(failures)}"
            self.set_status(summary)
            messagebox.showwarning("Processing complete with errors", summary + "\n\n" + "\n".join(failures))
        else:
            self.set_status(summary)
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
