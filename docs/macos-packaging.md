# macOS packaging

wattracker ships on macOS as `wattracker.app` inside a DMG. The whole pipeline
is one command:

```sh
packaging/build-macos.sh
```

That creates a throwaway build venv under `build/`, runs the test suite, freezes
the app, signs it, smoke-tests the signed bundle, and writes
`release/wattracker-macos-<arch>.dmg` plus a `.sha256`. Set
`WATTRACKER_BUILD_PYTHON` if `python3` on PATH is older than 3.10 (macOS
frequently has a 3.8 in `/Library/Frameworks` ahead of everything else).

## What gets built

`packaging/wattracker.spec` is one spec shared by Windows and macOS. The
Analysis - entry point, the `web/templates` and `web/static` trees, the uvicorn
and keyring hidden imports - is deliberately common, because a missing data tree
or hidden import is the failure mode that packaging actually has, and two specs
would drift. Only the genuinely per-OS parts branch:

| | Windows | macOS |
|---|---|---|
| bleak backend collected | `bleak.backends.winrt` | `bleak.backends.corebluetooth` |
| output | `dist/wattracker/wattracker.exe` | `dist/wattracker.app` (plus the same onedir) |

PyInstaller is pinned in the `package` extra of `pyproject.toml`
(`pyinstaller==6.16.0`) so the Windows and macOS release builds cannot drift.

### Info.plist choices

- `CFBundleIdentifier` is `com.wattracker.wattracker`.
- `CFBundleShortVersionString`/`CFBundleVersion` are read out of
  `pyproject.toml` by the spec, so the bundle version cannot drift from the
  wheel version.
- **`LSUIElement` is true.** wattracker runs a uvicorn server and hands the user
  to their browser; it has no Cocoa event loop. A regular Dock-icon app that
  never answers AppKit gets flagged "Application Not Responding" and the only
  Dock affordance becomes a Force Quit. An agent app has no Dock icon and no
  unresponsive-app UI, and the browser tab is the real interface. PyInstaller
  additionally sets `LSBackgroundOnly` because the inner executable is built
  with `console=True`.
- `console=True` is kept for macOS as well. It is what makes
  `dist/wattracker/wattracker` a usable CLI, and inside the `.app` macOS simply
  discards stdout when there is no terminal. The app only prints its URL before
  serving, so nothing depends on a readable console.
- `NSBluetoothAlwaysUsageDescription` is set. Without a purpose string macOS
  terminates the process the instant CoreBluetooth is touched, so the ride page
  would kill the app rather than prompt.

### Quitting

Because the app is `LSUIElement`, there is no Dock icon and no menu bar. Quit it
from Activity Monitor, or `pkill -f wattracker.app`. This is a known rough edge;
a proper fix is a menu-bar item or a `/quit` route, neither of which exists yet.

## Signing

`packaging/sign-macos.sh <path>` has two paths, selected by
`WATTRACKER_MACOS_SIGNING_IDENTITY`:

**Ad-hoc (the default, and all this repository can produce today).**
`codesign -s -` gives the bundle a valid signature bound to no identity. It is
enough for macOS's exec-time signature validation and for running the app on the
machine that built it. It does **not** satisfy Gatekeeper: a user who downloads
the DMG gets "wattracker is damaged and can't be opened" or a quarantine block,
and has to clear the quarantine attribute by hand
(`xattr -d com.apple.quarantine`). Ad-hoc is a build-integrity measure, not a
distribution measure.

**Developer ID.** Set `WATTRACKER_MACOS_SIGNING_IDENTITY` to the identity name
and the script signs inside-out (every nested `.dylib`/`.so` first, the bundle
last) with `--options runtime --timestamp` and
`packaging/macos-entitlements.plist`, verifies with
`codesign --verify --deep --strict`, and asserts the hardened-runtime flag
actually landed. It fails closed: an identity that is set but broken aborts
rather than quietly degrading to ad-hoc, matching `sign-windows.ps1`'s refusal
to emit an unsigned artifact.

The entitlements are the minimum a frozen CPython is known to need -
`allow-unsigned-executable-memory` for ctypes/libffi trampolines and
`disable-library-validation` because keyring's Keychain backend and bleak reach
system frameworks through ctypes/pyobjc.

### Notarization

Once an identity is set, the script notarizes when notary credentials are
present, and prints a loud warning when they are not:

- `WATTRACKER_MACOS_NOTARY_PROFILE` - a profile created out of band by
  `xcrun notarytool store-credentials`, which prompts for the password on a tty.
  Preferred for local releases; the secret stays in the keychain.
- `WATTRACKER_MACOS_NOTARY_KEY_PATH` + `WATTRACKER_MACOS_NOTARY_KEY_ID` +
  `WATTRACKER_MACOS_NOTARY_ISSUER` - an App Store Connect API key. The only
  secret is the `.p8` file; the key id and issuer are identifiers. This is what
  CI uses, because a keychain profile cannot be provisioned non-interactively.

There is deliberately **no** `--apple-id/--team-id/--password` form. `notarytool`
accepts an app-specific password only on its argv, and argv is readable by every
process on the machine (`ps -ww`) for the entire lifetime of a `--wait`
submission. `docs/windows-security.md` holds `sign-windows.ps1` to exactly this
bar when it refuses to put the PFX password on a child process command line, so
the macOS script is held to it too.

It archives the bundle with `ditto -c -k --keepParent` (notarytool refuses a
bare `.app`, and plain `zip` mangles the symlinked framework layout and
invalidates the signature before Apple ever sees it), runs
`xcrun notarytool submit --wait`, then staples the ticket into the `.app` so the
DMG built afterwards carries it, and finally asserts with `stapler validate` and
`spctl --assess`.

## Smoke test

`packaging/smoke_frozen_macos.py` is the macOS counterpart of
`smoke_frozen.ps1`, and shares its HTTP assertions with the installed-wheel
smoke test through `packaging/smoke_http.py`.

It launches the frozen app **twice**:

1. `Contents/MacOS/wattracker` as a plain child process, which gives a real pid
   and captured stdout/stderr;
2. `open` on the bundle, which goes through LaunchServices exactly as a
   double-click in Finder does - no tty, no inherited shell environment, the
   `Info.plist` actually consulted. This is the path the `BUNDLE` step exists
   for, so it is the one worth proving. Environment isolation still works here
   because `open --env` is available on current macOS.

Each run then asserts more than a 200: the login page's rendered markup, real
CSS from `/static/style.css`, both vendored chart bundles, the register form,
and - the macOS-specific check - that `/settings` reports the **system
keychain** backend. keyring resolves its backend by runtime discovery, so a
`keyring.backends.macOS` that failed to package degrades silently to the
file-key fallback; no HTTP status code would reveal that, but the settings page
does. It also reports whether the ride page sees Bluetooth, which is how a
missing CoreBluetooth backend shows up.

**Data safety.** Every run gets a fresh temporary directory and overrides `HOME`
as well as `WATTRACKER_DATA_DIR`, `WATTRACKER_DB` and
`WATTRACKER_ACTIVITIES_DIR`; all inherited `WATTRACKER_*` variables are stripped
from the child environment first. The port is kernel-assigned, never the default
8000, so a wattracker already running on the machine is never disturbed. As a
backstop the real `~/.wattracker/wattracker.db` size and mtime are captured
before the run and compared after; a mismatch fails the smoke test.

## CI

`.github/workflows/macos-release.yml` mirrors `windows-release.yml`: tag-push
only, protected `macos-code-signing` environment, full test suite, wheel smoke,
frozen build, sign, smoke the *signed* artifact, DMG + checksum, upload. It is
hard-disabled with `if: ${{ false }}` for exactly the same reason as every other
job here - Actions is blocked at the account level and macOS runners are
billable on a private repo at a 10x multiplier - so the real gate is a local
`packaging/build-macos.sh`.

The Developer ID key is imported in the workflow rather than in
`sign-macos.sh`, into a dedicated keychain created and deleted on the runner.
Locally the identity already lives in the developer's login keychain and the
script must not touch it. Configure:

- secrets `WATTRACKER_MACOS_SIGNING_P12_B64` and
  `WATTRACKER_MACOS_SIGNING_P12_PASSWORD`;
- variable `WATTRACKER_MACOS_SIGNING_IDENTITY`;
- secrets `WATTRACKER_MACOS_NOTARY_KEY_B64` (base64 of the App Store Connect
  `.p8`), `WATTRACKER_MACOS_NOTARY_KEY_ID`, `WATTRACKER_MACOS_NOTARY_ISSUER`.

The notary key is written to `$RUNNER_TEMP/notary.p8` under `umask 077` and
removed by a trap in the same step. Secrets are scoped to the steps that need
them: checkout, dependency installation, tests, the frozen build, the smoke
test, packaging and upload never see them.

**Residual, and it is not fixable in shell:** `security import` accepts the p12
passphrase only through `-P` - its only alternative is an interactive GUI
prompt, which a headless runner cannot answer - so that single argv exposure
remains. It is one short-lived `security` invocation on a single-tenant
ephemeral VM. The real fix is hosted signing with a non-exportable key (the
macOS analogue of the Azure Trusted Signing path `windows-security.md`
recommends), not a shell workaround. Notarization, which had the same problem
and *was* fixable, has been moved off argv entirely.

## Known gaps

- **Nothing beyond ad-hoc signing has been executed.** There is no Developer ID
  certificate and no Apple Developer account attached to this repository, so the
  hardened-runtime signing, the entitlements file, the notarization submission,
  the stapling, and the CI keychain import are all written but unrun. Treat the
  first real Developer ID build as a debugging session, not a release.
- **Ad-hoc artifacts are not distributable.** A downloaded DMG will be
  quarantined. Do not describe an ad-hoc build as signed in any user-facing
  sense.
- **arm64 only.** The build produces a single-architecture app for the machine
  it runs on; there is no universal2 binary, so an Intel Mac needs its own
  build. Making it universal means universal2 wheels for numpy/scipy/pandas and
  a `target_arch="universal2"` EXE, which has not been attempted.
- **No icon.** `icon=None`, so the app gets the generic macOS placeholder. It is
  `LSUIElement`, so this is only visible in Finder and Activity Monitor.
- **No quit affordance.** See "Quitting" above.
- **Bluetooth permission is unverified.** The purpose string is set, but the TCC
  prompt and an actual trainer connection from inside the `.app` have not been
  exercised - the smoke test only proves bleak and its CoreBluetooth backend
  import.
- **The `open --env` isolation is macOS-version dependent.** It works on current
  macOS; on an older release without that flag the LaunchServices half of the
  smoke test would run against the real `HOME`, which is why it must never be
  weakened into a non-isolated fallback.
- Like the Windows workflow, GitHub-authored actions are referenced by moving
  major tags rather than pinned commit SHAs. That residual supply-chain risk
  should be closed before any production release.
