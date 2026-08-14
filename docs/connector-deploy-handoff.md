# WP-8 handoff: the connector executable, and the ride nobody has ridden yet

**State on 2026-08-14.** WP-B, WP-C and WP-D have landed on
`feature/connector-deploy` (draft PR #93). The artifact has been built and run
for the first time, and everything a machine can check on its own is checked
(§6). **The next session is the live ride test** — a server, a paired device, a
trainer and a rider — and §7 is the run sheet for it.

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

**Nothing personal goes in this repository.** No device token, no Zwift player
ID, no home address of any kind — the whole point of the rewritten history is
that this repo could be made public without re-exposing the owner. The rider's
Zwift ID is needed for §7 and is deliberately not written down here; get it
from them.

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
writing a `ride-buffer.jsonl` into the rider's real directory — which the next
real connect would have uploaded as their ride. And
`test_ride_ws_erg_action_reports_unavailable_without_trainer` was not
"contention-flaky under parallel load"; it failed roughly two runs in three on
its own, because a zero `RIDE_POLL_INTERVAL_S` against a ride pinned at 0 W
spends the whole 300 s idle budget in milliseconds. Both are fixed. Both were
invisible on POSIX CI, which is the third member of a family this repository
keeps meeting — see the note at the end of §5.

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
| status the tray draws | `ConnectorStatus` — `client.py:65` |
| the window, and its ticket | `wattracker_connector/webview.py` |
| what the frozen build actually runs | `packaging/wattracker_connector_entry.py` |
| a network you can cut without cutting RDP | `tests/windows/breakable_proxy.py` |
| guards on all of it | `tests/test_connector_tray.py`, `test_connector_autostart.py`, `test_connector_packaging.py` |

**The earlier hardware findings are not on this branch.**
`docs/windows-connector-handoff.md` — the four defects found on the bike, the
ERG latency numbers, what is confirmed and what is not — lives on
`feature/server-client`, which is not an ancestor of this one. Read it without
switching branches:

```sh
git show feature/server-client:docs/windows-connector-handoff.md
```

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
**That path has never run against a real trainer.** It is §7's B-3.

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
  holding the log file, the temporary directory and the pipes — so anything
  waiting on those pipes waits forever. Kill the tree (`taskkill /F /T /PID`).
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
- **Windows 11's notification area is not the classic `Shell_TrayWnd` toolbar.**
  Enumerating the shell's own buttons — the usual way to ask whether an icon is
  really there — returns nothing either way. Read the connector's log instead:
  it records the broadcast arriving and the shell accepting the icon.
- **A paired connector that will not connect is usually not the token.** The
  Host allowlist covers websocket scopes, so an address not in
  `WATTRACKER_PUBLIC_HOSTS` is refused *before* the bearer token is read.
  Whatever follows `--server` must be listed on the server. Pinned by
  `tests/test_network_posture.py`.
- **Do not run ad-hoc scripts against the real database.** Only pytest
  redirects `HOME`; a script run by hand writes to the live
  `~/.wattracker/wattracker.db`.
- **`-v` writes the device token into the log in cleartext** (B-3 in the older
  handoff, still open). The plain log is enough to read handshakes, drops and
  reconnects.

**The recurring family.** `main`'s CI is POSIX, so a test that is wrong about
Windows is green upstream and only fails here. It has now happened with
`time.tzset`, with `monkeypatch.setenv("HOME", …)` (`ntpath.expanduser` reads
`USERPROFILE`), with `Path.read_text()` and no encoding, with symlinks needing
Developer Mode, and most recently with `WATTRACKER_CONNECTOR_DIR` — where
`config_dir` reads `LOCALAPPDATA` and no HOME redirect reaches it. When a
Windows-only failure appears after a rebase, suspect this before the product.

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
      threads left behind — against a stub server, with no trainer attached.
- [x] The icon comes back when `TaskbarCreated` arrives: both the posted
      version (`test_the_icon_comes_back_when_explorer_restarts`) and a real
      `taskkill /f /im explorer.exe`, after which the log records the broadcast
      arriving and the shell accepting the icon again 134 ms later.
- [x] Autostart's enable / disable / refresh against a real registry key — a
      scratch key under HKCU, removed after each test.
- [x] No stray folder appeared beside the exe, but **no window has been opened
      yet**, so this does not cover WebView2's profile. §7 A-5 owns that.

## 7. The next session: the live ride test

The rider is the scarce resource, so this is ordered to spend as little of
their time as possible: everything at the desk first, then everything on the
bike. Nothing here is reachable from CI, and the release job is hard-disabled.

**The rig is gone.** The 2026-08-06 server (a LAN address on port 8000) and its
account were deleted. Before any of this: stand up a new server, pair a device
in Settings → Connector devices, re-enter the rider's Zwift ID — no export
resolves a player folder without it — and set the activities and workouts
directories. The Windows box itself, and the B-8 evidence artifacts on it,
survive.

### Before the rider is anywhere near the bike

Each of these has cost a session before.

1. **Is the server running THIS branch?** Rebuilding is not automatic and a
   stale server silently re-tests old bugs. Unauthenticated probe: fetch
   `/static/style.css` and compare its length to the local file.
2. **If the server volume was rebuilt, check the migration.** Check the
   *column*, not the `SCHEMA_VERSION` number — this trapped on 2026-08-06 and
   would have silently lost `users.onboarding_complete`.
3. **Verify the handshake.** Start the connector and look for `connected`
   within fifteen seconds. `PROTOCOL_VERSION` must match on both halves.
4. **Run the full suite yourself** (§2). Not a summary of it, and never piped.
5. **Check `%LOCALAPPDATA%\wattracker-connector\` has no `ride-buffer.jsonl`**
   before starting, or a stale ride uploads on first connect. The test leak
   that used to put one there is fixed, but a real interrupted ride still
   leaves one, which is the point of it.
6. **Kill any pytest run** before the rider is on the bike. CPU contention
   muddies every timing measurement.

### A. Desk work — no bike needed

- [ ] **A-1** Run the exe. The tray shows connected; a rescan from the web UI
      works.
- [ ] **A-2** Double-click the icon → the window opens **already logged in**.
      Close it and reopen it.
- [ ] **A-3** Straight after A-2: **no stray profile folder beside the exe**.
      `webview.py:97` sets `WEBVIEW2_USER_DATA_FOLDER` to the config directory
      to prevent one. That is documented WebView2 loader behaviour and has
      **never been exercised**, so this is a real check, not a formality. If a
      folder appears anyway, that function is where to fix it.
- [ ] **A-4** Start a second copy → a balloon from the running icon, no second
      connection, exit code 0.
- [ ] **A-5** `taskkill /f /im explorer.exe` → the icon comes back *visibly*.
      The mechanism is confirmed (§6); this is the pair of eyes.
- [ ] **A-6** Revoke the device in the web UI while it runs → the socket
      closes, the tray shows the reason, and a double-click now explains the
      revocation instead of opening a window. Re-pair afterwards.
- [ ] **A-7** Toggle autostart on, reboot, confirm it reconnects; toggle off,
      confirm the registry value is gone. Do this before the rider arrives —
      it costs a reboot. Check the value with:
      `Get-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'`

### B. On the bike, in priority order

- [ ] **B-1** *A drop landing during an in-flight RPC.* The one B-6-family case
      still unexercised, and the suite's known blind spot:
      `tests/test_connector_reconnect.py` drops the socket *between* ticks,
      which is why 1953 tests missed B-6. **Consider writing the test first** —
      a fixture that fails the transport mid-call would catch the whole family
      at the desk and might make this item unnecessary.
- [ ] **B-2** *Close the ride page mid-outage*, then let the connector
      reconnect. It should release the trainer within 90 s (`CLAIM_TIMEOUT_S`)
      rather than holding it. Never attempted.
- [ ] **B-3** *Quit from the tray, mid-ride.* New in this branch and the item
      the new code most needs proving: the connector is cancelled after a
      three-second grace and the radio is released from the thread wrapper.
      Judge it with the cadence sweep below, not by coasting.
- [ ] **B-4** *A phone on the same wifi* loads the ride page and shows live
      watts.
- [ ] **B-5** *A Zwift workout ridden to completion*, to answer whether its
      `.fit` matches and completes the plan workout. 2026-08-06 saved after
      ~17 min of a 30-minute workout and the plan row stayed open —
      inconclusive, not known-broken.
- [ ] **B-6** *Does completing or OOTO-ing prune the `.zwo`?* Untested, and it
      now has to be tested against the LOCALAPPDATA tree (see B-8 in the older
      handoff).
- [ ] **B-7** *Count FTMS writes per ERG tick.* The old B-2 — remote issues 3
      where local issues 1 — is still software-only and unmeasured on hardware
      in either direction.
- [ ] **B-8** *A +100 W ERG step, timed.* Still ungeneralised from the 6 W
      figures.

### The rig for cutting the network

Never disable the adapter and never firewall the server: this box is reached
over RDP across the same LAN, so both sever the session doing the testing.
Proxy the connector instead.

```powershell
.venv\Scripts\python tests\windows\breakable_proxy.py 18000 <server-ip> 8000
.venv\Scripts\wattracker-connector --server http://127.0.0.1:18000
```

No `--save`, so the stored config keeps pointing at the real server. **Load the
ride page directly from the server, not through the proxy** — that isolation is
what lets a cut sever only the connector. To cut: kill the proxy
(`Get-NetTCPConnection -LocalPort 18000`). To restore: start it again.
Reconnect is automatic.

### Protocols that took several attempts — do not repeat the mistakes

**Testing whether the trainer released.** Coasting cannot tell "released" from
"holding 0 W", and neither can sprinting: after an FTMS stop the trainer falls
back to its *default resistance curve*. Gear shifting is the classic
discriminator, but **this bike has virtual gears**. Use a **cadence sweep**:
~60 rpm, then ~100 rpm. ERG engaged holds watts nearly constant across both;
released lets them climb with cadence. Take the ERG-on reading first.

**Riding an outage longer than five minutes.** The rider **must keep pedalling
throughout**. `UNATTENDED_IDLE_S` only counts up while the connector is
unclaimed *and* seeing no power, so a five-minute coast tests the connector's
own give-up instead of the server's, which is a different item. After the link
is restored the trainer stays held for up to 90 s (`CLAIM_TIMEOUT_S`) before
releasing — expected, not a hang.

**Reading the ride page during an outage.** There is still no connector-offline
UI. The page shows **"running" with every number frozen** — that is the success
signal, not a hang.

**Judging any Zwift result.** Verify at the *consumer*, not at what we
produced. B-8 passed every file-level check for weeks while being a silent
no-op. "The file is in the right folder with the right name" is not evidence
Zwift can see it — open Zwift and look, or read `workouts.files` in that folder
for Zwift's own index entry.

### Known-not-defects — do not re-chase, both pre-existing on `main`

- **ERG does not auto-engage at ride start.** Turn it on manually.
- **"Max effort" blocks clamp the rider** at 55% of FTP
  (`FREERIDE_ERG_FRACTION`). Workaround: toggle ERG off for the block.
- **Back-to-back rides can need a retry on the HR strap** — it recovers on its
  own.

### Timeouts worth having in your head

| | |
|---|---|
| server gives up on an absent connector | 300 s (`CONNECTOR_OFFLINE_TIMEOUT_S`) |
| connector waits to be re-claimed after reconnect | 90 s (`CLAIM_TIMEOUT_S`) |
| connector ends an unclaimed ride at 0 W | 300 s (`UNATTENDED_IDLE_S`) |
| reconnect backoff ceiling while riding | 5 s ×(0.5–1.5) jitter, so ~7.5 s max |
| zero-power grace before auto-pause | 3 s (`_DEFAULT_ZERO_GRACE_S`) |
| tray gives the connector this long to notice Quit | 3 s, then it is cancelled |

## 8. Out of scope

**WP-K — making first contact less hostile.** Pairing is clumsy: three
environment variables and a restart, a 43-character token transcribed between
machines onto a command line, and a refusal that names the wrong problem. The
tray sharpens one edge of this: an unpaired double-click of the exe exits 2
with no console to print to and no icon to show, so **nothing visible happens
at all**. That is not new — the same silence predates the tray — but it is the
first thing WP-K should fix. Ranked candidates are in the planning notes;
nothing is decided, and none of it should hold up the executable.
