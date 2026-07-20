[CmdletBinding()]
param(
    [ValidateSet("start", "stop", "restart", "status")]
    [string]$Action = "restart"
)

$ErrorActionPreference = "Stop"
$HostName = if ($env:WATTRACKER_HOST) { $env:WATTRACKER_HOST.Trim() } else { "127.0.0.1" }
if ($HostName -eq "[::1]") { $HostName = "::1" }
if ($HostName -notin @("127.0.0.1", "localhost", "::1")) {
    throw "WATTRACKER_HOST must be 127.0.0.1, localhost, or ::1."
}
$Port = if ($env:WATTRACKER_PORT) { [int]$env:WATTRACKER_PORT } else { 8000 }
if ($Port -lt 1 -or $Port -gt 65535) { throw "WATTRACKER_PORT must be 1..65535." }
$StartTimeout = if ($env:WATTRACKER_START_TIMEOUT) { [int]$env:WATTRACKER_START_TIMEOUT } else { 20 }
$StopTimeout = if ($env:WATTRACKER_TERM_TIMEOUT) { [int]$env:WATTRACKER_TERM_TIMEOUT } else { 10 }
$Root = Split-Path -Parent $PSScriptRoot
$DataDir = if ($env:WATTRACKER_DATA_DIR) { $env:WATTRACKER_DATA_DIR } else { Join-Path $env:USERPROFILE ".wattracker" }
$StatePath = Join-Path $DataDir "wattracker-process.json"
$LogPath = Join-Path $DataDir "wattracker.log"
$ErrorLogPath = Join-Path $DataDir "wattracker-error.log"
$UrlHost = if ($HostName -eq "::1") { "[::1]" } else { $HostName }
$HealthUrl = "http://${UrlHost}:${Port}/login"

function Read-State {
    if (-not (Test-Path -LiteralPath $StatePath)) { return $null }
    try { return Get-Content -LiteralPath $StatePath -Raw | ConvertFrom-Json }
    catch { throw "Managed process state is unreadable; refusing unsafe process action: $StatePath" }
}

function Get-Identity([int]$ProcessId) {
    $process = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
    if (-not $process) { return $null }
    $cim = Get-CimInstance Win32_Process -Filter "ProcessId = $ProcessId" -ErrorAction SilentlyContinue
    if (-not $cim) { return $null }
    [pscustomobject]@{
        pid = $ProcessId
        start_time_utc = $process.StartTime.ToUniversalTime().ToString("o")
        executable = [System.IO.Path]::GetFullPath([string]$cim.ExecutablePath)
        command_line = [string]$cim.CommandLine
    }
}

function Confirm-Managed($State) {
    if (-not $State -or -not $State.pid -or -not $State.marker -or -not $State.executable -or -not $State.start_time_utc) { return $false }
    if ([int]$State.port -ne $Port) { return $false }
    $identity = Get-Identity ([int]$State.pid)
    if (-not $identity) { return $false }
    return (
        $identity.start_time_utc -eq [string]$State.start_time_utc -and
        $identity.executable -eq [System.IO.Path]::GetFullPath([string]$State.executable) -and
        $identity.command_line.Contains([string]$State.marker)
    )
}

function Test-PortFree {
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $async = $client.BeginConnect($HostName, $Port, $null, $null)
        if ($async.AsyncWaitHandle.WaitOne(250)) {
            try { $client.EndConnect($async); return $false } catch { return $true }
        }
        return $true
    } finally { $client.Dispose() }
}

function Test-Health {
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri $HealthUrl -TimeoutSec 2
        return $response.StatusCode -eq 200
    } catch { return $false }
}

function Save-State($State) {
    New-Item -ItemType Directory -Force -Path $DataDir | Out-Null
    $temporary = "$StatePath.$PID.tmp"
    $State | ConvertTo-Json | Set-Content -LiteralPath $temporary -Encoding UTF8
    Move-Item -LiteralPath $temporary -Destination $StatePath -Force
}

function Stop-JustLaunched($Process) {
    try {
        $Process.Refresh()
        if (-not $Process.HasExited) {
            $Process.Kill()
            $Process.WaitForExit()
        }
    } catch {
        # This is the exact System.Diagnostics.Process returned by
        # Start-Process. Never substitute a name or port-owner lookup.
    }
}

function Cleanup-RecordedStartupFailure {
    $state = $null
    try { $state = Read-State } catch { return }
    if (-not $state) { return }
    $identity = Get-Identity ([int]$state.pid)
    if (-not $identity) {
        Remove-Item -LiteralPath $StatePath -ErrorAction SilentlyContinue
        return
    }
    if (-not (Confirm-Managed $state)) {
        return  # PID was reused or state changed: fail closed.
    }
    Stop-Process -Id ([int]$state.pid) -ErrorAction SilentlyContinue
    try { Wait-Process -Id ([int]$state.pid) -Timeout $StopTimeout -ErrorAction SilentlyContinue } catch {}
    if ((Get-Process -Id ([int]$state.pid) -ErrorAction SilentlyContinue) -and
        (Confirm-Managed $state)) {
        Stop-Process -Id ([int]$state.pid) -Force -ErrorAction SilentlyContinue
        try { Wait-Process -Id ([int]$state.pid) -Timeout $StopTimeout -ErrorAction SilentlyContinue } catch {}
    }
    if (-not (Get-Process -Id ([int]$state.pid) -ErrorAction SilentlyContinue)) {
        Remove-Item -LiteralPath $StatePath -ErrorAction SilentlyContinue
    }
}

function Start-Wattracker {
    $existing = Read-State
    if ($existing) {
        if (Confirm-Managed $existing) { Write-Host "running (PID $($existing.pid)) at $HealthUrl"; return }
        throw "Managed process state is stale or does not match exactly; refusing to start. Inspect $StatePath."
    }
    if (-not (Test-PortFree)) { throw "Port $Port is occupied; refusing to start or stop its owner." }

    if ($env:WATTRACKER_EXECUTABLE) {
        $Executable = [System.IO.Path]::GetFullPath($env:WATTRACKER_EXECUTABLE)
        $Arguments = @()
    } else {
        $Executable = Join-Path $Root ".venv\Scripts\python.exe"
        if (-not (Test-Path -LiteralPath $Executable)) { throw "Missing $Executable; create the virtual environment first." }
        $Arguments = @("-m", "wattracker")
    }
    if (-not (Test-Path -LiteralPath $Executable)) { throw "WATTRACKER_EXECUTABLE does not exist: $Executable" }
    $Marker = "--wattracker-managed=$([guid]::NewGuid().ToString('N'))"
    $Arguments += $Marker
    New-Item -ItemType Directory -Force -Path $DataDir | Out-Null
    $env:WATTRACKER_HOST = $HostName
    $env:WATTRACKER_PORT = [string]$Port
    $env:WATTRACKER_DATA_DIR = $DataDir
    $env:WATTRACKER_OPEN_BROWSER = "0"
    $process = Start-Process -FilePath $Executable -ArgumentList $Arguments -WorkingDirectory $Root -RedirectStandardOutput $LogPath -RedirectStandardError $ErrorLogPath -WindowStyle Hidden -PassThru
    Start-Sleep -Milliseconds 150
    $identity = Get-Identity $process.Id
    if (-not $identity -or -not $identity.command_line.Contains($Marker)) {
        Stop-JustLaunched $process
        throw "Started process identity could not be verified; refusing to create managed state."
    }
    try {
        Save-State ([pscustomobject]@{ pid=$process.Id; start_time_utc=$identity.start_time_utc; executable=$identity.executable; marker=$Marker; port=$Port })
    } catch {
        Stop-JustLaunched $process
        throw
    }
    try {
        $deadline = (Get-Date).AddSeconds($StartTimeout)
        while ((Get-Date) -lt $deadline) {
            if (-not (Confirm-Managed (Read-State))) { throw "wattracker exited during startup; see $ErrorLogPath" }
            if (Test-Health) { Write-Host "up at $HealthUrl (PID $($process.Id))"; return }
            Start-Sleep -Milliseconds 500
        }
        throw "Health check timed out; see $ErrorLogPath."
    } catch {
        Cleanup-RecordedStartupFailure
        throw
    }
}

function Stop-Wattracker {
    $state = Read-State
    if (-not $state) { Write-Host "not running (no managed state)"; return }
    $identity = Get-Identity ([int]$state.pid)
    if (-not $identity) { Remove-Item -LiteralPath $StatePath; Write-Host "not running (removed stale state)"; return }
    if (-not (Confirm-Managed $state)) { throw "Process identity does not match managed state; refusing to terminate PID $($state.pid)." }
    Stop-Process -Id ([int]$state.pid)
    $deadline = (Get-Date).AddSeconds($StopTimeout)
    while ((Get-Date) -lt $deadline) {
        if (-not (Get-Process -Id ([int]$state.pid) -ErrorAction SilentlyContinue)) {
            Remove-Item -LiteralPath $StatePath
            Write-Host "stopped PID $($state.pid)"
            return
        }
        Start-Sleep -Milliseconds 250
    }
    if (-not (Confirm-Managed $state)) { throw "Process identity changed while stopping; refusing force termination." }
    Stop-Process -Id ([int]$state.pid) -Force
    Remove-Item -LiteralPath $StatePath
    Write-Host "force-stopped PID $($state.pid)"
}

function Show-Status {
    $state = Read-State
    if (-not $state) { Write-Host "not running (no managed state)"; return }
    if (Confirm-Managed $state) { Write-Host "running (PID $($state.pid)) at $HealthUrl"; return }
    throw "Managed state is stale or does not match the process; refusing to claim ownership."
}

switch ($Action) {
    "start" { Start-Wattracker }
    "stop" { Stop-Wattracker }
    "restart" { Stop-Wattracker; Start-Wattracker }
    "status" { Show-Status }
}
