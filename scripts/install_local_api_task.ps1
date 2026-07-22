$ErrorActionPreference = "Stop"

$taskName = "Cloud Strategy Platform - Local API"
$runner = Join-Path $PSScriptRoot "run_local_api.ps1"
$action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$runner`""
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew `
    -RestartCount 10 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero)

try {
    Register-ScheduledTask `
        -TaskName $taskName `
        -Action $action `
        -Trigger $trigger `
        -Settings $settings `
        -Description "Owns cloud-scoped Alpaca credentials and serves the local keyless API." `
        -Force `
        -ErrorAction Stop | Out-Null
    Start-ScheduledTask -TaskName $taskName
    Write-Output "Installed and started scheduled task: $taskName"
}
catch {
    $startup = [Environment]::GetFolderPath("Startup")
    $shortcutPath = Join-Path $startup "$taskName.lnk"
    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($shortcutPath)
    $shortcut.TargetPath = "powershell.exe"
    $shortcut.Arguments = `
        "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$runner`""
    $shortcut.WorkingDirectory = Split-Path -Parent $PSScriptRoot
    $shortcut.WindowStyle = 7
    $shortcut.Save()
    Start-Process `
        -FilePath "powershell.exe" `
        -ArgumentList $shortcut.Arguments `
        -WorkingDirectory $shortcut.WorkingDirectory `
        -WindowStyle Hidden
    Write-Output "Installed Startup shortcut and started local API: $shortcutPath"
}
