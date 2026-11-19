# Shared by the Snipster launcher scripts. Dot-source it: . (Join-Path $PSScriptRoot 'snipster-common.ps1')
# ASCII only on purpose: Windows PowerShell 5.1 reads BOM-less files as ANSI.

$Backend      = Split-Path -Parent $PSScriptRoot
$ProjectRoot  = Split-Path -Parent $Backend
$VenvPython   = Join-Path $Backend '.venv\Scripts\python.exe'
$LogDir       = Join-Path $env:LOCALAPPDATA 'Snipster\logs'
$LauncherLog  = Join-Path $LogDir 'launcher.log'
$PidFile      = Join-Path $LogDir 'launcher.pid'
$StopFlag     = Join-Path $LogDir 'stop.flag'
$ChildrenFile = Join-Path $LogDir 'children.json'
$TaskName     = 'Snipster Worker'

function Initialize-LogDir {
    if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Force -Path $LogDir | Out-Null }
}

# Never pass URLs, passwords or keys to this: it writes to a plain text file.
function Write-Log([string]$Message) {
    Initialize-LogDir
    $line = '{0} {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Message
    Add-Content -Path $LauncherLog -Value $line
    Write-Host $line
}

# Keep the last $Keep runs of a log: log -> log.1 -> log.2 -> log.3
function Rotate-Log([string]$Path, [int]$Keep = 3) {
    if (-not (Test-Path $Path)) { return }
    for ($i = $Keep - 1; $i -ge 1; $i--) {
        if (Test-Path "$Path.$i") { Move-Item -Force -Path "$Path.$i" -Destination "$Path.$($i + 1)" }
    }
    Move-Item -Force -Path $Path -Destination "$Path.1"
}

# The worker and scheduler of THIS project (not every Python process on the machine).
function Get-SnipsterProcesses {
    Get-CimInstance Win32_Process |
        Where-Object { $_.Name -match '^python' -and $_.CommandLine -match 'app\.workers\.celery_app' }
}

function Stop-ProcessTree([int]$ProcessId) {
    & taskkill.exe /PID $ProcessId /T /F 2>&1 | Out-Null
}

function Test-ProcessAlive([int]$ProcessId) {
    return [bool](Get-Process -Id $ProcessId -ErrorAction SilentlyContinue)
}

# True when the worker's log says it lost its connection to the job queue and has not reconnected within
# $Minutes. The worker process stays alive in that state but takes no jobs, so the launcher must restart it.
# Celery writes "Connection to broker lost" when it drops and "Connected to ..." when it is back.
function Test-BrokerStuck([string[]]$Lines, [datetime]$Now, [int]$Minutes = 10) {
    $lostIndex = -1
    $lostTime = $null
    for ($i = 0; $i -lt $Lines.Count; $i++) {
        if ($Lines[$i] -match 'Connection to broker lost') {
            $lostIndex = $i
            $lostTime = $null
            if ($Lines[$i] -match '^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})') {
                $lostTime = [datetime]::ParseExact($Matches[1], 'yyyy-MM-dd HH:mm:ss', $null)
            }
        }
    }
    if ($lostIndex -lt 0 -or $null -eq $lostTime) { return $false }
    for ($j = $lostIndex + 1; $j -lt $Lines.Count; $j++) {
        if ($Lines[$j] -match 'Connected to ') { return $false }
    }
    return (($Now - $lostTime).TotalMinutes -ge $Minutes)
}
