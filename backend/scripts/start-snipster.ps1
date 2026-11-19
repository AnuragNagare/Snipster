# Starts the Snipster worker and scheduler in the background and keeps them running.
#
#   - Only one launcher can run at a time (named mutex).
#   - Waits until the database and Redis hosts resolve (DNS can lag right after login or wake).
#   - Restarts the worker or scheduler when one exits, waiting longer after repeated quick crashes.
#   - Logs go to %LOCALAPPDATA%\Snipster\logs. Secrets are never written there.
#
# Normally started by the "Snipster Worker" scheduled task (see install-autostart.ps1).
# Test switches: -NoStart (do everything except start the processes), -WaitHosts, -MaxWaitSeconds.
param(
    [switch]$NoStart,
    [string[]]$WaitHosts,
    [int]$MaxWaitSeconds = 900
)

. (Join-Path $PSScriptRoot 'snipster-common.ps1')
Initialize-LogDir

$mutex = New-Object System.Threading.Mutex($false, 'Local\SnipsterLauncher')
if (-not $mutex.WaitOne(0)) {
    Write-Log 'Another launcher is already running. Exiting.'
    exit 0
}

function Get-EnvValue([string]$Name) {
    $envFile = Join-Path $Backend '.env'
    if (-not (Test-Path $envFile)) { return $null }
    $prefix = "$Name="
    $line = Get-Content $envFile | Where-Object { $_.StartsWith($prefix) } | Select-Object -First 1
    if ($line) { return $line.Substring($prefix.Length).Trim() }
    return $null
}

function Get-HostFromUrl([string]$Url) {
    if ($Url -and $Url -match '@([^:/?#]+)') { return $Matches[1] }
    return $null
}

$kids = @(
    @{ Name = 'worker'; Args = '-m celery -A app.workers.celery_app worker --loglevel=info -P solo';
       Proc = $null; Fails = 0; Next = [datetime]::MinValue; Started = $null },
    @{ Name = 'beat'; Args = '-m celery -A app.workers.celery_app beat --loglevel=info';
       Proc = $null; Fails = 0; Next = [datetime]::MinValue; Started = $null }
)
$delays = @(0, 1, 5, 30, 60)

function Save-Children {
    $info = [ordered]@{ launcher = $PID; updated = (Get-Date -Format 's') }
    foreach ($k in $kids) {
        $info[$k.Name] = $(if ($k.Proc -and -not $k.Proc.HasExited) { $k.Proc.Id } else { $null })
    }
    $info | ConvertTo-Json | Set-Content -Path $ChildrenFile
}

function Start-Kid($k) {
    if ($k.Name -eq 'beat') {
        # a stale schedule file can stop the scheduler from starting
        Get-ChildItem -Path $Backend -Filter 'celerybeat-schedule*' -ErrorAction SilentlyContinue | Remove-Item -Force -ErrorAction SilentlyContinue
    }
    $out = Join-Path $LogDir ($k.Name + '.log')
    $err = Join-Path $LogDir ($k.Name + '.err.log')
    Rotate-Log $out
    Rotate-Log $err
    $k.Proc = Start-Process -FilePath $VenvPython -ArgumentList $k.Args -WorkingDirectory $Backend `
        -WindowStyle Hidden -RedirectStandardOutput $out -RedirectStandardError $err -PassThru
    $null = $k.Proc.Handle   # keeps the handle so ExitCode can be read later
    $k.Started = Get-Date
    Write-Log ("started {0} (pid {1}, attempt after {2} quick failure(s))" -f $k.Name, $k.Proc.Id, $k.Fails)
}

try {
    Set-Content -Path $PidFile -Value $PID
    Remove-Item -Path $StopFlag -Force -ErrorAction SilentlyContinue
    Write-Log "Launcher started (pid $PID)"

    if (-not (Test-Path $VenvPython)) {
        Write-Log "Python not found at $VenvPython. Run: python -m venv .venv and pip install -r requirements-worker.txt in the backend folder."
        exit 1
    }

    # Leftovers from an earlier run or a hand-started worker would run next to ours.
    foreach ($p in @(Get-SnipsterProcesses)) {
        Write-Log "stopping leftover process $($p.ProcessId)"
        Stop-ProcessTree $p.ProcessId
    }

    # Wait for the network: the database and Redis names must resolve first.
    # Each entry: the host, and whether public DNS (8.8.8.8) may stand in for the network's own DNS.
    # The database connection can use that fallback (app/db/sync_engine.py); Redis cannot.
    $targets = @()
    if (@($WaitHosts | Where-Object { $_ }).Count -gt 0) {   # @($null) would count as one blank entry
        foreach ($h in $WaitHosts) { if ($h) { $targets += @{ Host = $h; Fallback = $false } } }
    } else {
        $db = Get-HostFromUrl (Get-EnvValue 'DATABASE_URL')
        $redis = Get-HostFromUrl (Get-EnvValue 'REDIS_URL')
        if ($db) { $targets += @{ Host = $db; Fallback = $true } }
        if ($redis) { $targets += @{ Host = $redis; Fallback = $false } }
    }
    $deadline = (Get-Date).AddSeconds($MaxWaitSeconds)
    foreach ($t in $targets) {
        $tries = 0
        while ($true) {
            $checkArgs = @((Join-Path $PSScriptRoot 'check_host.py'), $t.Host)
            if ($t.Fallback) { $checkArgs += '--allow-fallback' }
            # Capture the whole output: "| Select-Object -First 1" would cut the process short and
            # leave $LASTEXITCODE at -1 even when the check succeeded.
            $result = @(& $VenvPython @checkArgs 2>$null)
            $code = $LASTEXITCODE
            if ($code -eq 0) {
                Write-Log "network ready: $($t.Host) (via $($result[0]))"
                break
            }
            $tries++
            if ((Get-Date) -gt $deadline) { Write-Log "gave up waiting for $($t.Host), starting anyway"; break }
            if ($tries -eq 1 -or $tries % 6 -eq 0) { Write-Log "waiting for the network ($($t.Host) not resolving yet, try $tries)" }
            # Sleep in short steps so a stop request is noticed quickly.
            for ($i = 0; $i -lt 10 -and -not (Test-Path $StopFlag); $i++) { Start-Sleep -Seconds 1 }
            if (Test-Path $StopFlag) { Write-Log 'Stop requested while waiting for the network.'; exit 0 }
        }
    }

    if ($NoStart) { Write-Log 'NoStart: not starting the worker or the scheduler.'; exit 0 }

    $env:PYTHONIOENCODING = 'utf-8'
    $env:PYTHONUNBUFFERED = '1'

    $lastBrokerCheck = Get-Date
    while (-not (Test-Path $StopFlag)) {
        # A worker that lost its queue connection stays alive but takes no jobs: restart it.
        if (((Get-Date) - $lastBrokerCheck).TotalSeconds -ge 30) {
            $lastBrokerCheck = Get-Date
            $w = $kids | Where-Object { $_.Name -eq 'worker' }
            $workerLog = Join-Path $LogDir 'worker.err.log'
            if ($w.Proc -and -not $w.Proc.HasExited -and (Test-Path $workerLog)) {
                $tail = @(Get-Content -Path $workerLog -Tail 400 -ErrorAction SilentlyContinue)
                if (Test-BrokerStuck $tail (Get-Date) 10) {
                    Write-Log 'worker lost its connection to the job queue more than 10 minutes ago and did not reconnect: restarting it'
                    Stop-ProcessTree $w.Proc.Id
                }
            }
        }
        foreach ($k in $kids) {
            if ($k.Proc -and $k.Proc.HasExited) {
                $up = (Get-Date) - $k.Started
                Write-Log ("{0} exited with code {1} after {2}s" -f $k.Name, $k.Proc.ExitCode, [int]$up.TotalSeconds)
                if ($up.TotalMinutes -ge 5) { $k.Fails = 0 } else { $k.Fails++ }
                $wait = $delays[[Math]::Min($k.Fails, $delays.Count - 1)]
                $k.Next = (Get-Date).AddSeconds($wait)
                $k.Proc = $null
            }
            if (-not $k.Proc -and (Get-Date) -ge $k.Next) { Start-Kid $k }
        }
        Save-Children
        Start-Sleep -Seconds 5
    }
    Write-Log 'Stop requested.'
}
finally {
    foreach ($k in $kids) {
        if ($k.Proc -and -not $k.Proc.HasExited) {
            Write-Log "stopping $($k.Name) (pid $($k.Proc.Id))"
            Stop-ProcessTree $k.Proc.Id
        }
    }
    Remove-Item -Path $PidFile, $ChildrenFile, $StopFlag -Force -ErrorAction SilentlyContinue
    Write-Log 'Launcher stopped.'
    try { $mutex.ReleaseMutex() } catch { }
}
