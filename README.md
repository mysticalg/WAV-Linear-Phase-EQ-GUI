# WAV HP/LP Filter GUI

Desktop GUI for batch filtering `.wav` files with default:

- High-pass: `120 Hz`
- Low-pass: `13000 Hz`

You can drag-and-drop files/folders (if `tkinterdnd2` is installed), or use file/folder picker buttons.
Processed files are saved next to originals with suffix:

`<original_name>_hp<highpass>_lp<lowpass>.wav`

## Requirements

- Python 3.10+
- `numpy`
- Optional for drag-and-drop support: `tkinterdnd2`

Install dependencies:

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
