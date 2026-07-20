$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$Script = Join-Path $Root "scripts\wattracker.ps1"
$TempRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("wattracker-lifecycle-" + [guid]::NewGuid().ToString("N"))
$env:WATTRACKER_DATA_DIR = Join-Path $TempRoot "data"
function Get-FreeLoopbackPort {
    $probe = New-Object System.Net.Sockets.TcpListener([System.Net.IPAddress]::Loopback, 0)
    $probe.Start()
    try { return [int]$probe.LocalEndpoint.Port }
    finally { $probe.Stop() }
}
$env:WATTRACKER_PORT = [string](Get-FreeLoopbackPort)
$env:WATTRACKER_HOST = "127.0.0.1"
$env:WATTRACKER_OPEN_BROWSER = "0"

New-Item -ItemType Directory -Force -Path $TempRoot | Out-Null
$unrelated = Start-Process -FilePath $env:ComSpec -ArgumentList "/c", "ping -t 127.0.0.1" -PassThru -WindowStyle Hidden
try {
    $listener = New-Object System.Net.Sockets.TcpListener([System.Net.IPAddress]::Loopback, [int]$env:WATTRACKER_PORT)
    $listener.Start()
    $occupiedFailed = $false
    try { & $Script start } catch { $occupiedFailed = $true }
    if (-not $occupiedFailed) { throw "occupied port did not fail safely" }
    if (-not $listener.Server.IsBound) { throw "occupied port owner was disturbed" }
    $listener.Stop()

    & $Script start
    & $Script status
    $state = Get-Content (Join-Path $env:WATTRACKER_DATA_DIR "wattracker-process.json") -Raw | ConvertFrom-Json
    if (-not (Get-Process -Id $state.pid -ErrorAction SilentlyContinue)) { throw "managed process missing" }
    & $Script restart
    & $Script stop
    if (Get-Process -Id $unrelated.Id -ErrorAction SilentlyContinue) { Write-Host "unrelated process preserved" }
    else { throw "unrelated process was terminated" }

    $tampered = [pscustomobject]@{ pid=$unrelated.Id; start_time_utc="bad"; executable=$unrelated.Path; marker="bad"; port=[int]$env:WATTRACKER_PORT }
    $tampered | ConvertTo-Json | Set-Content (Join-Path $env:WATTRACKER_DATA_DIR "wattracker-process.json")
    $failedClosed = $false
    try { & $Script stop } catch { $failedClosed = $true }
    if (-not $failedClosed) { throw "tampered state did not fail closed" }
    if (-not (Get-Process -Id $unrelated.Id -ErrorAction SilentlyContinue)) { throw "tampered state killed unrelated process" }
} finally {
    Stop-Process -Id $unrelated.Id -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $TempRoot -Recurse -Force -ErrorAction SilentlyContinue
}
