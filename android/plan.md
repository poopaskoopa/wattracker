# Android client for wattracker — implementation plan

Epic #192 and sub-issues #193–#199, plus the owner's
extra targets: Android 11+, dual-server support (cloud **and** local), offline
cache. Kotlin + Jetpack Compose, single app under `android/`.

**Resume point — Step 1 is done and merged (PR #257).** `android/` is on
`main`, so #194–#199 are unblocked. Verified on both AVDs (`medium_phone`
API 36, `medium_tablet` API 35): rail in landscape, bottom bar in portrait,
drawer on tablet. README measurement table filled. Pre-push hook installed.
**Next: Step 2 — Cloud API client + local client (issue #194 + local
backend).** See "Step 1 — completion notes" below.

**Revised 2026-09-09 — local transport settled.** The #257 review's open
transport question and the tailnet requirement recorded on #192 (2026-09-08)
resolve the same way: **the local backend is reached over HTTPS, terminated by
whatever front end the rider runs.** `tailscale serve` and an ordinary reverse
proxy (nginx, Caddy) are equally accepted — the tailnet was the *mechanism*
that comment picked, not the property it argued for, and either satisfies all
three properties it actually mandated (strict release NSC, no raw-socket
transport, loopback bind preserved). Widening it to any TLS terminator costs
nothing and means a rider without a Tailscale account is not required to get
one. The app speaks TLS to both backends and never cleartext in a release
build. Full rationale in the Resolved decisions table ("Local backend
transport"); spec in 2.3. The raw-socket path and the pinned self-signed cert
are both dead — do not build either. The Android CI decision is now answered
too (hosted `ubuntu-latest`, #259).

**Revised 2026-09-06.** Re-verified against `main` at `8c75eba`. What changed
since the 2026-09-01 draft:

1. **Tooling (owner decision 2026-09-06):** The dev agent is Android Studio 4's
   **built-in Agent mode with BYOK** (bring-your-own-key).
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
| Architecture | **Dual backend.** (1) Cloud read plane as #194–#198 specify (pairing code → Keystore P-256 → signed context refresh → `GET /api/v1/context/*` with the bearer reader context). (2) Local desktop server via the **connector model**: device token paired in the web UI Settings → session → local JSON API (`/api/state`, `/api/activities`, …), reached **over HTTPS through the rider's own reverse proxy** (see the transport row below). Screens read through one common `ReadModel` interface with one adapter per backend. |
| Local backend transport | **HTTPS only, terminated by whatever front end the rider runs** (owner, 2026-09-09). The requirement is TLS in front of the local server — **`tailscale serve` and an ordinary reverse proxy (nginx, Caddy) are both accepted**, and the app cannot tell them apart: it sees an HTTPS origin with a chain to a system trust anchor. The tailnet stays a valid deployment for anyone who wants it; it is no longer a *requirement*, because it was the mechanism the #192 comment (2026-09-08) picked, not the property it argued for. In either case the desktop server keeps its loopback bind and speaks plain http to a co-located terminator. The app never speaks cleartext in a release build: `usesCleartextTraffic="false"`, release `network_security_config.xml` strict, **no `domain-config`, no pinning, no custom `TrustManager`**. **No hostname is baked in anywhere** — the rider types the origin, so any front end, name, port or vhost works. This keeps all three properties #192's comment mandated (strict release NSC, no raw-socket transport, loopback bind preserved) while requiring no third-party account of a rider who does not already have one. Supersedes both options the #257 review put up: the raw-socket cleartext path and the self-signed cert pinned at pairing. |
| minSdk / targetSdk | minSdk **30** (Android 11, owner's floor; StrongBox API is 28+, Keystore EC P-256 is long stable). targetSdk **36** — Google Play has required API 36+ for new apps and updates **since 2026-08-31** (verify current policy at implementation time). |
| Release CI runner (#199) | **Hosted `ubuntu-latest`** (owner, on #257; supersedes the earlier "deferred, plan for `macos-ci`" answer, which predated the repo going public). Standard hosted runners are unmetered on a public repo and `cloud.yml` already runs `bicep-validation` and `containerized` on `ubuntu-latest`. `macos-release.yml`'s `if: ${{ false }}` and any note claiming Actions are blocked at the account level are stale. The build/unit-test job is tracked as **#259**, with an untested starting workflow in the #257 thread; the two gotchas it encodes are `compileSdk 37` (not on the runner image — accept SDK licences so AGP can fetch it) and `gradle/gradle-daemon-jvm.properties` pinning `toolchainVersion=25` (pin it with `setup-java` or Gradle re-downloads a JDK from foojay every run). Do **not** copy `cloud.yml`'s fork gate: it exists for a persistent physical runner, and a hosted ephemeral Android job can safely cover fork PRs. |
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
- **Reaching it from the phone is the proxy's job, not a wider bind.** The
  server binds `WATTRACKER_HOST` (default `127.0.0.1`) on `WATTRACKER_PORT`
  (default `8000`), and `config.server_host()` (`config.py:625`) refuses any
  non-loopback value unless `WATTRACKER_ALLOW_NON_LOOPBACK=1` is *also* set
  (`config.py:580` — a deliberate second opt-in whose docstring notes that
  every other control here, the Host allowlist and the cookie flags included,
  was written assuming it never happens). With the proxy on the same machine
  it never happens: the terminator dials `127.0.0.1:8000`, which *is* a loopback
  connection, so **neither variable moves off its default**. Only a proxy on a
  different box needs the pair — and there a host firewall rule, not a new
  app-level peer allowlist, is what limits who may dial the port.
- **What the rider does set** (`docker-compose.yml:31-35` and
  `README.md:540-549` say the same three): `WATTRACKER_COOKIE_SECURE=1` — the
  session cookie gets `Secure` (`config.py:752`, wired to `SessionMiddleware`'s
  `https_only` at `server.py:1595`); `WATTRACKER_PUBLIC_SCHEME=https`
  (`config.py:763`, already the default, because a fronting terminator was
  always the assumed shape); and `WATTRACKER_PUBLIC_HOSTS=<the hostname the
  phone uses>` — the tailnet name under `tailscale serve`, the proxy's
  `server_name` otherwise
  (`config.py:730`, comma-separated, bare hostname/IP entries, strictly
  validated, no wildcards), because the Host-header allowlist
  (`IPv6TrustedHostMiddleware`, `server.py:1056`) defaults to loopback +
  `testserver` and 400s anything else before routing. The proxy must pass the
  original `Host` through (`proxy_set_header Host $host;`) and forward at the
  **root** — the app mints absolute-rooted paths and has no `root_path`
  support, so a path-prefix mount will not work.
- **The README's "buttons return 403 behind an https proxy" wrinkle does not
  reach this app** (`README.md:548`). That guard is `_same_origin_or_absent`
  (`server.py:1317`) and its first line accepts a request carrying **no
  `Origin` header** — the codebase's convention for native clients.
  `HttpURLConnection` sends none, so `POST /api/connector/session` passes
  cleanly through the proxy. Two adjacent facts verified at the same time:
  `connector_session_redeem` returns a **relative** `Location: /`
  (`server.py:3021`), so the 303 cannot leak the backend's scheme or port to
  the phone; and `_ws_origin_ok` (`server.py:5622`) compares **host only**
  against `public_hosts()`, so the live-ride WebSocket survives the proxy too
  (nginx needs the usual `Upgrade`/`Connection` headers; `tailscale serve`
  handles it). The 403 stays real
  for the rider's *browser* over the proxy — so pair a device from the desktop
  at `127.0.0.1`, not from the phone browser.
- ⚠️ **`WATTRACKER_COOKIE_SECURE` goes in the launch environment, not a shell
  export.** `conftest`'s `delenv` list does not clear it, so an exported value
  produces mass Python-suite failures that look unrelated to it (#249, and the
  owner repeated the warning on #257). Same care as any other server env var
  set for a phone-facing run.
- **Docs corrected 2026-09-09** (was Step-1 debt, now done): `android/README.md`
  no longer claims the emulator needs `WATTRACKER_HOST=0.0.0.0` +
  `WATTRACKER_ALLOW_NON_LOOPBACK=1` — it needs neither — and both shipped
  config comments have dropped the superseded LAN framing (#260, item 3).
- **Security fact to keep visible:** a connector-origin session is a *full
  user session* — the server refuses only a handful of credential-minting
  routes for it (`server.py` `_from_connector` checks,
  `server.py:2990-3011`). Read-only on the local backend is a property of
  *this app*, not of the server. A leaked local device token is a full
  account login. HTTPS takes that token off the wire; it does not take away
  what the token *is*, so a leak from the phone, a screenshot or a paste buffer
  is still a full login. The existing Revoke button remains the mitigation.
  State this in the Settings UI copy ("remove this device") and in
  `android/README.md`.

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

## Step 0 — Prerequisites (install & set up first) — ✅ done

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
  $env:WATTRACKER_PUBLIC_HOSTS = "10.0.2.2"
  .\.venv\Scripts\python.exe wattracker\server.py
  ```
  **The bind stays on loopback for the emulator path** — one variable, not two.
  `10.0.2.2` is the emulator's NAT alias for the *host's* loopback, so a
  `127.0.0.1`-bound server is already reachable; only the Host-header allowlist
  needs the name, because the server 400s any `Host` it does not answer to.
  (Never `127.0.0.1` from an emulator — that is the emulator's own loopback.)
  - emulator, debug build → `http://10.0.2.2:8000`, under the `debug` source
    set's NSC exception;
  - physical device, or any release-shaped run → `https://<hostname>` through
    the rider's TLS terminator (`tailscale serve` or a reverse proxy — either),
    with that name in `WATTRACKER_PUBLIC_HOSTS` and
    `WATTRACKER_COOKIE_SECURE=1` set. A wider bind (`WATTRACKER_HOST` +
    `WATTRACKER_ALLOW_NON_LOOPBACK=1`) is needed **only** if the terminator
    runs on a different machine than the server.
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
   install`, `adb shell am start`, `adb shell uiautomator dump` (hierarchy —
   the reliable per-device UI check on this machine; on-disk `screencap` is
   broken here, see "How to inspect / capture the UI" in the Step 1 notes),
   `adb shell input`, `adb logcat`, `adb emu console`), and nothing in the
   validation plan requires the IDE agent. This is what CI runs. If Agent
   mode is unavailable or misbehaves, the whole plan still executes from the
   terminal.
4. **Verify the whole loop before any Step 1 work:**
   - Android Studio has the (future) `android/` project, or a scratch Compose
     project, open; the model picker shows an enabled BYOK model; Agent mode
     is on.
   - Instruct the agent to build + deploy the scratch app to `wt-phone`,
     pull a screenshot, and read a few logcat lines. Do the same three things
     once from the bare terminal (`\.\gradlew.bat :app:installDebug`, `adb
     shell uiautomator dump` (the UI check), `adb logcat -d`) so both paths
     are known-good.
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

## Step 1 — completion notes (2026-09-11, session 3)

**Status: DONE, in review.** PR #257 (draft, `feature/android-client` →
`main`) has three commits, all noreply identity (`wattrackerboss@users.
noreply.github.com`), rebased onto current `origin/main` (`14e4c4a`):
`02b59db` scaffold, `ac15376` shell, `52fb520` nav fix. The pre-push hook is
installed in this clone. The owner will merge when satisfied.

**Verified on device (both AVDs booted, app installed + exercised):**

- Phone (`medium_phone`, emulator-5554, API 36): **leading rail** in
  landscape (5 destinations), **bottom `NavigationBar`** in portrait
  (5 destinations, conventional thumb layout). The portrait bottom bar was
  added in session 3 — the plan's "leading rail" was only the right call for
  the wide-short landscape viewport.
- Tablet (`medium_tablet`, emulator-5556, API 35, Pixel_Tablet): **permanent
  drawer**, correct in both portrait and landscape.
- `smallestScreenWidthDp` measured from `dumpsys` (not assumed): phone 411dp
  (landscape); tablet **800dp in both orientations** → the `>= 600` predicate
  is rotation-stable (no rail/drawer flip). README table filled.
- Nav highlight unified: the rail, bottom bar and drawer rows all paint the
  same `Palette.accent`/`Palette.muted` scheme explicitly (session-3 fix — the
  rail had been falling back to M3 theme defaults, a different highlight).
- Dev loop end to end: `:app:assembleDebug` green, `adb install`/`am start`,
  screenshot, `logcat` (app pid clean, no `FATAL`).

**Deviations from the plan (flagged in the PR):**

1. **AVDs are the new `android` CLI's size profiles** (`medium_phone`,
   `medium_tablet`), not `pixel_9`/`pixel_tablet` — the old `avdmanager` NPEs
   on this SDK and the new CLI exposes profiles, not device models. The tablet
   lands a Pixel_Tablet model anyway.
2. **The tablet x86_64 image is API 35, not 36** — only
   `google_playstore_tablet/x86_64-35` is available. `minSdk` 30, so it runs;
   the phone stays on API 36.
3. **`material-icons-extended`** is one dep beyond the plan's list — AndroidX,
   the Android twin of the iOS shell's "SF Symbols ship with the system" rule.

**Screenshot note:** on this host, every on-disk capture path (`adb exec-out
screencap`, `adb screencap`+`pull`, `android screen capture`) returns a stale
lock frame even with `isKeyguardShowing=false` and the lock screen disabled — an
AEHD/Hyper-V surface-capture quirk, not an app problem. The IDE's capture of
the emulator window is the reliable path (and what the PR images come from).
Documented in `android/README.md`. The `dumpsys` measurements are unaffected.

**How to inspect / capture the UI on this machine (learned in session 3 —
every Step-2+ Done criterion that asks for a screenshot or UI check uses
these paths):**

- **Visual screenshot (to eyeball layout / a PR image):** the IDE's
  `take_screenshot` tool, or the agent's `ui_state` tool (returns a PNG of the
  focused window plus the XML hierarchy). This reads the emulator *window*
  directly, so it shows the live app. **Do not** use `adb exec-out screencap`,
  `adb shell screencap`+`pull`, or `android screen capture` for a visual shot
  here — all return the stale lock frame. The IDE capture is ephemeral
  (not saved to disk) and, being `gh`-PR-incompatible, the owner attaches the
  PR images manually.
- **UI hierarchy (the reliable, per-device, scriptable path):**
  `adb -s <serial> shell uiautomator dump /sdcard/ui.xml` then
  `adb -s <serial> pull /sdcard/ui.xml out.xml`, and parse the XML (each
  `node` has `text`, `content-desc`, `class`, `bounds`, `clickable`). This
  works per-serial (unlike `ui_state`, which only sees the IDE-attached
  device), needs no surface capture, and is how the portrait bottom-bar vs.
  landscape rail was verified (bounds of the five nav labels: one horizontal
  row at high Y = bottom bar; stacked at low X = left rail).
- **Config measurement (swdp / orientation / rotation):**
  `adb -s <serial> shell dumpsys activity <package>` → `mCurrentConfig`
  (`smallestScreenWidthDp`, `orient=`, `ROTATION_`), and
  `dumpsys input` → `mCurrentOrientation`. This is unaffected by the
  screenshot quirk — use it for the numbers, not pixels.
- **Forcing orientation:** `adb -s <serial> shell settings put system
  accelerometer_rotation 0` then `settings put system user_rotation <N>`.
  Phone: `0`=portrait, `1`=landscape. Tablet (natural = landscape): `0`=
  landscape, `1`=portrait. Then `am start -n <pkg>/.MainActivity` to re-apply.
- **Keeping the screen awake** (for repeated captures): `adb -s <serial>
  shell "settings put system screen_off_timeout 1800000; svc power stayon
  true"`.

**The rest of this section is retained for the next steps** — the BOM API
pitfalls (do not re-derive) and the toolchain state on this machine.

**Done in code (both variants build green):**

- `gradle/libs.versions.toml` + `app/build.gradle.kts`: navigation-compose
  2.10.0, room 2.8.4 (runtime/ktx/compiler), security-crypto 1.1.0,
  KSP 2.2.10-2.0.2 (the tag for Kotlin 2.2.10; `2.2.10-2.0.3` is a 404),
  material-icons-extended (BOM-managed). **Deviation to flag in the PR:**
  icons-extended is one line beyond the plan's dependency list — it is
  AndroidX and is the Android twin of the iOS shell's "SF Symbols ship with
  the system" rule (rail/drawer render platform `ImageVector`s, no bundled
  image assets). Build config: `buildConfig` on; `WATTRACKER_CLOUD_SCHEME` +
  `WATTRACKER_CLOUD_HOST` per build type (debug `http` / `10.0.2.2:8765`;
  release `https` / `cloud.wattracker.example` placeholder for Step 3).
- Manifest: `INTERNET`, `usesCleartextTraffic="false"`,
  `networkSecurityConfig="@xml/network_security_config"`.
- `res/xml/network_security_config.xml` (release: base cleartext false) and
  `app/src/debug/res/xml/network_security_config.xml` (debug: `domain-config`
  cleartext for `localhost`, `127.0.0.1`, `10.0.2.2`).
- `ui/theme/Palette.kt` (the CSS `:root` values, incl. `hr`) and dark-only
  `Theme.kt` (`darkColorScheme(…)` overrides; no dynamic color, no light).
  Template `Color.kt`/`Type.kt` deleted.
- `shell/Destination.kt` (five destinations + `ImageVector` icons + routes),
  `shell/Panel.kt` (`Panel`/`ScreenScaffold`/`StubPanel`, mirrors
  `Theme/Panel.swift`), `shell/RootScreen.kt` (phone `NavigationRail` /
  tablet `PermanentNavigationDrawer`; idiom predicate
  `smallestScreenWidthDp >= 600`, rationale in KDoc), five stub screens,
  `MainActivity` renders `RootScreen`.
- Root `.gitignore`: Android section added.
- `android/README.md`: written (build/run, both AVDs, rail/drawer
  rationale, dependency rule, BYOK pointer, `WATTRACKER_HOST=0.0.0.0` **and**
  `WATTRACKER_ALLOW_NON_LOOPBACK=1` for the emulator path — the second var is
  the gate `wattracker/config.py:server_host()` actually enforces, which the
  0.3 note abbreviates). **Its "Measured on the AVDs" table is still
  placeholder** — fill it in the device-verification pass.

**API ground truth for this BOM (2026.02.01 → M3 1.4.0, foundation 1.10.4) —
cost several compile cycles to find, verified against the resolved jars; do
not re-derive:**

- Foundation 1.10 **removed** the window-size-class / window-metrics APIs
  (`WindowWidthSizeClass`, `currentWindowWidthSizeClass`, and the
  `currentWindowMetricsInfo` replacement are absent from `foundation` *and*
  `ui`). The `smallestScreenWidthDp` predicate in `RootScreen.kt` is
  deliberate, not a shortcut (its KDoc says so).
- The M3 1.4 drawer API was revamped: `PermanentNavigationDrawer(
  drawerContent, modifier, content)` — **no `gesturesEnabled`**
  (modal-only); `PermanentDrawerSheet` **has no `containerColor`** — paint
  the background on the sheet's content instead. `DismissibleNavigationDrawer`
  / `DismissibleDrawerSheet` are the new names alongside the modal pair.
- M3 1.4 `ColorScheme` constructors have **no default arguments** — build
  the scheme from `darkColorScheme(…)` with named overrides.
- material-icons-extended 1.7.x is KMP: **no drawable XMLs** in the AAR; the
  icons are `ImageVector`s in `androidx.compose.material.icons.filled` (same
  package as core, e.g. `Icons.Filled.Speed`). Also: the plain
  `material-icons-extended-1.7.8.aar` Maven URL 404s — the KMP redirect
  makes the real artifact `material-icons-extended-android` (same for
  `material3-android`, `ui-android`, `foundation-android`).

**What comes next — Step 2 (issue #194 + local backend):**

1. **Keying (2.1):** P-256 keypair in the Android Keystore, StrongBox-backed
   with plain-Keystore fallback; assert non-exportability in a unit test;
   state which key was obtained in Settings. Mirror `DeviceKey.swift`.
2. **Canonical request + signing (2.2):** `CanonicalRequest.kt` framing
   byte-for-byte; DER→raw `r‖s` conversion (no low-s normalization); JVM unit
   tests against `tests/vectors/canonical_request_v1.json` and
   `tests/vectors/cloud_objects_v1.json`.
3. **Cloud session (2.3):** port the iOS refresh state machine
   (`CloudSession.swift` — single-flight, two-strike removal, clock-skew
   exclusion, backoff bounds, `DeviceState`/`Failure` taxonomy) keeping the
   constants in sync with the Swift names. Live signed refresh against the dev
   harness is the integration proof (depends on PR #240's harness, lands with
   #234).
4. **Local client + `ReadModel`:** connector-model token → local JSON API;
   one `ReadModel` interface, one adapter per backend, so the screens in
   Steps 4–6 read through it.
5. **Room cache (2.x):** revision-keyed cloud cache + last-payload local
   cache (~10 small tables, KSP codegen).

> [!CAUTION]
> **One decision still open before Step 2 code lands** (both were flagged in
> the #193 review; the transport half is now resolved — see Resolved
> decisions → "Local backend transport"):
>
> 1. **No Android CI exists.** `.github/workflows/` has cloud, ios-release,
>    macos-release, windows-release and windows — none compiles `android/`.
>    Nothing in the repo verifies the module builds, and the only test is the
>    (small) `DestinationTest`. Add an `:app:assembleDebug` +
>    `:app:testDebugUnitTest` job (windows runner or the `macos-ci` self-hosted
>    one — the latter must have SDK Platform 37 provisioned; see the
>    `compileSdk 37` / build-tools 36 note in the review). A Compose
>    `createComposeRule` test that asserts rail @ sw411/landscape, bottom bar
>    @ sw411/portrait, drawer @ sw800 (config-override driven, no AVDs) is the
>    high-value addition, but it needs the `ui-test` deps that the review
>    cleanup removed for size — a deliberate add-back, not a regression.
>
> 2. ~~The local-backend transport hand-rolls HTTP over a raw socket.~~
>    **Resolved 2026-09-09: HTTPS only, through the rider's own reverse proxy.**
>    No raw socket, no `domain-config`, no pinning; the release NSC stays
>    strict and `LocalClient` becomes an ordinary `HttpsURLConnection` caller.
>    Spec in 2.3, rationale in the Resolved decisions table.

Nothing in Step 2 touches the shell or the theme — `RootScreen.kt`, the
`Destination` enum and `Palette.kt` are stable unless a new destination or
colour is introduced by pairing (#195).

**Toolchain state on this machine (all discovered sessions 2–3):**

- No JDK on `PATH`; the only one is Android Studio's JBR:
  `C:\Program Files\Android\Android Studio\jbr`. Every `sdkmanager` /
  `avdmanager` / `android` invocation needs `$env:JAVA_HOME` set to it.
  PowerShell also has neither `JAVA_HOME` nor `HOME` — the **new** tools
  NPE without `$env:HOME` (`AvdManager.createInstance: parameter
  baseAvdFolder` is null), so set it per shell too.
- `cmdline-tools`: build 11076708 (sdkmanager 12.0) is **too old for this
  SDK's package metadata** ("only understands SDK XML versions up to 3") and
  its `avdmanager` NPEs (`Path.getFileSystem()`; "Error: AVD not created.
  null" right after "Copying files"). **Upgraded to 16111833**, now in
  `cmdline-tools\latest` — it ships the new `android.exe` CLI (1.0.16261425)
  and `sdkmanager` prints a deprecation notice pointing at `android sdk`.
  Newest-build lookup: `https://dl.google.com/android/repository/
  repository2-3.xml` → `commandlinetools-win-<build>_latest.zip`.
- All SDK licenses accepted. `sdkmanager --licenses` ignores piped input —
  feed the `y`s through a file: `cmd /c "sdkmanager … --licenses < ys.txt"`.
- `system-images;android-36;google_apis;x86_64` installed (~4.3 GB),
  verified via `sdkmanager --list_installed`.
- **Emulator 37.1.11 cannot boot hand-written AVDs.** It finds the AVD name
  ("Found AVD name 'wt-phone'"), then `path_getRootIniPath` returns NULL →
  "Failed to process .ini file (null)\config.ini" → falls back to the `arm`
  default ABI → FATAL "CPU Architecture 'arm' is not supported". Both
  layouts were tried (top-level `<name>.ini` pointer + `<name>.avd\config.ini`,
  and the full config duplicated into `<name>.ini`) with and without
  `ANDROID_AVD_HOME`/`ANDROID_SDK_HOME` (backslash and forward-slash forms).
  Do **not** spend another cycle on hand-written AVDs.
- **New `avdmanager` (16111833):** with `HOME` + `ANDROID_AVD_HOME` +
  `ANDROID_HOME` + `ANDROID_SDK_ROOT` all set, `create avd` fails with
  "Can't locate Android SDK installation directory for the AVD .ini file".
  It has **no** `--sdk_root` option (checked its help). Unresolved.
  Its failed runs leave partial `<name>.avd` dirs behind (next attempt then
  fails with "already exists") — `~/.android/avd` currently holds
  `wt-phone.avd` + `wt-tablet.avd` leftovers plus `avd.ini.bak` (the renamed
  top-level `avd.ini`); delete the two `.avd` dirs before the next attempt.
- **New `android` CLI passes environment detection:**
  `…cmdline-tools\latest\bin\android.exe info` → `sdk:
  C:\Users\Takazumi\AppData\Local\Android\Sdk`, `version: 1.0.16261425`.
  Per the `android-cli` skill its `emulator` subcommand has `create` / `start`
  ("returns when the emulator is fully started and ready to use") / `list` /
  `stop` / `remove`, plus `android screenshot` and `android layout` (JSON UI
  tree — the faster way to verify the rail/drawer than eyeballing a PNG).
  **Resolved (session 3):** the CLI worked as documented. `android emulator
  create` is profile-based, not device-model-based — there is **no**
  `pixel_9`/`pixel_tablet`; the profiles are `small_phone`, `medium_phone`,
  `medium_tablet`, `small_desktop`, `medium_desktop`, `large_desktop` (see
  `android emulator create --list-profiles`). `medium_phone` +
  `medium_tablet` were created and booted: the CLI auto-downloads the image
  and `start` blocks until the device is ready. `medium_tablet` lands a
  Pixel_Tablet model but only has an **API 35** x86_64 image (see the
  deviation notes above). The orphaned `wt-phone.avd`/`wt-tablet.avd` dirs
  from the old `avdmanager` attempts can be deleted from `~/.android/avd`
  (harmless either way).
- Scratch copies of the downloaded artifacts (cmdtools zips, the resolved
  AARs, javap'd jars) live in this conversation's artifacts `scratch/`
  directory — ephemeral, re-download if gone (URLs above).

> [!NOTE]
> Step 1 is committed and pushed (PR #257). The branch is
> `feature/android-client`; the working tree is clean. `plan.md` and
> `android/README.md` are committed on that branch — update them there, not
> on a detached checkout.

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
  via a debug-specific manifest merge or a debug resource. As built this is
  also the *final* shape: the Step 2 transport decision added no release
  `domain-config`, because the local backend is reached over HTTPS through the
  rider's TLS terminator — the shipped file's comment was corrected on
  2026-09-09 to say so, and to say why no `domain-config` may be added).
  No INTERNET-adjacent permissions beyond `INTERNET`; no camera, no location.
- **Build types/flavors:** `debug` (cloud base URL → dev harness
  `http://10.0.2.2:8765`; local-server default → `http://10.0.2.2:8000`;
  cleartext-localhost allowed) / `release` (cloud base URL → the production
  host as a **build config field**, never a literal in code; no cleartext on
  either backend, ever — the local server's origin is rider-entered at pairing
  and is not a build field). The base URLs are build fields (the iOS xcconfig
  precedent): two fields `WATTRACKER_CLOUD_SCHEME` + `WATTRACKER_CLOUD_HOST`
  (split because `//` can't be a whole config value cleanly) — same trick as
  `Config/Base.xcconfig`.
- **Shell (five destinations: Dashboard, Activities, Calendar, Volume,
  Settings):**
  - Phone, **landscape** (narrow, including landscape phones that report
    regular width): **leading `NavigationRail`**, not a bottom bar — the
    arithmetic from #193: a ~900×400dp landscape phone loses a fifth of its
    scarce height to a bottom bar vs ~8% of width to a rail.
  - Phone, **portrait** (added in implementation, session 3; the iOS app is
    landscape-only so the plan's "leading rail" only covered landscape):
    conventional **bottom `NavigationBar`** — the rail's wide-short-viewport
    case does not apply in portrait, so chrome goes on the bottom edge under
    the thumb. The shell branches on `Configuration.ORIENTATION_PORTRAIT`.
  - Large screen (tablet, `smallestScreenWidthDp ≥ 600`): **permanent
    drawer** (`PermanentNavigationDrawer`, the list-detail /
    `NavigationSplitView` analogue) — chosen over a collapsible rail because
    at the tablet's widths the list should always stay visible. (M3 1.4:
    `PermanentNavigationDrawer` has no `gesturesEnabled` and
    `PermanentDrawerSheet` has no `containerColor` — paint the surface on the
    sheet's content.)
  - Idiom predicate = size class **and** idiom (same lesson as iOS `RootView`:
    a Max-sized iPhone in landscape reports regular width). In practice the two
    collapse to `smallestScreenWidthDp >= 600` on Android (see `RootScreen.kt`
    KDoc): the window-size-class composables were removed in Foundation 1.10
    and `smallestScreenWidthDp` is the stable value they approximated.
  - Every tablet layout correct **and** in portrait (the standing requirement).
  - **The nav highlight is explicit and shared** across rail, bottom bar and
    drawer rows (amber `Palette.accent` selected / `Palette.muted` otherwise,
    16% accent indicator) — not the M3 theme defaults, so the phone and tablet
    read as one product.
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
  - **Transport: HTTPS for both backends, one code path.** Cloud and local
    traffic both use `HttpURLConnection` over TLS against the strict release
    `network_security_config.xml`. The local server is reached through whatever
    TLS terminator the rider runs — `tailscale serve` or an ordinary reverse
    proxy, both accepted (Resolved decisions → "Local backend transport") — so
    from the app's side it is an ordinary HTTPS origin with an ordinary chain to
    a system trust anchor, and the app has no way to tell which is in front.
    There is no local-only transport left to write: **no `java.net.Socket`, no
    hand-rolled HTTP/1.1, no `domain-config`, no pin set, no custom
    `TrustManager` or `HostnameVerifier`.**
    - **Flexible by construction: no hostname is baked in.** The rider types
      the origin and the app accepts any `https://` host, name or port, so an
      arbitrary proxy works without an app change. Nothing in the app, the
      manifest or the NSC may name a domain — a strict base config already
      permits every HTTPS host, and *that* is what buys the flexibility. Adding
      a `domain-config` for a specific name would take it away.
    - **Reject `http://` at the edge in release**, in URL validation (Step 3),
      rather than letting the platform throw: a refused scheme must read as
      "this backend needs https", not as an opaque network failure two screens
      later.
    - **Debug keeps its loopback and `10.0.2.2` cleartext exceptions**
      (`src/debug/res/xml/network_security_config.xml`) for the emulator and
      the cloud dev harness. That file stays in the `debug` source set so it
      cannot ship; debug-only scaffolding is fine, shipped cleartext is not.
    - Platform `HttpURLConnection` behaviour is now an asset rather than
      something to re-implement: connection reuse, timeouts, redirects, chunked
      decoding and proxy support all come from the SDK. Keep the cloud client's
      no-secrets-in-logs rule — the local device token and the session cookie
      are bearer secrets on this path too.
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
- **Local pairing screen:** server URL field — **`https://` required in
  release**, with any host, name or port accepted and **no domain allowlist**
  (the rider's front end can be called anything). Refuse `http://` with a message
  that says the local backend needs a TLS front end and points at
  `README.md:540-549`, not with a generic "invalid URL". Debug builds
  additionally accept `http://` for loopback and `10.0.2.2`, matching the debug
  NSC. Then the device token pasted from the web UI (Instructions: "open the
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
3. **Live, local:** `WATTRACKER_PUBLIC_HOSTS=10.0.2.2 ./start.sh` (loopback
   bind unchanged) → pair the phone (emulator, debug build:
   `http://10.0.2.2:8000`) → all four screens → Revoke in the web UI →
   session dies on next request → app shows unpaired. Then the run that
   actually exercises the transport decision: a **physical device against
   `https://<the phone-facing hostname>`** through the TLS terminator on a
   release-configured NSC, proving the whole local path works with cleartext
   off and with a hostname the app has never seen before.
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
