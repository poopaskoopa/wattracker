[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ArtifactPath,

    [Parameter(Mandatory = $true)]
    [string]$ExpectedThumbprint,

    [string]$TimestampUrl = "http://timestamp.digicert.com",
    [string]$SignToolPath = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Normalize-Thumbprint([string]$Value) {
    return ($Value -replace "[^0-9A-Fa-f]", "").ToUpperInvariant()
}

function Resolve-SignTool([string]$RequestedPath) {
    if ($RequestedPath) {
        if (-not (Test-Path -LiteralPath $RequestedPath -PathType Leaf)) {
            throw "signtool.exe was not found at the requested path"
        }
        return (Resolve-Path -LiteralPath $RequestedPath).Path
    }

    $command = Get-Command "signtool.exe" -ErrorAction SilentlyContinue
    if ($command) {
        return $command.Source
    }

    $kitsRoot = Join-Path ${env:ProgramFiles(x86)} "Windows Kits\10\bin"
    $candidate = Get-ChildItem -Path "$kitsRoot\*\x64\signtool.exe" `
        -File -ErrorAction SilentlyContinue |
        Sort-Object FullName -Descending |
        Select-Object -First 1
    if (-not $candidate) {
        throw "signtool.exe was not found; install the Windows SDK"
    }
    return $candidate.FullName
}

$expected = Normalize-Thumbprint $ExpectedThumbprint
if ($expected -notmatch "^[0-9A-F]{40}$") {
    throw "ExpectedThumbprint must be a 40-digit SHA-1 certificate thumbprint"
}
if ([string]::IsNullOrWhiteSpace($env:WATTRACKER_SIGNING_PFX_B64) -or
    [string]::IsNullOrWhiteSpace($env:WATTRACKER_SIGNING_PFX_PASSWORD)) {
    throw "Signing PFX material is required; no unsigned artifact will be produced"
}

$resolvedArtifact = Resolve-Path -LiteralPath $ArtifactPath
if ((Get-Item -LiteralPath $resolvedArtifact).PSIsContainer) {
    $signable = @(Get-ChildItem -LiteralPath $resolvedArtifact -Recurse -File |
        Where-Object { $_.Extension -in ".exe", ".dll", ".pyd" })
} else {
    $signable = @((Get-Item -LiteralPath $resolvedArtifact))
}
if ($signable.Count -eq 0) {
    throw "The artifact contains no Authenticode-signable files"
}

$signTool = Resolve-SignTool $SignToolPath
$tempPfx = Join-Path ([IO.Path]::GetTempPath()) `
    ("wattracker-signing-{0}.pfx" -f [Guid]::NewGuid().ToString("N"))
$securePassword = $null
$previewCertificate = $null
$certificate = $null
$removeImportedCertificate = $false

try {
    try {
        $pfxBytes = [Convert]::FromBase64String(
            $env:WATTRACKER_SIGNING_PFX_B64
        )
    } catch {
        throw "Signing PFX material is not valid base64"
    }
    [IO.File]::WriteAllBytes($tempPfx, $pfxBytes)
    [Array]::Clear($pfxBytes, 0, $pfxBytes.Length)

    $securePassword = ConvertTo-SecureString `
        $env:WATTRACKER_SIGNING_PFX_PASSWORD -AsPlainText -Force

    # Inspect the PFX without persisting a key, then require the independently
    # configured thumbprint before importing anything into the certificate store.
    $previewCertificate = [Security.Cryptography.X509Certificates.X509Certificate2]::new(
        $tempPfx,
        $env:WATTRACKER_SIGNING_PFX_PASSWORD,
        [Security.Cryptography.X509Certificates.X509KeyStorageFlags]::EphemeralKeySet
    )
    $pfxThumbprint = Normalize-Thumbprint $previewCertificate.Thumbprint
    if ($pfxThumbprint -ne $expected) {
        throw "The signing certificate does not match ExpectedThumbprint"
    }

    $storePath = "Cert:\CurrentUser\My\$expected"
    $alreadyPresent = Test-Path -LiteralPath $storePath
    $certificate = Import-PfxCertificate -FilePath $tempPfx `
        -CertStoreLocation "Cert:\CurrentUser\My" `
        -Password $securePassword
    $removeImportedCertificate = -not $alreadyPresent
    if (-not $certificate.HasPrivateKey) {
        throw "The signing certificate has no private key"
    }

    foreach ($file in $signable) {
        & $signTool sign /sha1 $expected /s My /fd SHA256 `
            /tr $TimestampUrl /td SHA256 $file.FullName
        if ($LASTEXITCODE -ne 0) {
            throw "signtool failed to sign $($file.Name)"
        }
    }

    foreach ($file in $signable) {
        # /tw makes signtool explicitly check for a timestamp.  The
        # TimeStamperCertificate check below fails closed even on signtool
        # versions that report a missing timestamp as a warning.
        & $signTool verify /pa /all /v /tw $file.FullName
        $verifyExitCode = $LASTEXITCODE
        if ($verifyExitCode -ne 0) {
            throw "signtool verification failed for $($file.Name)"
        }
        $signature = Get-AuthenticodeSignature -FilePath $file.FullName
        $actual = if ($signature.SignerCertificate) {
            Normalize-Thumbprint $signature.SignerCertificate.Thumbprint
        } else {
            ""
        }
        if ($signature.Status -ne "Valid" -or $actual -ne $expected) {
            throw "Authenticode verification failed for $($file.Name)"
        }
        $timestampProperty = $signature.PSObject.Properties[
            "TimeStamperCertificate"
        ]
        if (-not $timestampProperty -or -not $timestampProperty.Value) {
            throw "RFC3161 timestamp verification failed for $($file.Name)"
        }
    }

    Write-Output ("Verified Authenticode signatures on {0} file(s)." -f `
        $signable.Count)
} finally {
    if ($previewCertificate) {
        $previewCertificate.Dispose()
    }
    if ($removeImportedCertificate -and $certificate) {
        Remove-Item -LiteralPath ("Cert:\CurrentUser\My\{0}" -f `
            $certificate.Thumbprint) -Force -ErrorAction SilentlyContinue
    }
    if (Test-Path -LiteralPath $tempPfx) {
        Remove-Item -LiteralPath $tempPfx -Force -ErrorAction SilentlyContinue
    }
    $env:WATTRACKER_SIGNING_PFX_B64 = $null
    $env:WATTRACKER_SIGNING_PFX_PASSWORD = $null
    $securePassword = $null
}
