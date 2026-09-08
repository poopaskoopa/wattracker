# Android client for wattracker — implementation plan

Epic #192 and sub-issues #193–#199, plus the owner's
extra targets: Android 11+, dual-server support (cloud **and** local), offline
cache. Kotlin + Jetpack Compose, single app under `android/`.

**Revised 2026-09-06.** Re-verified against `main` at `8c75eba`. What changed
since the 2026-09-01 draft:

1. **Tooling (owner decision 2026-09-06):** no MCP servers at all — no
   community Android MCP tooling, no IDE MCP bridge, no `.kilo` MCP
   registration (the 2026-09-01 draft's approach is dropped entirely). The
   dev agent is Android Studio 4's **built-in Agent mode with BYOK**
   (bring-your-own-key).
   `./gradlew` + `adb` remain the authoritative path — nothing in any Done
   criterion depends on the agent.
2. **Cloud device revocation has landed** (#153): `GET /api/v1/devices` and
   `POST /api/v1/devices/{credential_id}/revoke` exist. The plan's "no route
   yet" text is gone; Step 3's "remove this device" now calls the real route.
3. **APIM is out of the architecture.** `main.bicep` has zero APIM resources;
   `docs/cloud-sync.md`: "the selected deployment has no managed API gateway"
   — the app enforces credentials, signed envelopes, **durable** quotas and
   the kill switch itself. Quota numbers update from "1000 req/day APIM" to
   the durable defaults (`wattracker/cloud/limits.py`: 50 000 read
   requests/day and 512 MiB read bytes/day per rider scope, deployment-
   configurable). The `Ocp-Apim-Subscription-Key` header name survives as
   application convention (`api.py:105`) — it is just a header now.
4. **The iOS refresh state machine is the built reference.**
   `ios/.../Cloud/CloudSession.swift` (post-#233, on `main`) implements
   single-flight refresh, two-strike removal, clock-skew exclusion, backoff
   bounds and a `DeviceState`/`Failure` taxonomy. Step 2.3 now says *port
   these decisions with the constants kept in sync with the Swift names*
   instead of "this plan is the spec both platforms follow".
5. **The cloud dev harness no longer needs extending.** Open PR #240 (iOS
   #234) rewrites `scripts/walking_skeleton_server.py` so it **always**
   publishes a full snapshot (no `--full-snapshot` flag) via
   `snapshot_publish_pages()`, with real enrollment/publish/pairing routes
   and new flags (`--host/--port/--db/--user-id/--lan/--codes/--json`).
   Android depends on that PR's harness; it lands when #234 merges.
6. **A second shared interop fixture exists:** `tests/vectors/
   cloud_objects_v1.json` (#167) pins the shape of all ten published object
   kinds; Python and Swift suites read it and the Kotlin tests read it too.
   The object-kind list is corrected — `race` and `scheduled_workout` were
   never standalone published kinds (a race is the `calendar_day.race` field).
7. **The local `GET /api/calendar` route still does not exist** — the Step 6
   server PR is still required (code locations updated).

---

## Resolved decisions (owner)

| Decision | Answer |
|---|---|
| `.fit` file uploads | **Dropped from scope** (2026-09-01). No assigned issue covers it; the cloud API has no `.fit` route (sync plane takes parsed objects only) and the local server's `POST /activities/upload` predates this work. Revisit later as a new issue. |
| Architecture | **Dual backend.** (1) Cloud read plane as #194–#198 specify (pairing code → Keystore P-256 → signed context refresh → `GET /api/v1/context/*` with the bearer reader context). (2) Local desktop server via the **connector model**: device token paired in the web UI Settings → session → local JSON API (`/api/state`, `/api/activities`, …). Screens read through one common `ReadModel` interface with one adapter per backend. |
| minSdk / targetSdk | minSdk **30** (Android 11, owner's floor; StrongBox API is 28+, Keystore EC P-256 is long stable). targetSdk **36** — Google Play has required API 36+ for new apps and updates **since 2026-08-31** (verify current policy at implementation time). |
| Release CI runner (#199) | **Deferred** — plan the job for the existing self-hosted macOS runner (`macos-ci`) as default; note that switching to a Linux runner later is a `runs-on` label change. |
| HTTP client | `HttpURLConnection` (plain, no deps). **Note:** issue #193 names "HttpURLConnection or `java.net.http`", but `java.net.http.HttpClient` does not exist in the Android SDK (JDK-11 API, never ported) — the choice is unambiguous. |
| Screen cache | **Room** (AndroidX, explicitly allowed by #194) for the revision-keyed cloud cache; last-payload cache for the local backend. Justification over hand-rolled SQLite: compile-time-checked SQL, Flow-driven screens, and the cache is ~10 tables of small objects — Room's codegen is the cheap part, not the liability. |
| App id | `com.wattracker.android` (mirrors `com.wattracker.ios`). |
| Third-party deps | None beyond AndroidX, Kotlin stdlib, and (Step 2) androidx.room + androidx.security. No Retrofit/OkHttp/Koin/Coil — the epic's dependency rule. |
| IDE agent | **Android Studio 4's built-in Agent mode with BYOK** (owner, 2026-09-06). First-party, supported feature of the IDE — not a community plugin, no MCP servers, no npm bridges, no `.kilo` MCP registration. The owner's API key lives in IDE-local settings only, never in the repo. Dev accelerator, not infrastructure: every Done criterion stands on `./gradlew` + `adb`, which is also what CI runs. |
| Local calendar data | Owner-approved (2026-09-01, unchanged): **extract the month builder out of `calendar_view` and serve it from a new read-only `GET /api/calendar?year=&month=` JSON route** in `wattracker/server.py`, so the HTML page and the app render the same data by construction. Verified still absent on 2026-09-06. Lands as a small server PR (green Python suite) before Step 6; it is the one local-backend exception to "no server changes". |

## Issue state (verified 2026-09-06)

- #192 (epic) open. **#193 `ready`** — the only startable Android issue.
  #194–#199 all `blocked` (chained on #193; screens also on #194/#195).
- Merged/closed and relied on here: #153 (device revocation routes), #165
  (deployment checks — and the APIM removal that followed), #167 (rider
  isolation + `cloud_objects_v1.json` fixture).
- Still open and relevant: **#156** (turn cloud sync on in the desktop app —
  a *deployed* cloud's pairing codes are minted from the desktop's writer
  credential) and **#102** (hosting decision — `infra/azure/DEPLOY.md` is an
  *unexecuted* runbook). Both gate only the "real deployment" half of #199's
  Done; the dev harness and the local backend are unaffected.
- **Queue rule:** the Android epic is not on the AGENTS.md work queue. The
  queue says to announce before taking work not on it — say so when starting
  #193 (and note the two open iOS PRs #228/#240 and issue #234 are the
  in-flight iOS epic work; do not touch iOS files).

## Ground truth (verified against the code on 2026-09-06)

**Cloud plane** (`wattracker/cloud/api.py`, `security.py`, `snapshot.py`,
`limits.py`; `docs/cloud-sync.md`):

- Routes the app uses: `POST /api/v1/devices/pair`,
  `POST /api/v1/context/refresh`, `GET /api/v1/devices`,
  `POST /api/v1/devices/{credential_id}/revoke`, `GET /api/v1/context`,
  `/context/profile`, `/context/activities`, `/context/activities/{id}`,
  `/context/dashboard`, `/context/volume`, `/context/curve`,
  `/context/calendar`, `/context/races`.
- Deployment has **no managed API gateway** (`docs/cloud-sync.md`): public
  HTTPS on Container Apps; the application enforces credentials, signed
  envelopes, durable per-rider quotas and the durable kill switch.
- Pair body: `{"code", "public_key" (hex), "signature_algorithm":
  "ecdsa-p256-sha256"}` plus an **optional `label`** (rider-set device name,
  bounded, validated before the single-use code is spent; the iOS app sends
  one — Android should too, so both phones are legible in the desktop device
  list). Returns `device_credential`, `device_subscription_key`,
  `device_signature_algorithm`, `device_capabilities` (always `["read"]` —
  `security.py:69`), `signing_namespace` (opaque; reproduce verbatim in every
  canonical request), `reader_context`, `expires_in` (=300).
- Refresh: no body; headers `X-Device-Credential`, `X-Device-Timestamp`
  (unix s), `X-Device-Nonce`, `X-Device-Signature`
  (`Ocp-Apim-Subscription-Key` for the subscription key — legacy name,
  application-enforced, `api.py:105`). Canonical request uses fixed
  idempotency key `context-refresh` and **empty** revision (`_REFRESH_REVISION
  = ""`, still framed: a zero-length prefix). Response: `reader_context`,
  `expires_in`, **`capabilities`** (new since the 2026-09-01 draft — hold it,
  it is the future write-path's grant surface).
- **Read routes authenticate with `Authorization: Bearer <reader_context>`** —
  reads are *not* signed per-request; the device's key is spent only at pair
  and refresh. The reader context is 32 bytes of server-generated secret.
- Canonical framing (`security.py:canonical_request`, `api.py:1160`
  region): unchanged — domain separator
  `b"wattracker-cloud-request-v1\x00"`, then for each of
  `[method (uppercased), path, namespace, timestamp (decimal text), nonce,
  body_digest (lowercase hex SHA-256), idempotency_key, revision]` a 4-byte
  big-endian UTF-8 length + the bytes. **Interoperable test vectors:**
  `tests/vectors/canonical_request_v1.json` (canonical cases, 4 body digests,
  boundary pairs, P-256 signature vectors incl. high-s malleable twin) and
  `tests/vectors/cloud_objects_v1.json` (shape-pinned examples for all ten
  kinds, #167). Both are read by the Python and Swift suites; the Kotlin test
  reads the same files referenced from the repo, not copies.
- P-256 wire formats: public key = uncompressed SEC1, 65 bytes
  `04‖X‖Y`, hex. Signature = **raw `r‖s`, 128 lowercase hex chars. Not DER.
  Not low-s normalised** (the server accepts malleable signatures on purpose;
  normalising `s` would sign something different). Algorithm is always taken
  from stored credential state, never inferred from the request.
- Every read-plane auth failure (unknown device, revoked, bad signature,
  replayed nonce, …) returns the **identical 404** body as an unknown reader
  context. Quota refusals are `429` with `Retry-After`. Kill switch → 404
  (or 503 if the kill state is unreadable — `api.py:_resolve_device`).
- Reader context TTL is 300 s (`READER_CONTEXT_TTL_SECONDS`,
  `security.py:31-32`); timestamp freshness window ±300 s
  (`_MAX_TIMESTAMP = 60 * 5`, `api.py:52`); replay guard keyed on
  `(namespace, credential_id, nonce)`.
- **Quotas are durable, per-rider-scope, application-level**
  (`wattracker/cloud/limits.py`, deployment-configurable): defaults
  `max_read_requests_per_day = 50_000`, `max_read_bytes_per_day = 512 MiB`,
  `max_upload_bytes_per_day = 256 MiB`, `max_objects_per_day = 100_000`.
- Mobile collection routes (`dashboard`, `volume`, `curve`) support
  `?since=N` (delta + tombstones `"deleted":true`), `?limit` (≤100),
  `?cursor`. Envelope: `{"items":[{id,kind,revision,data}], "revision",
  "next_cursor"}`. **`revision` is pinned when the page walk starts**
  (carried inside the signed cursor); checkpoint it only after the last page.
  The feed is **at-least-once** — applying an object must be idempotent
  (upsert keyed on per-object `revision`, drop if not newer).
- **Published object kinds (corrected):** `profile`, `training_state`,
  `load_point`, `curve`, `volume_week`, `calendar_day`, `activity`,
  `activity_detail`, `stream`, `ftp_history` — the ten kinds pinned in
  `cloud_objects_v1.json`. There are **no** standalone `race` or
  `scheduled_workout` kinds; races arrive as the `calendar_day.race` field.
  `stream` is downsampled to 1500 points (`DETAIL_MAX_POINTS`,
  `snapshot.py:34`); corrections are applied server-side.
- Dates are rider-local **as published** (the desktop buckets them in the
  rider's timezone); the phone must not re-bucket.
- **Device administration routes (#153, merged):** `GET /api/v1/devices`
  lists the caller's own-scope devices (id, label, created, last seen,
  **revoked flag** — revoked entries are listed and flagged, not dropped);
  `POST /api/v1/devices/{credential_id}/revoke` revokes durably. **Any
  credential in the same `(namespace, local_user_scope)` may call it —
  including the target device revoking itself** (the server documents the
  trade: a stolen phone can revoke its siblings, but can only ever revoke
  *device* credentials, never the desktop writer). Cross-namespace is 404,
  never 403.

**The iOS client is the built reference** (`ios/WatTracker/WatTracker/Cloud/`,
on `main` post-#233): `CloudSession.swift` carries the token-lifecycle
decisions Android must port, with the constants (keep the Kotlin names
greppable against the Swift ones): `refreshAhead = 60` (a fifth of the 300 s
TTL), `minimumUsableLifetime = 15`, `maximumContextLifetime = 300` (never
trust a response claiming more), `clockSkewTolerance = 240` (past this, a
refused refresh is probably the *clock*, not removal), `baseBackoff = 30` /
`maximumBackoff = 300` for refusals with no `Retry-After`,
`rejectionsBeforeRemoval = 2` — **two refused signed refreshes, with a
backoff between, before the device declares itself removed** (one 404 is
also what a deployment mid-restart produces, and removal wipes the cache and
credential), `maximumPages = 50` (a cursor that never terminates costs a
bounded number of requests, not a loop). `DeviceState`: `unpaired / paired /
removed`; `Failure`: `notPaired / deviceRemoved / throttled(retryAfter) /
clockSkew(seconds) / offline / server(…)`. Also `CloudClient`/
`CloudTransport`/`CanonicalRequest`/`DeviceKey`/`DeviceCredentialStore`/
`SnapshotCache` and the shared `PairingCode` normalization — read them before
writing the Kotlin equivalents. (The unmerged PRs #228/#240 extend this
code; track, don't copy, what is on `main`.)

**Local plane** (`wattracker/server.py`, `connectorauth.py`,
`connectorsession.py`):

- Pairing already exists in the web UI: Settings → pair a device (label) →
  **token shown exactly once** (only sha256 stored). Revocation: Settings →
  revoke (kills open sockets and in-flight tickets). No server changes needed.
- Session flow (verified, `server.py:2947-3025`): `POST /api/connector/
  session` with `Authorization: Bearer <token>` → `{"ticket", "expires_in":
  60}` (single-use, in-memory, one outstanding per device; 401 on a bad
  token — unlike the cloud, this path *can* distinguish). Then
  `GET /connector/session?token=<ticket>` (exempt route) → 303 and sets the
  session cookie; a bad/expired ticket 303s to `/login` without explaining.
  The session is stamped `SESSION_VIA=connector` + `SESSION_DEVICE_ID`;
  `AuthMiddleware` clears it on the next request if the device was revoked.
- Read endpoints (session-authenticated JSON, verified to exist):
  `GET /api/state` (`server.py:6796`, `pipeline.build_state(...).to_dict()` —
  **no calendar/plan data in it**), `GET /api/load?months=` (:6800),
  `GET /api/curve` (:6804), `GET /api/activities` (:6808,
  `db.list_activities` — **full history**, `ORDER BY start_time DESC`,
  summary rows, cutoff/duplicates filtered — the app pages client-side and
  never holds it all), `GET /api/activity/{id}` (:2822, detail + streams +
  `linked_workout`), `GET /api/ftp?months=` (:6812), `GET /api/ftp_series?
  months=` (:6816), `GET /api/volume` (:3983) → `{"weeks": [...]}` where each
  week is `week_start` (ISO Monday), `hours`, `tss`, `distance_km`,
  `calories` — the same field names as the cloud `volume_week` object.
  The remaining per-field shapes (summary row fields, detail streams) are
  pinned by `curl`-captured fixtures during Step 4; the web UI templates are
  the reference for what a screen needs.
- **There is no JSON calendar endpoint.** The web calendar (`GET /calendar`)
  is server-rendered HTML; `calendar_view` (`server.py:4079`) builds its
  month data inline from `db.plan_workouts_for_month`,
  `db.standalone_workouts_for_month`, `db.activities_for_month_unlinked`,
  `db.list_race_dates` + `races.attach_results_to_race_dates` +
  `planmod.race_priorities` (effective/demoted priority),
  `db.list_ooto_ranges`, and the active plan's phase, with per-workout
  `skipped`/`missed`/adjustment flags computed there. **Decision (owner,
  2026-09-01, unchanged):** extract that builder into a shared function,
  serve it from a new read-only `GET /api/calendar?year=&month=` JSON route,
  and make `calendar_view` consume it too — same data, two renderings, no
  second copy of the skipped/missed/priority rules. Session-authenticated
  like the other `/api/*` routes (no new auth surface). This is the small
  server PR in Step 6's preamble; the app's local calendar adapter is
  written against its test-pinned response.
- **Week-bucketing nuance (still true, re-verified at `db.py:2040-2064`):**
  cloud `volume_week` buckets by **UTC** day (`snapshot.py:_volume_objects`);
  local `db.weekly_volume` buckets by **rider-local** day only when a history
  cutoff is set (`timezone` applied iff `history_start_date` is set). A ride
  near midnight can land in different weeks across the two backends. The app
  renders what each backend publishes (the desktop is the source of truth for
  its own backend) and this discrepancy is noted in `android/README.md` — the
  app must not try to "fix" it client-side.
- The server binds `WATTRACKER_HOST` (default `127.0.0.1`) on
  `WATTRACKER_PORT` (default `8000`), and a Host-header allowlist
  (`IPv6TrustedHostMiddleware`, `server.py:1056`) defaults to **loopback +
  `testserver` only** — any other `Host` is a 400 before routing. LAN access
  therefore needs two env vars, not one: `WATTRACKER_HOST=0.0.0.0` (bind)
  and `WATTRACKER_PUBLIC_HOSTS=<hosts>` (comma-separated; `config.public_
  hosts()`, `config.py:730-745`; bare hostname/IP entries, strictly
  validated, no wildcards). Document in `android/README.md`; do not change
  either default.
- **Security fact to keep visible:** a connector-origin session is a *full
  user session* — the server refuses only a handful of credential-minting
  routes for it (`server.py` `_from_connector` checks,
  `server.py:2990-3011`). Read-only on the local backend is a property of
  *this app*, not of the server. A leaked local device token is a full
  account login. The existing Revoke button is the mitigation. State this in
  the Settings UI copy ("remove this device") and in `android/README.md`.

**Cloud dev harness** (`scripts/walking_skeleton_server.py`):

- **No extension is needed from this plan's side.** Open **PR #240** (iOS
  #234, branch `claude/234-ios-pairing`) rewrites the harness so it: starts
  the real cloud app in-process (in-memory security state, fresh random
  operator token and Ed25519 writer keypair per run, **never writes to the
  local database** — reads go through
  `wattracker.cloud.snapshot.readonly_connection`, `mode=ro`); enrolls the
  writer through the real enrollment routes; publishes the **complete**
  snapshot through real signed `/api/v1/sync/batches` in ordered pages via
  `snapshot_publish_pages(...)` (`wattracker/cloud/snapshot.py` — derived
  objects after activity objects, so a rider with 500+ activities still
  gets `profile`/`training_state`/`load_point`/`curve` published); mints
  pairing codes through the real writer-signed route; prints codes and waits.
- Flags on that branch: `--db`, `--user-id` (default 1), `--host`
  (default `127.0.0.1`), `--port` (default `8765`), `--lan` (bind every
  interface and print the reachable address), `--codes N`, `--json PATH`,
  `--no-xcconfig`. Refuses to publish anything if the rider has no FTP.
  Comes with `tests/test_walking_skeleton_harness.py` and
  `tests/test_cloud_snapshot_derived.py`.
- Running it needs the `cloud` extra: `pip install -e '.[cloud]'` in the
  repo's `.venv` (`cryptography` is missing from `.venv` today — PR #240
  calls this out as pre-existing).
- For the **emulator** no `--lan` is needed at all: keep the default
  loopback bind and point the app at `http://10.0.2.2:8765` (the emulator's
  alias for the host's loopback). `--lan`'s address detection is written for
  macOS (`ifconfig`, BSD `route`, `en*` preference); on this Linux box it
  degrades to "first non-tunnel private-IPv4 interface", which works but is
  unmeasured — for a physical-device run prefer `--host 0.0.0.0` and an
  explicit address in the app, or verify `--lan` once and record the result.
- **Dependency:** everything in this plan that assumes the full-snapshot
  harness (Step 2's Done, validation item 2) is gated on PR #240 merging.
  Until then the harness on `main` publishes only a `profile` object —
  enough for the pairing/refresh/FTP slice, not for the four screens. Do
  **not** duplicate the harness work in an Android branch; note the
  dependency in the PR description.

**UI:** dark-only theme; palette value-for-value from `wattracker/web/static/
style.css` `:root` (same rule as iOS `Theme/Palette.swift`): bg `#0f1419`,
panel `#1a2028`, surface-2 `#212934`, surface-inset `#10161d`,
surface-border `#2a333d`, text `#e6e6e6`, text-bright `#f5f7fa`, muted
`#8a94a0`, accent `#f2a900`, on-accent `#1a1a1a`, ok `#4caf7d`, alert
`#e05252`, hr `#d55181`. Keep the CSS custom-property names as the Compose
constant names so a change on one side is greppable on the other. The web UI
already scales to mobile (breakpoints at 900/820/720/700/600/420px) — use it
as the visual reference, not as a template.

**CI:** hosted runners are blocked; the working pattern is
`.github/workflows/cloud.yml` (self-hosted `macos-ci`, push-to-main + same-repo
PRs only, workspace venv, no Docker). Copy its guardrails (fork-PR exclusion,
pinned toolchain, workspace-pinned env) into any new workflow.

---

## Step 0 — Prerequisites (install & set up first)

This machine: **Windows x64**. The Android SDK is already installed at
`C:\Users\Takazumi\AppData\Local\Android\Sdk` (see `android/local.properties`);
the emulators use x86_64 system images accelerated by the Android Emulator
Hypervisor Driver (AEHD) / Hyper-V — not `/dev/kvm`. Throughout Step 0, read
`.venv/bin/...` as the Windows venv path `.venv\Scripts\...` and `./gradlew`
as `\.\gradlew.bat` (or `gradlew` if on `PATH`).

### 0.1 Install the toolchain

1. **Android Studio 4** (the current stable line with built-in Agent mode;
   at planning time the September 2026 "Quail 4" feature drop — install
   whatever is current at implementation time): **already installed** (the IDE
   this plan is written in, 2026.1.4 line). If reinstalling, use the Windows
   `.exe` from `https://developer.android.com/studio`; accept all licenses in
   the SDK Manager.
2. **JDK 17** (AGP floor; 21 if the installed AGP demands it) — Temurin via
   your package manager or `https://adoptium.net`. Android Studio also bundles
   a JBR for its own use; Gradle on the CLI needs a JDK on `PATH`.
   Verify: `java -version`, `javac -version`.
3. **Android SDK components** (already present at
   `C:\Users\Takazumi\AppData\Local\Android\Sdk`; manage via `sdkmanager` under
   `C:\Users\Takazumi\AppData\Local\Android\Sdk\cmdline-tools\latest\bin`, or
   the SDK Manager UI):
   - Android SDK Platform 36, Build-Tools (latest stable), Platform-Tools,
     Emulator.
   - System images: `google_apis;x86_64;36`.
   Verify: `sdkmanager --list_installed`, `adb version` (run from a shell that
   has `.../Sdk/platform-tools` and `.../Sdk/cmdline-tools/latest/bin` on
   `PATH`).
4. **A BYOK API key for the IDE agent** (owner-supplied): a Google AI Studio
   key for Gemini, or an Anthropic/OpenAI key for a third-party model —
   whatever the owner's key is. It goes into IDE-local settings only (0.4);
   it is a credential — never in the repo, never in `.kilo/`, never in a PR
   description, never pasted into a commit message.

### 0.2 Create the two AVDs (phone + large screen)

```sh
avdmanager create avd -n wt-phone -k "system-images;google_apis;x86_64;36" -d pixel_9
avdmanager create avd -n wt-tablet -k "system-images;google_apis;x86_64;36" -d pixel_tablet
```

- `wt-phone`: default. All app screens are designed landscape-first.
- `wt-tablet`: **must exercise portrait too** — targetSdk 36 means the app is
  built against the Android-16 large-screen rules, where orientation/
  resizability restrictions are ignored on large screens (the same change
  iPadOS 26 made for iOS, which #171 only discovered by measurement). Do not
  trust the changelog: boot both AVDs, check what `config.orientation` and
  `smallestScreenWidthDp` actually report on the tablet in both orientations,
  and record the findings (screenshot + numbers) in `android/README.md`. This
  measurement is part of Step 1's Done criteria.
- Boot check: launch `emulator -avd wt-phone -no-snapshot-load` (in a new
  window, or `Start-Process` in PowerShell — the POSIX `&` does not apply on
  Windows), then `adb wait-for-device`,
  `adb shell getprop ro.build.version.sdk` → 36. `avdmanager`/`sdkmanager`
  live under `C:\Users\Takazumi\AppData\Local\Android\Sdk\cmdline-tools\
  latest\bin`.

### 0.3 Local wattracker for the dev loop

- The repo's `.venv` already works (used by the Python suite); on Windows the
  interpreter is `.venv\Scripts\python.exe`. For the cloud harness it
  additionally needs `\.\.venv\Scripts\python.exe -m pip install -e '.[cloud]'`.
- **Local desktop server** (the rich local backend): `./start.sh` is a POSIX
  script — run it under Git-Bash/WSL, or start the server directly in
  PowerShell:
  ```powershell
  $env:WATTRACKER_HOST = "0.0.0.0"
  $env:WATTRACKER_PUBLIC_HOSTS = "10.0.2.2,<machine-LAN-IP>"
  .\.venv\Scripts\python.exe wattracker\server.py
  ```
  Both vars matter: the bind (`WATTRACKER_HOST=0.0.0.0`) and the
  Host-allowlist — the server rejects any request whose `Host` header is not
  loopback/listed, so the phone's host must be registered:
  - emulator → `http://10.0.2.2:8000` (the emulator's alias for the host
    machine's loopback; never `127.0.0.1` from an emulator — that is the
    emulator's own loopback);
  - physical device → `http://<machine-LAN-IP>:8000` (register that IP in
    `WATTRACKER_PUBLIC_HOSTS`; a `.local` name works too if the rider prefers
    typing a name).
  Pair the phone as a device in the web UI (Settings → connector pairing,
  label e.g. `Pixel-9`) to obtain a token.
- **Cloud dev harness** (the cloud backend in debug), once PR #240 has merged
  (see Ground truth — Cloud dev harness):
  `\.\.venv\Scripts\python.exe scripts\walking_skeleton_server.py --user-id
  <id>` — loopback by default, in-memory store, full snapshot published,
  prints pairing code(s). No harness changes in this epic's branches. The
  `--lan` auto-detect is written for macOS and is untested on Windows, so for
  a physical device use `--host 0.0.0.0` plus an explicit IP in the app (the
  emulator needs no `--lan`; it reaches the host via `10.0.2.2`).

### 0.4 IDE agent: Android Studio 4 Agent mode + BYOK

Goal: an agent that can build, deploy, test, inspect and screenshot the app
inside the IDE, with the owner's own model key. This is first-party IDE
functionality — there is **no MCP server to register and no plugin to
install**; consequently there is no `.kilo/kilo.json` MCP block either
(`.kilo/` stays untracked, local-only, as it is today).

1. **Sign in to Android Studio** (Google account), then **Settings → Tools →
   AI → Model providers** and configure BYOK:
   - **Gemini**: "Get Gemini API key" opens Google AI Studio; paste the key
     into the **API key** field, tick the models to enable, Apply. (A
     personal key also widens Agent mode's context window — up to 1M tokens
     with Gemini 3 Pro — versus the no-cost default tier's limits.)
   - **Third-party model** (Anthropic, OpenAI, …): the IDE's "using remote
     models" setup — add the provider's API endpoint and key under the same
     Model providers screen; the enabled models appear in the chat's model
     picker.
   Key handling: the key is stored in IDE-local settings (machine-local).
   Treat it like any credential — it must never reach the repo (AGENTS.md:
   the history was rewritten once to strip a personal address; keys are one
   class worse). If the key must not touch this machine at all, the no-cost
   default tier still works and nothing in this plan depends on the model
   being a particular vendor.
2. **Use Agent mode** from the AI chat (agent-mode toggle; prompts are
   executed with IDE tools: build, run to device, logcat, terminal). Android
   Studio's agent mode **honors `AGENTS.md`** — this repo's multi-agent
   agreement (fetch-before-rebase, branch ownership, test-suite rule,
   identity) is therefore visible to the IDE agent; keep it that way, and
   treat the IDE agent as one more participant in that agreement (it works in
   *this* checkout, so the worktree rule binds it: it must not push to
   `main`, must not touch the other agent's worktree, and commits it makes
   carry this clone's noreply identity).
3. **The terminal stays the floor.** Every thing the IDE agent can do has a
   `gradlew` + `adb` equivalent (`\.\gradlew.bat :app:assembleDebug`, `adb
   install`, `adb shell am start`, `adb exec-out screencap -p > shot.png`,
   `adb shell uiautomator dump`, `adb shell input`, `adb logcat`,
   `adb emu console`), and nothing in the validation plan requires the IDE
   agent. This is what CI runs. If Agent mode is unavailable or misbehaves,
   the whole plan still executes from the terminal.
4. **Verify the whole loop before any Step 1 work:**
   - Android Studio has the (future) `android/` project, or a scratch Compose
     project, open; the model picker shows an enabled BYOK model; Agent mode
     is on.
   - Instruct the agent to build + deploy the scratch app to `wt-phone`,
     pull a screenshot, and read a few logcat lines. Do the same three things
     once from the bare terminal (`\.\gradlew.bat :app:installDebug`, `adb
     exec-out screencap -p > shot.png`, `adb logcat -d`) so both paths are
     known-good.
   - `adb devices` shows the booted emulator, independent of the IDE.

### 0.5 Repo hygiene (AGENTS.md)

- Per-clone commit identity set to the noreply address; verify with
  `git config user.email` before the first commit.
- Announce before starting (the Android epic is not on the AGENTS.md work
  queue). Cut feature branches from a freshly fetched `origin/main`
  (`git fetch origin` first — never rebase onto a stale ref), named for the
  work (e.g. `android/skeleton`), never after a closed issue.
  **Note:** a local branch `feature/android-client` already exists, currently
  identical to `main` (no commits either side) — reuse it or re-cut it from
  the fetched base; check `git log --oneline main..feature/android-client`
  first.
- Install the pre-push hook if not already: `scripts/hooks/install.sh` is a
  POSIX script, so run it under Git-Bash/WSL (`bash scripts/hooks/install.sh`).
- Python suite green (`.venv\Scripts\python.exe -m pytest` on Windows) before
  any merge to main.

---

## Step 1 — Project skeleton (issue #193)

`android/` Gradle project, Kotlin, Jetpack Compose, single activity.

- **Modules:** single `:app` module for now (two riders, four screens — no
  multi-module ceremony). `settings.gradle.kts` + `gradle/libs.versions.toml`
  version catalog pinning: AGP, Kotlin, Compose BOM, activity-compose,
  material3, navigation-compose, core-ktx, lifecycle, room (+KSP, Step 2),
  security-crypto (Step 2). Versions: take the pair the current Android Studio
  template generates (AGP/Kotlin/Compose must be mutually compatible); record
  the chosen versions in `android/README.md`.
- **Manifest:** `minSdk 30`, `targetSdk 36`, `applicationId com.wattracker
  .android`, dark-only (force dark theme; no light variant exists upstream),
  `android:usesCleartextTraffic="false"` + `res/xml/network_security_config.xml`
  (base-config cleartext **false**; a `domain-config` permitting cleartext for
  `localhost`, `127.0.0.1`, `10.0.2.2` **in the debug build only** — released
  via a debug-specific manifest merge or a debug resource; the LAN-cleartext
  story for the *local backend* lives in the Step 2 transport, not here).
  No INTERNET-adjacent permissions beyond `INTERNET`; no camera, no location.
- **Build types/flavors:** `debug` (cloud base URL → dev harness
  `http://10.0.2.2:8765`; local-server default → `http://10.0.2.2:8000`;
  cleartext-localhost allowed) / `release` (cloud base URL → the production
  host as a **build config field**, never a literal in code; no cleartext for
  the cloud backend, ever). The base URLs are build fields (the iOS xcconfig
  precedent): two fields `WATTRACKER_CLOUD_SCHEME` + `WATTRACKER_CLOUD_HOST`
  (split because `//` can't be a whole config value cleanly) — same trick as
  `Config/Base.xcconfig`.
- **Shell (five destinations: Dashboard, Activities, Calendar, Volume,
  Settings):**
  - Phone (narrow, including landscape phones that report regular width):
    **leading `NavigationRail`**, not a bottom bar — the arithmetic from #193:
    a ~900×400dp landscape phone loses a fifth of its scarce height to a bottom
    bar vs ~8% of width to a rail.
  - Large screen (tablet, `smallestScreenWidthDp ≥ 600`): permanent
    `ModalNavigationDrawer` **or** list-detail scaffold — the
    `NavigationSplitView` analogue; pick one and justify in code comment.
  - Idiom predicate = size class **and** idiom (same lesson as iOS `RootView`:
    a Max-sized iPhone in landscape reports regular width).
  - Every tablet layout correct **and** in portrait (the standing requirement).
  - One shared container composable (`Panel` + screen scaffold) mirroring
    `Theme/Panel.swift`; palette in `Theme/Palette.kt` with CSS variable names.
- **No network code in this step** — stub screens only (that is #194+).
- **`.gitignore` (root file):** add `.gradle/`, `android/build/`,
  `android/*/build/`, `local.properties`, `*.iml`, `captures/`,
  `android/.idea/` (`.idea/` is already ignored repo-wide), plus
  `*.jks`/`*.keystore` (Step 8; `*.key`/`*.pem`/`*.p12` are already ignored).
- **`android/README.md`:** how to open/build/run on phone + tablet AVD, the
  dependency rule, the orientation split and why, the two AVDs, the IDE-agent
  setup pointer (BYOK key in IDE settings, never in the repo),
  `WATTRACKER_HOST=0.0.0.0` for LAN use, and the measured large-screen
  orientation findings from 0.2.

**Done (per #193):** `./gradlew :app:assembleDebug` from a clean checkout
(network only for Gradle/AndroidX); runs on the phone AVD (landscape rail,
five destinations navigable) and the tablet AVD (drawer/split, correct in
portrait); screenshots of each idiom incl. tablet portrait attached to the PR;
the 0.4 dev loop works end to end (IDE Agent mode build+deploy+screenshot,
and the terminal `gradlew`+`adb` equivalents).

---

## Step 2 — Cloud API client + local client (issue #194 + the local backend)

### 2.1 Keying (cloud)

- P-256 keypair in the Android Keystore: `KeyPairGenerator`
  (`KeyProperties.KEY_ALGORITHM_EC`, 256-bit), `setIsStrongBoxBacked(true)`
  with catch of `StrongBoxUnavailableException` → plain-Keystore fallback
  (not every device has StrongBox). `setUserAuthenticationRequired(false)` —
  the pairing code is the authorization; biometrics on every request is wrong
  for a phone on a bike. Non-exportable — **assert** non-exportability in a
  unit test (export attempts throw), don't assume.
- The app **states which key it got** (StrongBox vs TEE/soft) in Settings, per
  #194's Done criteria.
- Public key export: `04 ‖ X(32) ‖ Y(32)` from the `ECKey` coordinates —
  exactly the one encoding `validate_public_key` accepts.
- iOS precedent: `DeviceKey.swift` (Secure Enclave ↔ software fallback) and
  `DeviceCredentialStore.swift` — mirror the structure where it maps.

### 2.2 Canonical request + signing

- `CanonicalRequest.kt`: byte-for-byte the framing above (4-byte BE lengths,
  UTF-8, uppercased method, empty fields framed with zero length).
- **JVM unit test reads `tests/vectors/canonical_request_v1.json`** from the
  repo (path relative to the project, referenced not copied) and asserts every
  `canonical_base64`/`canonical_sha256` case, all four body digests, and the
  two `distinct_pairs` (the two serializations must differ). If a case Kotlin
  needs is missing, add it **to the generator** (`scripts/generate_canonical_
  vectors.py`) so all languages get it, and regenerate with the client change
  in the same PR (the file's `purpose` field says regeneration without a
  client change is a break).
- **Object-shape unit test reads `tests/vectors/cloud_objects_v1.json`** and
  round-trips every pinned item through the Kotlin models — the same
  interop pattern; drift between the app's models and the publisher fails a
  test in all three languages.
- **Signature encoding — the trap:** Android's Keystore EC signing
  (`Signature.getInstance("SHA256withECDSA")` or `keyPair.private.sign(bytes)`)
  returns **DER**. The server accepts raw `r‖s` 128-hex only. Implement
  `derToRawRorS(d): ByteArray` (~40 lines: SEQUENCE{INTEGER r, INTEGER s} →
  32-byte big-endian, zero-padded; strip `BigInteger.toByteArray()` sign bytes;
  reject anything out of `[1, n-1]` instead of emitting it). Unit-test it
  against the vectors' `signature_vectors` (parse the known DER form of the
  `must_verify` signatures and compare to the raw hex; both the low-s and the
  high-s vectors must survive unmodified — **no low-s normalization**).
  A live signed refresh against the dev harness is the integration proof.
  iOS precedent: `CanonicalRequest.swift` + `CloudClient` signing — compare
  behaviour, don't copy Swift into Kotlin.
- Nonce: 24 random bytes → urlsafe base64 without padding (iOS precedent;
  ≤512 bytes is the server limit, `security.py`).

### 2.3 The client state machine (port of `CloudSession.swift`)

`CloudClient` (Kotlin, `HttpURLConnection`, no deps) + a `CloudSession`
equivalent (Kotlin `synchronized`/`Mutex` around the single-flight refresh —
Kotlin has no actor; the coalescing rule is the same: callers arriving while
a refresh is in flight await it instead of signing a second one).

Port the iOS decisions, keeping the constants and their *names* in sync with
`ios/WatTracker/WatTracker/Cloud/CloudSession.swift` so the two can be diffed:

- TTL handling: trust the response's `expires_in` but **never longer than
  `maximumContextLifetime = 300`**; refresh proactively at
  `refreshAhead = 60` before expiry; never hand out a token with less than
  `minimumUsableLifetime = 15` (a token that dies mid-request is a 404 and a
  retry for no reason).
- **Removal detection — two strikes, not one.** A signed refresh refused
  (uniform 404) twice, with the backoff below between the attempts, declares
  the device removed: stop retrying, wipe the cloud cache and the persisted
  binding, surface `deviceRemoved` ("this device was removed / pairing
  failed" — the 404 is uniform by design; do not try to distinguish unknown
  from revoked). The second strike exists because a deployment mid-restart
  also 404s once, and removal is not an action to take on a single sample.
  **Clock skew is excluded first:** a refusal with `|now - timestamp| >
  clockSkewTolerance (240 s)` is reported as `clockSkew`, not counted toward
  removal (past 240 s the device's own clock is the likeliest cause, and
  calling the device removed would be a lie).
- On 429/5xx/network on a refresh: backoff with `baseBackoff = 30` →
  `maximumBackoff = 300` (honor `Retry-After` when present, it always wins),
  never a tight loop. While a refresh is pending, in-flight reads may fail
  with a neutral "signing in…" state — never hammer.
- Airplane mode / doze: on return to foreground (and on `CONNECTIVITY`
  change), if context age > TTL just refresh once and continue; no special
  re-pairing.
- `DeviceState` is exactly the iOS tri-state: `unpaired / paired / removed`.
  `Failure` mirrors the iOS taxonomy (`notPaired, deviceRemoved,
  throttled(retryAfter), clockSkew(seconds), offline, server(…)`).
- **Single-flight**: one in-flight refresh at a time; the in-flight
  `refreshTask`/future is shared by all concurrent callers (the Swift actor's
  reentrancy made this fall out naturally; in Kotlin it is an explicit
  `Mutex`-guarded future).
- Pairing: `pair(code, label)` — Keystore key; the **label is sent with the
  pair request** (rider-set, appears in the desktop device list — iOS #234
  precedent); on success persist the binding (2.5) and hold the fresh
  `reader_context`.
- Reads: bearer `Authorization: Bearer <reader_context>` — **no per-read
  signing**. `GET /api/v1/context` (capabilities + current revision) then per
  screen route with `since=<stored revision>`; walk `next_cursor` to
  exhaustion, bounded by `maximumPages = 50`; checkpoint the (pinned)
  `revision` only after the walk completes; upsert objects by `(kind, id)`
  comparing `revision` (at-least-once safety); apply tombstones
  (`deleted: true`) to the cache.
- **All credentials in EncryptedSharedPreferences**
  (`androidx.security:security-crypto`, AndroidX — allowed): cloud binding
  (credential id, signing namespace, subscription key, capabilities), local
  device token + local server URL. The subscription key and the local token
  are bearer secrets: never in logs, never in crash reports, never in plain
  `SharedPreferences`. Add a test/grep gate for log statements (the #194
  "nothing sensitive" Done criterion) — a `Log`-free policy in `CloudClient`
  (errors as typed exceptions, UI renders).
- **Local backend client** (`LocalClient`):
  - Store: server URL (user-entered, Step 3) + device token (EncryptedShared
    Preferences).
  - On launch / on 401-ish failure / on reconnect:
    `POST /api/connector/session` (Bearer) → ticket → `GET /connector/
    session?token=<ticket>` (do **not** send the ticket anywhere else; the
    server redacts it only because the query param is named `token`). Hold a
    cookie jar across requests (HttpURLConnection: persist the `Set-Cookie`
    manually — there is no built-in jar; the session cookie is a plain signed
    cookie, store name+value in memory, re-mint on process death; the token
    is the durable half).
  - Revoked device: the middleware clears the session and the app will get
    redirects/401s → detect, show "device unpaired", clear local state.
  - **Transport:** cloud traffic uses the strict-nsconfig HTTPS
    `HttpURLConnection` path. The local server is plain HTTP on the rider's
    LAN; rather than weakening the #194 cleartext rule (manifest `false`,
    nsconfig loopback-only), `LocalClient` opens its own `java.net.Socket`
    and speaks minimal HTTP/1.1 (request line, headers, `Content-Length` and
    chunked decoding — both used by uvicorn). Cleartext is thereby opened only
    to the host the rider explicitly typed, on their LAN, and the platform's
    cleartext gate keeps covering every other connection. ~150 lines, unit-
    tested against the real local server (request → status → JSON body), plus
    the same no-secrets-in-logs rule. *If manual-HTTP proves a time sink,
    fall back to a release `domain-config` allowlist for specific LAN
    hostnames the rider builds with, and record the trade in the README — but
    do not flip `usesCleartextTraffic` on app-wide in release.*
  - Read endpoints per the local API list; the adapter (Step 4) pins the
    shapes from `curl`-captured fixtures.

### 2.4 Cache (Room)

Schema (v1): `objects(kind TEXT, id TEXT, revision INTEGER, data_json TEXT,
deleted INTEGER, fetched_at INTEGER, PRIMARY KEY(kind,id))`;
`checkpoints(route TEXT PK, revision INTEGER)`;
`local_payloads(key TEXT PK, payload_json TEXT, fetched_at INTEGER)` (local
backend's last response per screen query); `meta(key TEXT PK, value TEXT)`
(backend selection, server URLs, pairing facts that are not secrets).
- Apply walk = one transaction: upsert (only when incoming `revision` >
  stored), tombstone, then set `checkpoints[route] = pinned revision` after
  the last page.
- Screens read the cache first (instant open), then a refresh walk runs.
- Revoked/unpaired: wipe `objects`, `checkpoints`, the bindings — keep the
  app in a clean "not paired" state (no half-restored device, per #195).

**Done (per #194):** vector test green for every canonical case incl.
empty-body/unicode/boundary and every `cloud_objects_v1.json` item; a paired
device reads FTP end-to-end against the **cloud dev harness** (the slice #171
proved on iOS) — with PR #240's full-snapshot harness, all four screen
queries work; recovery matrix demonstrated and screenshot- or test-logged:
token expiry mid-use, one hour airplane mode, doze, revoked device (two-
strike removal → "removed" state + cache cleared; a single restart-404 does
*not* remove), server 429 (backs off, honors `Retry-After`), clock skew
(>240 s reported as clock skew, not removal); Settings shows StrongBox vs
fallback; nothing sensitive in `SharedPreferences` dumps or `logcat`.

---

## Step 3 — Pairing (issue #195 + local pairing)

- **Cloud pairing screen:** code entry (Crockford Base32, 12 symbols, shown as
  `XXXX-XXXX-XXXX`). Input normalization exactly as the server does it
  (`security.py`; iOS precedent `PairingCode.swift`): uppercase, strip
  `-`/spaces/tabs, fold `I`/`L`→`1`, `O`→`0`, `U` is illegal. **Send a
  rider-set `label`** with the pair request (e.g. "Pixel 9"); the label is
  validated by the server before the single-use code is spent, so a bad label
  cannot burn the code. Failures must be distinguishable in the UI — but note
  the server answers 404 identically for unknown/expired/consumed/wrong-
  subject by design, so the app can say "that code did not work" with a
  *hint list* (expired? already used? check characters) rather than a false-
  precise reason (iOS precedent `PairingFailureMessage.swift`: every failure
  that could depend on the code's value renders one shared sentence; only
  conditions provably independent of the code — no connection, 429/503
  admission control, client-side signing/decoding fault, clock skew — earn
  their own text). 400 (malformed request, e.g. bad key) is the only case
  where the failure is not the code's fault. No partial state on failure (key
  generated first, credential persisted only on success — the keypair is
  reusable; a failed pairing keeps the same Keystore key so re-pairing after
  a revoked device is a clean re-registration, and the new credential binds
  the same public key, which is fine because the old one is gone).
- **Local pairing screen:** server URL field (scheme/host/port validation;
  `http` allowed here only — this is the LAN path; a prominent "not encrypted"
  note) + device token pasted from the web UI (Instructions: "open the
  wattracker web UI → Settings → pair a device, label it, copy the token — it
  is shown once"). Validate over the network (`POST /api/connector/session`
  returns 200 or 401 — unlike cloud, **this one can distinguish**: 401 =
  wrong/revoked token; say so).
- **Settings screen shows paired state, per backend:** which rider (cloud:
  `signing_namespace` is opaque — show the display name from the cached
  `profile`, not the namespace; local: username from the session), when paired,
  key type (cloud), server (local), and **"remove this device"**:
  - cloud → **real in-app revocation now exists** (#153): call
    `POST /api/v1/devices/{credential_id}/revoke` with the device's own
    signed envelope (self-revocation is explicitly allowed), then clear the
    binding + cache locally on 2xx. On the uniform 404, fall back to a local-
    only clear and say so ("could not confirm with the server; the pairing
    may already be gone — the device list will settle it"). Optionally list
    the rider's devices via `GET /api/v1/devices` (same-scope only; revoked
    entries appear flagged) — a nice-to-have on the same screen, no extra
    auth surface.
  - local → clears locally **and** tells the rider to Revoke in the web UI
    (in-app revocation would need a small JSON route on the server — out of
    scope for this epic; filed, not built).
- Re-pairing after removal works on both backends and does not resurrect the
  old credential.

**Done (per #195):** pair → read on cloud (dev harness) and local (desktop
server); expired/reused/malformed code fails with the right message and no
partial registration; revocation (local: web UI Revoke; cloud: in-app revoke
route, plus simulated server-side revocation) makes subsequent reads fail
like an unknown device; in-app cloud remove is durable across an app restart
and shows up flagged in `GET /api/v1/devices`; re-pairing is clean.

---

## Step 4 — Dashboard screen (issue #196)

- Source (per backend) via the `ReadModel` interface:
  - cloud: `training_state` (CTL/ATL/TSB/decoupling), `load_point` series
    (selectable window), `curve`, `profile` (current FTP, recent-rides strip
    data from the first page of `activities`). Models checked against
    `tests/vectors/cloud_objects_v1.json`.
  - local: `/api/state` (ctl/atl/tsb, ftp, cp/w'), `/api/load?months=`,
    `/api/curve`, `/api/activities` (most recent N for the strip).
- **Do not recompute training load on device** — render what the backend
  publishes (desktop is the source of truth).
- Load chart: **Compose `Canvas`** line + bars (a few thousand points max);
  no charting dependency. Selectable window (e.g. 30/90/180 days).
- Wide-short-first: phone landscape is the primary layout (rail + content
  columns); tablet adds columns, same screen.
- States: never synced (honest empty, pair CTA), stale (cache older than last
  known revision, "last synced …" + offline badge), offline (local: server
  unreachable — last payload with stale badge), quota 429 (cooldown notice
  with `Retry-After` value).
- **History cutoff (#172):** render exactly what the publisher sends; never
  reintroduce pre-cutoff objects from a stale cache (the cache is not
  authoritative — a delta can withdraw; tombstones/absence win over stored
  ghosts).

## Step 5 — Activities list + ride detail (issue #197)

- List: newest first, `date, duration, distance, NP, IF, TSS`; lazy-loaded
  (no full history in memory); paging: cloud = `since`+cursor walks against
  the cache (newest-first view derived from cached `activity` objects); local
  = `/api/activities` (verified to return the **full history** newest-first —
  the app pages it client-side and never holds it all, per #197's no-
  unbounded-memory criterion).
- Detail: summary + **power trace (1500-point downsample as published — no
  resampling, no raw requests)**, HR where present, zone time from
  `activity_detail.zones`. **Corrections are already applied server-side** —
  display the trace as-is; never "fix" ranges client-side.
- Phone landscape: **list and detail side by side** (the wide-short viewport
  has the width; push navigation costs the round-trip on the scarce axis) —
  state the justification in the layout code. Tablet: same, wider.
- Cutoff: a hidden ride is **absent**, not greyed.
- Perf check per #197's Done: realistic history scrolls without jank; verify
  with `adb shell dumpsys meminfo` before/after a long scroll (no unbounded
  growth) — the 1500-point downsampling decision gets validated here on a real
  device.

## Step 6 — Calendar + Volume (issue #198)

**Preamble — server PR (lands before any screen work on the local calendar):**
In `wattracker/server.py`, extract the month-data construction from
`calendar_view` (the block from `ooto_ranges` through the per-day workout/
activity/race/phase assembly, `server.py:4079` ff.) into a module-level
`build_calendar_month(uid, year, month)` function; `calendar_view` calls it,
and a new read-only route `GET /api/calendar?year=&month=` (same defaults and
month normalisation as the HTML route: current month, 1..12 wrap) returns
`build_calendar_month(...)` as JSON. No new auth (session, like the other
`/api/*` routes), no new db access (the builder already takes the db
functions), no change to any existing response. Focused test: a fixture month
with plan workouts (incl. one completed, one past-missed, one ooto-skipped),
a standalone workout, an activity, a race with demoted priority, and an ooto
range — assert the JSON carries the same flags the HTML calendar renders, and
that a history cutoff hides pre-cutoff activities. Full suite green per
AGENTS.md. This PR is Python-only and does not touch `wattracker/cloud/` or
the migrations.

- Calendar: month grid; planned workouts + completed rides in the same cells,
  visually distinct; day tap → day detail (workout/ride names, race, ooto
  phase). Source: cloud = `calendar_day` objects (handle `part`/`parts` split
  days by merging on `(kind, date)`); **local = `GET /api/calendar`** (the
  preamble PR) — the adapter is written against its test-pinned shape and
  renders whatever the builder computes (skipped/missed/adjustment, effective
  race priority, ooto, phase), so the app cannot drift from the desktop page.
- Volume: weekly `hours, tss, distance_km, calories`; last-N-weeks bars/table.
  Cloud = `volume_week`; local = `/api/volume`.
- **Dates are rider-local as published — never re-bucket on device** (the
  desktop already bucketed in the rider's timezone; a client-side re-bucket is
  a day/week-boundary bug waiting to happen, as happened on the desktop side).
- Cutoff: weeks/days before the cutoff are **absent, not empty**.
- Demand layout: month grid in phone landscape is the hard case; tablet
  portrait must be correct too.
- Cross-check requirement: weekly volume numbers must **match the desktop's**
  for the same rider/period — verify by eye against the web UI in the same
  session (do not assume).

**Done (all screens, #196–#198):** correct on phone landscape and tablet both
orientations; empty/stale/offline states render honestly; screenshots attached
per issue; both backends exercised on the same screen set (cloud via the
full-snapshot harness, local via the desktop server).

---

## Step 7 — Play Console internal testing (issue #199)

Owner-side (not code): Google Play Console developer account (**one-time $25**),
create the app entry, create the **internal testing track** (not open/closed —
up to 100 testers by email, no review queue, builds don't expire).

Code/config:

1. **App signing:** enroll in Play App Signing. Generate an **upload key**
   (`keytool -genkeypair -v -keystore wattracker-upload.jks -alias
   wattracker-upload -keyalg RSA -keysize 2048 -validity 10000`); the keystore
   + password go into a **GitHub repository secret** (or the runner's keystore
   dir) — never into git. The root `.gitignore` already covers `*.key`/`*.pem`/
   `*.p12`; add `*.jks`, `*.keystore`. Committed files contain no key material
   — verify by inspecting the PR diff, per #199's Done.
2. **Workflow** `.github/workflows/android-release.yml`, mirroring
   `cloud.yml`'s guardrails (fork-PR exclusion, pinned toolchain, workspace
   install):
   - trigger: tag `android/v*` (or push to a release branch — decide at
     implementation; tag-triggered per #199).
   - `runs-on: [self-hosted, macOS]` **by default** (the deferred decision
     from the owner: works today on `macos-ci`; switching to a Linux
     self-hosted runner later is this one label — keep the job OS-agnostic:
     JDK via Temurin, Android cmdline-tools installed in the workspace, no
     Docker).
   - steps: JDK + cmdline-tools (in-workspace install, no global mutation) →
     `./gradlew :app:bundleRelease` (release build config carries the cloud
     host) → `bundletool build-apks`/`zip` as needed → upload to the internal
     track via the Play CLI / JSON API with the service-account or upload-
     key secrets. No manual Android Studio steps.
   - First run on the shared `macos-ci` runner will download Gradle/SDK/
     AndroidX (GBs) — note the one-time cost; keep `GRADLE_USER_HOME` on the
     runner to make repeat builds fast.
3. **Docs** (`docs/android-distribution.md`): how a rider joins the internal
   track (invite by email, Play Install on the device, the app is sideloaded —
   pairing still goes through the in-app pairing screens), how to promote a
   build, how to rotate the upload key.
4. **The "pair against a real deployment" half is gated on #102 + #156:**
   the deployment is an unexecuted runbook and the desktop app does not yet
   mint pairing codes against it. Nothing here blocks on that — the internal
   track can run and riders can pair against the dev harness / local backend —
   but say so in the PR description if the Done wording implies a deployed
   cloud.

**Done (per #199):** both riders install from the internal track and pair
(with a code — harness/local until the deployment exists); release job runs
from a tag with no manual steps; no signing material in the repository (diff
inspection).

---

## Validation plan (cross-cutting)

1. **Unit (JVM, in `android/app/src/test`):** canonical vectors (all cases,
   digests, distinct pairs, DER→raw on both s-vectors) from
   `tests/vectors/canonical_request_v1.json`; object-shape round-trips for
   every `tests/vectors/cloud_objects_v1.json` item; pairing-code
   normalization (incl. `I/L/O` folding, illegal `U`); refresh state machine
   (single-flight, one-then-second-strike removal, 429 backoff,
   `Retry-After` precedence, 404→two-strikes→removed, clock-skew exclusion,
   `maximumPages` bound) against a fake HTTP layer, constants asserted equal
   to the Swift `CloudSession` values; Room apply-walk idempotency
   (re-delivered objects, tombstones, pinned revision); DER parsing edge
   cases (sign bytes, short integers).
2. **Live, cloud (gated on PR #240):** `python scripts/walking_skeleton_
   server.py --user-id <rider>` (`.venv` with `.[cloud]`) → pair from the app
   on `wt-phone` (emulator, `http://10.0.2.2:8765`) → Dashboard FTP matches
   the DB value (the #171 slice, repeated on Android) → all four screens
   against the full snapshot → in-app revoke (2xx → removed state; device
   listed with revoked flag in `GET /api/v1/devices`) and simulated
   server-side revocation (two 404 strikes → removed; one 404 does not).
3. **Live, local:** `WATTRACKER_HOST=0.0.0.0 ./start.sh` → pair the phone
   (emulator: `10.0.2.2:8000`) → all four screens → Revoke in the web UI →
   session dies on next request → app shows unpaired. Also one physical-
   device run over the LAN (real IP, not loopback) for the cleartext
   transport.
4. **Orientation/idiom:** screenshots on `wt-phone` landscape, `wt-tablet`
   portrait + landscape (and rotated live) — the #193/#196–#198 Done
   criteria; the tablet-portrait correctness is the standing requirement.
5. **Recovery matrix (screenshot/test log per #194):** token expiry mid-use;
   1 h airplane mode; doze (`adb shell svc power suspend`); revoked device;
   429 (harness with a tight quota — the durable quota is application-level,
   so the harness is the 429 source).
6. **Python suite:** `.venv/bin/python -m pytest` green before any merge
   (AGENTS.md). The full-snapshot harness tests come with PR #240; the
   `GET /api/calendar` PR (Step 6 preamble) carries its own focused tests
   (flag parity with the HTML calendar on the fixture month, month wrap,
   cutoff hiding).
7. **Dep audit:** `./gradlew :app:dependencies` reviewed — only AndroidX,
   Kotlin stdlib, room, security-crypto (and their transitive AndroidX). Any
   other artifact fails the review.
8. **Dev-loop evidence:** the implementer (or the IDE agent) runs at least the
   Step-1 build and an install+launch through the 0.4 loop (Agent mode in
   Android Studio, and/or the terminal `gradlew` + `adb` equivalents); record
   which BYOK provider/model was used — no key, ever.

## Risks & known trade-offs

- **IDE agent is a dev accelerator, full stop.** Agent mode is a supported
  first-party feature, so there is no community-plugin fragility to plan
  around, but nothing about it is load-bearing: every Done criterion and
  validation step stands on `./gradlew` + `adb` (which is what CI runs). The
  only real exposure is the BYOK key — it lives in IDE-local settings on one
  machine and must never enter the repo, a PR, a screenshot of Settings, or a
  log. Rotate the key if it is ever printed.
- **PR #240 dependency.** Step 2's Done and validation item 2 assume the
  full-snapshot harness, which lives in the open iOS PR #240. Until it
  merges, harness-based validation covers pairing/refresh/FTP only (the
  `main` harness publishes a single `profile` object). Do not duplicate the
  harness change in an Android branch; rebase after #240 lands and note the
  dependency in PR descriptions.
- **Android 16 large-screen behavior (targetSdk 36):** orientation locks are
  ignored on large screens — the iOS twin of this bug cost #171 a
  measurement. Mitigation: the 0.2 AVD measurement is a Step-1 prerequisite
  for screen work; every tablet layout correct in portrait.
- **DER→raw signature conversion** is the single most likely cause of a
  silent 401. Mitigation: vectors + unit tests before any live call; the live
  harness refresh is the final proof.
- **Local session = full account authority.** Read-only is app-side only;
  token leak = full login. Mitigations: EncryptedSharedPreferences, no logs,
  prominent Revoke guidance, "remove this device" copy that doesn't overclaim.
  Accepted by the owner (the connector model is the explicit request);
  document in `android/README.md`.
- **Deployed cloud does not exist yet.** #102's deployment is an unexecuted
  runbook; #156 (desktop cloud sync, which mints pairing codes on a real
  deployment) is unmerged. Consequence: #199's "pair with a code" and any
  "real cloud" validation run against the harness or the local backend until
  both land. APIM is no longer in the picture at all — there is no gateway
  gap to close, only a deployment to execute.
- **Cloud self-revocation is a documented server trade-off.** The revoke
  route deliberately allows any same-scope credential — including the target
  — to revoke (a lost-phone rider has the other device in hand). Corollary:
  a stolen phone can revoke its siblings. The server docstring calls this
  bounded (devices only, never the writer). Keep the in-app copy honest
  ("remove this device from this account") and mirror the trade in
  `android/README.md`.
- **Quota budget:** durable per-scope quota, deployment-configurable;
  defaults 50 000 read requests/day and 512 MiB read bytes/day
  (`wattracker/cloud/limits.py`). Steady state (foreground refresh ~5 min +
  screen opens) is far under that; the client must honor `Retry-After` and
  the budget math lives in a `CloudClient` comment so nobody tightens the
  poll interval later without doing the arithmetic.
- **Shared macOS runner:** first Android build is a heavy download; CI and
  Cloud workflows share one self-hosted box — keep build times bounded
  (`assembleDebug`/`bundleRelease` only, no emulator in CI).

## Explicitly out of scope

- `.fit` uploads (owner decision, 2026-09-01: out of scope; new issue later).
- Server-side changes beyond: (a) the owner-approved small read-only
  `GET /api/calendar` JSON route sharing the extracted `calendar_view` month
  builder (Step 6 preamble). The full-snapshot harness extension that the
  2026-09-01 draft planned is **already built** in PR #240 — it is not this
  epic's work. Everything else server-side (in-app *local* revocation, any
  write routes) is out of scope; file separate issues first.
- Light theme, widgets, notifications, BLE, anything write-path on the cloud
  (capability grants exist server-side; the app stays read-only).
- A second language/region pass (rider-facing app, two riders; strings in
  `values/` only, no translation).
- Any MCP server configuration (community or built-in IDE MCP, npm bridges):
  dropped 2026-09-06 in favor of the IDE's native Agent mode + BYOK.

## Suggested order of work

0 (prereqs + IDE agent) → 1 skeleton (#193) → 2 clients + cache (#194) → 3
pairing (#195) → 3b local-calendar server PR (shared `build_calendar_month` +
read-only `GET /api/calendar`, green Python suite) → 4/5/6 screens (#196,
#197, #198 — independent of each other after #195; #198's local half needs
3b) → 7 Play internal testing (#199, unblocked by #193 but its Done
criterium needs #195). External dependency to track, not build: PR #240
(#234) merging, which carries the full-snapshot harness that Step 2's Done
and validation item 2 assume. Each step ends with its issue's Done criteria
checked and a PR (screenshots included where required).
