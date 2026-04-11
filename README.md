# WAV Linear-Phase EQ GUI

Desktop GUI for batch processing `.wav` files with defaults:

- High-pass: `120 Hz`
- Low-pass: `13000 Hz`

## Features

- Drag/drop WAV files or folders (with optional `tkinterdnd2`)
- Linear-phase FIR EQ (high-pass + low-pass)
- Optional texture layer:
  - pink noise
  - room tone
  - vinyl crackle
- Texture fade-in/out and subtle low-level mixing controls
- Mild dynamic "humanize" variation (small gain movement over sections)
- Random per-channel stem offsets (0-100 ms or custom), with a randomizer button
- Output naming with active options appended to file name
- Configurable output folder for batch exports
- Existing WAV metadata is stripped on export, with an optional `Produced by <username>` metadata comment
- App settings are saved on close and restored automatically on the next launch
- Batch processing runs in the background so the window stays responsive during long exports
- NumPy-backed processing path for much faster filtering on long files
- Performance controls: configurable FIR tap count (quality vs speed) and worker count for parallel batch processing

## Requirements

- Python 3.10+
- Optional: `tkinterdnd2` for drag-and-drop

```bash
pip install -r requirements.txt
```

## Run

```bash
python wav_filter_gui.py
```

Use the `Output and Metadata` section in the GUI to:

- choose a destination folder for the processed batch
- leave the folder blank to export beside the source WAV files
- set the `Produced by` username written into the output WAV metadata

All other input WAV metadata is removed during export.

## Build Windows EXE

```powershell
.\build_exe.ps1
```

This produces:

```text
dist\WAVLinearPhaseEQ.exe
```

## Notes

- Supports PCM WAV sample widths: 8, 16, 24, and 32 bit.
- Compressed WAV formats are not supported.
