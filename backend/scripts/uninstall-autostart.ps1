# Removes the "Snipster Worker" scheduled task and stops the worker and scheduler.
. (Join-Path $PSScriptRoot 'snipster-common.ps1')
Initialize-LogDir

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Log "Task '$TaskName' removed"
} else {
    Write-Log "Task '$TaskName' was not installed"
}
& (Join-Path $PSScriptRoot 'stop-snipster.ps1')
