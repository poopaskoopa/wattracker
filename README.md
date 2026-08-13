# wattracker

A local, cross-platform cycling training app for `.fit` analysis, rider
profiling, adaptive workout planning, Zwift export, race/calendar tracking,
and optional BLE/ERG riding.

## Features

- **Ingest** `.fit` files from the Zwift Activities folder (auto-discovered per
  OS) or via upload; idempotent re-scan with `(start_time, duration)` dedup.
- **Metrics**: Normalized Power, Intensity Factor, TSS, mean-maximal power
  curve, Critical Power / W' fit, CTL/ATL/TSB (Fitness/Fatigue/Form), aerobic
  decoupling, efficiency factor.
- **FTP tracking over time**: best-20-min * 0.95 estimator, a monthly
  auto-update appended to an `ftp_history` table, and manual overrides. The
  current FTP (latest history row → config override → estimate) drives
  TSS/IF/zones/planner.
- **Analysis**: Coggan 7-zone model, plateau and overreach detection, a
  0-100 readiness score with alert strings, plus a rider power profile and
  reversible correction of anomalous full-resolution power samples.
- **Adaptive planning**: profile-informed multi-week plans and standalone
  workouts, with completion reconciliation, RPE feedback, out-of-office
  ranges, and automatic reflow around availability and races. Optional LLM
  refinement layers coaching text over the fully functional formula planner.
- **Calendar and races**: monthly training calendar, race priorities/results,
  race-aware planning, and private iCalendar subscription support.
- **Zwift workouts**: download `.zwo` files, export a plan as a bundle, or sync
  workouts directly into the selected Zwift Workouts folder.
- **In-app riding**: optional BLE cycling-power/heart-rate sensors and FTMS ERG
  control, with a hardware-free simulation mode; completed rides feed back
  into training history.
- **Backup and restore**: create database backups in Settings and restore them
  with the command-line restore flow.
- **Web UI** (FastAPI + Jinja2 + vendored Chart.js): Dashboard, Activities,
  Volume, Plan, Calendar, Races, Ride, Profile, and Settings, plus JSON API
  endpoints backing the charts and live ride state.
- **Multi-user with authentication**: register/login/logout with a signed-cookie
  session (`SessionMiddleware`); passwords hashed with `hashlib.scrypt` + a
  per-user salt. Every request is guarded — unauthenticated visitors are
  redirected to `/login`. All activities, streams, FTP history, and settings are
  scoped by `user_id`, so accounts are fully isolated.

## Run

For the shortest macOS/Linux setup, see [docs/quickstart.md](docs/quickstart.md)
or run this from the repository root:

```sh
./start.sh
```

The first run creates the local environment and installs the app automatically.
The manual commands below remain useful when you want to activate the virtual
environment yourself.
Python 3.12 or newer is required.

```sh
python -m venv .venv && . .venv/bin/activate
pip install -e .
python -m wattracker          # serves http://localhost:8000 and opens a browser
```

### Windows (native PowerShell)

Windows 10/11 with Python 3.12+ is supported directly; WSL and Docker are not
required. The local unsigned Inno Setup installer definition is
`packaging/wattracker.iss`; the Python install and PowerShell lifecycle script
are also available:

```powershell
py -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
.\scripts\wattracker.ps1 start
.\scripts\wattracker.ps1 status
.\scripts\wattracker.ps1 restart
.\scripts\wattracker.ps1 stop
```

The lifecycle script manages only the exact PID it started. Its atomic identity
record includes start time, executable, unique command marker, and port; stale
or tampered records fail closed. It never kills by process name or port owner,
and refuses to start on an occupied port. `WATTRACKER_EXECUTABLE` may name an
exact installed/frozen executable; otherwise `.venv\Scripts\python.exe` is used.

Windows folder discovery checks `%LOCALAPPDATA%\Zwift\Activities`, redirected
Windows Documents, OneDrive consumer/commercial Documents,
`%USERPROFILE%\Documents`, then home Documents. Workouts use all Documents
candidates. Settings and `WATTRACKER_ACTIVITIES_DIR`,
`WATTRACKER_WORKOUTS_DIR`, or `WATTRACKER_ZWIFT_WORKOUTS_ROOT` override discovery.

On first visit you are redirected to `/login`; create an account at `/register`
(username + password, min 8 chars). Each account sees only its own data.

### macOS

From the repository root, build the architecture-specific app DMG and SHA-256
checksum with:

```sh
packaging/build-macos.sh
```

The script requires Python 3.12+ and writes
`release/wattracker-macos-<arch>.dmg` plus its `.sha256` file. Open the DMG and
copy `wattracker.app` to Applications. Packaging uses the
`.[dev,ble,package]` extra, which pins PyInstaller 6.16.0. The default ad-hoc
signing is suitable for local use but does not satisfy Gatekeeper; see
[the macOS packaging guide](docs/macos-packaging.md) for signing and
notarization caveats.

## Test

```sh
pip install -e ".[dev]"
pytest
```

### Browser (DOM) tests

`tests/test_dom_smoke.py` drives the real app in Chromium with Playwright and
asserts on *rendered* state — chart canvases that actually paint, power-profile
SVGs with real geometry, a clean JS console on every page. Source-string tests
cannot catch those regressions.

They need a browser binary that pip does not install:

```sh
playwright install chromium   # ~150MB, one-off
```

Without it (CI, fresh checkouts) the whole module **skips** — it never fails.
They run by default and add roughly 15s. To select or exclude them:

```sh
pytest -m browser         # only the browser tests
pytest -m "not browser"   # everything else
```

## Ride (Bluetooth)

The **Ride** page runs a generated/planned workout directly in the app, talking
to cycling equipment over BLE:

- **Cycling Power Service (0x1818)** for power/cadence, **Heart Rate (0x180D)**
  for HR, and **FTMS (0x1826)** to set ERG target power (control trainer
  resistance).
- The workout clock **auto-pauses at 0 W and auto-starts when you pedal**, and
  auto-stops after a short grace period of continuous 0 W (or when segments
  finish). Completed rides are saved as activities and feed CTL/ATL/FTP.

BLE hardware support is an **optional extra** — the core app and test suite run
without it:

```sh
pip install .[ble]     # installs bleak; needs a Bluetooth adapter + trainer
```

Without an adapter (or without `bleak`), the page loads fine, reports Bluetooth
as unavailable, and offers a **Simulate** button that drives the same live
screen and state machine with a virtual trainer. **Real-hardware riding must be
verified against actual equipment** — it cannot be exercised in CI.

## Configuration

FTP override, ZwiftID, and folder paths are **per-user** settings stored in the
database. App-level config — the `ANTHROPIC_API_KEY` and the session secret —
is read from environment variables (`ANTHROPIC_API_KEY`, `WATTRACKER_SECRET`)
first, then an optional `config.json` in `~/.wattracker/` (the session secret is
generated and persisted there on first run). The app is fully functional without
an API key — LLM refinement is an optional layer over the pure-formula planner.

Runtime variables include `WATTRACKER_DATA_DIR`, `WATTRACKER_DB`,
`WATTRACKER_HOST`, `WATTRACKER_PORT`, `WATTRACKER_OPEN_BROWSER`, and
`WATTRACKER_AUTO_SCAN`. The host is loopback-only: `127.0.0.1` (default),
`localhost`, or `::1`; port must be 1–65535. Browser values accept
`1/true/yes/on` and `0/false/no/off`. IPv6 URLs are bracketed correctly.

`WATTRACKER_PUBLIC_HOST` (unset by default) names the one external hostname a
local reverse proxy — `tailscale serve` on the owner's tailnet — forwards under:
it is added to the Host allowlist as that exact name (no wildcards, no suffix
matching) and is the host calendar subscription links are minted from, with
`WATTRACKER_PUBLIC_SCHEME` (`https` by default) as their scheme.
`WATTRACKER_PUBLIC_HOSTS` is the comma-separated form, for a server on a LAN
that is legitimately reached as an IP, a short hostname and a `.local` name at
once; every entry goes through the same strict validator.

## Server and connector

wattracker can also be split in two: the storage and web UI run as a container
on a networked server, and a small **connector** runs on the machine where
Zwift is installed. Only three things actually need to be next to Zwift —
reading `.fit` files, writing `.zwo` files, and talking BLE to the trainer — so
that is all the connector does. Everything else stays on the server.

The connector dials *out* to the server and holds one WebSocket, so the Zwift
machine needs no open ports.

```sh
# On the server
docker compose up -d          # or: docker run -v wattracker-data:/data -p 8000:8000 wattracker
```

Set `WATTRACKER_PUBLIC_HOSTS` to every name you will reach it by — an IP, a
hostname and a `.local` name are three separate entries — or only `localhost`
is accepted. Then, in the web UI, open **Settings → Connector devices**, pair a
device, and copy the token (it is shown once).

```powershell
# On the Zwift machine
wattracker-connector --server http://192.168.1.10:8000 --token <TOKEN> --save
wattracker-connector          # from then on, reusing the saved settings
```

Only **one connector per account** may run at a time. A second one takes over,
and the first stops with a message saying so rather than fighting it for the
connection.

If the connector is not running, the server keeps working: pages load, plans
generate, and the **Download .zip** / **Download .zwo** buttons still export
by hand. Only the automatic reads and writes to the Zwift folders wait.

Multi-arch images (x86-64 and ARM, for a Pi or a NAS):

```sh
# One-off on an x86 host, to register ARM emulation:
docker run --privileged --rm tonistiigi/binfmt --install arm64
docker buildx build --platform linux/amd64,linux/arm64 -t wattracker .
```

The image needs no compiler on either architecture — numpy, pandas and scipy
all publish manylinux wheels for `aarch64`. **The `linux/amd64` build is the
one that has actually been run and tested; `linux/arm64` is expected to work
for that reason but has not been built here.**

The database lives on the `/data` volume in WAL mode, so it must be on **local
disk** — SQLite in WAL mode corrupts on NFS/SMB. The compose file uses a named
volume for that reason. To restore a backup, stop the container and run
`wattracker-restore` in a one-shot container against the same volume:

```sh
docker compose down
docker run --rm -it -v wattracker-data:/data wattracker wattracker-restore
```

Binding beyond loopback needs two variables, not one:

```sh
WATTRACKER_HOST=0.0.0.0 WATTRACKER_ALLOW_NON_LOOPBACK=1 python -m wattracker
```

The second is deliberately separate. Every other control here — the Host
allowlist, the WebSocket origin check, a session cookie with no `Secure` flag,
no rate limiting beyond `/login` — was written assuming a loopback bind, so
widening it must be a decision rather than a typo.

**What plain HTTP on a LAN actually exposes.** The session cookie and the
connector's bearer token both travel in clear text. Anyone who can see traffic
on that network — or who is on the same wifi — can read them and act as you.
That is an acceptable trade on a trusted home network and nowhere else. It is
not safe on shared, guest, or public wifi, and it must never be pointed at an
internet-facing name.

To remove that exposure, terminate TLS in front and set two more variables:

```sh
WATTRACKER_COOKIE_SECURE=1
WATTRACKER_PUBLIC_SCHEME=https
```

`tailscale serve` is the least-effort option (it does TLS and keeps the server
off the public internet); Caddy or nginx work equally well.

### From a phone on the same network

With the two variables above set, and the name the phone will use listed in
`WATTRACKER_PUBLIC_HOSTS`, a phone browser gets the whole app — pages, buttons
that change things, and the live ride screen showing watts as they arrive from
the connector on the Zwift machine:

```sh
WATTRACKER_HOST=0.0.0.0 WATTRACKER_ALLOW_NON_LOOPBACK=1 \
WATTRACKER_PUBLIC_HOSTS=wattracker.local,192.168.1.10 python -m wattracker
```

List every name you will actually type — an IP, a short hostname and a `.local`
name are three separate entries, and a name that is not listed is answered with
`400 Bad Request` rather than served. Nothing else needs configuring: the
connector keeps dialling the server, and the phone is just another browser.
`tests/test_phone_access.py` holds this path down end to end.

Read the exposure note above first. On plain HTTP this puts the session cookie
on the wifi in clear text, which is a fine trade at home and a bad one
anywhere else.

Behind an https proxy instead (`tailscale serve`), the ride screen still works
but buttons that change something return 403 — the proxy changes the scheme and
port the same-origin check compares. See `docs/calendar-feed.md`.

## Backup, packages, and Windows validation

Create backups in Settings. Stop the server before running `wattracker-restore`
or `wattracker-restore --restore 1`; a frozen bundle uses
`wattracker.exe restore [--restore N]`. Restore safety follows the configured
loopback port.

All GitHub Actions jobs are currently hard-disabled, so the test gate is a
local `python -m pytest tests` run. The Windows test job targets Python 3.12,
matching the project's minimum supported version and the version used in
development; it is disabled because Actions runs are blocked at the account
level (failed payment or spending limit) and every run reported a red check
that could not be told apart from a real failure. Windows packaging and
signed-release jobs were already disabled to avoid consuming hosted-runner
minutes, which are billable on this private repo. The retained workflow
definitions must be explicitly re-enabled by a later code change, and the test
job additionally needs billing resolved. Windows executables must be built on
Windows. Real BLE hardware and production signing cannot be verified here; use
[the Windows BLE checklist](docs/windows-ble-validation.md).

macOS ships as a `wattracker.app` inside a DMG, built end to end by
`packaging/build-macos.sh` (the same `packaging/wattracker.spec` serves both
platforms). Only ad-hoc signing can be produced here, so a downloaded DMG is
still quarantined by Gatekeeper; see
[the macOS packaging guide](docs/macos-packaging.md) for the Developer ID and
notarization path and the full list of gaps.

Chart.js and the zoom plugin are vendored with the application, so ride,
training-load, power-curve, and volume charts work without internet access.
