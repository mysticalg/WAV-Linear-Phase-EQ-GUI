param(
  [string]$Name = "WAVLinearPhaseEQ"
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

python -m pip install -r requirements-build.txt

python -m PyInstaller `
  --noconfirm `
  --clean `
  --onefile `
  --windowed `
  --name $Name `
  wav_filter_gui.py

Write-Host ""
Write-Host "Built executable:" (Join-Path $root "dist\\$Name.exe")
