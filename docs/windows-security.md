# Windows security and release gates

## Credential storage

On Windows, wattracker first accepts only the Windows Credential Manager
backend supplied by `keyring.backends.Windows`. Fail, plaintext, chained, and
non-Windows keyring backends are rejected. If Credential Manager cannot save
and read back the credential, wattracker uses Windows DPAPI with
`CRYPTPROTECT_UI_FORBIDDEN`. DPAPI protection is scoped to the current Windows
user and includes the wattracker service name and application user ID as
additional entropy.

New Windows credentials are never written with the legacy `credentials.key`
format. Existing `@keyring` and `enc1$` rows remain readable but are not
silently migrated. A user explicitly resaves the credential to upgrade it. A
database restored under a different Windows account can retain an unreadable
DPAPI marker; the user must re-enter the credential. The application must not
delete or overwrite that marker merely because unprotection failed.

DPAPI and Credential Manager protect against an offline database copy. They do
not protect secrets from malware already running as the same Windows user.

Credential Manager writes use unique versioned slots. A resave writes and
reads back a fresh slot, commits its marker to SQLite, and only then attempts
best-effort removal of the formerly referenced slot. Any verification or DB
failure removes only the staged slot, so even an unreadable legacy
`@keyring` secret is never overwritten during rollback.

## Network boundary

The supported Windows configuration is loopback-only. The installer and
launcher must not create a Windows Firewall exception. LAN/public binding
requires a separate security design covering TLS, Secure cookies, CSRF and
Origin validation, trusted hosts/proxies, login throttling, and registration
policy.

## Signed releases

`.github/workflows/windows-release.yml` creates no unsigned release artifact.
It runs only from the exact commit that triggered a `v*` tag push and uses the
protected `windows-code-signing` environment. There is no arbitrary-ref manual
dispatch path into the signing job. Configure:

- secret `WATTRACKER_SIGNING_PFX_B64`: base64 of the code-signing PFX;
- secret `WATTRACKER_SIGNING_PFX_PASSWORD`: PFX password;
- variable `WATTRACKER_SIGNING_THUMBPRINT`: expected 40-digit certificate
  thumbprint.

`packaging/sign-windows.ps1` imports the private key into the ephemeral
runner's CurrentUser certificate store without putting its password on a child
process command line. The PFX and password exist only in the signing step's
environment; checkout, dependency installation, tests, builds, smoke tests,
packaging, and upload do not inherit them. The script creates an RFC3161
SHA-256 timestamp for each PE file and requires both `signtool verify /tw` and
a populated `TimeStamperCertificate`, in addition to valid Authenticode status
and the expected signer thumbprint. It then removes the temporary PFX and the
certificate it imported. Any missing secret, timestamp failure, signature
failure, or thumbprint mismatch aborts the workflow before artifact upload.

Azure Trusted Signing with OIDC and a non-exportable key is preferred when a
signing account is available. The PFX workflow is an interim design and must
remain protected by tag rules, required reviewers, and environment-scoped
secrets.

The workflow uses maintained major tags for GitHub-authored actions because
their immutable commit SHAs have not yet been recorded in this repository.
That is a residual supply-chain risk. Before a production release, pin each
action to a reviewed commit SHA, lock all Python runtime dependencies with
hashes, enable artifact attestations/SBOM generation, and verify the certificate
and timestamp on a clean Windows machine.

No build may be described as signed unless `signtool verify` and
`Get-AuthenticodeSignature` both succeed for every `.exe`, `.dll`, and `.pyd`
inside the frozen distribution. Windows Credential Manager/DPAPI, SmartScreen,
OneDrive Known Folder behavior, and BLE hardware remain manual Windows release
checks.
