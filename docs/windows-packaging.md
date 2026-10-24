# Windows packaging

wattracker ships on Windows as a frozen onedir tree, wrapped two ways:

| artifact | what it is | built by |
|---|---|---|
| `wattracker-<version>-windows-x64-unsigned-setup.exe` | Inno Setup installer, double-click to install | `windows.yml`, job `package-unsigned` |
| `wattracker-windows-x64-signed.zip` | the same tree, signed, plus a `.sha256` | `windows-release.yml`, on a `v*` tag |

Note what that table does not contain: **there is no signed installer.**
`windows-release.yml` signs the frozen binaries and zips them; it never invokes
the setup compiler. The installer only exists on the unsigned path, and
`docs/windows-security.md` requires it to be labelled that way. The rules around
signing itself live in that document; this one is about the packaging.

## What gets built

`packaging/wattracker.spec` is one spec shared by Windows and macOS (see
`docs/macos-packaging.md` for why the Analysis is deliberately common). On
Windows it produces a PyInstaller **onedir** tree:

```
dist\wattracker\wattracker.exe
dist\wattracker\_internal\...      (CPython, the wheel, templates, static)
```

PyInstaller is pinned in the `package` extra of `pyproject.toml`
(`pyinstaller==6.16.0`), which is also what fixes the `_internal\` layout the
installer relies on.

The installer packages that tree; it does not replace it. Local build:

```powershell
python -m PyInstaller --clean --noconfirm packaging\wattracker.spec
$version = python packaging\_version.py
ISCC.exe "/DAppVersion=$version" packaging\wattracker.iss
```

`ISCC.exe` comes with Inno Setup 6. Nothing assumes it is already on the
machine: CI installs a pinned compiler itself (see **CI** below).

### Version

Git tags are the only version source. `setuptools-scm` reads `vX.Y.Z` release
tags and `preview-X.Y.Z` history tags; commits after a tag receive a PEP 440
version that includes their distance and Git node. The generated
`wattracker/_scm_version.py` supplies `wattracker.__version__` and the connector
version. `packaging/_version.py` reads the same setuptools-scm configuration for
the wheel, PyInstaller specs and Inno Setup define.

Inno receives the full PEP 440 version through `/DAppVersion=`. The macOS plist
and Windows numeric resource fields normalize that value to numeric components
because those formats do not accept PEP 440 suffixes. `OutputBaseFilename` is
built from the Inno define, so the installer name and displayed version agree.

## Installer choices

- **Inno Setup**, not MSIX and not WiX. MSIX wants a packaged-identity signing
  chain and a containerised filesystem view that the `~/.wattracker` data
  directory would have to be redesigned around; WiX is a heavier toolchain for
  what is a file copy and one shortcut.
- **Per-user install** to `{localappdata}\Programs\wattracker` with
  `PrivilegesRequired=lowest` and no elevation override. No UAC prompt, no
  Program Files ACL problems, and the app only ever writes into the same user's
  profile anyway.
- **A Start Menu group holding the app and its uninstaller, and nothing else.**
  No desktop shortcut, no `[Run]` section, no run-at-startup entry;
  `DisableProgramGroupPage=yes` because there is one right group name.
  `tests/test_windows_installer.py` fails if the phrase "run at startup" appears
  in the script.
- **The shortcut does not point at `wattracker.exe`.** It runs
  `scripts\wattracker.ps1 -Action start -OpenBrowser` through
  `powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden`, taking its
  icon from `wattracker.exe`. That launcher is shipped by the installer as a
  second `[Files]` entry and is what makes the frozen app usable from a
  double-click: it starts the server hidden with stdout and stderr redirected to
  `wattracker.log` / `wattracker-error.log` in the data directory, polls
  `/login` and only opens the browser once it answers 200. `console=True` in the
  spec is unchanged - running `wattracker.exe` directly still gives you a console
  and uvicorn's logging - but a user who clicks the Start Menu entry does not get
  a console window they must leave open.
- `ArchitecturesAllowed=x64compatible` (and `ArchitecturesInstallIn64BitMode`
  likewise), which the pinned 6.7.3 compiler supports.
- No firewall exception is created, ever. `docs/windows-security.md` requires
  the supported configuration to stay loopback-only, and
  `tests/test_windows_installer.py` fails if the word `firewall` shows up
  anywhere in the script. The launcher enforces the other half: it refuses any
  `WATTRACKER_HOST` that is not `127.0.0.1`, `localhost` or `::1`.

### Stopping the running app is the installer's hardest problem

`CloseApplications=no` and `RestartApplications=no`: the Restart Manager is
explicitly *not* used. Instead `[Code]` calls the installed launcher -
`PrepareToInstall` before any file is replaced, `InitializeUninstall` before any
file is removed - and aborts with a message if it cannot stop the app cleanly.

The reason is that everything else on Windows stops processes by name, port
owner or window: `taskkill`, `Stop-Process -Name`, `Get-NetTCPConnection`. All of
those can hit an unrelated process, and `tests/test_windows_installer.py`
asserts that none of them appear in the script, the smoke test or the workflow.
The launcher instead records a state file in the data directory
(`wattracker-process.json`) holding the PID, its creation time, the resolved
executable path, the port, and a random `--wattracker-managed=<32 hex>` marker it
put on the child's own command line. Before terminating anything it re-reads the
live process's creation time, executable path and command line and requires every
recorded field to match exactly, then kills through the
`System.Diagnostics.Process` handle it already holds. State that is malformed,
tampered with, or points at a PID that has been reused fails closed: the
installer and uninstaller stop, and say so, rather than replacing or deleting
files. `docs/windows-security.md` states the same contract from the security
side.

## Data, upgrades and uninstall

The data directory is **not** part of the install. `wattracker.config` resolves
it as `WATTRACKER_DATA_DIR` if set, otherwise `~/.wattracker` - on Windows,
`%USERPROFILE%\.wattracker`. It holds `wattracker.db` (rides, plans, goals,
password hashes, encrypted credential markers), `config.json` (session secret,
LLM API key), `backups/`, and the launcher's `wattracker-process.json` and
logs.

**Upgrade.** A fixed `AppId` GUID makes a reinstall an upgrade rather than a
second side-by-side installation, and `[Files]` uses `ignoreversion` so every
payload file is overwritten regardless of embedded version resources. There is
no `[InstallDelete]`: a file that existed in an older build and no longer exists
in the new one is *not* removed from an existing install. For the `_internal\`
tree that is a real risk - a stale `.pyd` or `.dll` from a different PyInstaller
build can be left behind - and the mitigation today is the exact PyInstaller pin,
not the installer. The data directory is untouched either way.

On first launch of the new version, `db.init_db` upgrades the schema **in
place**: it refuses outright to touch a database whose `user_version` is newer
than the code's `SCHEMA_VERSION` (currently 31) - running an old wattracker
against a new database is the failure that has twice wiped tables - and before
applying any migration it writes a `pre-migration` snapshot through
`backup.create_backup` and aborts if that snapshot cannot be written. So an
upgrade that goes wrong has a recovery anchor in `<data dir>\backups\`.
Downgrades are not supported: the old build will refuse to start against the
migrated database, by design. In-place migration is only for a version the
chain actually covers; a database too old to migrate is dropped and recreated,
which is why the version this ships against matters.

**Uninstall removes what it installed and nothing else.** There is no
`[UninstallDelete]` and no `[UninstallRun]`, and the script never names the data
directory or calls `Remove-Item`; the last three are asserted by
`tests/test_windows_installer.py`. There is deliberately no "also delete your
data?" prompt: an uninstaller that can delete rides is an uninstaller that will
eventually delete rides unattended, and `packaging/smoke_installer.ps1` asserts a
sentinel file in the data directory survives both an upgrade and a full
uninstall. Removing the data is a manual `rmdir` the user does knowingly.

The counterpart assertion is that uninstall really does clean up what it owns:
the same smoke test requires `wattracker.exe`, the Start Menu shortcut and the
`HKCU` uninstall registry key to be gone afterwards, and requires a *blocked*
uninstall (tampered launcher state) to have removed none of them.

## CI

The installer is built by the `package-unsigned` job in
`.github/workflows/windows.yml`, on GitHub-hosted **`windows-latest`**, in this order:

1. check out all history and tags, set up Python **3.12**, assert the runner
   carries no leftover install state,
   create a `.venv` **in the workspace** and install everything into it;
2. **install a pinned Inno Setup compiler.** `innosetup-6.7.3.exe` is downloaded
   from a pinned `github.com/jrsoftware/issrc` release URL, its SHA-256 compared
   against a literal in the workflow, and its Authenticode signature required to
   be `Valid` with signer simple name `Pyrsys B.V.` before it is executed. It is
   then installed `/VERYSILENT /CURRENTUSER` into `$RUNNER_TEMP\Inno Setup 6` and
   that directory appended to `GITHUB_PATH`. Nothing about the runner image is
   assumed, and a compromised or substituted compiler fails the job rather than
   silently building the installer;
3. `tests\windows\lifecycle.ps1`, the PowerShell launcher-safety test;
4. build the wheel and smoke the *installed* wheel in its own venv
   (`packaging/smoke_installed.py`);
5. freeze the onedir tree and smoke it (`packaging/smoke_frozen.ps1`);
6. read the version, run `ISCC.exe /DAppVersion=...`, and **throw if the setup
   file is not on disk** - a compiler that fails quietly must not reach upload;
7. smoke the installer itself (`packaging/smoke_installer.ps1`): install to a
   temp directory with spaces in the path, start through the launcher, assert
   `/login`, `/static/style.css` and `/register`, reinstall over the top,
   attempt an uninstall with tampered state and require it to fail, then
   uninstall for real;
8. assert that the setup exe and frozen connector exist, then upload those two
   payloads **only on a push to `main`** for the publisher job; the required
   handoff expires after one day.

The digest and publisher checks in step 2 have their own paragraph in
`docs/windows-security.md`, and `tests/test_windows_installer.py` pins the
version, the digest, the hash command and the publisher string so they cannot be
loosened without a test change.

### Rolling preview

On pushes to `main`, this workflow builds the Windows setup and connector in the
existing `package-unsigned` job and builds the macOS DMG in `macos-preview`. The
`publish-preview` job downloads only those three release payloads, writes
`SHA256SUMS.txt`, and replaces the `preview-latest` pre-release. The
cross-runner handoff is required and retained for one day; if Actions artifact
storage is unavailable, publication fails visibly.

### Historical: yielding the box to a hardware session

This operational material describes the persistent Windows machine used before
this job moved to GitHub-hosted `windows-latest`; it is retained for reference
and is not a current CI requirement.

The runner shares a physical machine with the trainer and Zwift, so a build can
begin in the middle of a ride. Three steps - the dependency install, the wheel
build, and the freeze plus installer compile - therefore start with

```powershell
(Get-Process -Id $PID).PriorityClass = 'BelowNormal'
```

so that PyInstaller and the Inno Setup LZMA compress, which each saturate every
core for minutes, yield to Zwift. Children inherit the class, so the whole build
tree is covered. `BelowNormal` rather than `Idle` is deliberate: at `Idle` the
runner's heartbeat can starve under sustained load and the job is reported lost.

This does **not** lower I/O priority, which is a separate axis on Windows and
cannot be set on another process from outside it, so a build still competes for
the disk.

**It does not replace stopping the runner** - see "Stopping the runner for a
trainer session" below, which remains the operational contract and is the only
thing that removes the contention rather than merely deprioritizing it. The
priority drop is what limits the damage on the ride where somebody forgets, and
for a session where ERG latency is being measured rather than merely ridden,
forgetting is still a spoiled measurement.

Nothing in the job touches Bluetooth, so the trainer link itself is never
contended, and every script binds a dynamically chosen free loopback port rather
than a fixed one, so a live session's server is never collided with either.

The three packaging smokes also set `WATTRACKER_AUTO_SCAN=0`;
`tests/windows/lifecycle.ps1` does not, and does not need to. That flag governs
the daily *activity-file* scan rather than a BLE scan, and lifecycle runs against
a throwaway `WATTRACKER_DATA_DIR` with no Zwift folder configured, so the scan
has nothing to walk.

### Historical: the former self-hosted runner

GitHub-hosted minutes are exhausted and `windows-latest` is billable on a private
repository, which is what kept this job gated. Self-hosted runners are not
billed, which is how the `Cloud` workflow's suite has been running on a
self-hosted macOS runner all along; `package-unsigned` now takes the same route.

**A self-hosted Windows box is not `windows-latest`.** Three things the hosted
image provides had to be provided here instead, and every one of them surfaced
in the first two runs' setup steps, before anything the installer job exists to
verify could be reached. They share a shape worth naming: the hosted image runs
jobs as an administrator with a fully provisioned toolchain, and this runner
deliberately does neither.

- **PowerShell 7, installed from the MSI.** Five steps declare `shell: pwsh`,
  which is PowerShell Core, not the `powershell.exe` 5.1 that Windows ships. It
  is preinstalled on `windows-latest`; on a fresh runner `pwsh` is simply not
  found. Install it machine-wide from an elevated shell:

  ```powershell
  winget install --id Microsoft.PowerShell --installer-type wix --scope machine
  ```

  `--installer-type wix` is load-bearing and not obvious. Without it winget
  selects the *MSIX bundle* and `--scope machine` becomes a provisioning
  operation, which fails `0x80070005` even elevated. More to the point it would
  be the wrong build if it succeeded: the packaged version surfaces `pwsh.exe`
  through a per-user App Execution Alias under
  `%LOCALAPPDATA%\Microsoft\WindowsApps`, so the runner account would still not
  find it on PATH - the same trap as this machine's per-user Python 3.12, which
  is invisible to `wattracker-ci` for exactly the same reason. The `wix` entry is
  the machine-wide MSI, which installs to `C:\Program Files\PowerShell\7` and
  edits the *system* PATH. **Restart the runner service afterwards**: a service
  inherits its environment at start, so `pwsh` stays not-found until it does.

  Do not answer this by rewriting the steps to `shell: powershell`. They would
  *mostly* survive 5.1, but the Inno Setup step appends to `$env:GITHUB_PATH`
  with `Out-File -Encoding utf8`, and 5.1 writes a BOM there while 7 does not. A
  BOM in `GITHUB_PATH` corrupts the entry it prefixes, and the symptom -
  `ISCC.exe` not found, two steps later - points nowhere near the cause. Matching
  the hosted image is also the point of the exercise.
- **Python 3.12, installed machine-wide, and no `actions/setup-python`.** The
  action's Windows path is not the tool-cache unpack it looks like:
  `installers/win-setup-template.ps1` in `actions/python-versions` runs the
  official installer with `InstallAllUsers=1` (or `ALLUSERS=1` for the MSI) and
  first clears keys under
  `HKLM\...\Installer\UserData\S-1-5-18\Products`. It requires administrator,
  full stop - on the hosted image that is free because the runner account is one.
  Here it is not, and it must not become one: admin on this account is exactly
  the isolation the installer smoke test relies on. So the job dropped the action
  and the runner carries a machine-wide CPython 3.12 instead
  (`winget install Python.Python.3.12 --scope machine`), which is on the system
  PATH and therefore visible to the service account.

  Machine-wide, not per-user. A per-user install under
  `%LOCALAPPDATA%\Programs\Python` belongs to the account that ran it and is
  invisible to `wattracker-ci` - the same trap as the MSIX PowerShell above,
  and worth stating twice because it is the single most repeated mistake in
  setting this machine up.

  The trade is that the interpreter is now a property of the box rather than
  something `windows.yml` pins, which is why the job's first step asserts the
  version and prints what it found. `tests/test_windows_installer.py` pins both
  halves - that the action is absent, and that the assertion is present - so
  neither can be quietly dropped.

- **A script execution policy the runner account can use.** Windows client
  defaults to `Restricted` with every scope `Undefined`, and
  `actions/setup-python` runs its own `setup.ps1` through a `powershell` call
  that does *not* pass `-ExecutionPolicy`, so it dies with
  `UnauthorizedAccess`. The runner passes `-ExecutionPolicy Unrestricted` when
  it launches a step's own script, which is why our steps look fine and this
  still fails; the same gap bites `powershell -File packaging\smoke_installer.ps1`
  and `smoke_frozen.ps1`, which spawn fresh processes from inside a step.
  Set it for the runner account only, not `LocalMachine` - the CI account's hive
  is loaded whenever the service is running:

  ```powershell
  $sid = (Get-LocalUser wattracker-ci).SID.Value
  $key = "Registry::HKEY_USERS\$sid\Software\Microsoft\PowerShell\1\ShellIds\Microsoft.PowerShell"
  New-Item -Path $key -Force | Out-Null
  Set-ItemProperty -Path $key -Name ExecutionPolicy -Value RemoteSigned
  ```

  `RemoteSigned`, not `Bypass`: everything the job runs is either in the checkout
  or written by a step, so nothing needs the weaker setting, and a downloaded
  script still has to be signed to run.

**`windows-release.yml` and the `test` job in `windows.yml` are still gated.**
The `test` job is gated because the full suite already runs on the `Cloud`
workflow's self-hosted macOS job every push and pull request; duplicating ~6
minutes of it on hosted Windows buys little for the wall time it costs. The
signed release workflow is tag-only and remains gated until its code-signing
certificate and secrets are available. The former physical runner carried only
the installer job, because it was the only part that could not run elsewhere.

**The runner service runs as a dedicated non-admin local account**
(`wattracker-ci`), not as the developer's. This is the least obvious decision in
the setup and the easiest to undo by accident, so: `packaging/smoke_installer.ps1`
installs to a temp directory via `/DIR=`, but two things it registers cannot be
redirected anywhere.

- The **Start Menu group name is fixed at compile time.** `wattracker.iss` sets
  `DisableProgramGroupPage=yes`, and Inno's documentation is explicit that
  `/GROUP` "is ignored" when it is.
- The **uninstall key is always `{AppId}_is1`**, and `AppId` is additionally
  "checked by subsequent installations to determine whether it may append to a
  particular existing uninstall log."

Both are per-user. Under one account, a real wattracker install on the same
machine would have its uninstall key repointed at the temp-directory uninstaller
and then deleted outright when the smoke test uninstalls - silently orphaning it.
A separate account puts those paths in a different hive entirely. **If you ever
reconfigure the runner to run as your own account, you re-arm this.**

The job also keeps a pre-flight step that fails if either path already exists.
That is not the same defence: it catches a run killed *between* install and
uninstall, whose leftovers would otherwise fail a later run somewhere much harder
to read.

Two smaller consequences of the runner being a real machine that persists:

- **Everything installs into a workspace `.venv`**, never into the interpreter.
  On a hosted runner the machine is discarded; here the tool cache outlives the
  job and pollution would accumulate run over run. `actions/checkout` runs
  `git clean -ffdx`, so the directory starts empty each time. The path is
  load-bearing beyond hygiene - `scripts/wattracker.ps1` resolves
  `<repo>\.venv\Scripts\python.exe` as its last-resort interpreter, which is what
  lets `tests\windows\lifecycle.ps1` start the app from source.
- **Runs serialize and are never cancelled.** The workflow sets a `concurrency`
  group with no `cancel-in-progress`, because cancelling skips
  `smoke_installer.ps1`'s `finally` block - the thing that uninstalls the product.
  A queued run is cheaper than a poisoned machine.

### Historical: stopping the runner for a trainer session

The runner shares a machine with the trainer setup, and a service-mode runner
picks up jobs whenever a pull request opens - including mid-ride. Before a
session:

```powershell
Stop-Service "actions.runner.poopaskoopa-wattracker.Windows-TT"
```

and `Start-Service` the same name afterwards. This is an operational contract,
not a suggestion: a PyInstaller build and a full install/uninstall cycle are not
what you want competing for the machine driving a trainer.

The service is named `actions.runner.<owner>-<repo>.<runner name>`, so it follows
the name the runner registered under rather than the machine's;
`Get-Service "actions.runner.*"` finds it without guessing. The same object
confirms the isolation above - `(Get-CimInstance Win32_Service -Filter "Name LIKE
'actions.runner%'").StartName` must read `.\wattracker-ci`, not
`NT AUTHORITY\NETWORK SERVICE`.

## Historical: what the first self-hosted run observed

Run
[32332933878](https://github.com/poopaskoopa/wattracker/actions/runs/32332933878),
on the former self-hosted runner, was the first time any of this executed. Recorded
here as observations, with what was actually seen - not as "the job is enabled
now".

- **`packaging/wattracker.iss` compiled.** `ISCC.exe` reported
  `Compiler engine version: Inno Setup 6.7.3`, then
  `Successful compile (28.094 sec)`, producing
  `dist\wattracker-0.1.0-windows-x64-unsigned-setup.exe`. No syntax error, and
  `ArchitecturesAllowed=x64compatible` was accepted rather than rejected - it
  had been valid-in-6.7.3-but-unverified until this run.
- **The `[Code]` section compiled.** `Reading [Code] section` is followed by
  `Compiling [Code] section` with no diagnostic between them, so the Pascal
  Script parses. Note what this does and does not establish: the section
  *compiles*. `PrepareToInstall` and `InitializeUninstall` running is evidenced
  by the smoke test below, not by these lines.
- **The installer installed, upgraded over itself, refused a tampered
  uninstall, and uninstalled.** `packaging/smoke_installer.ps1` ran to
  completion and the step exited 0, which means its whole sequence passed - the
  log shows the app answering at `http://127.0.0.1:50792/login` twice under
  different PIDs, the second being the reinstall over the top.
- **The pinned-compiler provenance check ran and passed.** The 6.7.3 asset still
  matches the pinned SHA-256 `9c73c3ba...97b732`, and the Authenticode signer's
  simple name still renders exactly `Pyrsys B.V.` - both are `-cne` comparisons,
  so passing means byte-exact, not merely similar. The step now prints both
  observed values rather than proving them by silence, so a future run answers
  the question directly instead of leaving it to be inferred from a green tick.
- **The account isolation held.** After the run, the developer account had no
  `HKCU` uninstall key and no Start Menu group, and `~/.wattracker/wattracker.db`
  was byte-identical to the pre-run capture (4096 bytes, SHA-256
  `42F67CDE...6302`). Checked, not assumed - the same standard
  `docs/macos-packaging.md` holds its smoke test to.

Still unobserved, and unchanged by the above:

- **The artifact has never been uploaded.** `actions/upload-artifact` matched
  all three files and then failed on `Artifact storage quota has been hit` -
  the same account-level billing wall that gated this workflow to begin with.
  The build products exist and are correct; nothing has retrieved them from the
  runner.
- **The installer is unsigned and there is no path that signs it.** Even after
  the signing story in `docs/windows-security.md` works, it produces a signed
  *zip*; SmartScreen judges the thing the user double-clicks, and that is the
  setup exe. Wrapping signed binaries in an unsigned installer is the worst of
  both worlds, and it is what would ship today.
- **SmartScreen reputation is not signing.** A brand-new certificate earns a
  warning on first downloads regardless; only accumulated reputation (or an EV
  certificate) removes it.
- **No icon.** `SetupIconFile` is unset and the spec passes no `icon=`, so setup,
  the shortcut and the taskbar all show PyInstaller's default rather than
  anything of wattracker's.
- **BLE is not covered by the packaging path at all.** `windows-release.yml` has
  a `smoke_frozen_ble.py` step; the installer job does not, and neither proves a
  radio works. That is `docs/windows-ble-validation.md`, which is a manual
  checklist on physical hardware.
- Like the rest of the workflows, GitHub-authored actions are referenced by
  moving major tags rather than pinned commit SHAs.
