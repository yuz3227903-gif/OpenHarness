$ErrorActionPreference = "Stop"

$codexNodeBin = Join-Path $env:USERPROFILE ".cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin"
$openHarnessExe = Join-Path $PSScriptRoot ".venv\Scripts\openh.exe"

if (-not (Test-Path -LiteralPath $codexNodeBin)) {
    throw "Bundled Node.js was not found at: $codexNodeBin"
}

if (-not (Test-Path -LiteralPath $openHarnessExe)) {
    throw "OpenHarness virtual environment was not found at: $openHarnessExe"
}

$env:PATH = "$codexNodeBin;$env:PATH"
$env:PYTHONUTF8 = "1"
[Console]::InputEncoding = [System.Text.UTF8Encoding]::new()
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()

& $openHarnessExe @args
exit $LASTEXITCODE
