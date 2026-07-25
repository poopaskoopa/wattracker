# wattracker

A local, cross-platform cycling training analyzer. It ingests
Zwift / TrainerRoad `.fit` files, computes training-science metrics, detects
plateau / overreach, and prescribes progressive workouts exported as Zwift
`.zwo` files.

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
  0-100 readiness score with alert strings.
- **Prescribe**: a pure-function planner (`plan_workout`) that picks a workout
  from training state + duration, optional LLM refinement of coaching text
  (Anthropic `claude-sonnet-5`; fully functional without a key), and `.zwo`
  export you can download or write straight into the Zwift Workouts folder.
- **Web UI** (FastAPI + Jinja2 + vendored Chart.js): dashboard, activities,
  generate, settings, plus JSON API endpoints backing the charts.
- **Multi-user with authentication**: register/login/logout with a signed-cookie
  session (`SessionMiddleware`); passwords hashed with `hashlib.scrypt` + a
  per-user salt. Every request is guarded — unauthenticated visitors are
  redirected to `/login`. All activities, streams, FTP history, and settings are
  scoped by `user_id`, so accounts are fully isolated.

## Run

```sh
python -m venv .venv && . .venv/bin/activate
pip install -e .
python -m wattracker          # serves http://localhost:8000 and opens a browser
```

### Windows (native PowerShell)

Windows 10/11 with Python 3.10+ is supported directly; WSL and Docker are not
required:

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

## Test

```sh
pip install -e ".[dev]"
pytest
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

## Backup, packages, and Windows validation

Create backups in Settings. Stop the server before running `wattracker-restore`
or `wattracker-restore --restore 1`; a frozen bundle uses
`wattracker.exe restore [--restore N]`. Restore safety follows the configured
loopback port.

All GitHub Actions jobs are currently hard-disabled, so the test gate is a
local `python -m pytest tests` run. The Windows test job targeted Python 3.10,
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

Chart.js and the zoom plugin are vendored with the application, so ride,
training-load, power-curve, and volume charts work without internet access.
