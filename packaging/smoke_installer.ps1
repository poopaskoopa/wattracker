param(
    [Parameter(Mandatory=$true)]
    [Alias("InstallerPath")]
    [string]$Installer
)

$ErrorActionPreference = "Stop"
$Installer = (Resolve-Path -LiteralPath $Installer).Path
$TempRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("wattracker-installer-" + [guid]::NewGuid().ToString("N"))
$InstallDir = Join-Path $TempRoot "Program Files With Spaces\wattracker"
$DataDir = Join-Path $TempRoot "user data"
$Sentinel = Join-Path $DataDir "keep-after-uninstall.txt"
$Launcher = Join-Path $InstallDir "scripts\wattracker.ps1"
$Executable = Join-Path $InstallDir "wattracker.exe"
$Shortcut = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\wattracker\wattracker.lnk"
$UninstallKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\{63E9478B-5D6D-4D36-9202-E8C7941AD567}_is1"

function Get-FreeLoopbackPort {
    $probe = New-Object System.Net.Sockets.TcpListener([System.Net.IPAddress]::Loopback, 0)
    $probe.Start()
    try { return [int]$probe.LocalEndpoint.Port }
    finally { $probe.Stop() }
}

function Invoke-CheckedProcess {
    param([string]$FilePath, [string[]]$ArgumentList)
    $process = Start-Process -FilePath $FilePath -ArgumentList $ArgumentList -Wait -PassThru
    if ($process.ExitCode -ne 0) {
        throw "$FilePath exited with code $($process.ExitCode)"
    }
}

function Install-Wattracker {
    Invoke-CheckedProcess -FilePath $Installer -ArgumentList @(
        "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/SP-", "/DIR=`"$InstallDir`""
    )
}

function Assert-HttpOk([string]$Url) {
    $response = Invoke-WebRequest -UseBasicParsing -Uri $Url -TimeoutSec 10
    if ($response.StatusCode -ne 200) { throw "HTTP smoke failed for $Url" }
}

New-Item -ItemType Directory -Force -Path $DataDir | Out-Null
Set-Content -LiteralPath $Sentinel -Value "preserve me" -Encoding UTF8
$env:WATTRACKER_DATA_DIR = $DataDir
$env:WATTRACKER_PORT = [string](Get-FreeLoopbackPort)
$env:WATTRACKER_HOST = "127.0.0.1"
$env:WATTRACKER_AUTO_SCAN = "0"
$env:WATTRACKER_OPEN_BROWSER = "0"

try {
    Install-Wattracker
    foreach ($required in @($Executable, $Launcher, (Join-Path $InstallDir "_internal"))) {
        if (-not (Test-Path -LiteralPath $required)) { throw "installed payload is missing: $required" }
    }
    if (-not (Test-Path -LiteralPath $Shortcut)) { throw "Start Menu shortcut was not installed" }
    if (-not (Test-Path -LiteralPath $UninstallKey)) { throw "uninstall metadata was not registered" }

    & $Launcher -Action start
    $base = "http://127.0.0.1:$($env:WATTRACKER_PORT)"
    Assert-HttpOk "$base/login"
    Assert-HttpOk "$base/static/style.css"
    Assert-HttpOk "$base/register"
    $firstState = Get-Content -LiteralPath (Join-Path $DataDir "wattracker-process.json") -Raw | ConvertFrom-Json

    # Reinstalling the same version exercises PrepareToInstall, which must stop
    # the exact identity-recorded process before replacing any files.
    Install-Wattracker
    if (Get-Process -Id ([int]$firstState.pid) -ErrorAction SilentlyContinue) {
        throw "same-version upgrade did not stop the managed process"
    }
    if (-not (Test-Path -LiteralPath $Sentinel)) { throw "upgrade removed user data" }

    & $Launcher -Action start
    $secondState = Get-Content -LiteralPath (Join-Path $DataDir "wattracker-process.json") -Raw | ConvertFrom-Json
    $StatePath = Join-Path $DataDir "wattracker-process.json"
    $Uninstaller = Join-Path $InstallDir "unins000.exe"
    if (-not (Test-Path -LiteralPath $Uninstaller)) { throw "uninstaller is missing" }

    # A failed identity check must abort before Uninstall removes any payload.
    $validState = Get-Content -LiteralPath $StatePath -Raw
    $secondState.marker = "tampered"
    $secondState | ConvertTo-Json | Set-Content -LiteralPath $StatePath -Encoding UTF8
    $blocked = Start-Process -FilePath $Uninstaller -ArgumentList @(
        "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"
    ) -Wait -PassThru
    if ($blocked.ExitCode -eq 0) { throw "tampered state did not block uninstall" }
    if (-not (Get-Process -Id ([int]$secondState.pid) -ErrorAction SilentlyContinue)) {
        throw "blocked uninstall terminated a process with tampered state"
    }
    if (-not (Test-Path -LiteralPath $Executable)) { throw "blocked uninstall removed application files" }
    if (-not (Test-Path -LiteralPath $Sentinel)) { throw "blocked uninstall removed user data" }
    Set-Content -LiteralPath $StatePath -Value $validState -Encoding UTF8

    Invoke-CheckedProcess -FilePath $Uninstaller -ArgumentList @(
        "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"
    )
    if (Get-Process -Id ([int]$secondState.pid) -ErrorAction SilentlyContinue) {
        throw "uninstall did not stop the managed process"
    }
    if (Test-Path -LiteralPath $Executable) { throw "installed executable survived uninstall" }
    if (Test-Path -LiteralPath $Shortcut) { throw "Start Menu shortcut survived uninstall" }
    if (Test-Path -LiteralPath $UninstallKey) { throw "uninstall metadata survived uninstall" }
    if (-not (Test-Path -LiteralPath $Sentinel)) { throw "uninstall removed user data" }
} finally {
    if (Test-Path -LiteralPath $Launcher) {
        try { & $Launcher -Action stop } catch { Write-Warning $_ }
    }
    $Uninstaller = Join-Path $InstallDir "unins000.exe"
    if (Test-Path -LiteralPath $Uninstaller) {
        try {
            Invoke-CheckedProcess -FilePath $Uninstaller -ArgumentList @(
                "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"
            )
        } catch { Write-Warning $_ }
    }
    Remove-Item -LiteralPath $TempRoot -Recurse -Force -ErrorAction SilentlyContinue
}
