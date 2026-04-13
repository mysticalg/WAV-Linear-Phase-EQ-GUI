# WAV Linear-Phase EQ GUI

Desktop GUI for batch processing `.wav` files with defaults:

- High-pass: `120 Hz`
- Low-pass: `13000 Hz`

## Features

- Drag/drop WAV files or folders (with optional `tkinterdnd2`)
- Linear-phase FIR EQ (high-pass + low-pass)
- Built-in premium unlock flow for paid advanced FX
- Pitch retuning tab with semitone, cent, millicent, or reference-A frequency modes
- Historical pitch presets for Bach/Baroque `415 Hz`, Modern `440 Hz`, and Beethoven/Classical `455 Hz`
- Tape-emulated saturation with oversampled soft clipping and wet/dry control
- Compressor with single-band or multiband mode, attack/release, makeup gain, and optional valve warmth
- Final lookahead limiter for export safety
- Optional texture layer:
  - pink noise
  - room tone
  - vinyl crackle
- Texture fade-in/out with a single amount control
- Preview button to render the selected file with current settings and listen before export
- Built-in waveform, spectrum, and spectrogram views for the processed preview
- Peak output meter showing rendered preview level in dBFS
- Mild dynamic "humanize" variation (small gain movement over sections)
- Random per-channel stem offsets (0-100 ms or custom), with a randomizer button
- Output naming with active options appended to file name
- Configurable output folder for batch exports
- Existing WAV metadata is stripped on export, with an optional `Produced by <username>` metadata comment
- App settings are saved on close and restored automatically on the next launch
- Batch processing runs in the background so the window stays responsive during long exports
- A Cancel button lets you stop the current batch before the remaining files are processed
- NumPy-backed processing path for much faster filtering on long files
- Performance controls: configurable FIR tap count (quality vs speed) and worker count for parallel batch processing

## Requirements

- Python 3.10+
- Optional: `tkinterdnd2` for drag-and-drop
- `pygame-ce` is included for preview playback across Windows/macOS/Linux

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

## Build Windows Installer

```powershell
.\build_installer.ps1 -Version 0.0.0-local
```

This produces:

```text
dist\WAVLinearPhaseEQ-Setup.exe
```

## GitHub Windows Builds

The repository now includes a GitHub Actions workflow at `.github/workflows/windows-build.yml`.

- Pushes to `main` build a fresh Windows EXE and installer on GitHub and upload them as workflow artifacts.
- Tags like `v1.0.0` build the same outputs and publish them to a GitHub Release.
- The old committed `dist/WAVLinearPhaseEQ.exe` should no longer be used as a distribution channel.

## Build macOS App

Run this on a Mac:

```bash
chmod +x ./build_macos.sh
./build_macos.sh
```

This produces:

```text
dist/WAVLinearPhaseEQ.app
```

Notes:

- macOS builds must be created on macOS. They cannot be cross-built from this Windows setup.
- For distribution outside your own machine, you will still need Apple signing and notarization.
- The app is now macOS-aware for settings storage and user defaults, but the actual `.app` bundle still needs to be tested on a Mac host.

## Notes

- Supports PCM WAV sample widths: 8, 16, 24, and 32 bit.
- Compressed WAV formats are not supported.

## Selling It

The app now supports a one-time premium unlock at `£29.99`.

- Free mode: base linear-phase EQ, export controls, preview, limiter, and analysis
- Premium unlock: Texture, Pitch, Tape, Compressor, Stereo, Humanize, and Offsets

The desktop app never needs your Stripe secret key. Payment and unlock-code generation run on your server, and the app only verifies signed unlock codes locally.

## Premium Unlock Setup

Generate a signing keypair:

```bash
python license_manager.py generate-keypair --private-out secrets/license_private_key.pem --public-out license_public_key.pem
```

Ship `license_public_key.pem` beside the EXE or set `WAV_EQ_LICENSE_PUBLIC_KEY_PEM` in the runtime environment.

Set the desktop app checkout endpoint:

```powershell
$env:WAV_EQ_LICENSE_SERVER_URL="https://your-aws-hostname.example.com"
python wav_filter_gui.py
```

The current live unlock host is [wavequnlock.promptshieldapp.co.uk](https://wavequnlock.promptshieldapp.co.uk).
The desktop app now defaults to that URL, and `WAV_EQ_LICENSE_SERVER_URL` only needs to be set if you want to override it.

## Stripe Seller Service

Install seller dependencies:

```bash
pip install -r requirements-seller.txt
```

Run the Stripe fulfillment service on AWS:

```powershell
$env:STRIPE_SECRET_KEY="sk_live_..."
$env:WAV_EQ_SELLER_BASE_URL="https://your-aws-hostname.example.com"
$env:WAV_EQ_LICENSE_PRIVATE_KEY_FILE="C:\path\to\license_private_key.pem"
python stripe_unlock_server.py
```

Recommended Stripe webhook:

- Endpoint: `https://your-aws-hostname.example.com/stripe/webhook`
- Event: `checkout.session.completed`

The server creates a Stripe Checkout Session for `£29.99`, binds the purchase to the app installation ID, and shows the signed unlock code on the success page after payment.
