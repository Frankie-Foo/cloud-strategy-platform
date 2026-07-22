$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"
$runs = Join-Path $root "runs"
$stdout = Join-Path $runs "sip-owner.stdout.log"
$stderr = Join-Path $runs "sip-owner.stderr.log"

if (-not (Test-Path -LiteralPath $python)) {
    throw "Cloud platform virtual environment is missing: $python"
}

New-Item -ItemType Directory -Path $runs -Force | Out-Null
Set-Location -LiteralPath $root
while ($true) {
    & $python -m scripts.run_sip_owner 1>> $stdout 2>> $stderr
    $record = @{
        ts_utc = [DateTime]::UtcNow.ToString("o")
        event = "sip_owner_restarting"
        return_code = $LASTEXITCODE
    } | ConvertTo-Json -Compress
    Add-Content -LiteralPath $stdout -Value $record -Encoding utf8
    Start-Sleep -Seconds 5
}
