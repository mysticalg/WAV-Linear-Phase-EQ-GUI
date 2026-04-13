param(
  [string]$Version = "0.0.0-local",
  [string]$Name = "WAVLinearPhaseEQ"
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$isccCandidates = @(
  (Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe"),
  (Join-Path $env:ProgramFiles "Inno Setup 6\ISCC.exe")
)

$iscc = $isccCandidates | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
if (-not $iscc) {
  throw "Inno Setup 6 was not found. Install it first or run this inside the GitHub Actions Windows build job."
}

& $iscc "/DMyAppVersion=$Version" "/DMyAppExeName=$Name.exe" "installer.iss"

Write-Host ""
Write-Host "Built installer:" (Join-Path $root "dist\WAVLinearPhaseEQ-Setup.exe")
