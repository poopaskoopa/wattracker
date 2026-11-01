# android/ — agent working agreement

This directory's own working agreement. The repo-root `AGENTS.md` still governs
everything repo-wide — branch ownership, fetch-before-rebase, commit identity,
worktrees, integration, the work queue, schema changes, and the live server/DB.
This file adds the Android-specific rules and the machine/tooling facts that
belong with this directory. `plan.md` is the step-by-step plan; `README.md` is
what the plan assumes of the machine around it. Neither repeats the rules here.

## Golden rules
1. **ASD-STE-100 in all communication** — keep comments and commit messages short. 
   Use US English.
2. **Test in the simulator first** — ask the user before using any connected
   devices.
3. **Wait for review before PR** — an internal review will be conducted beforehand.

## Tooling: Windows, PowerShell, `gh`

**Use the `cmd` shell for `gh` calls.** The IDE's shell tool runs
`powershell.exe -Command <cmd>`, and PowerShell's native-argument handling and
stdout capture both get in the way: arguments pass through unharmed in `cmd`,
and `>` writes `gh`'s raw UTF-8 bytes to a file.

- **Keep `--jq` trivial in PowerShell** — a bare `--jq .field`, or no `--jq`
  at all with `--json a,b,c`. A double-quoted jq expression arrives mangled
  (one session turned `--jq "title, headRefName"` into a PowerShell
  expression). Parse the JSON output yourself if you need more.
- **Never capture `gh` output containing non-ASCII into a PowerShell
  variable.** Native stdout is decoded with the console code page, so a PR
  body comes back mojibake (`WΓÇ▓` where `W′` was). Redirect to a file in
  `cmd`, then process it with .NET UTF-8:
  `[System.IO.File]::ReadAllText($p, [Text.Encoding]::UTF8)` and
  `WriteAllText(..., New-Object Text.UTF8Encoding($false))` (no BOM), then
  `gh pr edit N --body-file body.txt`.
- **`gh pr edit --body-file` must be given markdown, not a `--json body` dump.**
  Dumping `--json body` and feeding it back writes the literal JSON envelope
  (`{"body":"…"}`) as the PR body, which GitHub renders as one escaped line.
  Always hand `gh pr edit` a plain `.md` file.
- **`;` is the separator in PowerShell, `&` in `cmd`** — neither stops on
  failure. Verify intermediate results when a later step depends on an earlier
  one; a failed `git reset` still lets a queued `git commit` run and stacks
  junk on the old history.
- **`String.Replace` that swaps same-length text cannot be verified by
  comparing lengths** — check with `Contains`.
- **Multi-paragraph commit messages:** write the message to an LF-terminated,
  BOM-less file and use `git commit -F file`. Repeated `-m` paragraphs are
  stored as single unwrapped lines (hundreds of characters long), not wrapped.

**Disable the git pager.** The shell runs in a pseudo-terminal, so `git show`,
`git log`, `git diff` and friends open the pager and hang until the command
timeout kills them. Always prefix `git --no-pager`, or set `GIT_PAGER=` to
empty for the session.

## Setting up this machine

Windows x64. Install, in order:

1. **Android Studio** (the current stable line with built-in Agent mode) —
   Windows `.exe` from `https://developer.android.com/studio`; accept all
   licenses in the SDK Manager.
2. **A JDK** for the CLI (AGP floor; a newer one if the installed AGP demands
   it) — Temurin via a package manager or `https://adoptium.net`. Android
   Studio also bundles a JBR for its own use; Gradle on the CLI needs a JDK on
   `PATH`.
3. **SDK components:** Android SDK Platform 36, Build-Tools (latest stable),
   Platform-Tools, Emulator, and the `google_apis;x86_64;36` system image.
   Verify with `sdkmanager --list_installed` and `adb version`.
4. **A BYOK API key** (owner-supplied) for the IDE agent — a Google AI Studio
   key for Gemini, or an Anthropic/OpenAI key. It goes into IDE-local settings
   only (see "IDE agent" below). It is a credential: never in the repo, never
   in `.kilo/`, never in a PR description or commit message.

## The dev loop

The repo's `.venv` already works; on Windows the interpreter is
`.venv\Scripts\python.exe`. For the cloud harness it additionally needs
`.venv\Scripts\python.exe -m pip install -e '.[cloud]'`.

**Local desktop server** (the rich local backend). `./start.sh` is POSIX — run
it under Git-Bash/WSL, or start the server directly:

```powershell
$env:WATTRACKER_PUBLIC_HOSTS = "10.0.2.2"
.\.venv\Scripts\python.exe wattracker\server.py
```

**The bind stays on loopback for the emulator path** — one variable, not two.
`10.0.2.2` is the emulator's NAT alias for the *host's* loopback, so a
`127.0.0.1`-bound server is already reachable; only the Host-header allowlist
needs the name, because the server 400s any `Host` it does not answer to.

- emulator, debug build → `http://10.0.2.2:8000`, under the `debug` source
  set's NSC exception;
- physical device, or any release-shaped run → `https://<hostname>` through the
  rider's TLS terminator (`tailscale serve` or a reverse proxy — either), with
  that name in `WATTRACKER_PUBLIC_HOSTS` and `WATTRACKER_COOKIE_SECURE=1`. A
  wider bind (`WATTRACKER_HOST` + `WATTRACKER_ALLOW_NON_LOOPBACK=1`) is needed
  **only** if the terminator runs on a different machine than the server.

Pair the phone as a device in the web UI (Settings → connector pairing) to
obtain a token.

**Cloud dev harness** (the cloud backend in debug; on `main` since PR #240):

```powershell
.\.venv\Scripts\python.exe scripts\walking_skeleton_server.py --user-id <id>
```

Loopback by default, in-memory store, full snapshot published, prints pairing
code(s). Its `--lan` auto-detect is written for macOS and untested on Windows,
but a physical device needs neither it nor `--host 0.0.0.0`: use
`adb reverse tcp:8765 tcp:8765` and point the app at `http://localhost:8765`.
Keep the harness on its default loopback bind in both cases.

## IDE agent (Android Studio Agent mode + BYOK)

First-party IDE functionality: there is **no MCP server to register and no
plugin to install**, and no `.kilo/kilo.json` MCP block (`.kilo/` stays
untracked, local-only).

1. **Settings → Tools → AI → Model providers.** For Gemini, "Get Gemini API
   key" opens Google AI Studio; paste the key into the **API key** field, tick
   the models to enable, Apply. For a third-party model (Anthropic, OpenAI, …),
   use the IDE's "using remote models" setup — add the provider's endpoint and
   key under the same screen.
2. **Agent mode honors `AGENTS.md`.** The repo-root agreement is visible to
   the IDE agent, so treat it as one more participant: it works in *this*
   checkout, so the worktree rule binds it — no push to `main`, no touching the
   other agent's worktree, and commits it makes carry this clone's noreply
   identity. It also honors this directory's `AGENTS.md`.
3. **The terminal stays the floor.** Everything the agent can do has a
   `gradlew` + `adb` equivalent, and that is what CI runs. Nothing in any Done
   criterion depends on the agent.
4. **Verify the loop before starting work:** the model picker shows an enabled
   BYOK model and Agent mode is on; instruct the agent to build, deploy to
   `medium_phone`, pull a screenshot and read logcat; do the same three things
   once from the bare terminal.

Key handling: the key lives in IDE-local settings on one machine. It must never
reach the repo, a PR, a screenshot of Settings, or a log. Rotate it if it is
ever printed.

## Toolchain and device facts on this machine

**Environment.** Windows x64; the Android SDK is at
`C:\Users\Takazumi\AppData\Local\Android\Sdk` (see `local.properties`).

- **No JDK on `PATH`** — the only one is Android Studio's JBR,
  `C:\Program Files\Android\Android Studio\jbr`. Every `sdkmanager`,
  `avdmanager` and `android` invocation needs `$env:JAVA_HOME` set to it.
- **PowerShell has neither `JAVA_HOME` nor `HOME`**, and the new tools NPE
  without `$env:HOME` (`AvdManager.createInstance: parameter baseAvdFolder`
  is null). Set it per shell.
- `cmdline-tools` must be the newer build (16111833, in `cmdline-tools\latest`).
  Build 11076708 is too old for this SDK's package metadata ("only understands
  SDK XML versions up to 3") and its `avdmanager` NPEs.
- **`sdkmanager --licenses` ignores piped input** — feed the `y`s through a
  file: `cmd /c "sdkmanager … --licenses < ys.txt"`.
- Emulators run without KVM (AEHD/Hyper-V), so the first boot is slow. That is
  the emulator, not the app.

**AVDs are the new `android` CLI's size profiles, not device models.** There is
no `pixel_9`/`pixel_tablet`; the profiles are `small_phone`, `medium_phone`,
`medium_tablet`, `small_desktop`, `medium_desktop`, `large_desktop`
(`android emulator create --list-profiles`). The working set is `medium_phone`
(google_apis x86_64, API 36) and `medium_tablet` (API 35 — that is the only
x86_64 image the CLI pulls for it). `android emulator start` blocks until the
device is ready. Do **not** spend cycles on hand-written AVDs; emulator 37.1.11
cannot boot them (it falls back to the `arm` ABI and dies).

**Inspecting and capturing the UI.** Every on-disk capture path
(`adb exec-out screencap`, `adb shell screencap` + `pull`, `android screen
capture`) returns a stale lock frame on this host even with the lock screen
disabled — an AEHD/Hyper-V surface-capture quirk, not an app problem. Use:

- **Visual screenshot:** the IDE's `take_screenshot`, or the agent's `ui_state`
  (PNG of the focused window plus the XML hierarchy). This reads the emulator
  window directly. The IDE capture is ephemeral and cannot be attached by an
  agent, so PR images are attached by the owner.
- **UI hierarchy (scriptable, per-device):**
  `adb -s <serial> shell uiautomator dump /sdcard/ui.xml`, then
  `adb -s <serial> pull /sdcard/ui.xml out.xml`, and parse each `node`'s
  `text`, `content-desc`, `class`, `bounds`, `clickable`. This works per-serial
  (unlike `ui_state`, which only sees the IDE-attached device) and needs no
  surface capture.
- **Config measurement:** `adb -s <serial> shell dumpsys activity <package>` →
  `mCurrentConfig` (`smallestScreenWidthDp`, `orient=`, `ROTATION_`). Use this
  for the numbers, not pixels.
- **Forcing orientation:** `settings put system accelerometer_rotation 0` then
  `settings put system user_rotation <N>`; phone `0`=portrait, `1`=landscape;
  tablet (natural landscape) `0`=landscape, `1`=portrait. Re-apply with
  `am start -n <pkg>/.MainActivity`.
- **Keep the screen awake** for repeated captures:
  `settings put system screen_off_timeout 1800000; svc power stayon true`.

**Reaching the backend from a device.** `10.0.2.2` is the emulator's NAT alias
for the host's loopback, so a loopback-bound server is already reachable — only
the Host allowlist needs the name. A physical device needs no wider bind either:
`adb reverse tcp:8765 tcp:8765` over USB, then point the app at
`http://localhost:8765`. Never point an emulator at `127.0.0.1` (its own
loopback). `WATTRACKER_HOST` + `WATTRACKER_ALLOW_NON_LOOPBACK=1` are needed
**only** if the TLS terminator runs on a different machine than the server.

## Repo hygiene for this directory

- **Commit identity:** noreply address always, verified with
  `git config user.email` before the first commit. The repo is private but its
  history was rewritten once to strip a personal address, so treat every commit
  as publishable.
- **Fetch before you rebase or branch:** `git fetch origin` first. A
  remote-tracking ref is a cache; rebasing onto a stale one silently replays an
  old lineage and drops what landed in between.
- **Name a branch for the work it carries, cut from current `main`.** Never
  name a branch after a closed issue.
- **Install the pre-push hook:** `scripts/hooks/install.sh` is POSIX, so run it
  under Git-Bash/WSL (`bash scripts/hooks/install.sh`). It refuses direct pushes
  to `main` and refuses a rewrite that would strand remote commits.
- **Announce before starting an Android issue** — the Android epic (#192–#199,
  #259, #260) is taksmon's by GitHub assignee and is not on the root work
  queue.
- **Python suite green before any merge:** `.venv\Scripts\python.exe -m pytest`
  on Windows (`.venv/bin/python` on POSIX), run from the repo root.

## Build and verify

```
.\gradlew.bat :app:assembleDebug
.\gradlew.bat :app:testDebugUnitTest
.\gradlew.bat :app:assembleRelease -PallowPlaceholderHost
adb install -r app\build\outputs\apk\debug\app-debug.apk
adb shell am start -n com.wattracker.android/.MainActivity
```

`-PallowPlaceholderHost` suppresses **only** the placeholder-host guard. **#102
is closed (2026-10-27) and the cloud is deployed**, so the real authority must
be set: a Gradle property or `local.properties` `wattrackerCloudAuthority`. Do
not ship a build that points at `cloud.wattracker.example`. Retire the flag
once the real authority is in place. The guard covers `assembleRelease` **and**
`bundleRelease`.

**CI parity.** `.github/workflows/android.yml` gates every PR touching
`android/**` with `:app:assembleDebug`, `:app:testDebugUnitTest` and
`:app:assembleRelease -PallowPlaceholderHost` on hosted `ubuntu-latest`, no fork
gate. A red Android job is part of the Done evidence for every PR here. CI runs
no emulators. The workflow keeps the flag only until the real cloud authority
replaces the placeholder.

## Dependency rule

Only AndroidX, the Kotlin stdlib, and Room. No Retrofit/OkHttp/Koin/Coil and no
JSON, HTTP or DI library — the transport is `HttpURLConnection` and the JSON
model is in-project. `androidx.security:security-crypto` and its Tink tree are
**gone** (#457): the credential store is wrapped by an Android Keystore
AES-GCM key directly, so seeing `security-crypto` or any Tink artifact in
`./gradlew :app:dependencies` fails review. Any new third-party artifact comes
back to the owner first.

## Verification requirements

A PR is "ready to merge" only when:

1. `:app:testDebugUnitTest` is green and `:app:assembleDebug` assembles; the
   Android CI job is green.
2. **`./gradlew :app:dependencies` is audited** against the dependency rule.
3. **Test rules are proved by mutation locally, never on the PR branch.** Break
   the behavior, show the test go red, revert, and list the results in the PR
   body. Never push a deliberately broken state — merge-committed mutation
   commits land on `main`.
4. **Dev-loop evidence:** at least the build plus an install+launch through the
   IDE agent or the terminal `gradlew` + `adb` equivalents; record which BYOK
   provider/model was used.
5. **Say plainly whether it ran on a device or only in CI.** A screen that
   passes CI has not been shown to work. Screenshots/idiom checks are attached
   by the owner (the agent cannot attach PR images).
6. **State what could not be verified**, and why. Nothing here may claim
   Android-side behavior that only a device can settle — above all anything the
   Android Keystore does (key creation, non-exportability, the provider-chosen
   IV, the on-device round trip), which the JVM suite cannot reach and
   Robolectric is barred from.
7. **No signing material and no keys in the repository or the diff.** `*.jks` /
   `*.keystore` belong in `.gitignore`; the BYOK key lives in IDE-local settings
   only and must never appear in the repo, a PR, a screenshot of Settings, or a
   log.

## API ground truth for the current BOM — do not re-derive

Verified against the resolved jars; each cost several compile cycles:

- Foundation 1.10 **removed** the window-size-class / window-metrics APIs
  (`WindowWidthSizeClass`, `currentWindowWidthSizeClass`, and the
  `currentWindowMetricsInfo` replacement are absent from `foundation` *and*
  `ui`). The `smallestScreenWidthDp >= 600` predicate in `RootScreen.kt` is
  deliberate, not a shortcut.
- M3 1.4 drawer API: `PermanentNavigationDrawer(drawerContent, modifier,
  content)` — **no `gesturesEnabled`** (modal-only); `PermanentDrawerSheet`
  **has no `containerColor`** — paint the background on the sheet's content.
  `DismissibleNavigationDrawer` / `DismissibleDrawerSheet` are the new names
  alongside the modal pair.
- M3 1.4 `ColorScheme` constructors have **no default arguments** — build the
  scheme from `darkColorScheme(…)` with named overrides.
- material-icons-extended 1.7.x is KMP: **no drawable XMLs** in the AAR; icons
  are `ImageVector`s in `androidx.compose.material.icons.filled`. The plain
  `material-icons-extended-1.7.8.aar` Maven URL 404s — the KMP redirect makes
  the real artifact `material-icons-extended-android` (same for
  `material3-android`, `ui-android`, `foundation-android`).

## Scope boundaries

Out of scope for this directory, and a new issue is required before adding any
of it:

- `.fit` uploads (owner decision, 2026-09-01).
- Server-side changes beyond the owner-approved read-only `GET /api/calendar`
  JSON route. In-app *local* revocation and any write routes are out.
- Light theme, widgets, notifications, BLE, anything write-path on the cloud —
  the app stays read-only.
- A second language/region pass; strings live in `values/` only.
- Any MCP server configuration — the IDE's native Agent mode + BYOK is the
  tooling, and nothing in any Done criterion depends on the agent.
- Instrumented tests in CI (they need KVM + an emulator action — a separate
  decision).

## Large-screen orientation is a standing requirement

`targetSdk` 36 means Android 16 ignores orientation/resizability restrictions on
large screens, so a tablet can be portrait whatever the manifest says. Every
tablet layout must therefore be **correct in portrait**, measured (not assumed):
the tablet's `smallestScreenWidthDp` is 800dp in both orientations, so the
idiom predicate is rotation-stable and the drawer never flips to a rail.
