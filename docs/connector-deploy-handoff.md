# WP-8: the connector executable on Windows

**State on 2026-08-14.** WP-B, WP-C and WP-D have landed on
`feature/connector-deploy` (draft PR #93), and the artifact has been built and
run for the first time. What is left is the part no machine can do alone: the
manual checklist in §7, which needs a live server, a paired device and a rider.

This document is self-contained. It replaces the plan.

---

## 1. Ground rules

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

Work on `feature/connector-deploy` or a branch cut from it. Never edit another
agent's checkout. Do not push to `main`.

## 2. Environment

```powershell
git clone git@github.com:poopaskoopa/wattracker.git
cd wattracker
git checkout feature/connector-deploy
py -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev,ble,package,connector,webview]"
.venv\Scripts\python -m pytest -q      # never piped: a pipe reports its own exit status
```

**Expect 2 failures on Windows, neither of them a defect.** Both are
`WinError 1314` — `test_paths_windows.py::test_workouts_override_symlink_escape_is_refused`
and `test_install_bootstrap.py::test_without_lsof_hides_lsof_but_keeps_bash_on_usrmerge_layout`
create symlinks, which needs Developer Mode. The run takes about six minutes
and was 2294 passed / 45 skipped on 2026-08-14.

Two things about the suite changed that day and are worth knowing before you
trust a result. `tests/conftest.py` now sandboxes `WATTRACKER_CONNECTOR_DIR`
for every test: redirecting HOME does not move the connector's directory on
Windows, where `config_dir` reads `LOCALAPPDATA`, so the ride tests were
writing a `ride-buffer.jsonl` into the rider's real directory - which the next
real connect would have uploaded as their ride. And
`test_ride_ws_erg_action_reports_unavailable_without_trainer` was not
"contention-flaky under parallel load", it failed roughly two runs in three on
its own: a zero `RIDE_POLL_INTERVAL_S` against a ride pinned at 0 W spends the
whole 300 s idle budget in milliseconds. Both are fixed; both were invisible on
POSIX CI.

Build and check the artifact:

```powershell
.venv\Scripts\python -m PyInstaller --clean --noconfirm packaging\wattracker-connector.spec
.venv\Scripts\python packaging\smoke_frozen_connector.py dist\WattrackerConnector.exe
```

Both were run on 2026-08-14 and all four smoke checks pass. If the build
refuses with `PermissionError` on `dist\WattrackerConnector.exe`, a copy is
still running — `taskkill /F /T /IM WattrackerConnector.exe`.

## 3. What is where

| the thing | where it lives |
|---|---|
| the icon, its menu, its balloons | `wattracker_connector/tray_win32.py` |
| the "Start with Windows" toggle | `wattracker_connector/autostart.py` |
| `--tray`, the mutex, the three threads | `wattracker_connector/__main__.py` |
| status the tray draws | `ConnectorStatus` — `client.py:64` |
| the window, and its ticket | `wattracker_connector/webview.py` |
| what the frozen build actually runs | `packaging/wattracker_connector_entry.py` |
| guards on all of it | `tests/test_connector_tray.py`, `test_connector_autostart.py`, `test_connector_packaging.py` |

## 4. The three-thread model

`webview_run` blocks its thread and wants to be the main one (unconditionally
so on macOS, and there is no reason to keep two stories). The tray needs a
message pump of its own. Win32 gives every thread its own message queue, and
`Shell_NotifyIcon` works on any thread that pumps for its callback window, so:

| thread | runs |
|---|---|
| main | the window loop, when there is a window |
| tray | the icon, its hidden message window, and its pump |
| connector | `asyncio.run(connector.run_forever())` |

Cross-thread entry points are the C library's own: `webview_dispatch` posts work
onto the window's loop, `webview_terminate` ends it. That is what makes the
split legal rather than lucky.

**Shutdown on Quit** is `connector.stop()` → terminate the window → let the pump
exit. Note what `stop()` alone does *not* do: the run loop spends its life
awaiting a frame on a socket nobody is going to send anything on, and a flag
does not interrupt a read. So `_ConnectorThread` sets the flag, gives it three
seconds, and cancels the task if it has not noticed — and then releases the
radio itself, because a cancelled task never reaches `run_forever`'s last line.
Removing that would leave a trainer held in ERG by a process that has gone.

## 5. Traps, in the order they cost time

- **PyInstaller runs its entry file as a top-level module with no package.**
  Freezing `wattracker_connector/__main__.py` directly means its first relative
  import raises ImportError. Hence `packaging/wattracker_connector_entry.py`.
  `wattracker.spec` has always done this; the connector spec did not, and the
  binary could not start at all.
- **A `console=False` build reports a fatal error in a modal dialog.** So a
  broken frozen build does not fail a smoke run, it *hangs* one, invisibly, on
  a machine where nobody is looking at the screen. `smoke_frozen_connector.py`
  now times out, kills the tree and says so.
- **A onefile build is two processes.** The bootloader unpacks to
  `%TEMP%\_MEIxxxx` and starts a child. Terminating the parent leaves the child
  holding the log file, the temp directory and the pipes — so anything waiting
  on those pipes waits forever. Kill the tree (`taskkill /F /T /PID`).
- **The frozen build has no stderr.** `print(..., file=sys.stderr)` is a silent
  no-op there, which is how the unpaired "Missing: server, token" message
  reached nobody for two commits. Diagnostics go to `config.log_path()`. Never
  add an unconditional `StreamHandler` or a bare `print` to a path the frozen
  build reaches.
- **A window class carries the window procedure it was registered with.** A
  second `TrayIcon` in one process gets the first one's procedure and sends it
  messages about a window it has never heard of. Hence one class-level
  procedure and a per-window lookup (`_INSTANCES`).
- **A message-only window is never sent a broadcast**, and `TaskbarCreated` is
  a broadcast. The tray's hidden window is top-level and never shown.
- **A paired connector that will not connect is usually not the token.** The
  Host allowlist covers websocket scopes, so an address not in
  `WATTRACKER_PUBLIC_HOSTS` is refused *before* the bearer token is read.
  Whatever follows `--server` must be listed on the server. Pinned by
  `tests/test_network_posture.py`.
- **Do not run ad-hoc scripts against the real database.** Only pytest
  redirects `HOME`; a script run by hand writes to the live
  `~/.wattracker/wattracker.db`.
- **`-v` writes the device token into the log in cleartext** (B-3, still open).
  The plain log is enough to read handshakes, drops and reconnects.

## 6. What has been verified without a rider

On the built `dist\WattrackerConnector.exe`, 2026-08-14:

- [x] All four smoke checks: unpaired exits 2 and says so in the log; paired
      connects, authenticates and answers a real RPC; `bleak` and `webviewpy`
      both import inside the frozen process.
- [x] The frozen build with no arguments puts its icon up, attaches to a
      server and answers it.
- [x] A second launch finds the first, says so, and exits 0 without a second
      connection.
- [x] Quit stops the connector, ends the pump, and the process exits with no
      threads left behind.
- [x] The icon comes back when `TaskbarCreated` arrives — both the posted
      version (`test_the_icon_comes_back_when_explorer_restarts`) and the
      honest one: `taskkill /f /im explorer.exe` against the running exe, after
      which the log records the broadcast arriving and the shell accepting the
      icon again 134 ms later. Read it from the log; Windows 11's notification
      area is not the classic `Shell_TrayWnd` toolbar, so the old trick of
      enumerating the shell's own buttons reports nothing either way.
- [x] Autostart's enable/disable/refresh against a real registry key (a
      scratch key under HKCU, removed after each test).
- [x] No stray folder appeared beside the exe — but no window has been opened
      yet, so this does **not** yet cover WebView2's profile. §7 still owns it.

## 7. Manual checklist — what still needs a rider

Nothing below can be reached from CI, and the release job is hard-disabled.
The 2026-08-06 server and its account were deleted, so a server has to be stood
up and a device paired before any of it (see the trainer session run sheet).

- [ ] Pair on the server, run the exe, confirm the tray shows connected and a
      rescan works.
- [ ] Double-click → the window opens **already logged in**. Close and reopen.
- [ ] `taskkill /f /im explorer.exe` → the icon comes back *visibly*. The
      mechanism is confirmed (§6); this is the pair of eyes.
- [ ] Toggle autostart on, reboot, confirm it reconnects. Toggle off, confirm
      the registry value is gone.
- [ ] Revoke the device in the web UI while it runs → the socket closes, the
      tray shows the reason, and a double-click now explains the revocation
      instead of opening a window.
- [ ] **No stray profile folder appears beside the exe** after a window has
      been opened. `webview.py:97` sets `WEBVIEW2_USER_DATA_FOLDER` to the
      config directory to prevent it. That is documented WebView2 loader
      behaviour but **has never been exercised**, so this is a real check.
- [ ] From a phone on the same wifi, load the ride page and watch live watts.
- [ ] Quit mid-ride and confirm the trainer is released (cadence sweep, not
      coasting — see the run sheet's protocol).

## 8. Out of scope

**WP-K — making first contact less hostile.** Pairing is clumsy: three
environment variables and a restart, a 43-character token transcribed between
machines onto a command line, and a refusal that names the wrong problem. The
tray sharpens one edge of this: an unpaired double-click of the exe now exits
2 with no console to print to and no icon to show, so **nothing visible happens
at all**. That is not new — the same silence predates the tray — but it is the
first thing WP-K should fix. Ranked candidates are in the planning notes;
nothing is decided.
