param(
    [Parameter(Mandatory=$true)]
    [Alias("ExecutablePath")]
    [string]$Executable
)
$ErrorActionPreference = "Stop"
$TempRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("wattracker-frozen-" + [guid]::NewGuid().ToString("N"))
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
$env:WATTRACKER_AUTO_SCAN = "0"
New-Item -ItemType Directory -Force -Path $TempRoot | Out-Null
$process = Start-Process -FilePath (Resolve-Path $Executable) -WorkingDirectory $TempRoot -PassThru
try {
    $base = "http://127.0.0.1:$($env:WATTRACKER_PORT)"
    $deadline = (Get-Date).AddSeconds(30)
    while ((Get-Date) -lt $deadline) {
        try { if ((Invoke-WebRequest -UseBasicParsing "$base/login" -TimeoutSec 2).StatusCode -eq 200) { break } } catch {}
        Start-Sleep -Milliseconds 500
    }
    if ((Invoke-WebRequest -UseBasicParsing "$base/static/style.css").StatusCode -ne 200) { throw "static smoke failed" }
    foreach ($asset in @("chart.umd.min.js", "chartjs-plugin-zoom.umd.min.js")) {
        $response = Invoke-WebRequest -UseBasicParsing "$base/static/vendor/$asset"
        if ($response.StatusCode -ne 200 -or $response.Content.Length -lt 1000) {
            throw "vendored chart asset smoke failed: $asset"
        }
    }
    if ((Invoke-WebRequest -UseBasicParsing "$base/register").StatusCode -ne 200) { throw "register smoke failed" }
} finally {
    Stop-Process -Id $process.Id -ErrorAction SilentlyContinue
    Wait-Process -Id $process.Id -Timeout 10 -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $TempRoot -Recurse -Force -ErrorAction SilentlyContinue
}
