# Stops the Snipster launcher, worker and scheduler. Only this project's processes are touched.
. (Join-Path $PSScriptRoot 'snipster-common.ps1')
Initialize-LogDir

$launcherPid = $null
if (Test-Path $PidFile) {
    $raw = (Get-Content $PidFile -ErrorAction SilentlyContinue | Select-Object -First 1)
    if ($raw -match '^\d+$') { $launcherPid = [int]$raw }
}

if ($launcherPid -and (Test-ProcessAlive $launcherPid)) {
    Write-Log "Asking the launcher (pid $launcherPid) to stop"
    New-Item -ItemType File -Force -Path $StopFlag | Out-Null
    $limit = (Get-Date).AddSeconds(30)
    while ((Test-ProcessAlive $launcherPid) -and (Get-Date) -lt $limit) { Start-Sleep -Seconds 1 }
    if (Test-ProcessAlive $launcherPid) {
        Write-Log 'Launcher did not stop in 30 s, ending it'
        Stop-ProcessTree $launcherPid
    }
} else {
    Write-Log 'No launcher is running'
}

# Anything of this project still running (a hand-started worker, or an orphan).
foreach ($p in @(Get-SnipsterProcesses)) {
    Write-Log "ending leftover process $($p.ProcessId)"
    Stop-ProcessTree $p.ProcessId
}

Remove-Item -Path $PidFile, $ChildrenFile, $StopFlag -Force -ErrorAction SilentlyContinue
Write-Log 'Stopped.'
