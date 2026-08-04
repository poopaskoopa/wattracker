"""FastAPI application: auth + per-user dashboard, activities, generate, settings."""
from __future__ import annotations

import asyncio
import calendar as _cal
import datetime as _dt
import hashlib
import io
import logging
import math as _math
import os
import sys
import threading
import urllib.parse as _url
import zipfile
from contextlib import asynccontextmanager
from typing import List, Optional

from fastapi import Body, FastAPI, Form, Request, UploadFile, WebSocket
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import (
    auth,
    backup,
    calendarfeed,
    config,
    credstore,
    db,
    exporter,
    paths,
    power_corrections,
    races,
)
from .analysis import activity_cache, pipeline, power_profile, zones
from .ble import devices as bledevices
from .ble.runner import RideController, flatten_session
from .ingest import importer
from .metrics import durability as durabilitymod
from .metrics import profile_store
from .prescribe import adapt as adaptmod
from .prescribe import duration as durationmod
from .prescribe import goals as goalsmod
from .prescribe import phases as phasesmod
from .prescribe import plan as planmod
from .prescribe import present
from .prescribe import reflow
from .prescribe import zwo
from .prescribe import llm
from .prescribe.planner import (
    JUST_RIDE_DURATIONS,
    WORKOUT_TYPE_INFO,
    WORKOUT_TYPE_KEYS,
    build_workout,
    plan_workout,
    workout_type_info,
)
from .timeutil import local_today, utc_now, utc_today, valid_timezone

DAY_LABELS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

# Real-hardware ride loop cadence (seconds); module-level so tests can shrink it.
RIDE_POLL_INTERVAL_S = 1.0
RIDE_INACTIVITY_TIMEOUT_S = 300.0
# Consecutive failed per-tick ERG commands tolerated before ERG is switched off
# for the rest of the ride. One failure is not evidence of anything: a dropped
# characteristic write, a momentary radio fault or (in server mode) a network
# blip all surface here identically to a trainer that genuinely refused. Each is
# retried on the next tick with a full re-arm; only a sustained run of them is
# treated as "this trainer will not take targets" - and then the rider is told.
ERG_COMMAND_FAILURE_LIMIT = 5


def _ride_loop_time() -> float:
    return asyncio.get_running_loop().time()


async def _ride_sleep(delay: float) -> None:
    await asyncio.sleep(delay)


# Background activity-scan cadence (seconds); module-level so tests can shrink it.
SCAN_INTERVAL_S = 24 * 3600.0

_log = logging.getLogger(__name__)

# In-memory, per-user progress for interactive (button-triggered) rescans.
# Single-process app, so a plain dict guarded by a lock is enough; the
# background daemon thread doing the scan updates its entry, the status
# endpoint reads it. Keys are user ids; values are the status dicts returned
# verbatim as JSON by GET /api/scan/status.
_scan_lock = threading.Lock()
_scan_status: dict = {}


def _scan_status_snapshot(user_id: Optional[int]) -> Optional[dict]:
    with _scan_lock:
        st = _scan_status.get(user_id)
        return dict(st) if st else None


def _start_user_scan(user_id: int, directory: Optional[str]) -> Optional[dict]:
    """Begin an interactive rescan in a daemon thread.

    Returns None if a scan is already running for this user (caller should
    respond 409 with the current status); otherwise seeds and returns the fresh
    running status.
    """
    with _scan_lock:
        cur = _scan_status.get(user_id)
        if cur and cur.get("running"):
            return None
        status = {
            "running": True,
            "started_at": utc_now().isoformat(timespec="seconds"),
            "total": 0,
            "processed": 0,
            "imported": 0,
            "skipped": 0,
            "error": None,
            "finished_at": None,
            "directory": directory,
        }
        _scan_status[user_id] = status
        snapshot = dict(status)

    def _progress(fields: dict) -> None:
        with _scan_lock:
            _scan_status[user_id].update(fields)

    def _run() -> None:
        try:
            result = importer.scan_activities(
                user_id, directory=directory, progress=_progress
            )
            d = result.get("directory")
            with _scan_lock:
                _scan_status[user_id].update(
                    found=result.get("found", 0),
                    imported=result.get("imported", 0),
                    skipped=result.get("skipped", 0),
                    directory=d,
                    exists=bool(d and os.path.isdir(d)),
                )
        except Exception as exc:  # surface, don't crash the daemon thread
            with _scan_lock:
                _scan_status[user_id]["error"] = str(exc)
            _log.warning("interactive rescan failed for user %s", user_id,
                         exc_info=True)
        finally:
            with _scan_lock:
                st = _scan_status[user_id]
                st["running"] = False
                st["finished_at"] = utc_now().isoformat(
                    timespec="seconds"
                )

    threading.Thread(target=_run, daemon=True).start()
    return snapshot


def run_daily_maintenance() -> dict:
    """One synchronous pass of all daily jobs: import scan (FTP re-eval +
    completion matching inside), then per-user plan adaptation, race-result
    refresh, active-plan reflow and Zwift export sync. Each stage is
    fault-isolated per user.
    """
    totals = importer.run_auto_scan()
    totals["adapted"] = 0
    totals["races"] = 0
    totals["race_skipped"] = 0
    totals["reflowed"] = 0
    totals["exported"] = 0
    # Daily safety snapshot of the whole DB (once per ~day even if the sweep
    # runs more often). Never let a backup failure abort maintenance.
    try:
        if backup.create_daily_if_due() is not None:
            totals["backed_up"] = 1
    except Exception:
        _log.warning("daily backup failed", exc_info=True)
    for uid in db.all_user_ids():
        try:
            # Daily self-heal: the fast rescan only matches completions when new
            # files import, so re-run matching unconditionally here (cheap:
            # incomplete workouts x same-date activities) to catch plans created
            # after their rides were already imported.
            totals["completed"] += importer.match_plan_completions(uid)
        except Exception:
            _log.warning("completion matching failed for user %s", uid, exc_info=True)
        # The race sync runs BEFORE the profile is recomputed: an
        # authenticated refresh writes the rider's weight from Zwift, and
        # weight feeds wprime_j_per_kg and cp_w_per_kg. With the old ordering
        # every weight Zwift reported was a full day stale before it reached a
        # single prescription.
        try:
            races.refresh_race_results(uid, respect_backoff=True)
            totals["races"] += 1
        except Exception:
            _log.warning("race refresh failed for user %s", uid, exc_info=True)
        # None when the state could not be built; adaptation then reads as
        # "progress" and changes nothing, which is the right failure mode.
        state = None
        try:
            state = pipeline.build_state(uid)
            # Recompute the stored rider profile before anything prescribes:
            # adaptation, reflow and the .zwo export all read the snapshot, and
            # the profile depends on wall-clock time as well as on rides - FTP
            # decays across a layoff even when no new activity lands - so a
            # sweep that skipped this would keep prescribing against a rider
            # who no longer exists. Reuses the state just built rather than
            # building a second one.
            profile_store.refresh(uid, state=state)
        except Exception:
            _log.warning("rider profile refresh failed for user %s", uid,
                         exc_info=True)
        try:
            summary = adaptmod.apply_adaptations(uid, state)
            totals["adapted"] += summary.get("adjusted", 0)
            totals["race_skipped"] += summary.get("skipped_raced", 0)
        except Exception:
            _log.warning("plan adaptation failed for user %s", uid, exc_info=True)
        try:
            # This is an adaptive program, so the active plan is recomputed
            # every day: reflow re-reads the rider's measured profile and their
            # races, so upcoming workouts track the rider's current capacity
            # instead of the snapshot taken when the plan was created. It runs
            # BEFORE the export sync so the folder picks the rewrite up in the
            # same sweep.
            #
            # This is only safe because reflow now PRESERVES `adapted` rows
            # outside race windows - a naive daily reflow would otherwise have
            # reverted every one of adapt.py's adjustments overnight.
            plan = db.get_active_plan(uid)
            if plan is not None:
                # notify=True: this run is unattended, so anything it rewrites
                # has to be reported back to the rider (see reflow.reflow_plan).
                result = reflow.reflow_plan(uid, plan["id"], notify=True)
                totals["reflowed"] += (
                    result.get("updated", 0) + result.get("inserted", 0)
                    + result.get("deleted", 0)
                )
                # The first sweep after profile-aware targets shipped rewrites
                # every future workout and renames its .zwo; log the counts so
                # that (and any later churn) is diagnosable.
                _log.info("daily reflow for user %s: %s", uid, result)
        except Exception:
            _log.warning("plan reflow failed for user %s", uid, exc_info=True)
        try:
            # Keep the Zwift custom-workout folder in sync with the plan
            # (exports new/updated workouts, prunes completed/OOTO-skipped).
            res = exporter.sync_plan_exports(uid)
            totals["exported"] += res.get("exported", 0)
        except Exception:
            _log.warning("export sync failed for user %s", uid, exc_info=True)
    return totals


async def auto_scan_loop(stop: "asyncio.Event") -> None:
    """Daily background sweep: import new .fit files, re-evaluate FTP, match
    plan-workout completions, adapt upcoming workouts, refresh race results.
    Runs once immediately, then every SCAN_INTERVAL_S until `stop` is set. The
    sweep runs in a worker thread so FIT parsing never blocks the event loop.
    """
    while True:
        try:
            totals = await asyncio.to_thread(run_daily_maintenance)
            _log.info("auto-scan: %s", totals)
        except Exception:
            _log.warning("background activity scan failed", exc_info=True)
        try:
            await asyncio.wait_for(stop.wait(), timeout=SCAN_INTERVAL_S)
            return
        except asyncio.TimeoutError:
            continue  # interval elapsed -> next daily sweep


def _upcoming_monday(today: Optional[_dt.date] = None) -> _dt.date:
    today = today or utc_today()
    delta = (0 - today.weekday()) % 7  # 0 if today is Monday, else days to next Monday
    return today + _dt.timedelta(days=delta)

_HERE = os.path.dirname(os.path.abspath(__file__))
_TEMPLATES_DIR = os.path.join(_HERE, "web", "templates")
_STATIC_DIR = os.path.join(_HERE, "web", "static")

templates = Jinja2Templates(directory=_TEMPLATES_DIR)


def static_url(path: str) -> str:
    """Return /static/<path>?v=<mtime> for cache-busting; no v param if file is missing."""
    file_path = os.path.join(_STATIC_DIR, path)
    try:
        version = int(os.path.getmtime(file_path))
    except OSError:
        return f"/static/{path}"
    return f"/static/{path}?v={version}"


templates.env.globals["static_url"] = static_url


def _restore_command() -> str:
    if sys.platform.startswith("win"):
        if getattr(sys, "frozen", False):
            executable = os.path.basename(sys.executable) or "wattracker.exe"
            return f"{executable} restore"
        return "wattracker-restore"
    return ".venv/bin/python -m wattracker.restore_backup"


class NoCacheStaticFiles(StaticFiles):
    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response

# Paths served without authentication. Interactive docs are disabled (see the
# FastAPI() constructor), so no /docs, /openapi or /redoc prefixes are exempt.
# "/calendar.ics" is exempt from the SESSION check only - it authenticates the
# caller itself, from a per-user token, because a subscribing phone calendar
# app has no cookie jar (see calendarfeed.py). It is an exact match, so the
# session-protected "/calendar" page above it is untouched.
_EXEMPT = ("/login", "/register", calendarfeed.FEED_PATH)
_EXEMPT_PREFIXES = ("/static", "/favicon", "/apple-touch-icon")

# One log line per this many rejected /calendar.ics tokens. Not a limit:
# nothing is ever refused because of it (see CalendarFeedFailureCounter).
CALENDAR_TOKEN_FAILURE_THRESHOLD = 10


class CalendarFeedFailureCounter:
    """One process-wide count of rejected /calendar.ics tokens.

    What it is: a coarse "someone is guessing feed tokens" signal in the log.
    Not a per-client metric, not a rate limit, and it refuses nothing.

    Deliberately UNKEYED. The obvious key - the client address - is worthless
    here and actively harmful. Worthless because a loopback-bound single-user
    app sees one address; harmful because that address is client-controlled:
    uvicorn enables proxy_headers with a trusted range of 127.0.0.1, so on a
    loopback bind every caller is a "trusted proxy" and can set
    request.client.host to anything via X-Forwarded-For. A keyed version was
    tried and defeated exactly that way: 400 guesses under 400 forged
    addresses each sat at count 1, so no threshold ever fired and the
    key-eviction sweep additionally wiped the genuine tally accumulated
    alongside - the counter went quiet precisely while being attacked.
    ``proxy_headers=False`` in __main__ closes the header-spoofing hole; not
    keying on the address at all is what makes this counter unspoofable
    regardless of how the app is ever launched.

    It also refuses nothing, unlike auth.LoginThrottle on /login. An earlier
    version refused, and that was a self-inflicted outage: every caller shares
    one address on loopback (and behind the SSH/Tailscale/Cloudflare tunnel a
    phone would actually use), so a few bad guesses - or one stale
    subscription still presenting a rotated token - started 404-ing the valid
    subscriber. A calendar client answers a 404 by silently continuing to show
    a stale calendar, which is the worst possible failure mode for a feature
    whose whole point is that the schedule appears on the phone. /login can
    afford to refuse because a password is guessable and the user is standing
    there to read the error; a 256-bit token checked one indexed lookup at a
    time is not guessable, so visibility is the only control worth having.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._count = 0

    def record_failure(self) -> int:
        """Count one rejection; returns the running total."""
        with self._lock:
            self._count += 1
            return self._count

    @property
    def count(self) -> int:
        with self._lock:
            return self._count


# One log line per this many refused /login attempts. Not a limit: like the
# calendar counter it refuses nothing (see LoginAttemptCounter).
LOGIN_FAILURE_LOG_THRESHOLD = 25


class LoginAttemptCounter:
    """One process-wide count of refused /login attempts.

    Exists because auth.LoginThrottle cannot see this: it is keyed by username,
    so an attacker rotating the username on every request leaves every key at
    one failure and the throttle stays silent while thousands of attempts go
    through. A single unkeyed count is the only thing that notices that shape.

    Deliberately UNKEYED and deliberately refusing NOTHING, for exactly the
    reasons spelled out on CalendarFeedFailureCounter above: the obvious key
    (client address) is worthless on a loopback bind and forgeable via
    X-Forwarded-For, and a global refusal would let one bad client lock the
    legitimate owner out of their own machine - which is the outcome the
    throttle's per-username scoping exists to avoid. Refusal stays with the
    per-username throttle (bounded blast radius) and with the hash limiter
    (bounded memory); this is visibility only.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._count = 0

    def record_failure(self) -> int:
        with self._lock:
            self._count += 1
            return self._count

    @property
    def count(self) -> int:
        with self._lock:
            return self._count

# Max in-memory size for an uploaded activity file (bytes). A real .fit ride is
# well under this; the cap stops a huge upload from exhausting memory.
MAX_UPLOAD_BYTES = 50 * 1024 * 1024

# WebSocket handshakes are only accepted from same-origin (local) browsers; a
# cross-site page's Origin will never match, blocking cross-site WS hijacking.
_ALLOWED_WS_ORIGIN_HOSTS = ("localhost", "127.0.0.1", "::1")

# BLE addresses are opaque identifiers (UUIDs on macOS, MAC-like strings on
# other platforms). Bound user-supplied selections without imposing a format.
_MAX_SELECTED_POWER_SOURCES = 8
_MAX_BLE_ADDRESS_LENGTH = 256


class IPv6TrustedHostMiddleware(TrustedHostMiddleware):
    """TrustedHostMiddleware with correct bracketed-IPv6 host parsing.

    The Starlette version supported by the app splits Host on the first colon,
    which turns ``[::1]:8000`` into ``[``. Keep its exact allowlist semantics
    while parsing IPv6 literals without broadening trust to other IPv6 hosts.
    """

    @staticmethod
    def _host_only(value: str) -> Optional[str]:
        value = (value or "").strip().lower()
        if value.startswith("["):
            end = value.find("]")
            if end < 0:
                return None
            suffix = value[end + 1:]
            if suffix and (not suffix.startswith(":") or not suffix[1:].isdigit()):
                return None
            return value[:end + 1]
        if value == "::1":
            return value
        if value.count(":") > 1:
            return None
        if ":" in value:
            host, port = value.split(":", 1)
            if not port.isdigit():
                return None
            return host
        return value

    async def __call__(self, scope, receive, send) -> None:
        if self.allow_any or scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        raw_host = ""
        for name, value in scope.get("headers", []):
            if name.lower() == b"host":
                raw_host = value.decode("latin-1")
                break
        host = self._host_only(raw_host)
        if host in self.allowed_hosts:
            await self.app(scope, receive, send)
            return
        await PlainTextResponse("Invalid host header", status_code=400)(
            scope, receive, send
        )


class AuthMiddleware(BaseHTTPMiddleware):
    """Redirect unauthenticated requests to /login (except exempt paths)."""

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        exempt = path in _EXEMPT or any(path.startswith(p) for p in _EXEMPT_PREFIXES)
        if not exempt and not request.session.get("user_id"):
            return RedirectResponse("/login", status_code=303)
        return await call_next(request)


def _uid(request: Request) -> Optional[int]:
    return request.session.get("user_id")


def _username(request: Request) -> Optional[str]:
    return request.session.get("username")


def _ctx(request: Request, **kw) -> dict:
    kw["request"] = request
    kw.setdefault("username", _username(request))
    uid = _uid(request)
    if uid is not None:
        pending = []
        for item in db.pending_ratings(uid):
            if item["kind"] == "plan":
                workout = db.get_plan_workout(uid, item["id"])
                if not importer.plan_workout_completion_verified(uid, workout or {}):
                    continue
            pending.append(item)
        kw.setdefault("pending_ratings", pending)
        # What the rider's own ratings imply for a manually-set training FTP.
        # Only the pages that already show the FTP render it (settings,
        # profile); it lives here so both read the same one row.
        kw.setdefault("ftp_suggestion", db.pending_ftp_suggestion(uid))
    return kw


def _same_origin_or_absent(request: Request) -> bool:
    """Accept native/test POSTs without Origin; browser Origin must match exactly."""
    origin = request.headers.get("origin")
    if not origin:
        return True
    try:
        parsed = _url.urlparse(origin)
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in ("", "/")
            or parsed.params
            or parsed.query
            or parsed.fragment
        ):
            return False
        default_port = 443 if parsed.scheme == "https" else 80
        origin_port = parsed.port or default_port
        request_port = request.url.port or (
            443 if request.url.scheme == "https" else 80
        )
    except (ValueError, TypeError):
        return False
    return (
        parsed.scheme.lower() == request.url.scheme.lower()
        and parsed.hostname.lower() == (request.url.hostname or "").lower()
        and origin_port == request_port
    )


def _record_login_failure(counter: "LoginAttemptCounter") -> int:
    """Count one refused login and log every LOGIN_FAILURE_LOG_THRESHOLD-th.

    The log line is the whole point: nothing here refuses anything, and the
    per-username throttle is blind to an attacker who never reuses a username.
    """
    total = counter.record_failure()
    if total % LOGIN_FAILURE_LOG_THRESHOLD == 0:
        _log.warning(
            "%d refused login attempts since start (all usernames)", total
        )
    return total


def _trusted_origin_or_absent(request: Request, allowed_hosts: List[str]) -> bool:
    """Cross-origin guard for the UNAUTHENTICATED form posts (/login, /register).

    Same convention as _same_origin_or_absent: a request with no Origin (native
    clients, curl, the test client) is accepted; a browser's Origin must name a
    host this app is actually reached by - i.e. the same allowlist
    TrustedHostMiddleware enforces on the Host header. That is what stops the
    drive-by: a page on evil.example.com can POST a form to
    http://localhost:8000/login cross-origin and never read the reply, but the
    Origin it is forced to send is its own, so the request is refused before any
    scrypt runs.

    Why this and not _same_origin_or_absent, which compares scheme and port too:
    on the tailnet path the browser talks https to `tailscale serve`, which
    forwards plain http to this loopback socket, so the Origin's scheme (and
    port) legitimately differ from request.url's and a strict comparison would
    lock the owner out of logging in from their own phone. The host comparison
    survives that, because the proxy passes the original Host through and
    config.public_host() is the single literal name allowed here.

    Residual: another app on this machine (http://localhost:3000) has a host in
    the allowlist, so its pages are not blocked. That is accepted - a local
    process can hit this socket directly with no Origin at all, so the check was
    never the control against local callers. The hash limiter is.
    """
    origin = request.headers.get("origin")
    if not origin:
        return True
    try:
        parsed = _url.urlparse(origin)
    except (ValueError, TypeError):
        return False
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        return False
    # urlparse strips the brackets from an IPv6 literal; the allowlist carries
    # both spellings.
    return parsed.hostname.lower() in allowed_hosts


def _feed_base_url(request: Request) -> str:
    """Base URL to mint calendar subscription links from.

    ``request.base_url`` is derived from the Host header, which on this app is
    always a loopback name - a link a phone cannot resolve. When the deployment
    declares how it is reached from outside (WATTRACKER_PUBLIC_HOST, plus
    WATTRACKER_PUBLIC_SCHEME for the TLS-terminating front end), links are
    built from that instead. Unconfigured, this is exactly the previous
    behaviour.

    Only the base URL changes. The token, its hashing, and the feed's own
    authentication are untouched by which name the link carries.
    """
    public = config.public_host()
    if public:
        return f"{config.public_scheme()}://{public}"
    return str(request.base_url)


def create_app() -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        db.init_db()
        stop = asyncio.Event()
        task: Optional[asyncio.Task] = None
        if config.auto_scan_enabled():
            task = asyncio.create_task(auto_scan_loop(stop))
        yield
        if task is not None:
            stop.set()
            try:
                await asyncio.wait_for(task, timeout=5.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                task.cancel()

    # Local single-purpose app: interactive API docs / OpenAPI schema are
    # disabled to reduce surface area.
    app = FastAPI(
        title="wattracker",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.mount("/static", NoCacheStaticFiles(directory=_STATIC_DIR), name="static")

    # Safari (and some other agents) ignore the <link rel="icon"> SVG and fetch
    # these from the site root regardless of what the document declares, so
    # serve them there too rather than only under /static.
    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon_ico() -> FileResponse:
        return FileResponse(os.path.join(_STATIC_DIR, "favicon.ico"))

    @app.get("/apple-touch-icon.png", include_in_schema=False)
    async def apple_touch_icon() -> FileResponse:
        return FileResponse(os.path.join(_STATIC_DIR, "apple-touch-icon.png"))

    # Per-user cache of the last generated .zwo (avoids cross-user bleed).
    app.state.last = {}
    # In-process brute-force throttle for /login (per lowercased username).
    app.state.login_throttle = auth.LoginThrottle()
    # Hard ceiling on concurrent scrypt hashes (~128 MiB each). Shared by
    # /login and /register - both unauthenticated, both reachable by anyone who
    # can open a socket to this port, and the per-username throttle bounds
    # neither of them in memory terms.
    app.state.hash_limiter = auth.PasswordHashLimiter()
    # Refused /login attempts: a single unkeyed count, not a throttle.
    app.state.login_failures = LoginAttemptCounter()
    # Rejected /calendar.ics tokens: a single unkeyed count, not a throttle.
    # The route resolves the token first and serves any valid one before this
    # is consulted at all, so a subscribed calendar app can never be refused
    # because of someone else's guessing - or its own earlier attempts with a
    # rotated token.
    app.state.calendar_failures = CalendarFeedFailureCounter()
    # uvicorn logs the full request target; keep feed tokens out of the log.
    calendarfeed.install_access_log_redaction()

    # SessionMiddleware must be OUTER (added after AuthMiddleware) so
    # request.session is populated before AuthMiddleware runs. TrustedHost is
    # added last so it runs first and rejects spoofed Host headers early.
    app.add_middleware(AuthMiddleware)
    app.add_middleware(
        SessionMiddleware,
        secret_key=config.session_secret(),
        session_cookie="wattracker_session",
        same_site="lax",
    )
    allowed_hosts = ["localhost", "127.0.0.1", "[::1]", "::1", "testserver"]
    # An optional single extra name, so a reverse proxy on the owner's tailnet
    # (tailscale serve) can forward requests that still carry their original
    # Host. Exactly one literal hostname is added - never a wildcard, never a
    # suffix pattern; config.public_host() refuses anything else, and the
    # middleware below does a plain equality test with no pattern support.
    #
    # Why this is safe HERE: the app is still bound to loopback
    # (config.server_host() enforces that), so nothing can reach this socket
    # except the proxy on this machine, and that proxy only accepts connections
    # from the owner's tailnet, which is authenticated by Tailscale itself.
    # Widening the Host allowlist does not widen who can connect; it only stops
    # the server rejecting the name the tailnet proxy passes through.
    #
    # What it would mean otherwise: pointing a PUBLIC DNS name at this and
    # putting a proxy in front of it makes every route on this app reachable by
    # anyone who resolves that name, with only the session cookie (and, for the
    # feed, a URL-borne token) in the way. This app is designed as a
    # single-user local server - the CSRF story is a same-origin check, the
    # session cookie is not Secure, and there is no rate limiting beyond
    # /login. Do not set this to an internet-facing name.
    public_host = config.public_host()
    if public_host:
        # The value may carry ":port"; strip it with the middleware's own
        # parser so the entry is exactly what _host_only() will produce for an
        # incoming Host header (Starlette compares the host portion only).
        host_only = IPv6TrustedHostMiddleware._host_only(public_host)
        if host_only:
            allowed_hosts.append(host_only)
    app.add_middleware(
        IPv6TrustedHostMiddleware,
        allowed_hosts=allowed_hosts,
        www_redirect=False,
    )
    # The same allowlist, reused by the unauthenticated form posts to reject a
    # cross-origin browser Origin (see _trusted_origin_or_absent).
    app.state.allowed_hosts = allowed_hosts

    # -------------------------------------------------------------- auth
    @app.get("/register", response_class=HTMLResponse)
    def register_form(request: Request):
        if _uid(request):
            return RedirectResponse("/", status_code=303)
        return templates.TemplateResponse(
            request, "register.html", {"request": request, "error": None}
        )

    def _hash_capacity_response(request: Request, template: str):
        """Shed a request that arrived while the hash limiter was full.

        Cheap and identical for every caller: no scrypt has run, and nothing
        about the submitted username is revealed.
        """
        return templates.TemplateResponse(
            request,
            template,
            {"request": request,
             "error": "The server is busy. Please try again in a moment."},
            status_code=503,
            headers={"Retry-After": "5"},
        )

    @app.post("/register", response_class=HTMLResponse)
    def register_submit(
        request: Request,
        username: str = Form(...),
        password: str = Form(...),
    ):
        # /register hashes a password too (same ~128 MiB), and is exempt from
        # auth as well, so it gets the same two guards as /login - otherwise the
        # cheapest attack is simply to point the flood at this route instead.
        if not _trusted_origin_or_absent(request, app.state.allowed_hosts):
            return PlainTextResponse("Origin not allowed", status_code=403)
        username = (username or "").strip()
        err = auth.validate_credentials(username, password)
        if not err:
            try:
                with app.state.hash_limiter.reserve():
                    password_hash = auth.hash_password(password)
            except auth.HashCapacityExceeded:
                return _hash_capacity_response(request, "register.html")
            user_id = db.create_user(username, password_hash)
            if user_id is None:
                err = "That username is already taken."
        if err:
            return templates.TemplateResponse(
                request, "register.html", {"request": request, "error": err}
            )
        request.session["user_id"] = user_id
        request.session["username"] = username
        return RedirectResponse("/", status_code=303)

    @app.get("/login", response_class=HTMLResponse)
    def login_form(request: Request):
        if _uid(request):
            return RedirectResponse("/", status_code=303)
        return templates.TemplateResponse(
            request, "login.html", {"request": request, "error": None}
        )

    @app.post("/login", response_class=HTMLResponse)
    def login_submit(
        request: Request,
        username: str = Form(...),
        password: str = Form(...),
    ):
        # Refuse a cross-origin browser POST before spending anything on it: a
        # drive-by page rotating usernames never trips the per-username throttle.
        if not _trusted_origin_or_absent(request, app.state.allowed_hosts):
            return PlainTextResponse("Origin not allowed", status_code=403)
        uname = (username or "").strip()
        throttle = app.state.login_throttle
        failures = app.state.login_failures
        if throttle.retry_after(uname) > 0:
            # Locked out after repeated failures. Generic message, no hint about
            # whether the username exists.
            return templates.TemplateResponse(
                request,
                "login.html",
                {"request": request,
                 "error": "Too many failed attempts. Please wait and try again."},
                status_code=429,
            )
        user = db.get_user_by_username(uname)
        try:
            # The hash is the expensive part (~128 MiB); hold a slot for exactly
            # that, and only that. Both branches below hash, so shedding here
            # cannot tell an existing username from a missing one.
            with app.state.hash_limiter.reserve():
                if user:
                    ok = auth.verify_password(password, user["password_hash"])
                else:
                    # Spend the same scrypt time as a real verify so a missing
                    # username isn't distinguishable by timing.
                    auth.dummy_verify(password)
                    ok = False
        except auth.HashCapacityExceeded:
            _record_login_failure(failures)
            return _hash_capacity_response(request, "login.html")
        if not ok:
            throttle.record_failure(uname)
            _record_login_failure(failures)
            return templates.TemplateResponse(
                request,
                "login.html",
                {"request": request, "error": "Invalid username or password."},
            )
        throttle.record_success(uname)
        # Transparent upgrade: re-hash legacy/low-cost hashes at the current
        # cost. Best-effort and already authenticated, so a full limiter just
        # postpones the upgrade to the next login rather than failing the login.
        if auth.needs_rehash(user["password_hash"]):
            try:
                with app.state.hash_limiter.reserve():
                    db.set_password_hash(
                        user["username"], auth.hash_password(password)
                    )
            except auth.HashCapacityExceeded:
                _log.info("password rehash skipped: hashing at capacity")
            except Exception:
                _log.warning("password rehash on login failed", exc_info=True)
        request.session["user_id"] = user["id"]
        request.session["username"] = user["username"]
        return RedirectResponse("/", status_code=303)

    @app.post("/logout")
    def logout(request: Request):
        request.session.clear()
        return RedirectResponse("/login", status_code=303)

    # ------------------------------------------------------------- pages
    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request):
        uid = _uid(request)
        state = pipeline.build_state(uid)
        # Detection is actionable: adapt upcoming plan workouts (idempotent -
        # each workout is only ever adjusted once), then describe it.
        try:
            summary = adaptmod.apply_adaptations(uid, state)
        except Exception:
            _log.warning("plan adaptation failed", exc_info=True)
            summary = {"status": adaptmod.detection_status(state),
                       "adjusted": 0, "upcoming": {}}
        banner = adaptmod.banner_for(state, summary)
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            _ctx(request, state=state.to_dict(), banner=banner),
        )

    def _activities_context(request: Request, scan: Optional[dict] = None) -> dict:
        uid = _uid(request)
        settings = db.get_user_settings(uid)
        candidates = paths.annotated_candidates()
        saved = settings.get("activities_dir")
        prefill = saved or (candidates[0]["path"] if candidates else "")
        activities = db.list_activities(uid)
        merged = db.primaries_with_duplicates(uid)
        for a in activities:
            a["duration_fmt"] = races.format_duration(a.get("duration_s"))
            a["has_duplicate"] = a["id"] in merged
        return _ctx(
            request,
            activities=activities,
            scan=scan,
            candidates=candidates,
            saved_dir=saved,
            prefill_dir=prefill,
            linked=request.query_params.get("linked"),
        )

    @app.get("/activities", response_class=HTMLResponse)
    def activities_page(request: Request):
        return templates.TemplateResponse(
            request, "activities.html", _activities_context(request)
        )

    def _profile_response(request: Request, error: Optional[str] = None):
        uid = _uid(request)
        settings = db.get_user_settings(uid)
        return templates.TemplateResponse(
            request,
            "profile.html",
            _ctx(
                request,
                profile=zones.rider_profile(uid),
                power_profile=power_profile.for_user(uid),
                # What the rider's workout targets are actually built on, and
                # when it was last computed. Without this the app can silently
                # prescribe population defaults forever and never say so.
                targets=present.target_status(
                    profile_store.for_user(uid),
                    profile_store.computed_at(uid),
                ),
                manual_ftp=settings.get("ftp"),
                manual_hr_max=settings.get("hr_max"),
                error=error,
                saved=request.query_params.get("saved"),
            ),
        )

    @app.get("/profile", response_class=HTMLResponse)
    def profile_page(request: Request):
        return _profile_response(request)

    def _power_corrections_response(
        request: Request,
        threshold: str = "",
        error: Optional[str] = None,
        status_code: int = 200,
    ):
        uid = _uid(request)
        candidates = []
        searched = bool((threshold or "").strip())
        if searched and error is None:
            try:
                candidates = power_corrections.find_anomalies(uid, float(threshold))
            except (ValueError, power_corrections.CorrectionError) as exc:
                error = str(exc)
                status_code = 400
        return templates.TemplateResponse(
            request,
            "power_corrections.html",
            _ctx(
                request,
                threshold=threshold,
                searched=searched,
                candidates=candidates,
                candidate_cap=power_corrections.MAX_CANDIDATES,
                correction_range_cap=db.POWER_CORRECTION_MAX_SAMPLES,
                corrections=db.list_power_corrections(uid, active_only=True),
                error=error,
                saved=request.query_params.get("saved"),
            ),
            status_code=status_code,
        )

    @app.get("/profile/power-corrections", response_class=HTMLResponse)
    def power_corrections_page(request: Request, threshold: str = ""):
        return _power_corrections_response(request, threshold)

    @app.post("/profile/power-corrections/apply", response_class=HTMLResponse)
    def power_correction_apply(
        request: Request,
        activity_id: int = Form(...),
        start_index: int = Form(...),
        end_index: int = Form(...),
        reason: str = Form(""),
        threshold: str = Form(""),
    ):
        if not _same_origin_or_absent(request):
            return PlainTextResponse("Origin not allowed", status_code=403)
        try:
            power_corrections.apply(
                _uid(request), activity_id, start_index, end_index, reason
            )
        except power_corrections.CorrectionError as exc:
            return _power_corrections_response(
                request, threshold, str(exc), status_code=400
            )
        return RedirectResponse(
            "/profile/power-corrections?saved=applied", status_code=303
        )

    @app.post("/profile/power-corrections/undo", response_class=HTMLResponse)
    def power_correction_undo(
        request: Request,
        correction_id: int = Form(...),
    ):
        if not _same_origin_or_absent(request):
            return PlainTextResponse("Origin not allowed", status_code=403)
        try:
            power_corrections.undo(_uid(request), correction_id)
        except power_corrections.CorrectionError as exc:
            return _power_corrections_response(
                request, error=str(exc), status_code=400
            )
        return RedirectResponse(
            "/profile/power-corrections?saved=undone", status_code=303
        )

    @app.post("/profile/ftp", response_class=HTMLResponse)
    def profile_ftp_save(
        request: Request,
        ftp: str = Form(""),
        action: str = Form("save"),
    ):
        uid = _uid(request)
        if action == "reset":
            db.set_user_ftp_override(uid, None)
            try:
                profile_store.refresh(uid)
            except Exception:
                _log.warning("profile refresh after FTP reset failed", exc_info=True)
            return RedirectResponse("/profile?saved=ftp", status_code=303)
        try:
            value = int(ftp.strip())
        except (TypeError, ValueError):
            return _profile_response(request, "FTP must be a whole number from 1 to 2000 W.")
        if not 1 <= value <= 2000:
            return _profile_response(request, "FTP must be a whole number from 1 to 2000 W.")
        db.set_user_ftp_override(uid, value)
        try:
            profile_store.refresh(uid)
        except Exception:
            _log.warning("profile refresh after FTP save failed", exc_info=True)
        return RedirectResponse("/profile?saved=ftp", status_code=303)

    @app.post("/ftp-suggestion")
    def ftp_suggestion_resolve(
        request: Request,
        suggestion_id: int = Form(...),
        action: str = Form("dismiss"),
        next_path: str = Form("/settings"),
    ):
        """Accept ('use') or dismiss the rider's pending FTP suggestion.

        Accepting writes the number as their manual override - the same thing
        typing it into the FTP field does - so the training FTP still only ever
        moves because the rider said so. Dismissing changes nothing; the
        evidence behind it was consumed when the suggestion was filed, so it
        does not immediately come back.
        """
        uid = _uid(request)
        target = next_path if next_path in ("/settings", "/profile") else "/settings"
        row = db.resolve_ftp_suggestion(
            uid, suggestion_id, "accepted" if action == "use" else "dismissed"
        )
        if row is None:
            return RedirectResponse(target, status_code=303)
        if action == "use":
            value = int(round(float(row["suggested_ftp"])))
            db.set_user_ftp_override(uid, max(1, min(2000, value)))
            try:
                profile_store.refresh(uid)
            except Exception:
                _log.warning(
                    "profile refresh after FTP suggestion failed", exc_info=True
                )
        outcome = "used" if action == "use" else "dismissed"
        return RedirectResponse(f"{target}?ftp_suggestion={outcome}", status_code=303)

    @app.post("/profile/hr-max", response_class=HTMLResponse)
    def profile_hr_max_save(
        request: Request,
        hr_max: str = Form(""),
        action: str = Form("save"),
    ):
        uid = _uid(request)
        if action == "reset":
            db.set_user_hr_max(uid, None)
            try:
                profile_store.refresh(uid)
            except Exception:
                _log.warning("profile refresh after HRmax reset failed", exc_info=True)
            return RedirectResponse("/profile?saved=1", status_code=303)
        try:
            value = int(hr_max.strip())
        except (TypeError, ValueError):
            return _profile_response(request, "HRmax must be a whole number from 80 to 230 bpm.")
        if not 80 <= value <= 230:
            return _profile_response(request, "HRmax must be a whole number from 80 to 230 bpm.")
        db.set_user_hr_max(uid, value)
        try:
            profile_store.refresh(uid)
        except Exception:
            _log.warning("profile refresh after HRmax save failed", exc_info=True)
        return RedirectResponse("/profile?saved=1", status_code=303)

    @app.get("/activity/{activity_id}", response_class=HTMLResponse)
    def activity_detail_page(request: Request, activity_id: int):
        uid = _uid(request)
        detail = pipeline.activity_detail(uid, activity_id)
        if not detail:
            return RedirectResponse(url="/activities", status_code=303)
        # Only the scalar summary goes into the template; the (larger) stream
        # arrays load from the JSON endpoint so the HTML stays small.
        summary = {k: detail[k] for k in (
            "id", "filename", "start_time", "duration_s", "distance_m",
            "avg_power", "avg_hr", "np", "if_", "tss", "have", "points")}
        summary["duration_fmt"] = races.format_duration(summary.get("duration_s"))
        return templates.TemplateResponse(
            request, "activity_detail.html", _ctx(request, activity=summary)
        )

    @app.get("/api/activity/{activity_id}")
    def api_activity_detail(request: Request, activity_id: int):
        uid = _uid(request)
        detail = pipeline.activity_detail(uid, activity_id)
        if not detail:
            return JSONResponse({"error": "not found"}, status_code=404)
        # A ride completed against a plan/standalone workout drives that
        # workout's RPE (feeding the FTP loop) instead of a subjective rating;
        # only expose the link when the completion is verified.
        link = db.linked_workout_for_activity(uid, activity_id)
        if link:
            if link["kind"] == "plan":
                w = db.get_plan_workout(uid, link["id"])
                verified = bool(
                    w and importer.plan_workout_completion_verified(uid, w)
                )
            else:
                verified = True  # standalone links are verified once completed
            detail["linked_workout"] = {
                "kind": link["kind"],
                "id": link["id"],
                "name": link["name"],
                "rpe": link["rpe"],
                "rpe_eligible": verified,
            }
        return JSONResponse(detail)

    @app.post("/api/activity/{activity_id}/rpe")
    def api_activity_rpe(
        request: Request, activity_id: int, rpe: int = Body(..., embed=True)
    ):
        """Store a subjective effort rating (1-10) on an unmatched activity."""
        uid = _uid(request)
        try:
            rpe_val = int(rpe)
        except (TypeError, ValueError):
            return JSONResponse({"error": "rpe must be an integer"}, status_code=400)
        if rpe_val < 1 or rpe_val > 10:
            return JSONResponse(
                {"error": "rpe must be between 1 and 10"}, status_code=400
            )
        if not db.set_activity_rpe(uid, activity_id, rpe_val):
            return JSONResponse({"error": "activity not found"}, status_code=404)
        return JSONResponse({"id": activity_id, "rpe": rpe_val})

    @app.post("/activities/rescan")
    def rescan(request: Request, activities_dir: str = Form("")):
        """Start an asynchronous rescan and return immediately.

        The scan runs in a background daemon thread; the client polls
        GET /api/scan/status for live progress. A rescan already in flight for
        this user returns 409 with the running status (no second scan starts).
        """
        uid = _uid(request)
        # Confine the posted folder to the same roots /settings allows. This
        # endpoint both SCANS the path (reading and parsing every *.fit under
        # it) and PERSISTS it, so without this it was a way to point the
        # importer - and every later background scan - at any directory on the
        # machine, straight past the /settings check. Existence is not required
        # here: the status panel reports "folder not found" for a path that is
        # simply not on this machine, and scanning a path that does not exist
        # reads nothing.
        clean, err = paths.confine_storage_dir(activities_dir, must_exist=False)
        if err:
            return JSONResponse({"error": err}, status_code=400)
        # Persist a typed directory as the user's activities_dir setting.
        if clean:
            db.save_user_settings(uid, {"activities_dir": clean})
        started = _start_user_scan(uid, directory=clean or None)
        if started is None:
            return JSONResponse(_scan_status_snapshot(uid), status_code=409)
        return JSONResponse(started, status_code=202)

    @app.get("/api/scan/status")
    def scan_status(request: Request):
        snapshot = _scan_status_snapshot(_uid(request))
        if snapshot is None:
            return JSONResponse({"running": False})
        return JSONResponse(snapshot)

    @app.post("/activities/upload")
    async def upload(request: Request, file: UploadFile):
        # Reject oversized uploads before/while reading the whole body into
        # memory. Prefer the declared size when present, but always re-check the
        # bytes actually read (Content-Length can lie).
        size = getattr(file, "size", None)
        if size is not None and size > MAX_UPLOAD_BYTES:
            return Response("Uploaded file is too large.", status_code=413)
        content = await file.read()
        if len(content) > MAX_UPLOAD_BYTES:
            return Response("Uploaded file is too large.", status_code=413)
        importer.ingest_upload(_uid(request), file.filename or "upload.fit", content)
        return RedirectResponse(url="/activities", status_code=303)

    @app.post("/activities/link-duplicates")
    def link_duplicates(request: Request):
        """Repair pass: link rides this user recorded in-app AND in Zwift.

        New rides are linked as they land; this walks the existing history for
        pairs recorded before duplicate detection existed.
        """
        uid = _uid(request)
        linked = importer.backfill_duplicate_links(uid)
        if linked:
            # Duplicates are excluded from the mean-maximal curve, so linking
            # them moves CP, W' and every peak the profile is built from.
            # Never let a refresh failure lose the repair the user just ran.
            try:
                profile_store.refresh(uid)
            except Exception:
                _log.warning("rider profile refresh after duplicate linking "
                             "failed", exc_info=True)
        return RedirectResponse(url=f"/activities?linked={linked}", status_code=303)

    def _plan_defaults() -> dict:
        return {
            "weeks": 8,
            "hours_per_week": 6.0,
            "hit_days_per_week": 2,
            "days": [0, 2, 4, 5],
            "hard_days": [],
            "start_date": _upcoming_monday().isoformat(),
            "name": "Training Plan",
            "model": planmod.DEFAULT_MODEL,
            # No goal is the default: a goal-less plan is the flat plan the app
            # has always generated, and it must stay reachable in one click.
            "goal": None,
        }

    # ---------------------------------------------------------------- goals
    def _current_ctl(uid: Optional[int]) -> Optional[float]:
        """Latest CTL, for a plan-length recommendation. Never raises.

        Uses the load series rather than a full ``build_state``: CTL comes
        straight from stored daily TSS, so this costs one query instead of
        inflating months of power streams on a page render.
        """
        if uid is None:
            return None
        try:
            series = pipeline.load_series(uid)
        except Exception:  # noqa: BLE001 - a recommendation is never load-bearing
            _log.warning("could not read CTL for a goal recommendation",
                         exc_info=True)
            return None
        return series[-1]["ctl"] if series else None

    def _goal_options(uid: Optional[int], hours_per_week: float) -> List[dict]:
        """Every goal, with the length it recommends and how well-founded it is.

        The basis strength travels with the number deliberately: the FTP range
        is literature-informed while the other two are coaching convention, and
        showing all three as bare week counts would dress a heuristic as
        evidence. It is ADVISORY - nothing here gates or clamps the form.
        """
        ctl = _current_ctl(uid)
        options: List[dict] = []
        for goal in goalsmod.all_goals():
            rec = durationmod.recommend_weeks(goal.key, ctl, hours_per_week)
            options.append({
                "key": goal.key,
                "label": goal.label,
                "description": goal.description,
                "default_model": goal.default_model,
                "default_model_label":
                    planmod.MODELS[goal.default_model].label,
                "ideal_weeks": rec.ideal_weeks,
                "floor_weeks": rec.floor_weeks,
                "rationale": rec.rationale,
                "basis_strength": rec.basis_strength.value,
                "phases": [p.name for p in goal.arc],
                "min_viable_weeks": phasesmod.minimum_viable_weeks(goal.arc),
            })
        return options

    def _goal_length_note(
        goal_key: Optional[str], weeks: int, hours_per_week: float,
        uid: Optional[int],
    ) -> Optional[dict]:
        """What the chosen length means for this goal. Advice, never a refusal.

        A length below the goal's floor, or below the arc's own viability
        threshold, is generated anyway - the rider gets the plan they asked for
        plus a plain statement of what they gave up (phases dropped from the
        front, or an arc abandoned entirely).
        """
        goal = goalsmod.get(goal_key)
        if goal is None:
            return None
        rec = durationmod.recommend_weeks(goal.key, _current_ctl(uid),
                                          hours_per_week)
        summary = goalsmod.block_summary(goal.key, weeks) or {}
        return {
            "goal": goal.key,
            "label": goal.label,
            "classification": durationmod.classify_chosen_weeks(weeks, rec),
            "ideal_weeks": rec.ideal_weeks,
            "floor_weeks": rec.floor_weeks,
            "basis_strength": rec.basis_strength.value,
            "rationale": rec.rationale,
            "blocks": summary.get("blocks") or [],
            "omitted": summary.get("omitted") or [],
            "unphased_reason": summary.get("unphased_reason"),
        }

    def _durability_signal(uid: int) -> Optional[dict]:
        """Late 5-minute power retention, or None when the evidence is thin.

        Returning None (rather than a zero) is the point: durability needs a
        hard 5-minute effort late in a long ride, and the steady endurance rides
        this goal prescribes usually contain no such effort, so "no measurement"
        is the common case and must read as silence.
        """
        try:
            activities = db.recent_full_activities(uid, days=90)
            weight = (db.get_user_settings(uid) or {}).get("weight_kg")
            result = durabilitymod.compute_durability(activities, weight)
        except Exception:  # noqa: BLE001 - a progress panel never breaks a page
            _log.warning("durability computation failed", exc_info=True)
            return None
        if result.retention_ratio is None:
            return None
        return {
            "retention_pct": round(result.retention_ratio * 100.0, 1),
            "fresh_w": round(result.fresh_5min_power or 0.0, 0),
            "late_w": round(result.late_5min_power or 0.0, 0),
            "rides": result.qualifying_rides,
        }

    def _goal_progress(uid: Optional[int], goal_key: Optional[str]) -> Optional[dict]:
        """The goal's own progress signals, with absent ones left out entirely.

        Each goal names what "progress" means for it (see prescribe/goals.py),
        so a criterium rider is shown their 5s/1min peaks rather than FTP. A
        signal with no measurement behind it is omitted; a secondary signal is
        never rendered as a zero when it simply is not there.
        """
        goal = goalsmod.get(goal_key)
        if goal is None or uid is None:
            return None
        panels: List[dict] = []
        for signal in goal.signals:
            value: Optional[dict] = None
            if signal.key == "ftp_trend":
                try:
                    series = pipeline.ftp_rolling_series(uid, months=6)
                    points = series.get("estimated") or []
                except Exception:  # noqa: BLE001
                    points = []
                if points:
                    first, last = points[0], points[-1]
                    value = {
                        "current": round(float(last["ftp"]), 0),
                        "from": round(float(first["ftp"]), 0),
                        "from_date": first["date"][:10],
                        "change": round(float(last["ftp"]) - float(first["ftp"]), 0),
                    }
            elif signal.key == "peak_power":
                profile = profile_store.for_user(uid)
                if profile.peak_5s is not None or profile.peak_60s is not None:
                    value = {
                        "peak_5s": (round(profile.peak_5s, 0)
                                    if profile.peak_5s is not None else None),
                        "peak_60s": (round(profile.peak_60s, 0)
                                     if profile.peak_60s is not None else None),
                    }
            elif signal.key == "decoupling":
                try:
                    pct = activity_cache.get_digest(uid).decoupling
                except Exception:  # noqa: BLE001
                    pct = None
                if pct is not None:
                    value = {"percent": round(float(pct), 1)}
            elif signal.key == "durability":
                value = _durability_signal(uid)
            if value is None:
                continue
            panels.append({
                "key": signal.key,
                "label": signal.label,
                "description": signal.description,
                "role": signal.role,
                "value": value,
            })
        if not panels:
            return None
        return {"goal": goal.key, "label": goal.label, "signals": panels}

    def _plan_management(uid: Optional[int]) -> dict:
        """Summarize the user's plans for the management section.

        current: the plan the user explicitly marked active; failing that (no
        active plan - legacy users have none until they set one) the plan whose
        date range (start_date .. +weeks*7d) covers today; failing that the most
        recent plan, flagged in_effect=False. None only when the user has no
        plans. others: every other plan, newest first. Each entry carries name,
        model, dates, end_date, active, and progress (completed/total workouts).
        """
        if uid is None:
            return {"current": None, "others": []}
        today = utc_today()
        entries = []
        for p in db.list_plans(uid):  # created DESC
            workouts = db.plan_workouts_for_plan(uid, p["id"])
            total = len(workouts)
            completed = sum(
                1 for w in workouts if w.get("completed_activity_id")
            )
            try:
                start = _dt.date.fromisoformat(p["start_date"])
                end = start + _dt.timedelta(days=int(p["weeks"]) * 7)
            except (ValueError, TypeError):
                start = end = None
            covers = bool(start and end and start <= today < end)
            entries.append({
                "id": p["id"],
                "name": p["name"],
                "model": p["model"],
                "start_date": p["start_date"],
                "end_date": end.isoformat() if end else None,
                "weeks": p["weeks"],
                "total": total,
                "completed": completed,
                "covers_today": covers,
                "active": bool(p.get("active")),
                # Pending "the nightly sweep rewrote some of this" notice.
                "reflow_notice": p.get("reflow_notice"),
                "goal": goalsmod.get((p.get("recipe") or {}).get("goal")),
            })
        if not entries:
            return {"current": None, "others": []}
        # Explicit choice first; otherwise the date-coverage heuristic, which is
        # all a legacy user (no active plan) has. Falls back to the most recent
        # plan (list is created DESC). in_effect stays a statement about dates.
        current = next((e for e in entries if e["active"]), None)
        if current is None:
            current = next((e for e in entries if e["covers_today"]), entries[0])
        current["in_effect"] = current["covers_today"]
        others = [e for e in entries if e["id"] != current["id"]]
        return {"current": current, "others": others}

    def _generate_ctx(request: Request, **kw) -> dict:
        uid = _uid(request)
        defaults = kw.get("plan_defaults") or _plan_defaults()
        base = dict(
            session=None,
            error=None,
            duration=60,
            mode="workout",
            plan=None,
            plan_error=None,
            plan_defaults=defaults,
            goal_options=_goal_options(uid, defaults.get("hours_per_week") or 6.0),
            day_labels=DAY_LABELS,
            exported=None,
            exported_path=None,
            scheduled_date=utc_today().isoformat(),
            flash=None,
            plan_mgmt=_plan_management(uid),
        )
        base.update(kw)
        return _ctx(request, **base)

    def _plan_race_summary(uid: int, plan: dict, recipe: dict,
                           workouts: List[dict]) -> List[dict]:
        """What this plan did about the rider's races, as facts about its rows.

        Recomputed at VIEW time, because races are deliberately never stored in
        the recipe - they are an input read fresh on every reflow. This is the
        SINGLE place it is computed: unlike `phases`, a freshly generated plan
        must NOT overwrite it, or a plan would say one thing on creation and
        another when re-opened.

        ``describe_races`` works date by date: a claim needs both evidence (the
        STORED row differs from a raceless baseline) and attribution (the race
        predicts that difference), so a plan never recomputed for a race - only
        the ACTIVE plan is reflowed when one changes - describes nothing, while
        a plan whose taper partly landed describes exactly the part that did.
        """
        races = db.list_race_dates(uid)
        if not races:
            return []
        try:
            start = _dt.date.fromisoformat(plan["start_date"])
        except (ValueError, TypeError):
            return []
        end = start + _dt.timedelta(days=7 * int(plan["weeks"]) - 1)

        def _unrecomputable() -> List[dict]:
            """A plan that cannot be regenerated cannot have its race handling
            described - reflow refuses it too. State the priorities, which are
            true of the race list alone, and nothing else."""
            return [
                {**r, "no_recipe": True, "affects": [], "predicted": [],
                 "left_alone": [], "pending": [], "displaces_workout": False,
                 "shorter": [], "recovery_dates": [], "easy_dates": [],
                 "outside_plan": False}
                for r in planmod.race_priorities(races)
                if start.isoformat() <= r["date"] <= end.isoformat()
            ]

        if not recipe.get("days_of_week"):
            return _unrecomputable()   # predates recipes; reflow refuses it
        try:
            return planmod.describe_races(
                races, plan["name"], start, int(plan["weeks"]),
                days_of_week=recipe["days_of_week"],
                hours_per_week=recipe.get("hours_per_week"),
                hit_days_per_week=recipe.get("hit_days_per_week"),
                hard_days=recipe.get("hard_days") or None,
                model=recipe.get("model") or planmod.DEFAULT_MODEL,
                profile=profile_store.for_user(uid),
                phases=goalsmod.arc_for(recipe.get("goal")),
                stored={w["date"]: w for w in workouts},
                # Reflow's own cutoff: a row dated today or earlier is never
                # rewritten, so an effect missing there is explained, not owed.
                today=utc_today().isoformat(),
            )
        except (ValueError, TypeError):
            _log.warning("cannot describe races for plan %s", plan.get("id"),
                         exc_info=True)
            return _unrecomputable()

    def _plan_summary(uid: int, plan_id: int) -> Optional[dict]:
        plan = db.get_plan(uid, plan_id)
        if not plan:
            return None
        workouts = db.plan_workouts_for_plan(uid, plan_id)
        summary = dict(plan)
        summary["plan_id"] = plan_id
        summary["count"] = len(workouts)
        summary["workouts"] = workouts
        summary["total_tss"] = round(sum(w["tss"] for w in workouts), 1)
        # The goal lives in the recipe, so a stored plan can say what it was
        # built for - and show the arc and the progress signal that go with it -
        # long after the form that created it is gone.
        recipe = plan.get("recipe") or {}
        goal = goalsmod.get(recipe.get("goal"))
        if goal is not None:
            summary["goal"] = {
                "key": goal.key,
                "label": goal.label,
                "description": goal.description,
                "default_model": goal.default_model,
            }
            # The arc as it resolves for this plan's length. A freshly generated
            # plan overwrites this with what the generator actually applied;
            # they agree, because both come from the same resolver.
            summary["phases"] = goalsmod.block_summary(goal.key, int(plan["weeks"]))
            summary["length"] = _goal_length_note(
                goal.key, int(plan["weeks"]),
                float(recipe.get("hours_per_week") or 0.0) or 6.0, uid,
            )
            summary["progress"] = _goal_progress(uid, goal.key)
        summary["races"] = _plan_race_summary(uid, plan, recipe, workouts)
        return summary

    def _auto_export_plan(uid: int, plan_id: int) -> dict:
        """Export a new plan's .zwo files to the user's Zwift folder when the
        target can be determined; otherwise report why so the UI can point the
        user at the Settings picker. Never guesses between player folders.
        """
        settings = db.get_user_settings(uid)
        target, reason = paths.resolve_export_dir(
            settings.get("zwift_id"), settings.get("workouts_dir")
        )
        if not target:
            return {
                "auto_export": None,
                "auto_export_reason": reason,  # 'choose' | 'missing'
                "zwift_candidates": paths.candidate_zwift_ids(),
            }
        workouts = db.plan_workouts_for_plan(uid, plan_id, include_zwo=True)
        try:
            result = zwo.write_plan_to_zwift(
                [
                    {"date": w["date"], "name": w["name"], "zwo": w["zwo_or_segments"]}
                    for w in workouts
                ],
                settings.get("zwift_id") or "me",
                workouts_override=target,
            )
        except OSError as e:
            _log.warning("plan auto-export failed: %s", e)
            return {"auto_export": None, "auto_export_reason": f"error: {e}"}
        return {
            "auto_export": {
                "count": result["count"],
                "directory": result["directory"],
                "reason": reason,
            },
            "auto_export_reason": None,
        }

    @app.get("/plan", response_class=HTMLResponse)
    def plan_page(request: Request, plan_id: Optional[int] = None):
        uid = _uid(request)
        summary = None
        if plan_id is not None and uid is not None:
            summary = _plan_summary(uid, plan_id)  # None if missing/foreign - ignored
        return templates.TemplateResponse(
            request, "plan.html",
            _generate_ctx(
                request, mode="plan" if summary else "workout",
                plan=summary, flash=request.query_params.get("flash"),
            ),
        )

    @app.get("/generate")
    def generate_redirect():
        # Old link target; the page moved to /plan.
        return RedirectResponse(url="/plan", status_code=307)

    @app.post("/generate", response_class=HTMLResponse)
    def generate_submit(request: Request, duration_min: int = Form(...)):
        uid = _uid(request)
        state = pipeline.build_state(uid)
        error: Optional[str] = None
        session_dict = None
        try:
            session = plan_workout(state, duration_min,
                                   profile=profile_store.for_user(uid))
            session = llm.shape_session(session, state)
            session.compute_tss()
            zwo_str = zwo.zwo_string(session)
            app.state.last[uid] = {
                "zwo": zwo_str,
                "name": session.name,
                "type": session.workout_type,
                "duration_s": session.total_duration(),
                "tss": session.estimated_tss,
                "export_ftp": float(state.ftp),
            }
            session_dict = session.to_dict()
            session_dict["zwo"] = zwo_str
        except ValueError as e:
            error = str(e)
        return templates.TemplateResponse(
            request,
            "plan.html",
            _generate_ctx(
                request, mode="workout", session=session_dict,
                error=error, duration=duration_min,
            ),
        )

    @app.post("/generate/plan", response_class=HTMLResponse)
    def generate_plan_submit(
        request: Request,
        name: str = Form("Training Plan"),
        weeks: int = Form(...),
        hours_per_week: float = Form(...),
        hit_days_per_week: int = Form(...),
        start_date: str = Form(""),
        days: List[str] = Form([]),
        hard_days: List[str] = Form([]),
        model: str = Form("polarized"),
        goal: str = Form(""),
    ):
        uid = _uid(request)
        # An unrecognized goal key reads as no goal at all: the plan is still
        # generated, just flat. A goal must never be the reason a rider cannot
        # create a plan.
        goal_key = goalsmod.normalize_key(goal)
        try:
            day_ints = sorted({int(d) for d in days})
        except (ValueError, TypeError):
            day_ints = []
        try:
            hard_ints = sorted({int(d) for d in hard_days})
        except (ValueError, TypeError):
            hard_ints = []
        try:
            start = _dt.date.fromisoformat(start_date) if start_date else _upcoming_monday()
        except ValueError:
            start = _upcoming_monday()

        plan_error: Optional[str] = None
        summary = None
        # Preserve the user's inputs in the redisplayed form.
        defaults = {
            "weeks": weeks, "hours_per_week": hours_per_week,
            "hit_days_per_week": hit_days_per_week, "days": day_ints,
            "hard_days": hard_ints,
            "start_date": start.isoformat(), "name": name,
            "model": model, "goal": goal_key,
        }
        try:
            # Born profile-aware: without this a new plan is built on the
            # population constants and the very next nightly reflow rewrites
            # every workout in it.
            #
            # ``phases`` is None for a goal-less plan, which is the path the
            # generator took before goals existed - so an existing plan and a
            # plan created with no goal are the same plan, byte for byte.
            #
            # Races are read here exactly the way the nightly sweep reads them
            # (fresh from the DB, never into the recipe - see reflow.py). Born
            # race-blind, a plan made by a rider with races on the calendar
            # gets every skip and taper written in on night one, and the rider
            # is told their brand-new plan "changed overnight".
            generated = planmod.generate_plan(
                name, start, weeks, day_ints, hours_per_week, hit_days_per_week,
                hard_days=hard_ints or None, model=model,
                races=db.list_race_dates(uid),
                profile=profile_store.for_user(uid),
                phases=goalsmod.arc_for(goal_key),
            )
            # Persist the generator inputs, not just their output, so the plan
            # can be recomputed later (see prescribe/reflow.py). The goal is in
            # there because it is a choice, not a measurement (see goals.py).
            recipe = reflow.build_recipe(
                day_ints, hours_per_week, hit_days_per_week,
                hard_days=hard_ints, model=model, goal=goal_key,
            )
            plan_id = db.create_plan(
                uid, name or "Training Plan", generated["start_date"],
                generated["weeks"], model=generated["model"], recipe=recipe,
            )
            # The FTP these fractions were written for, stored alongside them
            # so a later completion match can sanity-check the wattage it
            # fitted - the same thing a standalone export has always carried.
            export_ftp = importer.current_ftp(uid)
            for w in generated["workouts"]:
                zwo_str = zwo.zwo_string(w["session"])
                db.add_plan_workout(
                    plan_id, uid, w["date"], w["name"], w["type"],
                    w["duration_s"], w["tss"], zwo_str,
                    variant=w.get("variant"), origin=reflow.GENERATED,
                    export_ftp=export_ftp,
                )
            # Match any already-imported activities against the new plan's
            # workouts now - the gated rescan path only matches when NEW files
            # import, so a plan created after its rides were imported would
            # otherwise never be marked completed until a future import.
            importer.match_plan_completions(uid)
            summary = _plan_summary(uid, plan_id)
            summary["polarized_hard_fraction"] = generated["polarized_hard_fraction"]
            summary["weekly"] = generated["weekly"]
            # The resolved arc as the generator actually applied it, including
            # any phase it had no room for and the reason an arc was abandoned.
            summary["phases"] = generated.get("phases")
            summary.update(_auto_export_plan(uid, plan_id))
        except ValueError as e:
            plan_error = str(e)

        return templates.TemplateResponse(
            request,
            "plan.html",
            _generate_ctx(
                request, mode="plan", plan=summary, plan_error=plan_error,
                plan_defaults=defaults,
            ),
        )

    @app.post("/plan/{plan_id}/export", response_class=HTMLResponse)
    def plan_export(request: Request, plan_id: int):
        uid = _uid(request)
        workouts = db.plan_workouts_for_plan(uid, plan_id, include_zwo=True)
        settings = db.get_user_settings(uid)
        exported = None
        if workouts:
            result = zwo.write_plan_to_zwift(
                [
                    {"date": w["date"], "name": w["name"], "zwo": w["zwo_or_segments"]}
                    for w in workouts
                ],
                settings.get("zwift_id") or "me",
                workouts_override=settings.get("workouts_dir"),
            )
            exported = {"count": result["count"], "directory": result["directory"]}
        summary = _plan_summary(uid, plan_id)
        return templates.TemplateResponse(
            request,
            "plan.html",
            _generate_ctx(request, mode="plan", plan=summary, exported=exported),
        )

    @app.post("/plan/{plan_id}/activate")
    def plan_activate(request: Request, plan_id: int):
        """Mark a plan as the user's active plan, then go back where we came from."""
        uid = _uid(request)
        plan = db.get_plan(uid, plan_id)
        if not plan:
            # Not this user's plan (or already gone) -> 404, no cross-user write.
            return JSONResponse({"error": "not found"}, status_code=404)
        db.set_active_plan(uid, plan_id)
        flash = f"“{plan['name']}” is now your active plan"
        # The button lives on both /plan and /calendar; only ever bounce back to
        # one of our own pages, never to an attacker-supplied Referer.
        referer = _url.urlparse(request.headers.get("referer") or "").path
        target = "/calendar" if referer == "/calendar" else "/plan"
        return RedirectResponse(
            url=f"{target}?flash=" + _url.quote(flash), status_code=303
        )

    @app.get("/plan/{plan_id}/download.zip")
    def plan_download_zip(request: Request, plan_id: int):
        uid = _uid(request)
        workouts = db.plan_workouts_for_plan(uid, plan_id, include_zwo=True)
        if not workouts:
            return RedirectResponse(url="/plan", status_code=303)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for w in workouts:
                fname = zwo.plan_filename(w["date"], w["name"])
                zf.writestr(fname, w["zwo_or_segments"])
        buf.seek(0)
        plan = db.get_plan(uid, plan_id)
        zipname = zwo._safe_filename(plan["name"] if plan else "plan").replace(".zwo", "")
        return Response(
            content=buf.getvalue(),
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{zipname}.zip"'},
        )

    @app.post("/plan/workout/{workout_id}/export", response_class=HTMLResponse)
    def plan_workout_export(request: Request, workout_id: int):
        uid = _uid(request)
        w = db.get_plan_workout(uid, workout_id)
        exported = None
        summary = None
        if w:
            settings = db.get_user_settings(uid)
            result = zwo.write_plan_to_zwift(
                [{"date": w["date"], "name": w["name"], "zwo": w["zwo_or_segments"]}],
                settings.get("zwift_id") or "me",
                workouts_override=settings.get("workouts_dir"),
            )
            exported = {"count": result["count"], "directory": result["directory"]}
            summary = _plan_summary(uid, w["plan_id"])
        return templates.TemplateResponse(
            request,
            "plan.html",
            _generate_ctx(request, mode="plan", plan=summary, exported=exported),
        )

    @app.get("/plan/workout/{workout_id}/download")
    def plan_workout_download(request: Request, workout_id: int):
        w = db.get_plan_workout(_uid(request), workout_id)
        if not w:
            return RedirectResponse(url="/plan", status_code=303)
        fname = zwo.plan_filename(w["date"], w["name"])
        return Response(
            content=w["zwo_or_segments"],
            media_type="application/xml",
            headers={"Content-Disposition": f'attachment; filename="{fname}"'},
        )

    @app.post("/plan/{plan_id}/delete")
    def plan_delete(request: Request, plan_id: int):
        uid = _uid(request)
        plan = db.get_plan(uid, plan_id)
        if not plan:
            # Not this user's plan (or already gone) -> 404, no cross-user delete.
            return JSONResponse({"error": "not found"}, status_code=404)
        # Remove Zwift .zwo files BEFORE the rows go (filenames come from rows).
        export = exporter.remove_plan_exports(uid, plan_id)
        counts = db.delete_plan(uid, plan_id)
        workouts_deleted = counts["workouts"] if counts else 0
        files_removed = export.get("removed", 0)
        flash = (
            f"Deleted plan “{plan['name']}” — "
            f"{workouts_deleted} workout"
            f"{'' if workouts_deleted == 1 else 's'}, "
            f"{files_removed} .zwo file"
            f"{'' if files_removed == 1 else 's'} removed from Zwift folder"
        )
        return RedirectResponse(
            url="/plan?flash=" + _url.quote(flash), status_code=303
        )

    @app.get("/volume", response_class=HTMLResponse)
    def volume_page(request: Request):
        return templates.TemplateResponse(request, "volume.html", _ctx(request))

    @app.get("/api/volume")
    def api_volume(request: Request):
        return JSONResponse({"weeks": db.weekly_volume(_uid(request))})

    # --------------------------------------------------- calendar feed
    def _record_calendar_feed_failure() -> None:
        """Tally a rejected feed token and surface sustained guessing.

        Reached only after the token failed to resolve, so it can never delay
        or refuse a valid subscriber. Nothing client-controlled is recorded or
        logged: not the token, and not the source address (which a caller can
        forge - see CalendarFeedFailureCounter). Only the running count.
        """
        count = app.state.calendar_failures.record_failure()
        if count % CALENDAR_TOKEN_FAILURE_THRESHOLD == 0:
            _log.warning(
                "%d rejected calendar-feed tokens so far (valid tokens are "
                "unaffected and still served)", count,
            )

    def _head_of(body: bytes, response: Response, is_head: bool) -> Response:
        """Drop the body for a HEAD while keeping the headers a GET would send.

        Starlette sends ``response.body`` regardless of method, so HEAD has to
        be handled here. RFC 9110 8.6: the Content-Length of a HEAD is the one
        the equivalent GET would have carried, so it is set explicitly (an
        explicit content-length also suppresses Starlette's auto-computed one,
        which would otherwise say 0).
        """
        if not is_head:
            return response
        response.headers["content-length"] = str(len(body))
        response.body = b""
        return response

    def _calendar_feed_missing(is_head: bool = False) -> Response:
        """The one answer for every rejected feed request.

        Missing, empty, malformed and unknown all land here with an identical
        404. A 401/403 would confirm that the endpoint takes a token and that
        the presented one merely failed - and distinguishing "unknown token"
        from "no token" would turn the endpoint into an existence oracle. HEAD
        and GET are rejected identically apart from the absent body.
        """
        body = b"Not Found"
        return _head_of(
            body,
            PlainTextResponse(
                body,
                status_code=404,
                headers={"Cache-Control": "private, no-store"},
            ),
            is_head,
        )

    # GET and HEAD: some calendar clients probe with a conditional HEAD before
    # fetching, and a 405 there makes them give up on the subscription.
    @app.api_route(
        calendarfeed.FEED_PATH, methods=["GET", "HEAD"], include_in_schema=False
    )
    def calendar_feed(request: Request, token: str = ""):
        """Per-user .ics feed, authenticated solely by ``token``.

        No session is consulted: the token alone names the user, and every row
        the response contains is read with that user's id. ``token`` defaults
        to "" rather than being required so that a missing parameter is a 404
        like any other bad token, not FastAPI's 422 (which would announce the
        parameter's existence).

        The token is resolved BEFORE any rate-limit state is consulted, so a
        valid token is served unconditionally - see the failure counter's note
        in create_app for why that ordering is load-bearing.
        """
        is_head = request.method == "HEAD"
        user = calendarfeed.user_for_token(token)
        if user is None:
            _record_calendar_feed_failure()
            return _calendar_feed_missing(is_head)
        body = calendarfeed.build_ics(user["id"]).encode("utf-8")
        return _head_of(
            body,
            Response(
                content=body,
                media_type="text/calendar; charset=utf-8",
                headers={
                    # The URL is a bearer credential; no shared cache may keep
                    # the response, and no client may write it to disk.
                    "Cache-Control": "private, no-store",
                    "X-Content-Type-Options": "nosniff",
                    "Content-Disposition": 'inline; filename="wattracker.ics"',
                },
            ),
            is_head,
        )

    @app.get("/calendar", response_class=HTMLResponse)
    def calendar_view(
        request: Request,
        year: Optional[int] = None,
        month: Optional[int] = None,
    ):
        uid = _uid(request)
        today = utc_today()
        y = year or today.year
        m = month or today.month
        # Normalise month into 1..12 (defensive).
        if m < 1:
            y, m = y - 1, 12
        elif m > 12:
            y, m = y + 1, 1

        ooto_ranges = db.list_ooto_ranges(uid)
        # Each row gets its matched ZwiftPower/local result (if the race has
        # passed and one was found) attached under "result" - see
        # races.match_result_for_race_date for why this is resolved live
        # rather than stored on race_dates.
        stored_races = db.list_race_dates(uid)
        race_dates = races.attach_results_to_race_dates(uid, stored_races)
        # The badge must show the priority the PLAN uses, not the one the row
        # stores: an A race inside an earlier A race's taper is planned as a B
        # race, and a calendar saying "A" next to a plan that tapers it as a B
        # is exactly the confusion this describes away. Same resolution the plan
        # summary uses - never a second copy of the rule.
        described = {d["id"]: d for d in planmod.race_priorities(stored_races)}
        for r in race_dates:
            d = described.get(r["id"])
            if d is not None:
                r["priority"] = d["priority"]        # EFFECTIVE
                r["demoted"] = d["demoted"]
                r["conflicts_with"] = d["conflicts_with"]
                r["separation_days"] = d["separation_days"]
        races_by_date = {r["date"]: r for r in race_dates}

        def _in_ooto(date_iso: str) -> bool:
            return any(r["start_date"] <= date_iso <= r["end_date"]
                       for r in ooto_ranges)

        today_iso = today.isoformat()
        by_date: dict = {}
        for w in db.plan_workouts_for_month(uid, y, m):
            wd = dict(w)
            wd["skipped"] = _in_ooto(w["date"]) and not w.get("completed_activity_id")
            # Missed: a past-dated workout left uncompleted that wasn't an
            # out-of-office skip (i.e. its day passed without completion).
            wd["missed"] = (
                w["date"] < today_iso
                and not w.get("completed_activity_id")
                and not wd["skipped"]
            )
            by_date.setdefault(w["date"], []).append(wd)
        for w in db.standalone_workouts_for_month(uid, y, m):
            wd = dict(w)
            wd.update({
                "date": w["scheduled_date"],
                "standalone": True,
                "adapted": None,
                "skipped": False,
                "missed": (
                    w["scheduled_date"] < today_iso
                    and not w.get("completed_activity_id")
                ),
            })
            by_date.setdefault(w["scheduled_date"], []).append(wd)

        # Which block of the active plan's arc each day belongs to, so a day
        # cell can say "build" rather than leaving the rider to count weeks.
        # Empty for a plan with no goal, which is every plan that predates them.
        active = db.get_active_plan(uid) if uid is not None else None
        phase_by_date = {}
        if active is not None:
            phase_by_date = goalsmod.phase_by_date(
                active["start_date"], active["weeks"],
                (active.get("recipe") or {}).get("goal"),
            )

        cal = _cal.Calendar(firstweekday=0)  # Monday
        weeks = []
        for week in cal.monthdatescalendar(y, m):
            row = []
            for d in week:
                iso = d.isoformat()
                row.append(
                    {
                        "date": iso,
                        "day": d.day,
                        "in_month": d.month == m,
                        "ooto": _in_ooto(iso),
                        "race": races_by_date.get(iso),
                        "workouts": by_date.get(iso, []),
                        "phase": phase_by_date.get(iso),
                    }
                )
            weeks.append(row)

        prev_y, prev_m = (y - 1, 12) if m == 1 else (y, m - 1)
        next_y, next_m = (y + 1, 1) if m == 12 else (y, m + 1)
        return templates.TemplateResponse(
            request,
            "calendar.html",
            _ctx(
                request,
                year=y,
                month=m,
                month_name=_cal.month_name[m],
                weeks=weeks,
                day_labels=DAY_LABELS,
                prev=f"?year={prev_y}&month={prev_m}",
                next=f"?year={next_y}&month={next_m}",
                plans=db.list_plans(uid),
                ooto_ranges=ooto_ranges,
                race_dates=race_dates,
                export_result=request.query_params.get("exported"),
                flash=request.query_params.get("flash"),
                # An unattended overnight rewrite the rider was never told
                # about is indistinguishable from us changing their training
                # behind their back, so it is surfaced until dismissed.
                reflow_notice=(active or {}).get("reflow_notice"),
                reflow_notice_plan_id=(active or {}).get("id"),
            ),
        )

    @app.post("/plan/{plan_id}/reflow-notice/dismiss")
    def plan_reflow_notice_dismiss(request: Request, plan_id: int):
        """Acknowledge the "your plan changed overnight" notice."""
        uid = _uid(request)
        if db.get_plan(uid, plan_id) is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        db.set_plan_reflow_notice(uid, plan_id, None)
        # Only ever bounce back to one of our own pages, never to an
        # attacker-supplied Referer.
        ref = _url.urlparse(request.headers.get("referer") or "")
        if ref.path == "/calendar":
            return RedirectResponse(url="/calendar", status_code=303)
        # Dismissing from /plan?plan_id=N must leave the rider on the plan
        # they were reading, not bounce them to the active one. Only the id
        # we just dismissed is echoed back, so the Referer cannot steer the
        # redirect anywhere the rider does not already have access to.
        if ref.path == "/plan":
            viewing = _url.parse_qs(ref.query).get("plan_id", [""])[0]
            if viewing.isdigit() and int(viewing) == plan_id:
                return RedirectResponse(
                    url=f"/plan?plan_id={plan_id}", status_code=303
                )
        return RedirectResponse(url="/plan", status_code=303)

    @app.post("/ratings/{kind}/{workout_id}")
    def save_rating(
        request: Request,
        kind: str,
        workout_id: int,
        rpe: int = Form(...),
        next_path: str = Form("/"),
    ):
        uid = _uid(request)
        if rpe < 1 or rpe > 10:
            return JSONResponse({"error": "rpe must be between 1 and 10"}, status_code=400)
        ok = importer.save_workout_rpe(uid, kind, workout_id, rpe)
        if not ok:
            return JSONResponse({"error": "workout not found or incomplete"}, status_code=404)
        target = next_path if next_path in ("/", "/calendar") else "/"
        return RedirectResponse(target, status_code=303)

    @app.post("/plan/export-all")
    def plan_export_all(request: Request):
        uid = _uid(request)
        result = exporter.sync_plan_exports(uid)
        return RedirectResponse(
            url=f"/calendar?exported={result['status']}", status_code=303
        )

    @app.post("/ooto/add")
    def ooto_add(
        request: Request,
        start_date: str = Form(...),
        end_date: str = Form(""),
        note: str = Form(""),
    ):
        uid = _uid(request)
        start = (start_date or "").strip()
        end = (end_date or "").strip() or start
        if start:
            try:
                _dt.date.fromisoformat(start)
                _dt.date.fromisoformat(end)
                db.add_ooto_range(uid, start, end, note.strip() or None)
                # Keep the Zwift folder in sync (drop newly-skipped .zwo).
                try:
                    exporter.sync_plan_exports(uid)
                except Exception:
                    _log.warning("export sync after OOTO add failed", exc_info=True)
            except ValueError:
                pass
        return RedirectResponse(url="/calendar", status_code=303)

    @app.post("/ooto/{ooto_id}/delete")
    def ooto_delete(request: Request, ooto_id: int):
        uid = _uid(request)
        db.delete_ooto_range(uid, ooto_id)
        try:
            exporter.sync_plan_exports(uid)  # re-export days that are back in
        except Exception:
            _log.warning("export sync after OOTO delete failed", exc_info=True)
        return RedirectResponse(url="/calendar", status_code=303)

    # ------------------------------------------------------------- races
    def _reflow_for_races(uid: int, lead: str = "Race saved.") -> str:
        """Reflow the active plan around the user's races. Returns flash text.

        Races are read fresh by reflow, so this is simply "recompute now".
        Failures never block the CRUD that triggered them: the race row is the
        user's data and is already saved; a plan that missed a reflow is fixed
        by the next one (reflow is idempotent and self-healing).
        """
        plan = db.get_active_plan(uid)
        if plan is None:
            return f"{lead} No active plan to reflow — mark one active."
        try:
            result = reflow.reflow_plan(uid, plan["id"])
        except Exception:
            _log.warning("reflow after race change failed", exc_info=True)
            return f"{lead} The plan could not be recomputed."
        if result.get("status") != "ok":
            return (f"{lead} “{plan['name']}” could not be recomputed "
                    f"({result.get('reason')}).")
        try:
            exporter.sync_plan_exports(uid)
        except Exception:
            _log.warning("export sync after race change failed", exc_info=True)
        changed = (result["updated"] + result["inserted"] + result["deleted"])
        msg = f"{lead} {changed} workout{'' if changed == 1 else 's'} changed."
        for c in result.get("race_conflicts") or []:
            # Same wording as the calendar aside and the overnight notice
            # rendered beside it: one rule, one number, one sentence.
            msg += (f" Note: the race on {c['date']} is within "
                    f"{planmod.A_RACE_SEPARATION_DAYS} days of your A race on "
                    f"{c['conflicts_with']}, so it is planned as a B race "
                    f"(only one taper is possible).")
        return msg

    def _race_form(date: str, priority: str, name: str, duration_min: str):
        """Validate the race form. Returns parsed fields, or None if unusable."""
        try:
            day = _dt.date.fromisoformat((date or "").strip()).isoformat()
        except (TypeError, ValueError):
            return None
        try:
            minutes = int(duration_min) if str(duration_min).strip() else None
        except (TypeError, ValueError):
            minutes = None
        if minutes is not None and minutes <= 0:
            minutes = None
        return day, (priority or "B"), (name or "").strip() or None, minutes

    @app.post("/race/add")
    def race_add(
        request: Request,
        date: str = Form(...),
        priority: str = Form("B"),
        name: str = Form(""),
        duration_min: str = Form(""),
    ):
        uid = _uid(request)
        parsed = _race_form(date, priority, name, duration_min)
        if parsed is None:
            return RedirectResponse(url="/calendar", status_code=303)
        day, prio, race_name, minutes = parsed
        db.add_race_date(uid, day, prio, race_name, minutes)
        flash = _reflow_for_races(uid)
        return RedirectResponse(
            url="/calendar?flash=" + _url.quote(flash), status_code=303
        )

    @app.post("/race/{race_id}/update")
    def race_update(
        request: Request,
        race_id: int,
        date: str = Form(...),
        priority: str = Form("B"),
        name: str = Form(""),
        duration_min: str = Form(""),
    ):
        uid = _uid(request)
        parsed = _race_form(date, priority, name, duration_min)
        if parsed is None:
            return RedirectResponse(url="/calendar", status_code=303)
        day, prio, race_name, minutes = parsed
        if not db.update_race_date(uid, race_id, day, prio, race_name, minutes):
            # Not this user's race (or already gone) -> no cross-user write.
            return JSONResponse({"error": "not found"}, status_code=404)
        flash = _reflow_for_races(uid)
        return RedirectResponse(
            url="/calendar?flash=" + _url.quote(flash), status_code=303
        )

    @app.post("/race/{race_id}/delete")
    def race_delete(request: Request, race_id: int):
        uid = _uid(request)
        if not db.delete_race_date(uid, race_id):
            return JSONResponse({"error": "not found"}, status_code=404)
        flash = _reflow_for_races(uid, "Race removed.")
        return RedirectResponse(
            url="/calendar?flash=" + _url.quote(flash), status_code=303
        )

    @app.post("/generate/export")
    def generate_export(request: Request, scheduled_date: str = Form("")):
        uid = _uid(request)
        last = app.state.last.get(uid)
        if not last:
            return RedirectResponse(url="/plan", status_code=303)
        try:
            scheduled = _dt.date.fromisoformat(scheduled_date).isoformat()
        except (TypeError, ValueError):
            return templates.TemplateResponse(
                request, "plan.html",
                _generate_ctx(request, mode="workout", error="Choose a valid scheduled date."),
                status_code=400,
            )
        settings = db.get_user_settings(uid)
        result = zwo.write_plan_to_zwift(
            [{"date": scheduled, "name": last["name"], "zwo": last["zwo"]}],
            settings.get("zwift_id") or "me",
            workouts_override=settings.get("workouts_dir"),
        )
        export_key = hashlib.sha256(
            f"{scheduled}\0{last['name']}\0{last['zwo']}".encode("utf-8")
        ).hexdigest()
        db.add_standalone_workout(
            uid, export_key, scheduled, last["name"], last["type"],
            last["duration_s"], last["tss"], last["zwo"], last["export_ftp"],
        )
        importer.match_plan_completions(uid)
        return templates.TemplateResponse(
            request,
            "plan.html",
            _generate_ctx(request, mode="workout", exported_path=result["paths"][0]),
        )

    @app.get("/generate/download")
    def generate_download(request: Request):
        last = app.state.last.get(_uid(request))
        if not last:
            return RedirectResponse(url="/plan", status_code=303)
        # Sanitize with the same helper the plan/zip paths use so the header
        # can't be injected/broken by an odd workout name. _safe_filename
        # already appends ".zwo".
        fname = zwo._safe_filename(last["name"] or "workout")
        return Response(
            content=last["zwo"],
            media_type="application/xml",
            headers={"Content-Disposition": f'attachment; filename="{fname}"'},
        )

    def _settings_ctx(request: Request, uid: int, saved: bool,
                      cred_message: Optional[str] = None,
                      backup_message: Optional[str] = None,
                      dir_message: Optional[str] = None,
                      calendar_message: Optional[str] = None,
                      calendar_feed_url: Optional[str] = None) -> dict:
        settings = db.get_user_settings(uid)
        return _ctx(
            request,
            settings=settings,
            # Whether a link exists is safe to render; the token itself is only
            # ever passed in as calendar_feed_url, by the route that just
            # minted it, and is never read back out of the database.
            calendar_token_set=calendarfeed.token_is_set(uid),
            calendar_message=calendar_message,
            calendar_feed_url=calendar_feed_url,
            # Drives the setup guide on the page: whether this deployment knows
            # the name a phone reaches it by. Unset means any link minted here
            # would carry a loopback host and be useless off this machine, so
            # the guide says so rather than letting the user find out after
            # subscribing. The hostname is configuration, not a secret.
            calendar_public_host=config.public_host(),
            calendar_public_scheme=config.public_scheme(),
            current_ftp=round(importer.current_ftp(uid), 1),
            recent_best_effort_ftp=round(importer.recent_best_effort_ftp(uid), 1),
            api_key_set=config.anthropic_api_key_set(),
            saved=saved,
            zwift_candidates=paths.candidate_zwift_ids(),
            watch_default=paths.activities_dir(),
            zwift_creds_saved=credstore.credentials_saved(uid),
            zwift_cred_backend=credstore.storage_backend(),
            cred_message=cred_message,
            backups=backup.list_backups(),
            backup_message=backup_message,
            dir_message=dir_message,
            restore_cmd=_restore_command(),
        )

    def _validate_dir(value: str) -> "tuple[Optional[str], Optional[str]]":
        """Confine a user-supplied folder path (must already exist).

        Thin wrapper over paths.confine_storage_dir so this route and
        /activities/rescan share one rule; see that function for the policy.
        """
        return paths.confine_storage_dir(value, must_exist=True)

    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request):
        return templates.TemplateResponse(
            request, "settings.html", _settings_ctx(request, _uid(request), False)
        )

    @app.post("/settings", response_class=HTMLResponse)
    def settings_save(
        request: Request,
        ftp: str = Form(""),
        zwift_id: str = Form(""),
        zwift_id_choice: str = Form(""),
        activities_dir: str = Form(""),
        workouts_dir: str = Form(""),
        anthropic_api_key: str = Form(""),
        zwift_email: str = Form(""),
        zwift_password: str = Form(""),
        weight_kg: str = Form(""),
        timezone: str = Form(""),
    ):
        uid = _uid(request)
        # A picked player folder (radio) wins over the free-text field.
        chosen_zwift_id = (zwift_id_choice or "").strip() or zwift_id
        weight_val: Optional[float] = None
        try:
            weight_val = float(weight_kg) if weight_kg.strip() else None
            if weight_val is not None and weight_val <= 0:
                weight_val = None
        except ValueError:
            weight_val = None
        # Confine user-supplied folders to existing directories under $HOME; a
        # rejected folder is dropped from the update (existing value kept).
        dir_msgs: List[str] = []
        clean_activities, act_err = _validate_dir(activities_dir)
        if act_err:
            dir_msgs.append(act_err)
            clean_activities = ""  # don't persist an invalid path
        clean_workouts, wk_err = _validate_dir(workouts_dir)
        if wk_err:
            dir_msgs.append(wk_err)
            clean_workouts = ""
        clean_timezone = (timezone or "").strip()
        if clean_timezone and not valid_timezone(clean_timezone):
            dir_msgs.append("Invalid IANA time zone.")
            clean_timezone = ""
        # The Zwift player id is a FOLDER NAME under the Zwift Workouts root
        # (paths.workouts_dir joins it, and the exporters makedirs() the
        # result), so it gets confined exactly like the folders above rather
        # than being passed through as free text. db.save_user_settings refuses
        # it too; this is the half that can tell the user why.
        if chosen_zwift_id and not paths.safe_zwift_id(chosen_zwift_id):
            dir_msgs.append(
                "Zwift ID must be a plain folder name (no slashes, no '..'): "
                f"{chosen_zwift_id}"
            )
            chosen_zwift_id = ""
        db.save_user_settings(
            uid,
            {
                "ftp": ftp,
                "zwift_id": chosen_zwift_id,
                "activities_dir": clean_activities,
                "workouts_dir": clean_workouts,
                "weight_kg": weight_val,
                "timezone": clean_timezone,
            },
        )
        # A manual FTP entry records a source='manual' row for today (per user).
        if ftp not in (None, ""):
            try:
                watts = float(ftp)
                if watts > 0:
                    db.add_ftp_entry(uid, utc_today().isoformat(), watts, "manual")
            except ValueError:
                pass
        try:
            profile_store.refresh(uid)
        except Exception:
            _log.warning("profile refresh after settings save failed", exc_info=True)
        # The Anthropic API key is app-level (shared).
        if anthropic_api_key:
            config.set_anthropic_api_key(anthropic_api_key)
        # Zwift account credentials: only saved when both fields are supplied;
        # the password is never redisplayed. Saving re-arms authenticated
        # race fetching after a previous login failure.
        cred_message = None
        if zwift_email.strip() and zwift_password:
            try:
                backend = credstore.save_zwift_credentials(
                    uid, zwift_email, zwift_password
                )
            except credstore.CredentialStorageError as exc:
                cred_message = (
                    "Zwift credentials NOT saved securely. Check Windows "
                    f"Credential Manager and try again ({exc})."
                )
            else:
                db.clear_race_auth_failure(uid)
                cred_message = f"Zwift credentials saved ({backend})."
        elif zwift_email.strip() or zwift_password:
            cred_message = ("Zwift credentials NOT saved - both email and "
                            "password are needed.")
        return templates.TemplateResponse(
            request, "settings.html",
            _settings_ctx(request, uid, True, cred_message=cred_message,
                          dir_message="; ".join(dir_msgs) or None),
        )

    @app.post("/settings/zwift-credentials/clear", response_class=HTMLResponse)
    def settings_clear_zwift_credentials(request: Request):
        uid = _uid(request)
        credstore.clear_zwift_credentials(uid)
        return templates.TemplateResponse(
            request, "settings.html",
            _settings_ctx(request, uid, False,
                          cred_message="Zwift credentials cleared."),
        )

    @app.post("/settings/calendar-feed", response_class=HTMLResponse)
    def settings_calendar_feed_token(request: Request):
        """Mint (or rotate) this user's calendar feed token.

        Session-authenticated and same-origin checked like the other state-
        changing settings actions. The plaintext token is rendered straight
        into this one response and then discarded - only its hash is stored -
        so a rotation is also the only moment the URL can be copied.
        """
        if not _same_origin_or_absent(request):
            return PlainTextResponse("Origin not allowed", status_code=403)
        uid = _uid(request)
        rotated = calendarfeed.token_is_set(uid)
        token = calendarfeed.generate_token(uid)
        if token is None:
            return templates.TemplateResponse(
                request, "settings.html",
                _settings_ctx(request, uid, False,
                              calendar_message="Could not create a calendar link."),
                status_code=500,
            )
        message = (
            "New calendar link created. The previous link has stopped working."
            if rotated else
            "Calendar link created. Copy it now - it is not shown again."
        )
        response = templates.TemplateResponse(
            request, "settings.html",
            _settings_ctx(
                request, uid, False,
                calendar_message=message,
                calendar_feed_url=calendarfeed.feed_url(
                    _feed_base_url(request), token
                ),
            ),
        )
        # This page body contains the plaintext token exactly once.
        response.headers["Cache-Control"] = "private, no-store"
        return response

    @app.post("/settings/backup", response_class=HTMLResponse)
    def settings_backup_now(request: Request):
        uid = _uid(request)
        try:
            path = backup.create_backup("manual")
            msg = f"Backup created: {os.path.basename(path)}"
        except Exception:  # never crash the settings page on a bad backup
            _log.warning("manual backup failed", exc_info=True)
            # Don't leak the raw error/local paths to the UI.
            msg = "Backup failed. See the server log for details."
        return templates.TemplateResponse(
            request, "settings.html",
            _settings_ctx(request, uid, False, backup_message=msg),
        )

    # ------------------------------------------------------------- races
    def _races_ctx(request: Request, uid: int, refreshed: Optional[dict] = None) -> dict:
        data = races.race_page_data(uid)
        settings = db.get_user_settings(uid)
        zid = (settings.get("zwift_id") or "").strip()
        return _ctx(
            request,
            results=data["results"],
            sync=data["sync"],
            bests=data["bests"],
            durations=data["durations"],
            duration_labels=data["duration_labels"],
            profile_durations=data["profile_durations"],
            profile_labels=data["profile_labels"],
            weight_kg=data["weight_kg"],
            rider_id=zid if zid.isdigit() else "",
            saved_zwift_id=zid,
            workouts_root=paths.zwift_workouts_root(),
            refreshed=refreshed,
        )

    @app.get("/races", response_class=HTMLResponse)
    def races_page(request: Request):
        return templates.TemplateResponse(
            request, "races.html", _races_ctx(request, _uid(request))
        )

    @app.post("/races/refresh", response_class=HTMLResponse)
    def races_refresh(request: Request, rider_id: str = Form("")):
        uid = _uid(request)
        rider_id = (rider_id or "").strip()
        # Persist a typed numeric rider ID as the user's zwift_id setting.
        if rider_id.isdigit():
            db.save_user_settings(uid, {"zwift_id": rider_id})
        try:
            refreshed = races.refresh_race_results(uid, rider_id or None)
        except Exception:  # never let a refresh kill the page
            _log.warning("race refresh failed", exc_info=True)
            # Generic message; the detail (and any local paths) stays in the log.
            refreshed = {"source": None, "count": 0,
                         "error": "Race refresh failed. See the server log."}
        return templates.TemplateResponse(
            request, "races.html", _races_ctx(request, uid, refreshed=refreshed)
        )

    # ------------------------------------------------------- ride (BLE)
    def _upcoming_plan_workouts(uid: int, limit: int = 40) -> List[dict]:
        timezone = db.get_user_settings(uid).get("timezone")
        today = local_today(timezone, utc_now()).isoformat()
        out: List[dict] = []
        for p in db.list_plans(uid):
            for w in db.plan_workouts_for_plan(uid, p["id"]):
                if w["date"] >= today:
                    out.append(w)
        out.sort(key=lambda w: w["date"])
        return out[:limit]

    def _validate_just_ride(wtype, minutes):
        """Validate an ad-hoc (Just Ride) type/duration pair.

        Returns (kind, whole minutes). Raises ValueError on an unknown kind, a
        non-numeric/non-finite duration, or a duration that is not one of the
        offered JUST_RIDE_DURATIONS (30-240 minutes in 15-minute steps).
        """
        kind = str(wtype or "").strip()
        if kind not in WORKOUT_TYPE_KEYS:
            raise ValueError(f"unknown workout type: {kind or '(missing)'}")
        if minutes is None or str(minutes).strip() == "":
            raise ValueError("duration is required")
        try:
            raw = float(minutes)
        except (TypeError, ValueError, OverflowError):
            raise ValueError(f"invalid duration: {minutes}")
        if not _math.isfinite(raw):
            raise ValueError(f"invalid duration: {minutes}")
        try:
            mins = int(round(raw))
        except (TypeError, ValueError, OverflowError):
            raise ValueError(f"invalid duration: {minutes}")
        if mins not in JUST_RIDE_DURATIONS:
            raise ValueError(
                f"duration must be between {JUST_RIDE_DURATIONS[0]} and "
                f"{JUST_RIDE_DURATIONS[-1]} minutes, in 15-minute steps"
            )
        return kind, mins

    def _ride_session(uid, workout_id=None, wtype=None, minutes=None):
        """Build the Session to ride, from a plan workout or an ad-hoc type/duration.

        An explicit ad-hoc type that is invalid (or cannot be built) raises
        ValueError - the caller reports it rather than silently riding something
        else. Only the no-type-at-all case falls back to the default ride.
        """
        # Ridden in ERG here, exported as a .zwo there - both must be the
        # same session, so the rebuild gets the rider's profile just as the
        # export did.
        profile = profile_store.for_user(uid)
        if workout_id:
            w = db.get_plan_workout(uid, int(workout_id))
            if w:
                return build_workout(w["type"], max(1, w["duration_s"] / 60),
                                     w.get("variant"), profile=profile), \
                    w["name"], w["id"]
            raise ValueError("workout not found")
        if wtype:
            kind, mins = _validate_just_ride(wtype, minutes)
            s = build_workout(kind, mins, profile=profile)
            return s, s.name, None
        s = build_workout("endurance", 45, profile=profile)
        return s, s.name, None

    def _ride_workout_payload(session, ftp: float, name: Optional[str] = None) -> dict:
        blocks, total_s = flatten_session(session)
        profile = []
        for start, end, kind, value in blocks:
            lo, hi = value if kind == "ramp" else (value, value)
            # A "free" block is a maximal effort with no prescribed power. The
            # watts are the ERG resistance the trainer will hold, NOT a target,
            # so the block is flagged and the UI shows no wattage for it -
            # otherwise the chart would contradict the segment row next to it,
            # which reads "Max effort - no target". Plotting the rider's
            # load-accounting figure here is what produced a 750 W point on
            # exactly that block.
            row = {
                "start": start,
                "end": end,
                "watts_start": int(round(lo * ftp)),
                "watts_end": int(round(hi * ftp)),
            }
            if kind == "free":
                row["free"] = True
            profile.append(row)
        return {
            "name": name or session.name,
            "duration_s": total_s,
            "ftp": round(ftp, 1),
            "profile": profile,
        }

    def _watts(fraction, ftp: float) -> Optional[int]:
        return present.watts(fraction, ftp)

    def _fmt_clock(seconds: int) -> str:
        return present.fmt_clock(seconds)

    @app.get("/ride/workout/preview")
    def ride_workout_preview(request: Request, type: str = "", minutes: str = ""):
        uid = _uid(request)
        try:
            kind, mins = _validate_just_ride(type, minutes)
            session = build_workout(kind, mins,
                                    profile=profile_store.for_user(uid))
        except (ValueError, OverflowError) as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        ftp = importer.current_ftp(uid)
        payload = _ride_workout_payload(session, ftp)
        info = workout_type_info(kind) or {}
        info["low_watts"] = _watts(info.get("low"), ftp)
        info["high_watts"] = _watts(info.get("high"), ftp)
        info["work_watts"] = _watts(info.get("work"), ftp)
        payload.update({
            "description": session.description,
            "workout_type": session.workout_type,
            "estimated_tss": session.estimated_tss,
            "type_info": info,
            "segments": present.segment_rows(session, ftp),
        })
        return JSONResponse(payload)

    @app.get("/ride", response_class=HTMLResponse)
    def ride_page(request: Request, workout_id: Optional[int] = None):
        uid = _uid(request)
        selected_workout = None
        if workout_id is not None:
            if workout_id <= 0 or workout_id > 2**63 - 1:
                return PlainTextResponse("Workout not found", status_code=404)
            selected_workout = db.get_plan_workout(uid, workout_id)
            if selected_workout is None:
                return PlainTextResponse("Workout not found", status_code=404)
        workouts = _upcoming_plan_workouts(uid)
        if selected_workout is not None and not any(
            w["id"] == selected_workout["id"] for w in workouts
        ):
            workouts.append(selected_workout)
        available, reason = bledevices.bluetooth_available()
        return templates.TemplateResponse(
            request,
            "ride.html",
            _ctx(
                request,
                ble_available=available,
                ble_reason=reason,
                workouts=workouts,
                selected_workout_id=workout_id,
                ride_types=WORKOUT_TYPE_INFO,
                ride_durations=JUST_RIDE_DURATIONS,
                ftp=round(importer.current_ftp(uid), 0),
                ble_cache_key=f"wattracker.ride.ble.v1.user-{uid}",
            ),
        )

    @app.get("/ride/status")
    def ride_status(request: Request):
        available, reason = bledevices.bluetooth_available()
        return JSONResponse({"available": available, "reason": reason})

    @app.post("/ride/scan")
    async def ride_scan(request: Request):
        available, reason = bledevices.bluetooth_available()
        if not available:
            return JSONResponse(
                {"available": False, "reason": reason, "devices": []}
            )
        try:
            found = await bledevices.scan()
            if not found:
                return JSONResponse(
                    {
                        "available": True,
                        "reason": (
                            "No Bluetooth devices found. Wake or spin the device, "
                            "make sure no other app owns it, then retry."
                        ),
                        "devices": [],
                    },
                    status_code=404,
                )
            return JSONResponse({"available": True, "devices": found})
        except Exception as e:  # no adapter, timeout, etc.
            return JSONResponse(
                {"available": False, "reason": str(e), "devices": []}
            )

    def _ws_origin_ok(websocket: WebSocket) -> bool:
        """Allow only same-origin (local) browsers; a cross-site page always
        sends an Origin that won't match. A missing Origin (native BLE/CLI
        clients that aren't browsers) is allowed."""
        origin = websocket.headers.get("origin")
        if not origin:
            return True
        try:
            host = _url.urlparse(origin).hostname
        except ValueError:
            return False
        return host in _ALLOWED_WS_ORIGIN_HOSTS

    def _selected_ble_addresses(params) -> Optional[dict]:
        """Parse an explicit, bounded sensor selection from WS query params."""
        if params.get("selected") != "1":
            return None

        power = params.getlist("power")
        hr = params.getlist("hr")
        trainer = params.getlist("trainer")
        if len(power) > _MAX_SELECTED_POWER_SOURCES:
            raise ValueError(
                f"Select at most {_MAX_SELECTED_POWER_SOURCES} power sensors."
            )
        if len(hr) > 1 or len(trainer) > 1:
            raise ValueError("Select at most one heart-rate monitor and one trainer.")

        selected = {"power": [], "hr": [], "trainer": []}
        for role, addresses in (("power", power), ("hr", hr), ("trainer", trainer)):
            for address in addresses:
                if not address or len(address) > _MAX_BLE_ADDRESS_LENGTH:
                    raise ValueError(f"Invalid {role} sensor address.")
                if address not in selected[role]:
                    selected[role].append(address)
        return selected

    async def _stop_ble_trainer(conn: Optional[dict]) -> None:
        """Best-effort stop for a prepared trainer without a RideController."""
        trainer = (conn or {}).get("trainer")
        if trainer is None:
            return
        try:
            async_set_target = getattr(trainer, "async_set_target_power", None)
            if callable(async_set_target):
                await async_set_target(0)
            else:
                trainer.set_target_power(0)
        except Exception:
            pass
        try:
            async_stop = getattr(trainer, "async_stop", None)
            if callable(async_stop):
                await async_stop()
            else:
                trainer.stop_erg()
        except Exception:
            pass

    def _connection_erg_state(conn: Optional[dict]) -> tuple:
        trainer = (conn or {}).get("trainer")
        available = bool(
            trainer is not None and getattr(trainer, "erg_available", True)
        )
        enabled = bool(
            available and getattr(trainer, "erg_enabled", True)
        )
        return available, enabled

    async def _set_connection_erg(
        conn: dict,
        enabled: bool,
        target_watts: int,
        force_rearm: bool = False,
    ) -> tuple:
        trainer = conn.get("trainer")
        available, current = _connection_erg_state(conn)
        if not available:
            return False, False, "No controllable FTMS trainer is connected."
        try:
            if enabled:
                async_set_target = getattr(
                    trainer, "async_set_target_power", None
                )
                async_enable = getattr(trainer, "async_enable_erg", None)
                if current and not force_rearm and callable(async_set_target):
                    await async_set_target(target_watts)
                elif current and not force_rearm:
                    trainer.set_target_power(target_watts)
                elif callable(async_enable):
                    await async_enable(target_watts)
                else:
                    trainer.start_erg()
                    trainer.set_target_power(target_watts)
            else:
                async_disable = getattr(trainer, "async_disable_erg", None)
                if callable(async_disable):
                    await async_disable()
                else:
                    trainer.set_target_power(0)
                    trainer.stop_erg()
        except Exception as exc:
            available, current = _connection_erg_state(conn)
            return available, False if enabled else current, str(exc)
        available, actual = _connection_erg_state(conn)
        # Generic trainers may not expose state; successful commands are the
        # authoritative result for those backwards-compatible implementations.
        if not hasattr(trainer, "erg_enabled"):
            actual = enabled
        return available, actual, None

    @app.websocket("/ride/ws")
    async def ride_ws(websocket: WebSocket):
        if not _ws_origin_ok(websocket):
            # Reject cross-origin handshakes before accepting (1008 = policy
            # violation).
            await websocket.close(code=1008)
            return
        await websocket.accept()
        uid = None
        try:
            uid = websocket.session.get("user_id")
        except Exception:
            uid = None
        if not uid:
            await websocket.send_json({"status": "error", "error": "not authenticated"})
            await websocket.close()
            return

        params = websocket.query_params
        sim = params.get("sim")
        try:
            selected = _selected_ble_addresses(params)
        except ValueError as e:
            await websocket.send_json({"status": "error", "error": str(e)})
            await websocket.close()
            return
        try:
            session, ride_name, selected_workout_id = _ride_session(
                uid,
                workout_id=params.get("workout_id"),
                wtype=params.get("type"),
                minutes=params.get("minutes"),
            )
        except (ValueError, OverflowError) as e:
            await websocket.send_json({"status": "error", "error": str(e)})
            await websocket.close()
            return
        ftp = importer.current_ftp(uid)
        workout_payload = _ride_workout_payload(session, ftp, ride_name)
        await websocket.send_json({"status": "workout", "workout": workout_payload})
        available, reason = bledevices.bluetooth_available()

        # Without a simulation request and without hardware, report the
        # unavailable state (page still works) and close cleanly.
        if not sim and not available:
            await websocket.send_json(
                {
                    "status": "unavailable",
                    "ble_available": available,
                    "reason": reason,
                    "message": "Bluetooth riding needs an adapter and `pip install "
                    ".[ble]`. Use Simulate to preview the live screen.",
                }
            )
            await websocket.close()
            return

        if not sim:
            # Real hardware: connect sensors, put the trainer in ERG (Request
            # Control + Start inside prepare), then poll in real time. A ride
            # without an FTMS trainer still works read-only (power/HR display).
            conn = None
            controller = None
            receive_task = None
            action_queue = None
            abnormal_cleanup = False
            try:
                try:
                    if selected is None:
                        conn = await bledevices.connect_sensors()
                    else:
                        conn = await bledevices.connect_sensors(selected=selected)
                except Exception as e:  # no adapter, scan failure, ...
                    await websocket.send_json({"status": "error", "error": str(e)})
                    return
                if not conn["power_source"] and not conn["trainer"]:
                    details = " ".join(conn.get("errors", []))
                    await websocket.send_json(
                        {
                            "status": "error",
                            "error": (
                                "No selected power meter or FTMS trainer could be set up."
                                if selected is not None
                                else "No power meter or FTMS trainer found. "
                                     "Scan first, or use Simulate."
                            ) + (f" {details}" if details else ""),
                        }
                    )
                    return
                erg_available, erg_enabled = _connection_erg_state(conn)
                await websocket.send_json(
                    {"status": "connected", "devices": conn["names"],
                     "erg": erg_available,
                     "erg_available": erg_available,
                     "erg_enabled": erg_enabled,
                     "warnings": conn.get("errors", []),
                     "prepared": False,
                     "workout": workout_payload}
                )

                async def _receive_actions() -> None:
                    try:
                        while True:
                            await action_queue.put(await websocket.receive_json())
                    except BaseException as exc:
                        await action_queue.put(exc)

                async def _handle_action(message) -> Optional[str]:
                    nonlocal controller, erg_failures
                    if isinstance(message, BaseException):
                        raise message
                    if not isinstance(message, dict):
                        await websocket.send_json(
                            {"status": "error", "error": "Invalid ride action."}
                        )
                        return None
                    action = message.get("action")
                    if action == "stop":
                        return "stop"
                    if action == "disconnect":
                        address = message.get("address")
                        if (
                            not isinstance(address, str)
                            or not address
                            or len(address) > _MAX_BLE_ADDRESS_LENGTH
                        ):
                            await websocket.send_json(
                                {
                                    "status": "error",
                                    "action": "disconnect",
                                    "error": "Invalid device address.",
                                }
                            )
                            return None
                        try:
                            await bledevices.disconnect_sensor(conn, address)
                            if controller is not None:
                                controller.update_sources(
                                    trainer=conn.get("trainer"),
                                    power_source=conn.get("power_source"),
                                    hr_source=conn.get("hr_source"),
                                )
                            ending_session = not (
                                conn.get("power_source") or conn.get("trainer")
                            )
                            available_now, enabled_now = _connection_erg_state(conn)
                            await websocket.send_json(
                                {
                                    "status": "device_disconnected",
                                    "address": address,
                                    "devices": conn.get("names", {}),
                                    "erg_available": available_now,
                                    "erg_enabled": enabled_now,
                                    "ending_session": ending_session,
                                    "message": (
                                        "Device disconnected. Releasing Bluetooth "
                                        "before another scan or connection."
                                        if ending_session
                                        else "Device disconnected."
                                    ),
                                }
                            )
                            if ending_session:
                                return "stop"
                        except Exception as exc:
                            await websocket.send_json(
                                {
                                    "status": "error",
                                    "action": "disconnect",
                                    "address": address,
                                    "error": str(exc),
                                }
                            )
                        return None
                    if action == "set_erg":
                        enabled = message.get("enabled")
                        if type(enabled) is not bool:
                            await websocket.send_json(
                                {
                                    "status": "erg",
                                    "available": _connection_erg_state(conn)[0],
                                    "enabled": _connection_erg_state(conn)[1],
                                    "error": "ERG enabled must be a boolean.",
                                }
                            )
                            return None
                        target = (
                            controller.target_watts(
                                min(controller.elapsed, controller.total_s)
                            )
                            if controller is not None
                            else int(workout_payload["profile"][0]["watts_start"])
                            if workout_payload.get("profile")
                            else 0
                        )
                        available_now, enabled_now, error = (
                            await _set_connection_erg(conn, enabled, target)
                        )
                        # An explicit toggle is the rider asking for a fresh
                        # start, so it clears the consecutive-failure count that
                        # may have switched ERG off in the first place.
                        erg_failures = 0
                        if controller is not None:
                            controller.erg_available = available_now
                            controller.set_erg_enabled(
                                enabled_now, command_trainer=False
                            )
                        await websocket.send_json(
                            {
                                "status": "erg",
                                "available": available_now,
                                "enabled": enabled_now,
                                "error": error,
                            }
                        )
                        return None
                    await websocket.send_json(
                        {"status": "error", "error": "Unknown ride action."}
                    )
                    return None

                if callable(getattr(websocket, "receive_json", None)):
                    action_queue = asyncio.Queue()
                    receive_task = asyncio.create_task(_receive_actions())

                initial_erg_available, initial_erg_enabled = (
                    _connection_erg_state(conn)
                )
                controller = RideController(
                    session,
                    ftp,
                    trainer=conn["trainer"],
                    power_source=conn["power_source"],
                    hr_source=conn["hr_source"],
                    user_id=uid,
                    workout_id=selected_workout_id,
                    autosave=True,
                    erg_enabled=initial_erg_enabled,
                    manage_trainer_commands=False,
                )
                controller.current_target = controller.target_watts(0)
                initial_erg_error = None
                erg_failures = 0
                if initial_erg_enabled:
                    (
                        initial_erg_available,
                        initial_erg_enabled,
                        initial_erg_error,
                    ) = await _set_connection_erg(
                        conn, True, controller.current_target
                    )
                    controller.erg_available = initial_erg_available
                    if initial_erg_error:
                        # Same reasoning as the per-tick block below: one failed
                        # arming command is not proof the trainer will not take
                        # targets, and clearing erg_enabled here would gate the
                        # ride loop off before it ever ran. Count it and let the
                        # loop retry with a re-arm.
                        erg_failures = 1
                    else:
                        controller.set_erg_enabled(
                            initial_erg_enabled, command_trainer=False
                        )
                if initial_erg_error:
                    await websocket.send_json(
                        {
                            "status": "erg",
                            "available": initial_erg_available,
                            "enabled": initial_erg_enabled,
                            "error": initial_erg_error,
                        }
                    )
                inactive_s = 0.0
                while controller.status != "finished":
                    tick_started = _ride_loop_time()
                    if action_queue is not None:
                        while not action_queue.empty():
                            outcome = await _handle_action(action_queue.get_nowait())
                            if outcome == "stop":
                                controller.stop()
                                break
                    if controller.status == "finished":
                        break
                    previous_status = controller.status
                    controller.poll(dt=1)
                    if (
                        controller.erg_enabled
                        and controller.status in
                        ("running", "cooldown", "finished")
                    ):
                        (
                            command_available,
                            command_enabled,
                            command_error,
                        ) = await _set_connection_erg(
                            conn,
                            True,
                            controller.current_target,
                            force_rearm=(
                                # A failed command may have left the trainer out
                                # of ERG (BleakTrainer clears its own flag before
                                # re-raising), so a bare target would not put it
                                # back. Retry with the full arming sequence.
                                erg_failures > 0
                                or (previous_status == "paused"
                                    and controller.status == "running")
                            ),
                        )
                        controller.erg_available = command_available
                        if not command_error:
                            erg_failures = 0
                            controller.set_erg_enabled(
                                command_enabled, command_trainer=False
                            )
                        else:
                            erg_failures += 1
                            # Do NOT mirror the failure into controller.erg_enabled
                            # while retrying. The per-tick ERG block is gated on
                            # that flag and nothing outside the block ever sets it
                            # back, so clearing it here would latch ERG off for the
                            # rest of the ride on a single transient fault.
                            if erg_failures >= ERG_COMMAND_FAILURE_LIMIT:
                                controller.set_erg_enabled(
                                    False, command_trainer=False
                                )
                                await websocket.send_json(
                                    {
                                        "status": "erg",
                                        "available": command_available,
                                        "enabled": False,
                                        "error": command_error,
                                        "message": (
                                            "ERG switched off after "
                                            f"{erg_failures} consecutive failed "
                                            "trainer commands. Re-enable it to "
                                            "try again."
                                        ),
                                    }
                                )
                    await websocket.send_json(controller.state())
                    if controller.current_power > 0:
                        inactive_s = 0.0
                    else:
                        inactive_s += 1.0
                    if inactive_s >= RIDE_INACTIVITY_TIMEOUT_S:
                        saved = controller.has_started
                        if saved:
                            controller.stop()
                        await websocket.send_json(
                            {
                                "status": "inactivity_timeout",
                                "message": (
                                    "Ride saved and Bluetooth disconnected after "
                                    "5 minutes without power."
                                    if saved
                                    else "Bluetooth disconnected after 5 minutes "
                                         "without power. No activity was saved."
                                ),
                                "saved": saved,
                            }
                        )
                        if not saved:
                            return
                        break
                    tick_elapsed = _ride_loop_time() - tick_started
                    await _ride_sleep(
                        max(0.0, RIDE_POLL_INTERVAL_S - tick_elapsed)
                    )
                await websocket.send_json(controller.state())
            except BaseException as exc:
                abnormal_cleanup = True
                # Client closed the socket or BLE failed mid-ride: stop cleanly
                # once a ride actually started. An idle controller must not
                # create a zero-duration activity.
                try:
                    if (
                        controller is not None
                        and controller.has_started
                        and controller.status != "finished"
                    ):
                        controller.stop()
                except BaseException:
                    pass
                if not isinstance(exc, Exception):
                    raise
            finally:
                if receive_task is not None:
                    receive_task.cancel()
                    try:
                        await receive_task
                    except BaseException:
                        pass
                try:
                    await _stop_ble_trainer(conn)
                except BaseException:
                    pass
                for client in (conn or {}).get("clients", []):
                    try:
                        await client.disconnect()
                    except BaseException:
                        pass
                try:
                    await websocket.close()
                except BaseException:
                    pass
            return

        # Simulated ride: pedal at a steady wattage, compressing time so the
        # whole session streams quickly. (Real hardware ticks at dt=1 in real
        # time from a connected power source.)
        trainer = bledevices.SimulatedTrainer()
        controller = RideController(
            session, ftp, trainer=trainer, user_id=uid,
            workout_id=selected_workout_id, autosave=True
        )
        step_dt = 30
        pedal = max(60, int(0.55 * ftp))
        max_frames = int(controller.total_s / step_dt) + 5
        frames = 0
        try:
            # The demo replays the prescription only; reaching the cooldown
            # state means the workout is done, so stop instead of spinning on.
            while (
                controller.status not in ("cooldown", "finished")
                and frames < max_frames
            ):
                controller.tick(power=pedal, dt=step_dt)
                await websocket.send_json(controller.state())
                frames += 1
                await asyncio.sleep(0.01)
            if controller.status != "finished":
                controller.stop()
            await websocket.send_json(controller.state())
        except Exception:
            pass
        finally:
            try:
                await websocket.close()
            except Exception:
                pass

    # --------------------------------------------------------- JSON API

    @app.post("/api/plan/workout/{workout_id}/reconcile")
    def api_plan_workout_reconcile(request: Request, workout_id: int):
        """Reconcile one non-future workout against existing activity data."""
        uid = _uid(request)
        workout = db.get_plan_workout(uid, workout_id)
        if not workout:
            return JSONResponse({"error": "workout not found"}, status_code=404)

        today = utc_today()
        verified = importer.plan_workout_completion_verified(uid, workout)
        if workout["date"] > today.isoformat():
            status = (
                "completed"
                if verified
                else "unverified_completion"
                if workout.get("completed_activity_id")
                else "future"
            )
            return JSONResponse({"id": workout_id, "status": status, "matched": False})
        if workout.get("completed_activity_id") is not None:
            return JSONResponse(
                {
                    "id": workout_id,
                    "status": "completed" if verified else "unverified_completion",
                    "matched": False,
                }
            )

        matched = importer.match_plan_workout_completion(
            uid, workout_id, _dt.date.fromisoformat(workout["date"])
        )
        return JSONResponse(
            {
                "id": workout_id,
                "status": "matched" if matched else "no_match",
                "matched": matched,
            }
        )

    @app.post("/api/plan/workout/{workout_id}/complete")
    def api_plan_workout_complete(request: Request, workout_id: int):
        """Manually link the best same-day, unused activity to a workout.

        Repeating the action for an already completed workout is an idempotent
        success and returns its existing activity link.
        """
        uid = _uid(request)
        result = importer.manually_complete_plan_workout(uid, workout_id)
        if result == "not_found":
            return JSONResponse({"error": "workout not found"}, status_code=404)
        if result == "future":
            return JSONResponse(
                {"error": "future workouts cannot be marked complete"},
                status_code=400,
            )
        if result == "no_activity":
            return JSONResponse(
                {"error": "no activity exists on this workout's scheduled date"},
                status_code=400,
            )
        if result == "activities_used":
            return JSONResponse(
                {"error": "all activities on this date are already linked to another workout"},
                status_code=409,
            )
        if result in ("invalid_date", "conflict"):
            return JSONResponse(
                {"error": "the workout could not be marked complete"},
                status_code=409,
            )
        workout = db.get_plan_workout(uid, workout_id)
        return JSONResponse(
            {
                "id": workout_id,
                "status": result,
                "activity_id": (
                    workout.get("completed_activity_id") if workout else None
                ),
            }
        )

    @app.get("/api/plan/workout/{workout_id}")
    def api_plan_workout_detail(request: Request, workout_id: int):
        """Structured segments + power profile for one plan workout (user-scoped).

        Segments are reconstructed deterministically via ``build_workout``, so
        no extra persistence is needed - but the rebuild must be given the SAME
        rider profile the stored .zwo was generated with, or this endpoint and
        the exported file describe different workouts (measured before it was
        threaded through: 1.16 stored vs 1.18 rebuilt on the same vo2max day).
        The profile is read fresh rather than stored, exactly as reflow reads
        it; the two therefore agree as long as neither has gone stale, and the
        nightly reflow is what closes any gap.
        """
        uid = _uid(request)
        w = db.get_plan_workout(uid, workout_id)
        if not w:
            return JSONResponse({"error": "workout not found"}, status_code=404)
        completion_verified = importer.plan_workout_completion_verified(uid, w)
        ftp = importer.current_ftp(uid)
        session = build_workout(w["type"], max(1, w["duration_s"] / 60),
                                w.get("variant"),
                                profile=profile_store.for_user(uid))

        # The SAME formatter the ride preview uses. These two endpoints
        # describing one session in two hand-written shapes is precisely how a
        # sprint came to read "Max effort - no target" in one place and
        # "0% - 0 W" in the other.
        segments = present.segment_rows(session, ftp)

        # Flattened timeline (intervals expanded, ramps kept) for the chart.
        blocks, total_s = flatten_session(session)
        profile = []
        for (s, e, kind, val) in blocks:
            lo, hi = val if kind == "ramp" else (val, val)
            # See _ride_workout_payload: a "free" block carries the ERG
            # resistance, not a target, and the UI must not read it as one.
            row = {
                "start": s,
                "end": e,
                "watts_start": int(round(lo * ftp)),
                "watts_end": int(round(hi * ftp)),
            }
            if kind == "free":
                row["free"] = True
            profile.append(row)

        return JSONResponse(
            {
                "id": w["id"],
                "plan_id": w["plan_id"],
                "date": w["date"],
                "name": w["name"],
                "type": w["type"],
                "duration_s": w["duration_s"],
                "tss": w["tss"],
                "adapted": w.get("adapted"),
                "rpe": w.get("rpe"),
                "too_hard": w.get("rpe") == 10,
                "ftp_feedback_applied": bool(w.get("feedback_applied")),
                "completed": w.get("completed_activity_id") is not None,
                "completion_verified": completion_verified,
                "rpe_eligible": completion_verified,
                "can_mark_complete": (
                    w.get("completed_activity_id") is None
                    and w["date"] <= utc_today().isoformat()
                ),
                "ftp": round(ftp, 1),
                "description": session.description,
                "total_duration": total_s,
                "segments": segments,
                "profile": profile,
            }
        )

    @app.post("/api/plan/workout/{workout_id}/rpe")
    def api_plan_workout_rpe(
        request: Request, workout_id: int, rpe: int = Body(..., embed=True)
    ):
        """Grade a completed plan workout's perceived exertion (1-10)."""
        uid = _uid(request)
        try:
            rpe_val = int(rpe)
        except (TypeError, ValueError):
            return JSONResponse({"error": "rpe must be an integer"}, status_code=400)
        if rpe_val < 1 or rpe_val > 10:
            return JSONResponse(
                {"error": "rpe must be between 1 and 10"}, status_code=400
            )
        w = db.get_plan_workout(uid, workout_id)
        if not w:
            return JSONResponse({"error": "workout not found"}, status_code=404)
        if not importer.plan_workout_completion_verified(uid, w):
            return JSONResponse(
                {"error": "only verified completed workouts can be graded"},
                status_code=400,
            )
        importer.save_workout_rpe(uid, "plan", workout_id, rpe_val)
        updated = db.get_plan_workout(uid, workout_id)
        return JSONResponse(
            {
                "id": workout_id,
                "rpe": rpe_val,
                "too_hard": rpe_val == 10,
                "ftp_feedback_applied": bool(
                    updated and updated.get("feedback_applied")
                ),
            }
        )

    @app.post("/api/standalone-workout/{workout_id}/rpe")
    def api_standalone_rpe(
        request: Request, workout_id: int, rpe: int = Body(..., embed=True)
    ):
        uid = _uid(request)
        try:
            rpe_val = int(rpe)
        except (TypeError, ValueError):
            return JSONResponse({"error": "rpe must be an integer"}, status_code=400)
        if rpe_val < 1 or rpe_val > 10:
            return JSONResponse({"error": "rpe must be between 1 and 10"}, status_code=400)
        if not importer.save_workout_rpe(uid, "standalone", workout_id, rpe_val):
            return JSONResponse({"error": "workout not found or incomplete"}, status_code=404)
        return JSONResponse({"id": workout_id, "rpe": rpe_val})

    @app.get("/api/state")
    def api_state(request: Request):
        return JSONResponse(pipeline.build_state(_uid(request)).to_dict())

    @app.get("/api/load")
    def api_load(request: Request, months: Optional[float] = None):
        return JSONResponse(pipeline.load_series(_uid(request), months=months))

    @app.get("/api/curve")
    def api_curve(request: Request):
        return JSONResponse(pipeline.curve_points(_uid(request)))

    @app.get("/api/activities")
    def api_activities(request: Request):
        return JSONResponse(db.list_activities(_uid(request)))

    @app.get("/api/ftp")
    def api_ftp(request: Request, months: Optional[float] = None):
        return JSONResponse(pipeline.ftp_recorded(_uid(request), months=months))

    @app.get("/api/ftp_series")
    def api_ftp_series(request: Request, months: Optional[float] = None):
        return JSONResponse(
            pipeline.ftp_rolling_series(_uid(request), months=months)
        )

    return app


app = create_app()
