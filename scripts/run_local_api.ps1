$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"
$runs = Join-Path $root "runs"
$stdout = Join-Path $runs "api.stdout.log"
$stderr = Join-Path $runs "api.stderr.log"

if (-not (Test-Path -LiteralPath $python)) {
    throw "Cloud platform virtual environment is missing: $python"
}

New-Item -ItemType Directory -Path $runs -Force | Out-Null
Set-Location -LiteralPath $root
& $python -m scripts.serve_api 1>> $stdout 2>> $stderr
exit $LASTEXITCODE
