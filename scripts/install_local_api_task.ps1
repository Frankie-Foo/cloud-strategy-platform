$ErrorActionPreference = "Stop"

$services = @(
    @{
        Name = "Cloud Strategy Platform - Local API"
        Runner = (Join-Path $PSScriptRoot "run_local_api.ps1")
        Description = "Serves the local keyless cloud strategy API."
        ModulePattern = "-m scripts\.serve_api"
    },
    @{
        Name = "Cloud Strategy Platform - SIP Owner"
        Runner = (Join-Path $PSScriptRoot "run_local_sip_owner.ps1")
        Description = "Owns the single Alpaca SIP connection and persists scoped events."
        ModulePattern = "-m scripts\.run_sip_owner"
    }
)

foreach ($service in $services) {
    $action = New-ScheduledTaskAction `
        -Execute "powershell.exe" `
        -Argument "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$($service.Runner)`""
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
            -TaskName $service.Name `
            -Action $action `
            -Trigger $trigger `
            -Settings $settings `
            -Description $service.Description `
            -Force `
            -ErrorAction Stop | Out-Null
        Start-ScheduledTask -TaskName $service.Name
        Write-Output "Installed and started scheduled task: $($service.Name)"
    }
    catch {
        $startup = [Environment]::GetFolderPath("Startup")
        $shortcutPath = Join-Path $startup "$($service.Name).lnk"
        $shell = New-Object -ComObject WScript.Shell
        $shortcut = $shell.CreateShortcut($shortcutPath)
        $shortcut.TargetPath = "powershell.exe"
        $shortcut.Arguments = `
            "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$($service.Runner)`""
        $shortcut.WorkingDirectory = Split-Path -Parent $PSScriptRoot
        $shortcut.WindowStyle = 7
        $shortcut.Save()
        $running = Get-CimInstance Win32_Process | Where-Object {
            $_.CommandLine -match $service.ModulePattern
        }
        if (-not $running) {
            Start-Process `
                -FilePath "powershell.exe" `
                -ArgumentList $shortcut.Arguments `
                -WorkingDirectory $shortcut.WorkingDirectory `
                -WindowStyle Hidden
            Write-Output "Installed Startup shortcut and started service: $shortcutPath"
        }
        else {
            Write-Output "Installed Startup shortcut; service is already running: $shortcutPath"
        }
    }
}
