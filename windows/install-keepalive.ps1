# Run ONCE in an *Administrator* PowerShell:
#   powershell -ExecutionPolicy Bypass -File \\wsl.localhost\Ubuntu\home\hamza\AldiTalk\windows\install-keepalive.ps1
# What it does:
#   1. copies wsl-keepalive.vbs to %USERPROFILE%\AldiTalk
#   2. registers a "run at log on" task that starts it (keeps WSL alive, hidden)
#   3. disables sleep/hibernate while plugged in (AC power)
$ErrorActionPreference = "Stop"
$dir = Join-Path $env:USERPROFILE "AldiTalk"
New-Item -ItemType Directory -Force -Path $dir | Out-Null
Copy-Item -Force (Join-Path $PSScriptRoot "wsl-keepalive.vbs") $dir

$action   = New-ScheduledTaskAction -Execute "wscript.exe" -Argument "`"$dir\wsl-keepalive.vbs`""
$trigger  = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
            -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable
Register-ScheduledTask -TaskName "AldiTalk WSL keep-alive" -Action $action -Trigger $trigger `
                       -Settings $settings -Force | Out-Null
Start-ScheduledTask -TaskName "AldiTalk WSL keep-alive"

powercfg /change standby-timeout-ac 0
powercfg /change hibernate-timeout-ac 0

Write-Host "Done. WSL now stays up after log on, and the PC won't sleep on AC power."
Write-Host "Laptop? Also set Control Panel > Power Options > 'When I close the lid' (plugged in) = Do nothing."
Write-Host "Undo: Unregister-ScheduledTask -TaskName 'AldiTalk WSL keep-alive'"
