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

## Notes

- Supports PCM WAV sample widths: 8, 16, 24, and 32 bit.
- Compressed WAV formats are not supported.
