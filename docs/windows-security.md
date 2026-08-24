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
the session secret and LLM API key; `wattracker.db` and its `-wal`/`-shm`
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

`icacls.exe` is named by **absolute path**, built from `%SystemRoot%`
(`config._icacls_path`). Passing a list to `subprocess.run` reaches
`CreateProcessW` with `lpApplicationName=NULL`, and Windows then resolves a
bare program name starting from the calling executable's own directory and the
current working directory, **both ahead of System32**. The connector ships as a
portable `.exe` a rider drops in Downloads, and `_restrict` is on the first code
path its `__main__` reaches — so a bare `"icacls"` meant an `icacls.exe` planted
beside that download ran as the rider on every launch, every config save and
every log rotation, invisibly (`CREATE_NO_WINDOW` + `capture_output`). The
absolute path removes the search entirely. `tests/test_windows_acl.py` asserts
the property, not just the string: any relative spelling fails.
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

**A path that arrived over RPC is confined to the folder it is FOR, not to
`$HOME`.** That is the rule the rest of this section applies, and it is the
general form of four findings that were first fixed one field at a time.
`paths.confine_storage_dir` - the whole home directory plus the Zwift roots -
is the right rule for a folder the rider typed into their own Settings form on
the machine it describes. It is the wrong rule for a folder an unauthenticated
peer named, because it treats the entire home directory as one trust domain:
every field hardened that way is still a primitive over everything else in it.
Each RPC therefore measures a submitted path against the folders its own
operation is defined on:

- **Folders the server names** clear this machine's trusted roots
  (`paths.confine_storage_dir`, the one rule for a submitted path) *and* the
  scope of the operation - see the two entries below. Folders the connector
  discovered under a root it already trusts take the lenient final-component
  rule (`confine_detected_dir`) instead; that split is by provenance, and a
  path that arrived over RPC is always *submitted*, never *discovered*.
- **`activities.read` is not a file-read primitive, and `activities.list` is
  not an oracle.** The folder must be one of this machine's own Activities
  folders (`handlers.activities_scope`: what the connector was configured
  with, plus what it discovered), and the file must be one the listing
  offered - a `.fit` sitting directly in that folder, never Zwift's in-progress
  buffer, symlinks resolved before either test. One predicate (`_in_scope`) is
  applied by both, so the listing and the read cannot disagree. Confining the
  file within the folder while the peer still chose the folder left any `.fit`
  under `$HOME` readable by naming its parent, and made the listing answer as a
  directory-existence and symlink-target probe for the rest; an out-of-scope
  folder now answers identically whether or not it exists.

  The consequence is deliberate: on a split install an Activities folder in an
  unusual place is configured on the connector (`--activities-dir`), by the
  person sitting at that machine. The web UI can choose between the folders the
  connector reports and cannot name one of its own. It says so when a folder is
  saved rather than accepting one and then quietly scanning nothing -
  `paths.validate_dir` takes the field name and answers with the rule that
  field will be judged by in use.
- **`workouts.sync` is a write-and-delete primitive**, so the folder and both
  halves are guarded. The folder must be a Zwift Workouts folder
  (`paths.within_workouts_roots`), not merely somewhere under a trusted root,
  or the sync is refused as `blocked` - as `$HOME`, `remove` deleted any file
  the peer named in it and `write`, which creates its target, planted files in
  folders like `~/.config/autostart` that it brought into being. A `remove`
  entry must be a bare filename *and* a `.zwo`: inside the Zwift folder that is
  the rider's own workout data, and "bare filename" alone still empties it of
  everything else. A `write` entry's `date` must be a bare `YYYY-MM-DD` - it
  leads the `.zwo` filename, so an absolute or traversing date was an
  arbitrary-path write with no `..` required. `zwo.plan_filename` sanitises
  both halves and `zwo.write_plan_to_zwift` refuses any name that is not one
  path component, independently of it.

The narrow rule stops at the RPC boundary. `LocalBackend` keeps
`confine_storage_dir` alone, because in a single-machine install there is no
untrusted peer and the rider is entitled to point the app at their own folders;
adding the narrower rule there would refuse Zwift layouts this app has always
supported and protect nothing.

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

## The connector as a portable executable

The connector also ships as one windowed `WattrackerConnector.exe`
(`packaging/wattracker-connector.spec`), which changes three things about its
security posture and nothing else.

**A window that opens already logged in.** Double-clicking the tray icon shows
the server's own web UI. The connector holds a device token, not a cookie, so
it exchanges the token for a single-use ticket over the same
bearer-authenticated HTTP path it already uses for buffered ride uploads
(`POST /api/connector/session`), and the window spends that ticket once
(`GET /connector/session?token=...`). The ticket is held only as a sha256 in
memory, expires in 60 seconds, is redeemable once, and is dropped when the
device is revoked.

State this plainly: **a device token now escalates to a full web session.**
That session can read the rider's whole history, change settings, revoke
connector devices and take backups. It cannot change the account password,
which has no route.

Be precise about why that is accepted, because the obvious justification is
wrong. The token grants read/write to the rider's Zwift folders **on its own
machine**; the session's reach is wider. `wattracker/backend/remote.py` resolves
the connector by `user_id` alone, and nothing about escalating requires an
attached socket — so a token off a laptop that has been in a drawer for a year
escalates to a session that drives whichever connector is attached *now*. The
pre-merge review of PR #93 executed exactly that: a never-connected device
enumerated a `.fit` file on a different machine's filesystem. The same session
can also clear or overwrite the stored Zwift credentials.

What makes the widening defensible is not that it grants nothing new — it does
— but that it grants nothing **durable**, and that the alternative (a password
prompt in a window a tray icon opened) teaches exactly the habit phishing
depends on. A connector-derived session is stamped `via=connector` in the
signed session cookie at redemption, and three routes refuse it: pairing
another device (`POST /settings/connector`), rotating the calendar link
(`POST /settings/calendar-feed`), and changing the app-global — not per-user —
LLM settings (the `llm_endpoint`, `llm_custom_url`, `llm_model`, `api_key` and
legacy `anthropic_api_key` fields of `POST /settings`). Each would otherwise
leave behind a credential that revoking the device does not reach.

The third one covers the whole LLM group, not just the key, because the
provider endpoint became settable in the UI (PR #126) and that is the same
threat one size larger: a connector session that repoints `llm_endpoint` at a
base URL it controls is handed the shared API key on the first refinement call
and every rider's prompt payload after that, from a server that otherwise looks
like it is working — and revoking the device does not undo it, exactly as with
a swapped key. The model travels with them because the same write decides
whether the layer runs at all. Refusal means asking to *change* the group: the
LLM fields sit in the same form as the folder settings, so the tray window
echoes the provider and model the page just rendered on every ordinary save,
and treating that echo as an attempt would 403 the folder save the window
exists for while preventing nothing. A connector session writes none of these
settings either way.

Revoking is deliberately still allowed: it is the way out, and the rider who
has lost a laptop may well be looking at the tray window of the machine still
in front of them. Signing in with the password inside that window lifts
the restriction, because a device token can neither obtain nor change a
password.

**The LLM-settings refusal does not hold against `/register`, and that is a
known gap.** Registration is unauthenticated by design — this is a single-user
local app that has to let its first user in — and a successful registration drops the
`via=connector` marker, because proving a password is what the marker exists to
wait for. But the password proved at `/register` is a *new account's*, chosen by
whoever is registering, not the rider's. So a connector session can register a
throwaway account and, as that account, write the LLM settings: they are
app-global rather than per-user, so they are the settings a different `uid` can
still reach — and since #126 that includes the endpoint, so the throwaway
account can capture the rider's existing key and prompts without ever seeing
either. Device pairing and calendar-feed rotation are not reachable this
way — both are per-user, and the new account is a different user.

The mitigating half is that anyone who can reach the port can already register
without a device token at all, so this is a pre-existing property of open
registration rather than something the connector introduced. It is recorded
here because the refusal above would otherwise read as stronger than it is. If
open registration is ever closed, close this with it.

Revocation reaches the session as well as the token, over **both** protocols.
A session cookie is a signed blob with no server-side record, so
`settings_connector_revoke` has nothing to delete; instead the cookie carries
the `device_id` it came from and `AuthMiddleware` ends any connector session
whose device is gone. Without that, revoking would kill the token and leave the
window it opened working for the fortnight the cookie is valid.

`AuthMiddleware` alone is not enough, and the reason generalises. It is a
`BaseHTTPMiddleware`, and Starlette hands any scope that is not `http` straight
to the application — so **no websocket route is dispatched through it**. For one
commit that left `/ride/ws` authenticating on `user_id` alone: revoking cut the
browser half and left the thief riding, driving whichever connector was
attached at the time. The ride socket now runs the pairing check itself. Any
new websocket route that authenticates on the session must do the same; there
is no arrangement of middleware that will do it for them.

**And it runs that check every tick, not only at the handshake.** Checking once
is enough for a request and not enough for a socket that stays open for an
hour: a `/ride/ws` opened a second before Revoke was pressed simply kept
streaming, and kept steering the trainer through whichever connector is
attached now. The pre-merge review reproduced it and counted three frames
delivered after a revoke that had already emptied the device list. Both ride
loops — simulated and real hardware — now re-ask
`_connector_session_still_paired` each iteration and close cleanly the moment
the answer changes,
stopping the ride (a started ride is still saved; an idle one still writes
nothing) and sending a refusal frame rather than raising. The revoke handler
was left alone on purpose: making revocation reach out to sockets by device
would only cover the socket types someone remembered to register, whereas a
check inside the loop cannot be forgotten by a future revoke path.
`tests/test_connector_session.py` covers both loops with the socket opened
*before* the revoke, which is the ordering the earlier test did not have.

The query parameter is
named `token` rather than `ticket` deliberately: uvicorn logs the full request
target and `calendarfeed`'s redaction filter only scrubs parameter names
beginning `token`, so any other name would write live credentials to the
access log in plaintext. Both ends have tests pinning that name.

**Autostart is HKCU, opt-in, and nothing else.** The tray's "Start with
Windows" toggle writes one value under
`HKEY_CURRENT_USER\Software\Microsoft\Windows\CurrentVersion\Run` and deletes
it when untoggled. Never HKLM, never a Windows service, never Task Scheduler —
all three would ask for elevation, which is the promise this document makes
everywhere else. The application installer stays clear of startup entries
entirely (`tests/test_windows_installer.py` asserts `wattracker.iss` never
mentions one), so autostart lives in the connector at runtime and the installer
is untouched.

One qualification to "writes only when toggled": every launch of the packaged
executable checks whether an entry that already exists still names the file
now running, and repoints it if not (`autostart.refresh`). It never creates
one, never touches the key for a rider who has not opted in, and a connector
running from a Python environment leaves the value alone entirely. Without it,
moving the exe out of Downloads silently disables autostart — a failure with
nothing to see anywhere.

Only one connector runs per logon session, held by a `Local\`-scoped named
mutex; a second launch tells the running icon and exits. `Local\` rather than
`Global\` on purpose: two riders signed in to one machine are two riders with
two trainers, and a machine-wide mutex would let either of them deny the other
a connector. This is distinct from the server's one-connector-per-*account*
rule, which is enforced at the other end and surfaces in the tray as a stopped
icon explaining that another connector took the account over.

The window's WebView2 profile is pointed at the connector's own config
directory (`WEBVIEW2_USER_DATA_FOLDER`), which is already created owner-only.
The default would be a folder beside the executable — which for a single
portable file means browser profile data appearing wherever the rider dropped
it, including a USB stick.

Autostart is also what makes the trust-boundary work above load-bearing rather
than theoretical: an unattended connector holds a token across reboots until
somebody revokes it, so revocation closing the live socket — not merely the
next connection — is the control that matters.

**Unsigned, self-extracting, and autostarting is the worst profile for
heuristics.** A onefile build re-extracts to `%TEMP%\_MEIxxxx` on every launch.
That, plus an autostart entry, plus a held credential, is the shape antivirus
software dislikes most, and there is no certificate yet
(`packaging/sign-windows.ps1` is wired into the release job, which remains
hard-disabled). Until then every connector binary in existence is unsigned, and
comes from one of two places.

A **local Windows build** should be treated as one: check the `.sha256`
published beside it. A **CI artifact**, uploaded by `windows.yml` on a merge to
`main`, has no checksum beside it and deliberately so — it would be generated
by the same run that built the binary and travel in the same archive, which
proves nothing a tampered run could not also forge. What stands in for it is
the run itself: the artifact names the workflow run that produced it, that run
names the commit, and the log shows the freeze and every smoke check. That
is provenance rather than integrity, and it is not a substitute for signing.
Neither source should be handed to anyone outside the people testing this.

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
