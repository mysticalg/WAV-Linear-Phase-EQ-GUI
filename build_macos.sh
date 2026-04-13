#!/usr/bin/env bash
set -euo pipefail

NAME="${1:-WAVLinearPhaseEQ}"
ROOT="$(cd "$(dirname "$0")" && pwd)"

cd "$ROOT"

python3 -m pip install -r requirements-build.txt

python3 -m PyInstaller \
  --noconfirm \
  --clean \
  --windowed \
  --name "$NAME" \
  --osx-bundle-identifier "com.mysticalg.wavlinearphaseeq" \
  --add-data "license_public_key.pem:." \
  wav_filter_gui.py

echo
echo "Built macOS app bundle: $ROOT/dist/$NAME.app"
