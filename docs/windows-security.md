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

## Data directory permissions

The data directory and every sensitive file inside it (`config.json`, which holds
the session secret and Anthropic API key; `wattracker.db` and its `-wal`/`-shm`
sidecars and `backups/`, which hold password hashes and encrypted credential
markers) are locked to the current Windows user with an explicit owner-only ACL.

POSIX `chmod` is inert on Windows: it only toggles the read-only attribute and
sets no ACL. `config._restrict` therefore runs
`icacls <path> /inheritance:r /grant:r <user>:...F` on Windows (`(OI)(CI)F` for
directories so children inherit the same lock, `F` for files), disabling
inheritance and granting full control to the current user only. This matters
when the data directory is relocated off `%USERPROFILE%` via
`WATTRACKER_DATA_DIR` or `WATTRACKER_DB` onto a drive or share whose inherited
ACL would otherwise grant e.g. `Users:(R)`, letting another local standard
account read the session secret and password hashes. The default
`%USERPROFILE%\.wattracker` is already protected by the profile ACL; this closes
the relocated-directory gap.

The lockdown is best-effort and never crashes the app: if `icacls` is missing or
fails (e.g. a filesystem that does not support ACLs), the failure is logged at
debug and startup continues, mirroring the existing chmod-can-fail contract.
This is defense against another local account reading an at-rest copy; it does
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

## Server/connector trust boundary

In a split install the server runs in a container and the connector runs on
the Windows box where Zwift lives. The connector dials out and holds one
long-lived WebSocket; requests only ever flow server to connector.

**The connector does not trust the server.** The connector authenticates to
the server with a per-device bearer token; the server does not authenticate
itself in return, so "the server" is only ever "whatever is attached to this
socket". Every handler therefore judges what it is asked to do rather than
assuming a well-behaved peer. This is a deliberate decision, not an accident
of implementation, and it is what the checks in `wattracker_connector/
handlers.py` are for:

- **Folders the server names** clear this machine's trusted roots before
  anything acts on them (`paths.confine_storage_dir`, the one rule for a
  submitted path). Folders the connector discovered under a root it already
  trusts take the lenient final-component rule (`confine_detected_dir`)
  instead - that split is by provenance, and a path that arrived over RPC is
  always *submitted*, never *discovered*.
- **`activities.read` is not a file-read primitive.** It serves only what
  `activities.list` offered: a `.fit` sitting directly in the resolved
  Activities folder, never Zwift's in-progress buffer, symlinks resolved
  before either test. One predicate (`_in_scope`) is applied by both, so the
  listing and the read cannot disagree.
- **`workouts.sync` is a write-and-delete primitive**, so both halves are
  guarded. A `remove` entry must be a bare filename. A `write` entry's `date`
  must be a bare `YYYY-MM-DD` - it leads the `.zwo` filename, so an absolute
  or traversing date was an arbitrary-path write with no `..` required.
  `zwo.plan_filename` sanitises both halves and `zwo.write_plan_to_zwift`
  refuses any name that is not one path component, independently of it.

What keeps the same-UID argument behind `confine_detected_dir` intact under
this architecture: the container never sees the Zwift folder. `docker-compose.
yml` mounts only the `wattracker-data` named volume and the image runs as uid
10001, so all Zwift path resolution happens connector-side, as the rider's own
user. The split does not put the export path across a privilege boundary.

Revocation covers the live socket, not only the next connection: a connector
holds its WebSocket for as long as it runs, so `settings_connector_revoke`
closes the attached session (`connectorhub.close_device`) as well as deleting
the row. Stolen-laptop is the scenario the button exists for.

Token confidentiality is bounded by the transport. `ws://` without TLS is the
documented posture for a trusted LAN; a WAN deployment needs TLS. The
connector never follows an HTTP redirect when uploading a buffered ride,
because urllib replays `Authorization` across hosts and a redirecting server
would otherwise harvest the token. Pass `--token` once with `--save` and omit
it afterwards: an argument is visible to every process on the machine, while
the saved config file is written 0600.

## Installer lifecycle and compiler provenance

The per-user installer never requests elevation, adds no firewall rule, and
keeps profile data outside its application directory. Upgrade and uninstall
invoke only the installed launcher, which validates the recorded process ID,
creation time, executable path, random marker syntax and exact command-line
token before terminating the already-opened process handle. State for a
recorded PID that no longer exists is safely cleared. Malformed or tampered
state, or state that points to a live process whose identity does not match,
aborts upgrade or uninstall before application files are replaced or removed;
state is never used to kill a process by name or port.

The unsigned CI packaging job pins the Inno Setup release and asset name. It
requires both an exact reviewed SHA-256 digest and a valid Authenticode
signature whose exact publisher simple name is `Pyrsys B.V.` before executing
the compiler installer. The workflow token is read-only. The application
installer remains unsigned and must be labeled as such; these compiler checks
do not provide end-user publisher authentication.

The launcher uses process-scoped `-ExecutionPolicy Bypass` because the shipped
PowerShell script is itself unsigned. This does not elevate privileges or
change machine/user execution policy, and all installer invocations use an
absolute script path under the current user's installation. Its remaining
risk is same-user modification of that launcher; production releases should
prefer a signed native launcher.

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
