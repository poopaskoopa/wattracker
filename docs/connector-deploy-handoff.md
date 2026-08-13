# WP-8 handoff: finishing the connector executable on Windows

**State on 2026-08-14.** Everything that can be written and tested off Windows
is done and open as **draft PR #93** on `feature/connector-deploy`. What is
left is the Windows-runtime surface: the tray icon, the autostart toggle, and
the entry point that ties them to the connector and the window.

This document is self-contained. You should not need the original plan.

---

## 1. Ground rules before the first commit

From `AGENTS.md`, and both of these have caused an incident before:

```sh
git config user.name  wattrackerboss
git config user.email wattrackerboss@users.noreply.github.com
git config user.email   # must print the noreply address
```

The repository is private but its history was deliberately rewritten once to
strip a personal address. The machine's *global* git config still carries one,
so a clone that does not override it locally re-publishes that address in every
commit. Check before pushing:

```sh
git log --format='%h %an <%ae>' origin/main..HEAD
```

Work on `feature/connector-deploy` (the branch PR #93 is open against) or a
branch cut from it. Never edit another agent's checkout. Do not push to `main`.

## 2. Environment

```powershell
git clone git@github.com:poopaskoopa/wattracker.git
cd wattracker
git checkout feature/connector-deploy
py -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev,ble,package,connector,webview]"
.venv\Scripts\python -m pytest -q          # expect 2226 passed, 13 skipped
```

The full suite takes roughly 2.5 minutes and must be green before any merge.
Two ride-WebSocket tests are known to be contention-flaky under parallel load;
one isolated failure proves nothing either way, so re-run before believing it.

Build and check the artifact:

```powershell
.venv\Scripts\python -m PyInstaller --clean --noconfirm packaging\wattracker-connector.spec
.venv\Scripts\python packaging\smoke_frozen_connector.py dist\WattrackerConnector.exe
```

## 3. What already exists — use it, do not rebuild it

| you need | it is already here |
|---|---|
| status for the tray to display | `ConnectorStatus` — `wattracker_connector/client.py:64`. Plain attributes (`connected`, `last_error`, `last_connected_at`, `server_url`), written by the connector thread, read by yours. |
| starting and stopping the connector | `Connector.run_forever()` / `Connector.stop()` — `client.py:143`. `stop()` already tears the radio down mid-ride. |
| the log file to open from the menu | `config.log_path()` — `wattracker_connector/config.py:49`. Owner-only, rotating. |
| the config folder to open from the menu | `config.config_dir()` — `config.py:23`. |
| opening the window, already logged in | `webview.session_url(server_url, token)` then `webview.open_window(url)` — `wattracker_connector/webview.py:48` and `:114`. Raises `WindowUnavailable` with a rider-facing message; fall back to `webview.open_in_browser(url)`. |
| running frozen without a tray | `--headless`, already in the parser. |
| asking the frozen binary whether an import survived | `--smoke-import`, already in the parser, allowlisted to `bleak` and `webviewpy`. |
| the tray icon file | the spec already bundles `wattracker/web/static/favicon.ico` into `wattracker/web/static`. |

**Do not add flags that overlap `--headless` or `--smoke-import`.** They were
added early precisely so this work would inherit them.

## 4. The three-thread model — settle this first

`webview_run()` blocks its thread and wants to be the main one (unconditionally
so on macOS, and it is simplest to keep the same shape on Windows). The tray
needs a message pump of its own. Win32 gives every thread its own message
queue, and `Shell_NotifyIcon` works on any thread that pumps for its callback
window, so:

| thread | runs |
|---|---|
| main | the window loop (`webview_run`) |
| tray | the icon, its hidden message window, and its pump |
| connector | `asyncio.run(connector.run_forever())` |

Cross-thread entry points: `webview_dispatch` posts work onto the window's loop
(navigate, show); `webview_terminate` ends it. Both are the C library's
documented cross-thread calls — that is what makes this split legal.

Shutdown order on **Quit**: `connector.stop()` → destroy/terminate the window →
let the pump exit.

## 5. WP-B · Tray icon — `wattracker_connector/tray_win32.py` (new)

Hand-rolled `Shell_NotifyIconW` via `ctypes`, plus a hidden message window. No
new dependency: the whole point of the ctypes tray is that the exe stays small.

- **Menu**: a status line (server, connected-since, last error), **Open
  wattracker** (§4 and the table in §3), **Open log**, **Open config folder**,
  **Start with Windows** (checked state from WP-C), **Quit**.
- **Double-click** is *Open wattracker*.
- Reads `ConnectorStatus`; owns no connector state of its own.
- **`WM_TASKBARCREATED`**: re-add the icon. Without this it vanishes for good
  when explorer restarts, and explorer restarts more often than you think.
- **The `_Replaced` state must be visible.** When another connector takes the
  account over, `run_forever` deliberately stops instead of reconnecting
  (`client.py:155-168`). Show a distinct icon state and balloon the reason, or
  the rider sees a dead icon and no explanation.
- **Must import cleanly on any OS** and raise only on *construction* off
  Windows, so the Linux suite can test its structure. This is the pattern
  `wattracker_connector/webview.py` already follows.

## 6. WP-C · Autostart — `wattracker_connector/autostart.py` (new)

`winreg` against `HKEY_CURRENT_USER\Software\Microsoft\Windows\CurrentVersion\Run`,
value name `wattracker-connector`, data = the quoted frozen `sys.executable`.

- `enabled()` / `enable()` / `disable()`. Idempotent, and self-heals a stale
  value when the exe has been moved.
- **Refuse to register when not frozen.** Pointing `Run` at a venv's
  `python.exe -m` breaks silently the moment the venv moves.
- **HKCU only** — never HKLM, never a service, never Task Scheduler. All three
  would ask for elevation, which is the promise `docs/windows-security.md`
  makes everywhere else.
- Written only when toggled on; deleted when toggled off.
- **Do not touch `packaging/wattracker.iss`.** `tests/test_windows_installer.py`
  asserts the installer never mentions a startup entry, and
  `tests/test_connector_packaging.py` asserts it a second time for exactly this
  reason. Autostart is a runtime toggle in the connector, not an installer
  feature.

## 7. WP-D · Entry point — `wattracker_connector/__main__.py`

- Add `--tray`, implied when `sys.frozen` and no other action was requested.
  **`--headless` must suppress it** — the packaging smoke test drives the
  frozen binary that way and will hang if the tray starts.
- `--show-config` keeps its console behaviour for the pip script; the tray
  answers the same need with *Open config folder* / *Open log*.
- **Single instance per user**: a `Local\wattracker-connector` named mutex. A
  second launch balloons the existing icon and exits 0. This is distinct from
  the server's one-connector-per-*account* rule, which is `_Replaced`.
- Owns the startup and shutdown ordering in §4.

## 8. Tests to add as those land

`tests/test_connector_packaging.py` deliberately contains **no** assertions
about `tray_win32.py` or `autostart.py` — a test about a file nobody has
written passes for the wrong reason. Add them with the code:

- `autostart.py` names HKCU and never HKLM, and writes nothing at import time;
- `tray_win32.py` imports off Windows and refuses to construct;
- the tray reads `ConnectorStatus` rather than duplicating connector state.

**Mutation-test before trusting any of them.** The convention on this branch is
that every test was proved by breaking the code it guards and watching it fail;
the commit messages record which mutations were run. A guard that passes
against broken code is worse than no guard.

## 9. Manual checklist — the only real test any of this gets

CI cannot reach any of it, and the release job is hard-disabled.

- [ ] Pair on the server, run the exe, confirm the tray shows connected and a
      rescan works.
- [ ] Double-click → the window opens **already logged in**. Close and reopen.
- [ ] `taskkill /f /im explorer.exe` → the icon comes back
      (`WM_TASKBARCREATED`).
- [ ] Toggle autostart on, reboot, confirm it reconnects. Toggle off, confirm
      the registry value is gone.
- [ ] Revoke the device in the web UI while it runs → the socket closes, the
      tray shows the reason, and a double-click now explains the revocation
      instead of opening a window.
- [ ] Start a second copy → balloon, no second connection.
- [ ] **No stray profile folder appears beside the exe.**
      `wattracker_connector/webview.py:97` sets `WEBVIEW2_USER_DATA_FOLDER` to
      the config directory to prevent it. That is documented WebView2 loader
      behaviour but **has never been run**, so this box is a real check, not a
      formality. If a folder appears anyway, that function is where to fix it.
- [ ] From a phone on the same wifi, load the ride page and watch live watts.

## 10. Things that will bite

- **A paired connector that will not connect is usually not the token.** The
  Host allowlist covers websocket scopes, so an address not in
  `WATTRACKER_PUBLIC_HOSTS` is refused *before* the bearer token is read.
  Whatever follows `--server` must be listed on the server. Pinned by
  `tests/test_network_posture.py`.
- **The frozen build has no stderr.** `sys.stderr is None`, and writing to that
  handle raises on Windows. Diagnostics go to `config.log_path()`. Never add an
  unconditional `StreamHandler` or a bare `print` to a path the frozen build
  reaches.
- **onefile re-extracts to `%TEMP%\_MEIxxxx` on every launch.** That is the
  price of one portable file, and the reason antivirus heuristics take an
  interest in an unsigned binary that also autostarts and holds a credential.
- **Do not run ad-hoc scripts against the real database.** Only pytest
  redirects `HOME`; a script run by hand writes to the live
  `~/.wattracker/wattracker.db`.
- **`webviewpy` is optional and imported lazily.** A subprocess guard asserts
  it never enters the connector core's import graph. Keep any import of it
  inside the function that needs it.

## 11. Out of scope here

**WP-K — making first contact less hostile.** Pairing is clumsy: three
environment variables and a restart, a 43-character token transcribed between
machines onto a command line, and a refusal that names the wrong problem. Ranked
candidates are in the planning notes; nothing is decided, and none of it should
hold up the executable. Do not fold it into this work.
