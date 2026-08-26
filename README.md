# wattracker

A local, cross-platform cycling training app for `.fit` analysis, rider
profiling, adaptive workout planning, Zwift export, race/calendar tracking,
and optional BLE/ERG riding.

## How it is put together

wattracker is one application with three roles. On a single machine all three
sit on the same box and there is nothing to think about; the roles matter the
moment they come apart.

| role | what it is | how many |
|---|---|---|
| **server** | the database and the web UI. Owns every decision. | exactly one |
| **connector** | the half that must be next to Zwift: reads `.fit` files, writes `.zwo` files, talks BLE to the trainer. Dials *out* to the server. | one per account |
| **screen** | a browser. That is all it is. | as many as you like |

The third row is the one that is easy to miss. A **screen is not a machine that
has to be installed or paired** — it is any browser that can reach the server
and log in. The desktop the server runs on is a screen; so is a laptop in
another room; so is **a phone propped against the bars**, which sees the live
ride page with watts arriving from the connector in real time. Nothing is
configured per device, and nothing identifies a device: sessions are cookies,
so a phone that rotates its MAC address or takes a new DHCP lease every time it
joins the wifi is of no interest to the app at all.

What *does* need naming is the **server's** own address — see
[Reaching the server from other devices](#reaching-the-server-from-other-devices).

Three shapes this takes in practice:

- **All-in-one.** Server and connector on the Zwift machine, ridden from that
  screen. The default; start at [docs/quickstart.md](docs/quickstart.md).
- **Split.** Server on a NAS or in a container, connector on the Zwift
  machine. See [Server and connector](#server-and-connector).
- **Either, plus a phone.** Any of the above with extra screens on the network.
  See [Reaching the server from other devices](#reaching-the-server-from-other-devices).

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
database. App-level config — the LLM settings and the session secret — is read
from environment variables first, then an optional `config.json` in
`~/.wattracker/` (the session secret is generated and persisted there on first
run). The app is fully functional without any of these — LLM refinement is an
optional layer over the pure-formula planner.

LLM refinement uses `API_KEY` plus `LLM_ENDPOINT`, which takes `anthropic`
(the default; model `claude-sonnet-5`), `openai` (model `gpt-5.6-luna`), or
`openrouter` (model `google/gemini-3.7-flash`) — or the base URL of any other
OpenAI-compatible server (vLLM, LM Studio, Ollama: a bare host like
`http://localhost:11434` gets `/v1` appended). `LLM_MODEL` overrides the
per-provider default and is required for a custom URL; without it the LLM
layer is disabled with a warning. A custom URL may be keyless (local
servers); the three named providers require `API_KEY`. `ANTHROPIC_API_KEY` is
the legacy name for `API_KEY` and still works as a fallback.

The refinement call is one chat-completion request: a 60 second timeout and a
2000-token output budget (`max_tokens`), made without retries. For a custom
endpoint, use a non-reasoning (instruct) model. Reasoning models spend the
output budget on thinking tokens first, so the JSON answer can come back with
zero content tokens, and thinking can also overrun the 60 second window, in
which case the request is dropped (visible as a canceled request in the
server's log). Either way the layer degrades to the unrefined formula plan, so
a thinking model costs latency and refinement quality, not correctness.

Like the key, the endpoint and model are **app-level and shared**: any
authenticated user of the installation can change them from Settings, and the
prompt sent to a custom URL contains the requesting rider's training state
and planned workout. On a single-user, loopback install this is a
non-issue; on a shared or networked deployment (docker compose,
`WATTRACKER_ALLOW_NON_LOOPBACK`) treat the endpoint like the key — only run
it with people you would also hand the `API_KEY`, since any of them can
redirect every user's LLM traffic to a URL they control. Endpoint changes are
logged to the server log.

Runtime variables include `WATTRACKER_DATA_DIR`, `WATTRACKER_DB`,
`WATTRACKER_HOST`, `WATTRACKER_PORT`, `WATTRACKER_OPEN_BROWSER`, and
`WATTRACKER_AUTO_SCAN`. The host is loopback-only: `127.0.0.1` (default),
`localhost`, or `::1`; port must be 1–65535. Browser values accept
`1/true/yes/on` and `0/false/no/off`. IPv6 URLs are bracketed correctly.

`WATTRACKER_ALLOW_REGISTRATION` (unset by default) decides whether `/register`
may create an **additional** account. The first one is always allowed — an
install has to start somewhere — and after that sign-up is closed until this is
set, because any account can change the shared AI settings above. Parsed exactly
like `WATTRACKER_ALLOW_NON_LOOPBACK` (`1/true/yes/on`); see
[Reaching the server from other devices](#reaching-the-server-from-other-devices).

`WATTRACKER_PUBLIC_HOST` (unset by default) names the one external hostname a
local reverse proxy — `tailscale serve` on the owner's tailnet — forwards under:
it is added to the Host allowlist as that exact name (no wildcards, no suffix
matching) and is the host calendar subscription links are minted from, with
`WATTRACKER_PUBLIC_SCHEME` (`https` by default) as their scheme.
`WATTRACKER_PUBLIC_HOSTS` is the comma-separated form, for a server on a LAN
that is legitimately reached as an IP, a short hostname and a `.local` name at
once; every entry goes through the same strict validator.

## Server and connector

The [server and connector roles](#how-it-is-put-together) can run on different
machines: the storage and web UI as a container on a networked server, and a
small **connector** on the machine where Zwift is installed. Only three things
actually need to be next to Zwift — reading `.fit` files, writing `.zwo` files,
and talking BLE to the trainer — so that is all the connector does. Everything
else stays on the server, including every decision: the connector answers
questions and never asks any.

The connector dials *out* to the server and holds one WebSocket, so the Zwift
machine needs no open ports and no inbound firewall rule.

```sh
# On the server
docker compose up -d          # or: docker run -v wattracker-data:/data -p 8000:8000 wattracker
```

The server has to be reachable and has to be named — both halves of
[Reaching the server from other devices](#reaching-the-server-from-other-devices),
and the second half is what most commonly stops a paired connector from
connecting. Then, in the web UI, open **Settings → Connector devices**, pair a
device, and copy the token (it is shown once; only its hash is stored).

```powershell
# On the Zwift machine
wattracker-connector --server http://192.168.1.10:8000 --token <TOKEN> --save
wattracker-connector          # from then on, reusing the saved settings
```

The address after `--server` is the one that must appear in the server's
`WATTRACKER_PUBLIC_HOSTS`. If it does not, the handshake is refused with
`400 Bad Request` before the token is examined, so the symptom — a good token
that will not connect — points nowhere near the cause.

#### How a finished ride reaches the server

The connector watches the Zwift Activities folder and tells the server when a
new `.fit` has finished being written, so a ride appears under Activities a
minute or two after you save it — without anyone pressing **Rescan**. A file is
reported only once its size has stopped changing, so a `.fit` Zwift is still
writing is left alone, and Zwift's live recording buffer is ignored entirely.

```powershell
wattracker-connector --scan-interval 30 --save   # check every 30s (default 60)
wattracker-connector --scan-interval 0  --save   # stop watching
```

The check is a directory listing on the Zwift machine, so it costs nothing when
nothing has changed: after the first pass the server is contacted only when a
file has actually settled. The first pass after the connector starts always
reports — even when the Zwift folder is empty or does not exist yet — because a
ride may have been ridden while the connector was down, and that one report is
what makes starting the connector the cold-start trigger for a scan. With it
turned off — or if the connector is not running — rides are still imported by
the server's daily sweep, and **Rescan** still works at any time.

### The connector as one file (Windows)

The pip install above is the supported path on every OS. On Windows the
connector also builds as a single portable `WattrackerConnector.exe` — drop it
anywhere, run it, and it lives in the notification area: the same connector,
with a tray icon, a "Start with Windows" toggle, and a window onto the server's
UI that opens already logged in.

It is **not yet published**, because it is not yet signed: the release job that
builds it stays disabled until a certificate exists (see
[docs/windows-security.md](docs/windows-security.md)). Until then it comes
either from the unsigned artifact `windows.yml` uploads on merges to `main` —
which carries provenance, not integrity, and is not a substitute for signing —
or from a local build:

```powershell
python -m pip install ".[dev,ble,package,connector,webview]"
python -m PyInstaller --clean --noconfirm packaging\wattracker-connector.spec
python packaging\smoke_frozen_connector.py dist\WattrackerConnector.exe
```

Four things worth knowing about the exe specifically:

- **It asks for the pairing itself.** A copy that has never been paired opens a
  small window for the server address and the token instead of the command line
  above — that one is for the pip install, and the exe is a file people
  double-click. Cancel it and nothing is saved. `--headless` skips the window
  and keeps the old behaviour of exiting 2 with the instructions, which is what
  the packaging smoke test drives.
- **It has no console**, so diagnostics go to `connector.log` beside its config
  (`%LOCALAPPDATA%\wattracker-connector\`), owner-only. Use the pip console
  script when you want output in a terminal, or `--headless` to run the frozen
  build without the tray. Note that `%LOCALAPPDATA%` is per-process: pair the
  exe from a normal shell or from Explorer, because a terminal running inside a
  packaged app (an MSIX container) writes its config into that app's private
  `LocalCache` and no ordinary launch will ever find it again.
- **Autostart is one `HKEY_CURRENT_USER` value**, written only when you toggle
  it on and deleted when you toggle it off. No service, no scheduled task, no
  elevation, and the application installer is not involved at all.
- **It is unsigned**, and a self-extracting binary that autostarts and holds a
  credential is the profile SmartScreen and antivirus heuristics like least.
  Verify the `.sha256` beside it.

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

## Reaching the server from other devices

Everything above serves loopback only, which means the server is reachable from
its own machine and nowhere else. Opening it up is what lets the connector on
another box dial in, and what lets a phone or a laptop act as a
[screen](#how-it-is-put-together).

Three things have to be true, and they are separate on purpose.

**1. Bind an interface the network can reach.** Two variables, not one:

```sh
WATTRACKER_HOST=0.0.0.0 WATTRACKER_ALLOW_NON_LOOPBACK=1 python -m wattracker
```

The second is deliberately separate. Every other control here — the Host
allowlist, the WebSocket origin check, a session cookie with no `Secure` flag,
no rate limiting beyond `/login` — was written assuming a loopback bind, so
widening it must be a decision rather than a typo.

**2. Name the server.** A request whose `Host` header is not on the allowlist
gets `400 Bad Request` rather than a page, so every name you will actually type
has to be listed:

```sh
WATTRACKER_PUBLIC_HOSTS=nas,nas.local,192.168.1.10
```

An IP, a short hostname and a `.local` name are **three separate entries** —
each goes through the same strict validator, and there are no wildcards or
suffix matching anywhere. It is read at startup, so adding one needs a restart.

> **This applies to the connector too, and it is the likeliest reason a
> freshly paired machine will not connect.** The connector dials
> `ws://<server>:8000/connector/ws`, and that handshake carries the address it
> dialled as its `Host`. The allowlist covers websockets as well as pages, so
> an address that is not listed is refused **before the token is even looked
> at** — a perfectly good token, rejected for a reason that has nothing to do
> with the token, and nothing the pairing page can fix. Whatever you put after
> `--server` must be listed here. Pinned by
> `tests/test_network_posture.py`.

The names in that list are the **server's**, not its clients'. There is nothing
to set up per device and no device is identified by MAC, IP, or hostname
(`proxy_headers=False` in `wattracker/__main__.py` exists to keep it that way).
A phone with a randomized MAC and a fresh DHCP lease is simply a browser, and
pairing a connector stores only the label you typed.

**3. Decide who may create an account.** Once the port is reachable from the
network, `/register` is reachable from the network, and it is the one page that
does not ask who you are — it cannot, because it is how the first account gets
made. So it closes itself as soon as there is something to protect:

- **No accounts yet** → registration is open. That is how you set the server
  up, and nothing else works until you have.
- **At least one account** → registration is refused unless you say otherwise:

```sh
WATTRACKER_ALLOW_REGISTRATION=1 python -m wattracker
```

Add the second rider, then restart without it. The refusal is a page that names
this variable, so nobody has to go looking for it.

Why this matters on a LAN and not on loopback: an account here is not just a
private history. The AI provider settings — endpoint, model, and the `API_KEY`
behind them — are **app-level and shared** (see above), so anyone who can make
an account can point every rider's LLM traffic at a URL they control and be
handed the stored key. Leaving sign-up open is handing that to whoever else is
on the wifi. Same reasoning as the bind flag: the safe default, and one explicit
variable to change it.

`tests/test_phone_access.py` holds this path down end to end: a browser on a
LAN name registers, loads pages, presses buttons that change things, and reads
live ride frames whose watts came from the connector's radio.

### Where DHCP actually bites

Not on the phone — on the **server**. If its address is handed out by DHCP and
the lease moves, the bookmarked URL stops resolving *and* the new address is
not on the allowlist, so the two failure modes look nothing alike and neither
says what happened. In preference order:

1. **Reserve the server's address** on the router, or give it a static one.
   One-time, and no URL ever moves.
2. **Prefer a name to an IP.** A `.local` name is resolved fresh over mDNS, so
   it survives a lease change. wattracker advertises nothing itself — that is
   the OS responder (Bonjour, avahi, Windows), and a **container does not
   inherit its host's mDNS name** unless it runs on host networking or beside a
   responder.
3. **List the spares now.** There is no cost to naming the hostname, the
   `.local` name and a reserved IP together, and it is one less restart on the
   day something moves.

### What plain HTTP on a LAN exposes

The session cookie and the connector's bearer token both travel in clear text.
Anyone who can see traffic on that network — or who is on the same wifi — can
read them and act as you. That is an acceptable trade on a trusted home network
and nowhere else. It is not safe on shared, guest, or public wifi, and it must
never be pointed at an internet-facing name.

To remove that exposure, terminate TLS in front and set two more variables:

```sh
WATTRACKER_COOKIE_SECURE=1
WATTRACKER_PUBLIC_SCHEME=https
```

`tailscale serve` is the least-effort option (it does TLS and keeps the server
off the public internet); Caddy or nginx work equally well. One wrinkle to know
about: **behind an https proxy, buttons that change something return 403**,
because the proxy speaks https to the browser and plain http to the server, and
the same-origin check compares scheme and port. Reading and the live ride page
are unaffected. On a direct LAN bind there is no mismatch and buttons work
normally. See [docs/calendar-feed.md](docs/calendar-feed.md).

## Backup, packages, and Windows validation

Create backups in Settings. Stop the server before running `wattracker-restore`
or `wattracker-restore --restore 1`; a frozen bundle uses
`wattracker.exe restore [--restore N]`. Restore safety follows the configured
loopback port.

CI runs on self-hosted runners, which are not billed and so survived the
account-level block on hosted minutes. The suite runs on a macOS runner
(`cloud.yml`) on every push and pull request, and a Windows runner
(`windows.yml`) builds and smoke-tests the packaged artifacts on every pull
request: the wheel, the frozen application, the Inno Setup installer through a
full install/upgrade/uninstall, and the frozen connector. A merge to `main`
uploads the wheel, the installer and the connector, best-effort — the account's
artifact storage is full, so that step may go yellow on an otherwise green
build.

What stays disabled needs something CI cannot supply. The Windows *test* job is
gated to keep a duplicate of the suite off the single physical box; the
signed-release workflows (`windows-release.yml`, `macos-release.yml`) fire only
on a `v*` tag and are gated for want of a code-signing certificate, so every
shipped binary is still an unsigned local build. The containerized cloud
checks need billable Linux minutes. Windows executables must be built on
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
