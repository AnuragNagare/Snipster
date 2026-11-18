# Registers the "Snipster Worker" task: at every log on, the launcher starts the worker and scheduler
# hidden in the background. Runs for the current user only, no password is stored, no admin rights needed.
# Remove with uninstall-autostart.ps1.
param([switch]$NoStartNow)

. (Join-Path $PSScriptRoot 'snipster-common.ps1')
Initialize-LogDir

if (-not (Test-Path $VenvPython)) { throw "Python not found at $VenvPython. Create the venv first (see requirements-worker.txt)." }
if (-not (Test-Path (Join-Path $Backend '.env'))) { throw "No .env in $Backend. Copy .env.example to .env and fill it in first." }

# Stop anything already running (a hand-started worker would run next to the new one).
& (Join-Path $PSScriptRoot 'stop-snipster.ps1')

$launcher = Join-Path $PSScriptRoot 'start-snipster.ps1'
$user = "$env:USERDOMAIN\$env:USERNAME"
$argument = '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "{0}"' -f $launcher

$action    = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $argument -WorkingDirectory $Backend
$trigger   = New-ScheduledTaskTrigger -AtLogOn -User $user
$settings  = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
                 -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew `
                 -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal `
    -Description 'Starts the Snipster worker and scheduler in the background at log on.' -Force | Out-Null
Write-Log "Task '$TaskName' installed for $user (runs at log on)"

if (-not $NoStartNow) {
    Start-ScheduledTask -TaskName $TaskName
    Write-Log 'Task started now'
}
Write-Host ''
Write-Host 'Done. Check with: powershell -File scripts\status-snipster.ps1'
Write-Host 'Remove with:      powershell -File scripts\uninstall-autostart.ps1'
