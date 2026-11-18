# Shows whether the Snipster launcher, worker and scheduler are running, and the latest log lines.
. (Join-Path $PSScriptRoot 'snipster-common.ps1')

function Show-Proc([string]$Label, $ProcId) {
    if ($ProcId -and (Test-ProcessAlive ([int]$ProcId))) {
        $p = Get-Process -Id ([int]$ProcId)
        Write-Host ("  {0,-9} running  pid {1}  since {2}" -f $Label, $ProcId, $p.StartTime.ToString('yyyy-MM-dd HH:mm:ss'))
    } else {
        Write-Host ("  {0,-9} NOT running" -f $Label)
    }
}

Write-Host 'Snipster status'
$info = $null
if (Test-Path $ChildrenFile) { $info = Get-Content $ChildrenFile -Raw | ConvertFrom-Json }
$launcherPid = $null
if (Test-Path $PidFile) { $launcherPid = (Get-Content $PidFile | Select-Object -First 1) }
Show-Proc 'launcher' $launcherPid
Show-Proc 'worker' $(if ($info) { $info.worker } else { $null })
Show-Proc 'scheduler' $(if ($info) { $info.beat } else { $null })

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($task) {
    $ti = Get-ScheduledTaskInfo -TaskName $TaskName
    Write-Host ("  autostart task: installed, state {0}, last run {1}, last result {2}" -f $task.State, $ti.LastRunTime, $ti.LastTaskResult)
} else {
    Write-Host '  autostart task: NOT installed (run install-autostart.ps1)'
}

# How long ago the scheduler last ticked, as recorded in the database (read only).
Push-Location $Backend
try { $out = & $VenvPython (Join-Path $PSScriptRoot 'last_tick.py') 2>&1; Write-Host ($out | Select-Object -Last 1) }
catch { Write-Host '  last scheduler tick: could not run the check' }
Pop-Location

foreach ($name in 'launcher', 'worker', 'beat') {
    $f = Join-Path $LogDir ($name + '.log')
    if ($name -ne 'launcher') {
        $f2 = Join-Path $LogDir ($name + '.err.log')
        if ((Test-Path $f2) -and (Get-Item $f2).Length -gt 0) { $f = $f2 }
    }
    if (Test-Path $f) {
        Write-Host ""
        Write-Host ("--- last lines of {0}" -f (Split-Path $f -Leaf))
        Get-Content $f -Tail 4 | ForEach-Object { if ($_.Length -gt 160) { $_.Substring(0, 160) } else { $_ } }
    }
}
Write-Host ""
Write-Host "Logs folder: $LogDir"
