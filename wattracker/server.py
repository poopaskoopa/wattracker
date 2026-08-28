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
import re as _rex
import sys
import threading
import time as _time
import urllib.parse as _url
import zipfile
from contextlib import asynccontextmanager
from typing import List, Optional

from fastapi import Body, FastAPI, File, Form, Request, UploadFile, WebSocket
from fastapi import WebSocketDisconnect
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
from starlette.requests import HTTPConnection

from . import (
    auth,
    backup,
    calendarfeed,
    config,
    connectorauth,
    connectorhub,
    connectorsession,
    credstore,
    db,
    exporter,
    paths,
    rpc,
    power_corrections,
    races,
    ramp_test as ramp_test_mod,
)
from .analysis import activity_cache, pipeline, power_profile, zones
from .backend import (
    BackendUnavailable,
    ExportManifest,
    discover,
    get_backend,
    is_offline,
)
from .backend import mode as backend_mode
from .backend import remote_ble
from .rpc import ConnectorUnavailable
from .ble import devices as bledevices
from .ble.runner import RideController, flatten_session
from .ingest import importer
from .metrics import durability as durabilitymod
from .metrics import profile_store
from .metrics.power import DEFAULT_FTP, is_plausible_ftp
from .ftp_input import (
    FTP_INPUT_MAX_WATTS,
    FTP_INPUT_MIN_WATTS,
    parse_ftp_input,
)
from .weight_input import (
    WEIGHT_INPUT_MAX_KG,
    WEIGHT_INPUT_MIN_KG,
    parse_weight_input,
)
from .prescribe import adapt as adaptmod
from .prescribe import duration as durationmod
from .prescribe import goals as goalsmod
from .prescribe import phases as phasesmod
from .prescribe import plan as planmod
from .prescribe import present
from .prescribe import reflow
from .prescribe import zwo
from .prescribe import llm
from .prescribe import ooto_adjust
from .prescribe.planner import (
    JUST_RIDE_DURATIONS,
    RAMP_TEST_NAME,
    WORKOUT_TYPE_INFO,
    WORKOUT_TYPE_KEYS,
    VARIANTS,
    build_workout,
    plan_workout,
    ramp_test_window,
    ramp_test_prescribed_window,
    workout_type_info,
    validate_variant,
)
from .timeutil import (
    local_today,
    parse_naive,
    to_user_timezone,
    utc_now,
    utc_today,
    valid_timezone,
)

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

# How long a ride waits for a connector that has gone quiet before giving up on
# it. Losing the connector is not losing the ride: the connector holds the
# trainer, keeps sampling and keeps writing every second to its own buffer, and
# a reconnect inside this window replays the missed seconds into the controller
# so the activity comes out whole. Past it, nobody is coming back in time to be
# worth holding a rider's workout open for.
CONNECTOR_OFFLINE_TIMEOUT_S = 300.0


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
# Every rescan thread currently running, so shutdown can wait them out (see
# wait_for_scans). Guarded by _scan_lock, like the status dict beside it.
_scan_threads: "set[threading.Thread]" = set()

# How long shutdown waits for in-flight rescans before giving up on them. A
# scan is bounded by the size of the activities folder, not by anything a user
# is sitting in front of, so this is generous; overrunning it is logged rather
# than passed over in silence.
SCAN_SHUTDOWN_TIMEOUT_S = 30.0


def live_scan_threads() -> "list[threading.Thread]":
    """The rescan threads still running. Empty once shutdown has joined them."""
    with _scan_lock:
        return [t for t in _scan_threads if t.is_alive()]


def wait_for_scans(timeout: float = SCAN_SHUTDOWN_TIMEOUT_S) -> bool:
    """Join every in-flight rescan. True if they all finished in time.

    A scan resolves the database and the data directory from the environment on
    every call it makes, so a thread that outlives the app it was started from
    does not stop working - it starts working on whatever configuration the
    process has moved on to. That is a real bug in two places. Under the test
    suite, where each test re-points WATTRACKER_DB and WATTRACKER_DATA_DIR at
    its own temp directory, a straggler writes into the NEXT test's sandbox:
    CI saw a pre-migration backup land in a test that never migrated anything,
    and a test's own tables dropped out from under it mid-run. In a real
    install it is the same contract one step milder - shutdown must not abandon
    a half-finished import.

    Called after connectorhub.reset() so a scan blocked on a connector call is
    already failing rather than sitting on its full timeout.
    """
    deadline = _time.monotonic() + max(0.0, timeout)
    while True:
        pending = live_scan_threads()
        if not pending:
            return True
        remaining = deadline - _time.monotonic()
        if remaining <= 0:
            _log.warning(
                "%d activity scan(s) still running after %.0fs; giving up on "
                "the wait", len(pending), timeout,
            )
            return False
        pending[0].join(min(remaining, 0.5))


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
            "not_offered": 0,
            "error": None,
            "finished_at": None,
            "ftp_estimate": None,
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
                    not_offered=result.get("not_offered", 0),
                    directory=d,
                    exists=bool(result.get("exists")),
                    ftp_estimate=(
                        round(importer.recent_best_effort_ftp(user_id), 1)
                        if result.get("imported", 0) else None
                    ),
                )
        except Exception as exc:  # surface, don't crash the daemon thread
            with _scan_lock:
                _scan_status[user_id]["error"] = str(exc)
            _log.warning("interactive rescan failed for user %s", user_id,
                         exc_info=True)
        finally:
            with _scan_lock:
                # Deregistered first: whatever happens to the status row, a
                # finished thread must not be left for shutdown to wait on.
                _scan_threads.discard(threading.current_thread())
                st = _scan_status[user_id]
                st["running"] = False
                st["finished_at"] = utc_now().isoformat(
                    timespec="seconds"
                )

    thread = threading.Thread(
        target=_run, daemon=True, name=f"wattracker-scan-{user_id}"
    )
    # Registered BEFORE start(), or a scan that finishes immediately could
    # remove itself from the set before it was ever put in it.
    with _scan_lock:
        _scan_threads.add(thread)
    thread.start()
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
            # Hand back any row whose OOTO adjustment has outlived its trip
            # BEFORE adaptation and reflow read the plan, so the recipe owns it
            # again in the same sweep rather than a day later.
            db.retire_elapsed_ooto_adjustments(uid)
        except Exception:
            _log.warning("OOTO adjustment retirement failed for user %s", uid,
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


def _ooto_proposal_for(
    user_id: int, plan: dict, ooto: dict, today: Optional[_dt.date] = None,
) -> dict:
    """Evaluate one OOTO range against the active plan without mutating it."""
    phase_by_date = goalsmod.phase_by_date(
        plan["start_date"], plan["weeks"],
        (plan.get("recipe") or {}).get("goal"),
    )
    return ooto_adjust.evaluate_ooto(
        plan,
        db.plan_workouts_for_plan(user_id, plan["id"], include_zwo=True),
        ooto["start_date"], ooto["end_date"],
        (today or utc_today()).isoformat(),
        phase_by_date=phase_by_date,
        race_dates=db.list_race_dates(user_id),
        window_days=14,
        # The rebalance option rebuilds sessions at a higher dose, so it needs
        # the same measured profile the generator and adaptation prescribe
        # against - otherwise a confirmed rebalance would write a
        # population-constant session into a profile-aware plan.
        profile=profile_store.for_user(user_id),
    )


def _plural(count: int, word: str) -> str:
    return f"{count} {word}{'' if count == 1 else 's'}"


def _ooto_option_summary(option: dict) -> str:
    """One honest sentence per option.

    ``len(actions)`` is a count of proposed EDITS, never of affected workouts:
    reschedule emits one edit per workout it can place and none for the ones it
    cannot, and rebalance emits several edits per canceled workout. Reporting
    the edit count as "N key workouts affected" told a rider "1 key workout
    affected" directly under a banner saying four workouts fall in the range.
    So affected and resolved are separate numbers here, and any key workout the
    option cannot help is NAMED rather than quietly dropped.
    """
    kind = str(option.get("kind") or "")
    affected = int(option.get("affected_keys") or 0)
    resolved = int(option.get("resolved_keys") or 0)
    unresolved = option.get("unresolved") or []
    parts = [f"{_plural(affected, 'key workout')} affected."]
    if kind == "reschedule":
        parts.append(f"{resolved} moved to a later day.")
        delta_min = int(option.get("volume_delta_s") or 0) // 60
        if resolved and delta_min:
            direction = "less" if delta_min < 0 else "more"
            parts.append(
                f"Planned volume changes by {abs(delta_min)} min {direction} "
                "than leaving the plan alone."
            )
        elif resolved:
            parts.append("Planned volume is unchanged.")
    elif kind == "rebalance":
        parts.append("Nothing moves and no session is lost.")
        boosted = len(option.get("actions") or [])
        if boosted:
            parts.append(
                f"{_plural(boosted, 'easy session')} step up one dose level at "
                "the same length, so weekly minutes are unchanged."
            )
    if unresolved:
        named = ", ".join(
            f"{item.get('type') or 'workout'} on {item.get('date')}"
            for item in unresolved
        )
        verb = "recovered" if kind == "rebalance" else "moved"
        parts.append(f"Not {verb}: {named}.")
    return " ".join(parts)


def _ooto_adjustment_view(adjustment: Optional[dict]) -> Optional[dict]:
    """Add stable, rider-facing labels to a stored proposal."""
    if not adjustment:
        return None
    out = dict(adjustment)
    proposal = dict(adjustment.get("proposal") or {})
    out["affected"] = proposal.get("affected") or []
    labels = {
        "skip": "Keep the workouts skipped",
        "reschedule": "Reschedule key workouts to later days",
        "rebalance": "Keep the calendar, raise the dose",
    }
    options = []
    for raw in proposal.get("options") or []:
        option = dict(raw)
        kind = str(option.get("kind") or "")
        option["actions"] = [dict(action) for action in (option.get("actions") or [])]
        option["label"] = labels.get(kind, kind.replace("_", " ").title())
        option["summary"] = _ooto_option_summary(option)
        options.append(option)
    proposal["options"] = options
    out["proposal"] = proposal
    return out

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
# Paths that authenticate themselves rather than by session cookie. The
# connector upload carries a bearer token and belongs to a process with no
# browser, so a redirect to /login would be the wrong answer to a bad
# credential - it checks its own and returns 401.
# "/api/connector/session" and "/connector/session" are the two halves of the
# tray window's login: the first is bearer-authenticated like the ride upload
# above it, and the second authenticates itself from a single-use ticket and
# then *creates* the session, so requiring one first would be circular.
_EXEMPT = (
    "/login", "/register", calendarfeed.FEED_PATH, "/api/connector/ride",
    "/api/connector/session", "/connector/session",
)
_EXEMPT_PREFIXES = ("/static", "/favicon", "/apple-touch-icon")

# One log line per this many rejected /calendar.ics tokens. Not a limit:
# nothing is ever refused because of it (see CalendarFeedFailureCounter).
CALENDAR_TOKEN_FAILURE_THRESHOLD = 10


class _ConnectorSocket:
    """Adapts a Starlette WebSocket to the two methods RpcPeer needs.

    RpcPeer is deliberately transport-agnostic - the connector end wraps a
    plain client socket with the same two methods - so neither end of the
    protocol has to import the other's web framework.
    """

    def __init__(self, websocket: WebSocket) -> None:
        self._ws = websocket

    async def send_text(self, text: str) -> None:
        await self._ws.send_text(text)

    async def receive_text(self) -> str:
        return await self._ws.receive_text()


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
# Onboarding accepts a batch of browser-selected files. Keep the whole request
# bounded before importing any member, even when a browser omits file sizes.
MAX_ONBOARDING_UPLOAD_BYTES = 50 * 1024 * 1024
MAX_ONBOARDING_UPLOAD_FILES = 200
# Body weight shares one input policy (wattracker.weight_input) the way FTP
# does (wattracker.ftp_input, issue #64); these exist only so the onboarding
# template and the wizard keep a stable pair of names to import.
ONBOARDING_WEIGHT_MIN_KG = WEIGHT_INPUT_MIN_KG
ONBOARDING_WEIGHT_MAX_KG = WEIGHT_INPUT_MAX_KG
# Said when the connector goes away mid-wizard. Matches the wording
# RemoteBackend.validate_dir uses for the same condition, so a rider who
# retries does not get two different accounts of one problem.
_CONNECTOR_OFFLINE_CHECK = (
    "Cannot check that folder: the connector is offline. Start the wattracker "
    "connector on the machine where Zwift is installed, then try again."
)
# The onboarding wizard's FTP field no longer has bounds of its own: every
# surface a rider types an FTP into shares one policy (wattracker.ftp_input,
# issue #64), so the wizard's old 1-1000 W window is gone rather than being one
# of four.

# WebSocket handshakes are only accepted from same-origin (local) browsers; a
# cross-site page's Origin will never match, blocking cross-site WS hijacking.
_ALLOWED_WS_ORIGIN_HOSTS = ("localhost", "127.0.0.1", "::1")

# BLE addresses are opaque identifiers (UUIDs on macOS, MAC-like strings on
# other platforms). Bound user-supplied selections without imposing a format.
_MAX_SELECTED_POWER_SOURCES = 8
_MAX_BLE_ADDRESS_LENGTH = 256
# Ceiling on the remembered picker names (all users together). A rider sees a
# handful of devices; this only has to outlive the trip from scan to ride.
_MAX_REMEMBERED_SENSOR_NAMES = 512

# Per-sensor failures collected by connect_sensors (local or connector-side)
# are written for the log: they name the role, the device and the raw BLE
# error ("Characteristic 00002a5b-... was not found!"). A rider cannot act on
# that, so the socket carries a translation while the log keeps the original.
_SENSOR_ERROR_RE = _rex.compile(
    r"^(?P<kind>Timed out connecting|Could not connect|Could not set up) "
    r"(?P<role>trainer|power|hr|cadence) sensor "
    r"(?P<name>.+?) \((?P<address>[^()]*)\)"
)
_SENSOR_ROLE_LABEL = {
    "trainer": "Trainer",
    "power": "Power meter",
    "hr": "Heart rate strap",
    "cadence": "Cadence sensor",
}
_SENSOR_ROLE_READING = {
    "power": "power",
    "hr": "heart rate",
    "cadence": "cadence",
}


def _friendly_sensor_name(name: str, address: str, names=None) -> str:
    """The device as the rider knows it, given what the error line carries.

    When a ride is started from an explicit selection, connect_sensors only
    ever knew the address, so the "name" in its error line *is* the address
    ("Could not set up cadence sensor 7B2A660B-... (7B2A660B-...)"). A rider
    cannot match that to anything they own, so it is looked up in the names the
    picker showed them. Falls back to the address when nothing knows better -
    an unmatched identifier still beats no device at all.
    """
    if name and name != address:
        return name
    for key in (address, address.lower(), address.upper()):
        known = (names or {}).get(key)
        if known and known != address:
            return str(known)
    return name or address


def _rider_facing_sensor_warning(message: str, names=None) -> str:
    """One connect_sensors error line, said the way a cyclist would say it.

    Anything that does not match the shapes devices.py emits is passed through
    untouched rather than mangled - a wrong translation is worse than a
    technical one. ``names`` maps address to the friendly name the rider was
    shown, for the (usual) case where the error line has only the address.
    """
    match = _SENSOR_ERROR_RE.match(message or "")
    if not match:
        return message
    role = match.group("role")
    label = _SENSOR_ROLE_LABEL[role]
    name = _friendly_sensor_name(
        match.group("name"), match.group("address"), names
    )
    kind = match.group("kind")
    if kind == "Timed out connecting":
        return (
            f"{label} {name} didn't answer in time \u2014 it may still be connected "
            f"to another app or a previous ride. Wait a few seconds and try again."
        )
    if kind == "Could not connect":
        return (
            f"{label} {name} couldn't be connected \u2014 check it's awake, in "
            f"range, and not connected to another app."
        )
    if role == "trainer":
        return (
            f"Trainer {name} couldn't be used \u2014 wattracker can't control its "
            f"resistance."
        )
    return (
        f"{label} {name} couldn't be used \u2014 it doesn't report "
        f"{_SENSOR_ROLE_READING[role]} in a way wattracker can read."
    )


def _sensor_names_by_address(conn, scanned=None) -> dict:
    """Every address -> friendly name this ride can account for.

    ``conn["bindings"]`` names the devices that did bind; on the connector
    (RemoteConnection) those are real names, so a KICKR whose cadence role
    failed while its power role bound is named from its own connection.
    ``scanned`` is the picker's view - the only source that can name a device
    that bound nothing at all.
    """
    names = dict(scanned or {})
    for address, binding in ((conn or {}).get("bindings") or {}).items():
        name = str((binding or {}).get("name") or "")
        if name and name != str(address):
            names[str(address)] = name
    return names


def _rider_facing_sensor_warnings(conn, scanned=None) -> List[str]:
    """The rider-facing form of ``conn["errors"]``, for either BLE backend.

    RemoteConnection (the Windows connector) fills the same ``errors`` key
    from the connector's own connect_sensors, so both paths translate here.
    """
    names = _sensor_names_by_address(conn, scanned)
    return [
        _rider_facing_sensor_warning(str(message), names)
        for message in (conn or {}).get("errors", []) or []
    ]


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


# How a session came to exist. Only connector_session_redeem sets it, so a
# session carrying it is one a device token opened rather than one somebody
# typed a password into - and the two are not entitled to the same things.
#
# The marker is not forgeable: SessionMiddleware base64s the session and signs
# it with config.session_secret() (256 bits) through itsdangerous, so the
# payload is readable by its holder but any edit invalidates the signature and
# an unsigned cookie is not a session at all. Readable is fine; there is
# nothing secret about the word "connector".
SESSION_VIA = "via"
VIA_CONNECTOR = "connector"
# The device whose token opened the session, so revoking that device can end
# the session too (see _connector_session_still_paired).
SESSION_DEVICE_ID = "device_id"


def _from_connector(conn: HTTPConnection) -> bool:
    """Whether this session was opened by a device token rather than a password.

    Typed on HTTPConnection, not Request: a WebSocket is one too, and the ride
    socket has to ask this question for itself because no middleware can ask it
    on the socket's behalf (see _connector_session_still_paired).
    """
    return conn.session.get(SESSION_VIA) == VIA_CONNECTOR


def _promote_to_password_session(request: Request) -> None:
    """Drop any connector provenance, because a password has just been proven.

    Called from /login and /register. The reasoning is airtight for /login: a
    rider who signs in inside the tray's own window is a rider who knows the
    password, and a device token can neither obtain nor change one (every
    writer of password_hash goes through db.set_password_hash, whose only route
    callers are these two). Without this the restriction would be permanent for
    that window, which is a UI that quietly stops working rather than a
    security control.

    It is NOT airtight for /register, and the difference is worth stating
    rather than leaving for the next reader to find. The password proven there
    is a brand-new account's, chosen by whoever is registering - so a connector
    session can register a throwaway user and shed the marker without ever
    knowing the rider's password. What that buys is bounded: pairing and the
    calendar feed are per-user, and the new account is a different uid. The
    app-global LLM settings are the exception - the key, and since the provider
    became settable the endpoint that decides who receives it - and they are
    the reason this is documented in docs/windows-security.md instead of being
    waved through.

    That shed is now bounded at its source rather than here. /register no
    longer creates an account once one exists unless
    WATTRACKER_ALLOW_REGISTRATION says so (config.allow_registration), so a
    connector session can only reach this line at all on a server that has
    deliberately opened registration or has no accounts yet. Clearing the
    marker here is still the right thing to do when it IS reached - the
    password just proven is real - and this function is deliberately not the
    place that judges whether the account should have existed.
    """
    request.session.pop(SESSION_VIA, None)
    request.session.pop(SESSION_DEVICE_ID, None)


def _connector_session_still_paired(conn: HTTPConnection) -> bool:
    """False only for a connector session whose device has been revoked.

    A password session is never charged the lookup, so this costs nothing on
    the ordinary path.

    Every caller has to invoke this itself, and there is no way to arrange
    otherwise. AuthMiddleware is a BaseHTTPMiddleware, whose __call__ hands any
    scope that is not "http" straight to the app - so a websocket route is
    never dispatched through it and cannot inherit this check. That is why the
    parameter is an HTTPConnection: the ride socket calls it directly. A new
    websocket route that authenticates on the session and forgets to is a
    revocation bypass, which is exactly the hole this function was written to
    close and exactly the hole it had for one commit.
    """
    if not _from_connector(conn):
        return True
    try:
        return connectorauth.device_exists(
            conn.session.get("user_id"), conn.session.get(SESSION_DEVICE_ID)
        )
    except Exception:
        # An authorization check that cannot answer has to say no. The cost of
        # being wrong here is one re-login; the cost of the other default is a
        # revoked device keeping its session through a transient database error.
        _log.warning(
            "could not confirm the connector device behind a session", exc_info=True
        )
        return False


async def _end_revoked_ride(websocket, controller) -> None:
    """Wind up a ride whose device was revoked while it was already running.

    The handshake check is necessary but not sufficient: it runs once, and a
    ride socket is long-lived, so a socket opened a second before Revoke was
    pressed went on streaming - and, in server mode, went on driving whichever
    connector is attached now, because _ble_session resolves by user_id alone.
    That is the same harm the handshake check closes, reached by opening the
    door first and revoking second. So the ride loops re-ask each tick and call
    this the moment the answer changes.

    Stopping the controller here rather than abandoning it is deliberate: the
    seconds already ridden are the RIDER'S data, streamed while the device was
    still trusted, and _finish() only writes an activity when the ride actually
    started - so this saves a real partial ride and never fabricates an empty
    one. What must not survive is the socket, not the workout.

    The refusal is an ordinary frame followed by the caller's ordinary close,
    never an exception: revocation is something the owner deliberately did from
    the Settings page, and a traceback in the log for it would train whoever
    reads that log to ignore it. Both sends are guarded because the peer may
    have gone away first.
    """
    try:
        if controller is not None:
            controller.stop()
    except Exception:
        _log.debug("could not stop a revoked ride cleanly", exc_info=True)
    try:
        await websocket.send_json(
            {
                "status": "error",
                "error": "not authenticated",
                "message": (
                    "This device was unpaired. The ride has been stopped."
                ),
            }
        )
    except Exception:
        _log.debug("could not tell a revoked ride socket why it closed",
                   exc_info=True)


class AuthMiddleware(BaseHTTPMiddleware):
    """Redirect unauthenticated requests to /login (except exempt paths)."""

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        exempt = path in _EXEMPT or any(path.startswith(p) for p in _EXEMPT_PREFIXES)
        if not exempt and not request.session.get("user_id"):
            return RedirectResponse("/login", status_code=303)
        if not exempt and not _connector_session_still_paired(request):
            # The device that opened this window is gone. Clearing the session
            # here is the only way revocation can reach it: the cookie is a
            # signed blob with no server-side record, so settings_connector_
            # revoke has nothing to delete. Without this, the Settings page
            # tells the owner the token no longer works while the thief's
            # window keeps reading their history - and keeps driving whichever
            # connector is attached NOW, because RemoteBackend resolves by
            # user_id alone, not by the device that asked.
            _log.info("a connector session ended because its device was revoked")
            request.session.clear()
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


def _checked(raw: str) -> bool:
    """Whether a checkbox came back ticked.

    Browsers send an unchecked box as nothing at all and a ticked one as "on",
    but a fetch() caller may send "1"/"true"; only an explicit falsey spelling
    is treated as unticked.
    """
    return (raw or "").strip().lower() not in ("", "0", "false", "off", "no")


def _ftp_field_value(watts) -> str:
    """A stored FTP as the text to prefill an FTP field with.

    Whole watts render without a decimal point (275, not 275.0); a fractional
    value renders as it is stored rather than being truncated for display - the
    app itself produces one-decimal FTPs, so the field has to be able to echo
    one back honestly.
    """
    if watts is None:
        return ""
    try:
        value = float(watts)
    except (TypeError, ValueError):
        return ""
    return str(int(value)) if value == int(value) else f"{value:g}"


def _weight_field_value(kg) -> str:
    """A stored weight as the text to prefill a weight field with (see
    _ftp_field_value: whole kilos render without a decimal point, fractions
    render as stored)."""
    if kg is None:
        return ""
    try:
        value = float(kg)
    except (TypeError, ValueError):
        return ""
    return str(int(value)) if value == int(value) else f"{value:g}"


def _setup_number(raw: str, minimum: float, maximum: float) -> Optional[float]:
    try:
        value = float((raw or "").strip())
    except (AttributeError, TypeError, ValueError):
        return None
    if not _math.isfinite(value) or value < minimum or value > maximum:
        return None
    return value


def _weight_log_date(
    raw: Optional[str], timezone: Optional[str]
) -> tuple[Optional[str], Optional[str]]:
    """A typed weigh-in date as ISO, or an error message.

    It must be a real calendar date and not after the rider's local today: a
    future weigh-in is a mistake, and a future-dated row would skew every
    "latest known weight on or before" resolution for present records.
    """
    text = (raw or "").strip()
    try:
        parsed = _dt.date.fromisoformat(text)
    except (TypeError, ValueError):
        return None, "Enter the weigh-in date as YYYY-MM-DD."
    if parsed > local_today(timezone):
        return None, "The weigh-in date cannot be in the future."
    return parsed.isoformat(), None


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
        # Fail every in-flight connector call before the loop goes away, so a
        # worker thread blocked on one is told at once instead of sitting on
        # its full timeout against a loop that will never run again.
        connectorhub.reset()
        # Rescans are started per request in their own threads, so stopping the
        # sweep task above does not account for them. They have to be waited
        # out here or they keep writing against configuration this app no
        # longer owns - see wait_for_scans.
        wait_for_scans()
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
    # Serializes the "is this the bootstrap account?" test with the INSERT that
    # answers it. Sync route handlers run in a threadpool, so without this two
    # simultaneous POSTs to an empty database could both read "no users yet"
    # and both be admitted as the first account. A plain Lock is the right
    # size for the same reason the scan-progress dict is: one process.
    app.state.registration_lock = threading.Lock()
    # Refused /login attempts: a single unkeyed count, not a throttle.
    app.state.login_failures = LoginAttemptCounter()
    # Rejected /calendar.ics tokens: a single unkeyed count, not a throttle.
    # The route resolves the token first and serves any valid one before this
    # is consulted at all, so a subscribed calendar app can never be refused
    # because of someone else's guessing - or its own earlier attempts with a
    # rotated token.
    app.state.calendar_failures = CalendarFeedFailureCounter()
    # Rejected connector tokens, counted the same way and for the same reasons
    # (see CalendarFeedFailureCounter): a 256-bit token checked one indexed
    # lookup at a time is not guessable, every client on a home LAN can share
    # one apparent address, and refusing would only lock out the connector
    # that is trying to reconnect. Visibility, not enforcement.
    app.state.connector_failures = CalendarFeedFailureCounter()
    # Single-use tickets a connector exchanges its device token for, so the
    # tray's window opens already logged in. In memory on purpose: one is worth
    # a session and lives for a minute (see connectorsession).
    app.state.connector_tickets = connectorsession.TicketStore()
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
        # same_site="lax" is the CSRF control and holds regardless: a
        # cross-site POST carries no cookie at all. Secure is separate - it
        # keeps the cookie off a plain-http hop - and defaults off because
        # this app speaks http. Turn it on the moment TLS is terminated in
        # front, or the cookie is readable by anyone on the same network.
        https_only=config.cookie_secure(),
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
    # session cookie is not Secure by default, and there is no rate limiting
    # beyond /login. Do not set this to an internet-facing name.
    #
    # WATTRACKER_PUBLIC_HOSTS takes several, because one server on a LAN is
    # legitimately reached as an IP, a short hostname and a .local name at the
    # same time. Each value goes through the identical validator, so the count
    # widens but what any single entry may be does not.
    for public_host in config.public_hosts():
        # The value may carry ":port"; strip it with the middleware's own
        # parser so the entry is exactly what _host_only() will produce for an
        # incoming Host header (Starlette compares the host portion only).
        host_only = IPv6TrustedHostMiddleware._host_only(public_host)
        if host_only and host_only not in allowed_hosts:
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
    def _registration_open() -> bool:
        """Whether /register may create an account right now.

        Two ways to be open, and only two. An empty database is open because
        every install bootstraps through this route and there is nothing to
        protect yet. After that it takes WATTRACKER_ALLOW_REGISTRATION, for
        the reasons written out in config.allow_registration - in short, an
        account on this app can repoint the APP-GLOBAL LLM endpoint at a host
        it controls (harvesting the rider's stored key and every prompt) and
        can launder a connector session past the /settings refusal.

        The env var is checked FIRST so the ordinary multi-account case costs
        no query at all, and so a rider who has opted in is never refused
        because the database is momentarily unhappy.

        A database that cannot answer refuses. This is an authorization
        decision, and the same reasoning as _connector_session_still_paired
        applies: the cost of being wrong this way is that a rider who wanted a
        second account retries; the cost of the other default is that a broken
        query silently reopens registration on a populated server.
        """
        if config.allow_registration():
            return True
        try:
            return not db.user_ids()
        except Exception:
            _log.warning(
                "could not determine whether any account exists; "
                "refusing registration", exc_info=True
            )
            return False

    def _registration_closed_response(request: Request):
        """The refusal: a real page that says how to turn registration on.

        Deliberately about POLICY, not about inventory. It never says how many
        accounts exist or names one - an unauthenticated caller learns only
        that this server does not accept new accounts, which is the minimum the
        refusal has to imply in order to be a refusal at all.

        A rendered page rather than a bare status: the rider who hits this is
        usually the owner adding a second account on their own machine, and
        telling them the variable to set is the difference between a working
        feature and a bug report.
        """
        return templates.TemplateResponse(
            request,
            "register.html",
            {"request": request, "error": None, "registration_closed": True},
            status_code=403,
        )

    @app.get("/register", response_class=HTMLResponse)
    def register_form(request: Request):
        if _uid(request):
            return RedirectResponse("/", status_code=303)
        if not _registration_open():
            return _registration_closed_response(request)
        return templates.TemplateResponse(
            request, "register.html",
            {"request": request, "error": None, "registration_closed": False},
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
        # Refused BEFORE the ~128 MiB scrypt, not after: a closed server must
        # not be a free way to burn the hash limiter, and refusing costs one
        # environment read plus (only when the var is unset) one indexed query.
        if not _registration_open():
            return _registration_closed_response(request)
        username = (username or "").strip()
        err = auth.validate_credentials(username, password)
        if not err:
            try:
                with app.state.hash_limiter.reserve():
                    password_hash = auth.hash_password(password)
            except auth.HashCapacityExceeded:
                return _hash_capacity_response(request, "register.html")
            # Re-asked under the lock, and this is the test that actually
            # decides. The check above sheds cheaply; hashing took a quarter of
            # a second during which another request may have become the first
            # account, and "the database was empty when we started" is not a
            # policy. Held across the INSERT only - never across the hash.
            with app.state.registration_lock:
                if not _registration_open():
                    return _registration_closed_response(request)
                user_id = db.create_user(username, password_hash)
            if user_id is None:
                err = "That username is already taken."
        if err:
            return templates.TemplateResponse(
                request, "register.html",
                {"request": request, "error": err,
                 "registration_closed": False},
            )
        _promote_to_password_session(request)
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
        _promote_to_password_session(request)
        request.session["user_id"] = user["id"]
        request.session["username"] = user["username"]
        return RedirectResponse("/", status_code=303)

    @app.post("/logout")
    def logout(request: Request):
        request.session.clear()
        return RedirectResponse("/login", status_code=303)

    def _ftp_values_match(left, right) -> bool:
        try:
            return _math.isclose(float(left), float(right), rel_tol=0.0, abs_tol=1e-9)
        except (TypeError, ValueError, OverflowError):
            return False

    def _setup_ftp_source(uid: int, resolved, settings=None, latest=None) -> str:
        """Name the basis that ``current_ftp`` actually resolved."""
        if settings is None:
            settings = db.get_user_settings(uid)
        if latest is None:
            latest = db.latest_ftp(uid)
        stated = settings.get("ftp")
        if stated is not None and _ftp_values_match(stated, resolved):
            return "manual"
        if latest and _ftp_values_match(latest.get("ftp_watts"), resolved):
            return latest.get("source") or "history"
        if not _ftp_values_match(resolved, DEFAULT_FTP):
            return "estimated"
        return "default"

    def _setup_ftp_display(uid: int, settings=None, estimate=None) -> tuple[str, str]:
        # Choosing the estimated option will persist a plausible recent
        # estimate, so show that value even before it has a history row. When
        # there is no such estimate, fall back to the value current_ftp uses.
        if estimate is not None and is_plausible_ftp(estimate):
            resolved = estimate
            source = "estimated"
        else:
            resolved = importer.current_ftp(uid)
            source = _setup_ftp_source(uid, resolved, settings=settings)
        display = f"{float(resolved):.1f}".rstrip("0").rstrip(".")
        return display, source

    # ------------------------------------------------------------- pages
    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request):
        uid = _uid(request)
        state = pipeline.build_state(uid)
        # Detection is actionable: adapt upcoming plan workouts (idempotent -
        # each workout is only ever adjusted once), then describe it.
        try:
            # Hand back any row whose OOTO adjustment has outlived its trip
            # BEFORE adaptation and reflow read the plan, so the recipe owns it
            # again in the same sweep rather than a day later.
            db.retire_elapsed_ooto_adjustments(uid)
        except Exception:
            _log.warning("OOTO adjustment retirement failed for user %s", uid,
                         exc_info=True)
        try:
            summary = adaptmod.apply_adaptations(uid, state)
        except Exception:
            _log.warning("plan adaptation failed", exc_info=True)
            summary = {"status": adaptmod.detection_status(state),
                       "adjusted": 0, "upcoming": {}}
        banner = adaptmod.banner_for(state, summary)
        complete = db.onboarding_complete(uid)
        # Candidate discovery walks a filesystem - the connector's, in a
        # split install - and the FTP estimate decompresses the whole activity
        # history; both only feed the setup wizard, which a completed rider
        # never renders. Skipping them keeps the normal dashboard off a cost
        # that grows with ride count, and off a round trip to another machine.
        setup_ctx: dict = {}
        if not complete:
            setup_settings = db.get_user_settings(uid)
            setup_estimate = importer.recent_best_effort_ftp(uid)
            setup_current_ftp, setup_ftp_source = _setup_ftp_display(
                uid, settings=setup_settings, estimate=round(setup_estimate, 1)
            )
            setup_ctx = dict(
                setup_candidates=discover(uid, "activity_candidates"),
                setup_connector_offline=is_offline(uid),
                setup_settings=setup_settings,
                setup_estimate=round(setup_estimate, 1),
                setup_fallback_ftp=round(DEFAULT_FTP),
                setup_current_ftp=setup_current_ftp,
                setup_ftp_source=setup_ftp_source,
                setup_error=None,
                setup_message=None,
                setup_form={},
                setup_ftp_confirm_required=False,
                setup_ftp_min=round(FTP_INPUT_MIN_WATTS),
                setup_ftp_max=round(FTP_INPUT_MAX_WATTS),
            )
        # Body weight card: the value effective on the rider's local today
        # (their own log, then a Zwift-derived row, then the settings scalar);
        # hidden when nothing is known at all.
        weight_settings = db.get_user_settings(uid)
        body_weight = db.weight_resolution(
            uid, local_today(weight_settings.get("timezone")).isoformat()
        )
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            _ctx(
                request,
                state=state.to_dict(),
                banner=banner,
                onboarding_complete=complete,
                body_weight=body_weight,
                **setup_ctx,
            ),
        )

    def _setup_context(
        request: Request, error: Optional[str] = None, message: Optional[str] = None,
        form: Optional[dict] = None, confirm_required: bool = False,
    ) -> dict:
        uid = _uid(request)
        settings = db.get_user_settings(uid)
        estimate = importer.recent_best_effort_ftp(uid)
        latest = db.latest_ftp(uid)
        current, source = _setup_ftp_display(
            uid, settings=settings, estimate=round(estimate, 1)
        )
        return _ctx(
            request,
            setup_candidates=discover(uid, "activity_candidates"),
            setup_connector_offline=is_offline(uid),
            setup_settings=settings,
            setup_estimate=round(estimate, 1) if estimate > 0 else None,
            setup_fallback_ftp=round(DEFAULT_FTP),
            setup_current_ftp=current,
            setup_ftp_source=source,
            setup_latest_ftp=latest,
            setup_error=error,
            setup_message=message,
            setup_form=form or {},
            setup_ftp_confirm_required=confirm_required,
            setup_ftp_min=round(FTP_INPUT_MIN_WATTS),
            setup_ftp_max=round(FTP_INPUT_MAX_WATTS),
        )

    def _setup_closed(request: Request) -> bool:
        """The wizard is a first-run surface; Settings owns later edits."""
        return db.onboarding_complete(_uid(request))

    def _setup_closed_json() -> JSONResponse:
        return JSONResponse(
            {"error": "Setup is already complete. Change this in Settings."},
            status_code=409,
        )

    @app.get("/setup", response_class=HTMLResponse)
    def setup_page(request: Request):
        if _setup_closed(request):
            return RedirectResponse("/settings", status_code=303)
        return templates.TemplateResponse(request, "setup.html", _setup_context(request))

    @app.post("/setup/check-directory")
    def setup_check_directory(request: Request, activities_dir: str = Form("")):
        if not _same_origin_or_absent(request):
            return JSONResponse({"error": "Cross-origin request rejected."}, status_code=403)
        if _setup_closed(request):
            return _setup_closed_json()
        uid = _uid(request)
        # Through the backend, not `paths`: in a split install this folder is
        # on the rider's machine, and the wizard is the FIRST thing a new
        # account sees. Checked here, every real Zwift path failed as "not
        # found or not a directory" - the container has no Zwift install and
        # never will - which made onboarding unfinishable in server mode.
        clean, error = _validate_dir(activities_dir, uid, scope="activities")
        if error:
            return JSONResponse({"error": error, "exists": False, "fit_count": 0}, status_code=400)
        if not clean:
            return JSONResponse({"error": "Choose or enter an Activities folder.",
                                 "exists": False, "fit_count": 0}, status_code=400)
        try:
            listing = get_backend(uid).list_activities(clean)
        except BackendUnavailable:
            # The connector answered validate_dir and dropped before this one.
            return JSONResponse(
                {"error": _CONNECTOR_OFFLINE_CHECK, "exists": False, "fit_count": 0},
                status_code=400,
            )
        # The offered files, so the count is what a scan will actually import:
        # the in-progress recording buffer is filtered by the machine that owns
        # the folder and must not be advertised here as a ride to be found.
        fit_count = len(listing.files)
        db.save_user_settings(uid, {"activities_dir": clean})
        started = _start_user_scan(uid, directory=clean)
        if started is None:
            status = _scan_status_snapshot(uid) or {"running": True}
        else:
            status = started
        return JSONResponse({
            "path": clean,
            "exists": bool(listing.exists),
            "fit_count": fit_count,
            "status": "files-found" if fit_count else "no-files",
            "scan": status,
        }, status_code=202)

    @app.post("/setup/upload")
    def setup_upload(request: Request, files: List[UploadFile] = File(...)):
        if not _same_origin_or_absent(request):
            return JSONResponse({"error": "Cross-origin request rejected."}, status_code=403)
        if _setup_closed(request):
            return _setup_closed_json()
        if not files or len(files) > MAX_ONBOARDING_UPLOAD_FILES:
            return JSONResponse({"error": "Select between one and 200 FIT files."}, status_code=400)
        staged = []
        total = 0
        for index, uploaded in enumerate(files):
            original = os.path.basename(uploaded.filename or "")
            if not original or not original.lower().endswith(".fit"):
                return JSONResponse({"error": "Only .fit files can be imported."}, status_code=400)
            declared = getattr(uploaded, "size", None)
            if declared is not None and declared > MAX_ONBOARDING_UPLOAD_BYTES:
                return JSONResponse({"error": "Selected files are too large."}, status_code=413)
            content = uploaded.file.read()
            total += len(content)
            if total > MAX_ONBOARDING_UPLOAD_BYTES:
                return JSONResponse({"error": "Selected files are too large."}, status_code=413)
            # The generated name is deliberately independent of browser paths.
            staged.append((f"onboarding-{index}.fit", content))
        imported = 0
        skipped = 0
        failed = 0
        imported_activity_ids = []
        uid = _uid(request)
        batch_ftp = importer.current_ftp(uid)
        for safe_name, content in staged:
            try:
                activity_id = importer.ingest_upload(
                    uid, safe_name, content, ftp=batch_ftp, refresh=False
                )
            except Exception:
                failed += 1
                continue
            if activity_id is None:
                skipped += 1
            else:
                imported += 1
                imported_activity_ids.append(activity_id)
        if imported > 0:
            importer.evaluate_ftp(uid)
            # The batch was scored against the FTP that existed *before* it
            # landed - on a first run that is the 200 W default. evaluate_ftp
            # has just computed the real one, so rescore against the FTP in
            # effect on each activity's own date. scan_activities does the
            # same; leaving it out here is what made the wizard's two import
            # routes disagree (100.0 vs 172.9 TSS for the same rides).
            importer.rescore_imported_activities(
                uid, imported_activity_ids, path=config.db_path()
            )
            importer.match_plan_completions(uid)
            importer.profile_store.refresh(uid)
            from .metrics import curve_store
            curve_store.ensure(uid)
        estimate = importer.recent_best_effort_ftp(uid)
        return JSONResponse({
            "selected": len(staged),
            "fit_count": len(staged),
            "imported": imported,
            "skipped": skipped,
            "failed": failed,
            "ftp_estimate": round(estimate, 1) if estimate > 0 else None,
        })

    @app.post("/setup/ftp")
    def setup_ftp(
        request: Request,
        choice: str = Form(""),
        manual_ftp: str = Form(""),
        confirm_low_ftp: str = Form(""),
    ):
        if not _same_origin_or_absent(request):
            return JSONResponse({"error": "Cross-origin request rejected."}, status_code=403)
        if _setup_closed(request):
            return _setup_closed_json()
        uid = _uid(request)
        choice = (choice or "").strip().lower()
        source = choice
        if choice == "manual":
            parsed = parse_ftp_input(manual_ftp, confirmed=_checked(confirm_low_ftp))
            if parsed.watts is None:
                return JSONResponse(
                    {"error": parsed.error, "confirm_required": parsed.needs_confirmation},
                    status_code=400,
                )
            watts = parsed.watts
            db.save_user_settings(uid, {"ftp": watts})
            db.add_ftp_entry(uid, utc_today().isoformat(), watts, "manual")
        elif choice == "estimated":
            watts = importer.recent_best_effort_ftp(uid)
            db.set_user_ftp_override(uid, None)
            if is_plausible_ftp(watts):
                db.add_ftp_entry(
                    uid,
                    utc_today().isoformat(),
                    round(watts, 1),
                    "estimated",
                    replace_existing=True,
                )
            else:
                # There is no analysis to record: either no rides at all, or an
                # estimate that failed the plausibility floor (#60). Nothing is
                # written - a number nobody measured must not be readable later
                # as one (#55) - and nothing is deleted either. An earlier row
                # for today is the rider's own doing: a value they typed, or a
                # real estimate from a ride they logged. Removing it to make
                # room for a placeholder would destroy data the wizard did not
                # create. So report what current_ftp will actually resolve,
                # which is that row if it exists and DEFAULT_FTP if it does not.
                watts = importer.current_ftp(uid)
                latest = db.latest_ftp(uid)
                source = (
                    latest["source"]
                    if latest and _ftp_values_match(latest.get("ftp_watts"), watts)
                    else "default"
                )
        else:
            return JSONResponse({"error": "Choose an estimated or manual FTP."}, status_code=400)
        return JSONResponse({"choice": choice, "ftp": round(watts, 1), "source": source})

    @app.post("/setup/complete", response_class=HTMLResponse)
    def setup_complete(
        request: Request,
        weight_kg: str = Form(""),
        ftp_choice: str = Form(""),
        manual_ftp: str = Form(""),
        zwiftpower: str = Form("no"),
        zwift_id: str = Form(""),
        zwift_email: str = Form(""),
        zwift_password: str = Form(""),
        activities_dir: str = Form(""),
        confirm_low_ftp: str = Form(""),
    ):
        if not _same_origin_or_absent(request):
            return PlainTextResponse("Cross-origin request rejected.", status_code=403)
        if _setup_closed(request):
            # HTML route: send the rider somewhere useful, not to a dead page.
            return RedirectResponse("/settings", status_code=303)
        uid = _uid(request)
        weight = _setup_number(weight_kg, ONBOARDING_WEIGHT_MIN_KG, ONBOARDING_WEIGHT_MAX_KG)
        form = {"weight_kg": weight_kg, "ftp_choice": ftp_choice,
                "manual_ftp": manual_ftp, "zwiftpower": zwiftpower,
                "zwift_id": zwift_id, "zwift_email": zwift_email,
                "activities_dir": activities_dir}
        if weight is None:
            return templates.TemplateResponse(
                request, "setup.html", _setup_context(
                    request, error="Enter a body weight from 20 to 300 kg.", form=form
                ), status_code=400
            )
        choice = (ftp_choice or "").strip().lower()
        analyzed = True
        if choice == "manual":
            parsed = parse_ftp_input(manual_ftp, confirmed=_checked(confirm_low_ftp))
            if parsed.watts is None:
                return templates.TemplateResponse(
                    request, "setup.html", _setup_context(
                        request, error=parsed.error, form=form,
                        confirm_required=parsed.needs_confirmation,
                    ), status_code=400
                )
            ftp = parsed.watts
        elif choice == "estimated":
            ftp = importer.recent_best_effort_ftp(uid)
            if not is_plausible_ftp(ftp):
                # See /setup/ftp above: with no analysis to record, nothing is
                # written to ftp_history and the placeholder is resolved at read
                # time instead (#55). A sub-floor estimate is a failed one, not
                # a weak one, and must not become a scoring basis (#60).
                ftp = DEFAULT_FTP
                analyzed = False
        else:
            return templates.TemplateResponse(
                request, "setup.html", _setup_context(
                    request, error="Choose the FIT-derived estimate or enter a manual FTP.", form=form
                ), status_code=400
            )
        clean_dir = None
        if activities_dir.strip():
            clean_dir, dir_error = _validate_dir(
                activities_dir, uid, scope="activities"
            )
            if dir_error:
                return templates.TemplateResponse(
                    request, "setup.html", _setup_context(request, error=dir_error, form=form), status_code=400
                )
        zwiftpower_choice = (zwiftpower or "").strip().lower()
        if zwiftpower_choice not in ("yes", "no"):
            return templates.TemplateResponse(
                request, "setup.html", _setup_context(
                    request, error="Choose whether you have a ZwiftPower profile.", form=form
                ), status_code=400
            )
        if zwiftpower_choice == "yes":
            rider_id = (zwift_id or "").strip()
            if not rider_id.isdigit() or len(rider_id) > 20 or not zwift_email.strip() or not zwift_password:
                return templates.TemplateResponse(
                    request, "setup.html", _setup_context(
                        request, error="Enter the numeric ZwiftPower rider ID, email, and password.", form=form
                    ), status_code=400
                )
            try:
                backend = credstore.save_zwift_credentials(uid, zwift_email, zwift_password)
            except Exception:
                # Do not expose backend details, exception text, or password.
                _log.warning("onboarding credential save failed for user %s", uid)
                return templates.TemplateResponse(
                    request, "setup.html", _setup_context(
                        request, error="ZwiftPower details could not be saved securely. Try again.", form=form
                    ), status_code=400
                )
            db.clear_race_auth_failure(uid)
            cred_saved = True
        else:
            backend = None
            cred_saved = False
            rider_id = ""
        updates: dict = {}
        if clean_dir:
            updates["activities_dir"] = clean_dir
        if choice == "manual":
            updates["ftp"] = ftp
        else:
            # A rider may resume setup after trying a manual value. Choosing
            # the analyzed estimate must remove that override so the estimate
            # really drives the dashboard and workout targets.
            db.set_user_ftp_override(uid, None)
        if cred_saved:
            updates["zwift_id"] = rider_id
        db.save_user_settings(uid, updates)
        # The onboarding weight is a measurement like any other: a manual log
        # for the rider's local today, not the legacy scalar (record_weight
        # re-syncs the scalar for the readers that still use it).
        tz = db.get_user_settings(uid).get("timezone")
        db.record_weight(uid, local_today(tz).isoformat(), weight, "manual")
        if analyzed:
            db.add_ftp_entry(
                uid,
                utc_today().isoformat(),
                round(ftp, 1),
                choice,
                replace_existing=choice == "estimated",
            )
        # No `else`: with no analysis there is nothing to record, and any
        # existing row for today belongs to the rider, not to the wizard.
        db.complete_onboarding(uid)
        return templates.TemplateResponse(
            request, "setup.html", _setup_context(
                request,
                message=("Setup complete. " + (f"Credentials saved in the {backend}." if backend else "")),
                form={"completed": True},
            )
        )

    def _activities_context(request: Request, scan: Optional[dict] = None) -> dict:
        uid = _uid(request)
        settings = db.get_user_settings(uid)
        candidates = discover(uid, "activity_candidates")
        saved = settings.get("activities_dir")
        prefill = saved or (candidates[0]["path"] if candidates else "")
        activities = db.list_activities(uid)
        merged = db.primaries_with_duplicates(uid)
        for a in activities:
            # A verified completed workout owns the effort rating; an unlinked
            # activity keeps its own subjective rating. Match the detail API's
            # verified-link semantics so the list never presents stale activity
            # feedback for a workout-backed ride.
            link = db.linked_workout_for_activity(uid, a["id"])
            a["linked_workout"] = link
            if link:
                if link["kind"] == "plan":
                    workout = db.get_plan_workout(uid, link["id"])
                    verified = bool(
                        workout
                        and importer.plan_workout_completion_verified(uid, workout)
                    )
                else:
                    verified = True  # standalone links are verified once completed
                if verified:
                    a["rpe"] = link["rpe"]
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

    @app.post("/activity/{activity_id}/drop", response_class=HTMLResponse)
    @app.post("/activities/{activity_id}/drop", response_class=HTMLResponse)
    def drop_activity(request: Request, activity_id: int):
        result = db.delete_activity(_uid(request), activity_id)
        if result == "not_found":
            return RedirectResponse("/activities?drop=not_found", status_code=303)
        if result == "linked":
            return RedirectResponse("/activities?drop=linked", status_code=303)
        return RedirectResponse("/activities?drop=deleted", status_code=303)

    @app.post("/api/activity/{activity_id}/drop")
    @app.delete("/api/activity/{activity_id}")
    def api_drop_activity(request: Request, activity_id: int):
        result = db.delete_activity(_uid(request), activity_id)
        if result == "not_found":
            return JSONResponse({"error": "activity not found"}, status_code=404)
        if result == "linked":
            return JSONResponse(
                {"error": "linked activities must be uncompleted first"},
                status_code=409,
            )
        return JSONResponse({"id": activity_id, "status": "deleted"})

    def _profile_response(request: Request, error: Optional[str] = None,
                          confirm_required: bool = False):
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
                manual_ftp_value=_ftp_field_value(settings.get("ftp")),
                manual_hr_max=settings.get("hr_max"),
                ftp_min=round(FTP_INPUT_MIN_WATTS),
                ftp_max=round(FTP_INPUT_MAX_WATTS),
                # Body weight: the full dated history for the log table and
                # chart, and the value effective on the rider's local today
                # for the "current" card and the log form's default.
                weight_entries=db.weight_history_list(uid),
                weight_now=db.weight_resolution(
                    uid, local_today(settings.get("timezone")).isoformat()
                ),
                # The log form's date default: the rider's local today.
                weight_today=local_today(settings.get("timezone")).isoformat(),
                weight_min=round(WEIGHT_INPUT_MIN_KG),
                weight_max=round(WEIGHT_INPUT_MAX_KG),
                ftp_confirm_required=confirm_required,
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
        confirm_low_ftp: str = Form(""),
    ):
        uid = _uid(request)
        if action == "reset":
            db.set_user_ftp_override(uid, None)
            try:
                profile_store.refresh(uid)
            except Exception:
                _log.warning("profile refresh after FTP reset failed", exc_info=True)
            return RedirectResponse("/profile?saved=ftp", status_code=303)
        parsed = parse_ftp_input(ftp, confirmed=_checked(confirm_low_ftp))
        if parsed.watts is None:
            return _profile_response(
                request, parsed.error, confirm_required=parsed.needs_confirmation
            )
        db.set_user_ftp_override(uid, parsed.watts)
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
        does not immediately come back. A suggestion the input policy refuses
        is recorded as rejected rather than falsely accepted.
        """
        uid = _uid(request)
        target = next_path if next_path in ("/settings", "/profile") else "/settings"
        row = db.pending_ftp_suggestion_by_id(uid, suggestion_id)
        if row is None:
            return RedirectResponse(target, status_code=303)
        outcome = "dismissed"
        if action == "use":
            # Accepting writes a training FTP, so it goes through the same
            # policy a typed one does. It used to clamp with
            # max(1, min(2000, ...)), which would silently store a value the
            # scoring layer then refuses - the row reads back as the rider's
            # setting while nothing it touches can be scored. A suggestion the
            # policy rejects is a bug in the suggester, not something to round
            # into range, so refuse it and say so. Clicking Use is the explicit
            # rider confirmation for the policy's low-but-usable band.
            parsed = parse_ftp_input(row["suggested_ftp"], confirmed=True)
            if parsed.watts is None:
                if db.resolve_ftp_suggestion(uid, suggestion_id, "rejected") is None:
                    return RedirectResponse(target, status_code=303)
                _log.warning(
                    "refusing implausible FTP suggestion %s for user %s: %s",
                    row["suggested_ftp"], uid, parsed.error,
                )
                outcome = "rejected"
            else:
                if db.resolve_ftp_suggestion(uid, suggestion_id, "accepted") is None:
                    return RedirectResponse(target, status_code=303)
                db.set_user_ftp_override(uid, parsed.watts)
                outcome = "used"
                try:
                    profile_store.refresh(uid)
                except Exception:
                    _log.warning(
                        "profile refresh after FTP suggestion failed", exc_info=True
                    )
        elif db.resolve_ftp_suggestion(uid, suggestion_id, "dismissed") is None:
            return RedirectResponse(target, status_code=303)
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

    @app.post("/weight", response_class=HTMLResponse)
    def weight_log(
        request: Request,
        date: str = Form(""),
        weight_kg: str = Form(""),
    ):
        """Log a manual weigh-in for a date (default: the rider's local today)."""
        uid = _uid(request)
        tz = db.get_user_settings(uid).get("timezone")
        parsed = parse_weight_input(weight_kg)
        if (date or "").strip() == "":
            date_iso = local_today(tz).isoformat()
        else:
            date_iso, date_error = _weight_log_date(date, tz)
            if date_error:
                return _profile_response(request, date_error)
        if parsed.kg is None:
            return _profile_response(request, parsed.error or "Weight not saved.")
        db.record_weight(uid, date_iso, parsed.kg, "manual")
        return RedirectResponse("/profile?saved=weight", status_code=303)

    @app.post("/weight/delete", response_class=HTMLResponse)
    def weight_delete(request: Request, date: str = Form("")):
        """Remove one dated entry. Deleting a Zwift-derived row is fine: the
        next refresh re-derives it from the ride record."""
        uid = _uid(request)
        text = (date or "").strip()
        if not db.delete_weight_entry(uid, text):
            return _profile_response(
                request, "No weight is logged for that date."
            )
        return RedirectResponse("/profile?saved=weight", status_code=303)

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
            "avg_power", "avg_hr", "np", "if_", "tss", "rpe", "have", "points",
            "weight_kg", "weight_source", "weight_date")}
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

    # Rides recorded by a connector while the link was down. Bounded so a
    # buggy or hostile client cannot make the server hold an arbitrary amount:
    # a day of 1 Hz samples is 86400, and no ride is a day long.
    MAX_BUFFERED_RIDE_SAMPLES = 86400

    @app.post("/api/connector/ride")
    async def connector_ride_upload(request: Request):
        """Accept a ride a connector buffered while it could not reach us.

        Bearer-token authenticated like /connector/ws, not session
        authenticated: this is the connector talking, and it has no cookie. It
        goes through importer.save_ride_record, the same chain an in-process
        ride uses, so a ride that happened to span a reconnect lands as exactly
        the same row as one that did not - including the duplicate-linking that
        pairs it with Zwift's own .fit for the same ride.
        """
        header = request.headers.get("authorization") or ""
        scheme, _, token = header.partition(" ")
        device = None
        if scheme.lower() == "bearer" and token:
            device = connectorauth.device_for_token(token.strip())
        if device is None:
            app.state.connector_failures.record_failure()
            return JSONResponse({"error": "unauthorized"}, status_code=401)

        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "body must be JSON"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"error": "body must be an object"}, status_code=400)

        samples = body.get("samples") or {}
        if not isinstance(samples, dict):
            return JSONResponse({"error": "samples must be an object"},
                                status_code=400)
        power = samples.get("power") or []
        if not isinstance(power, list) or not power:
            return JSONResponse({"error": "samples.power is required"},
                                status_code=400)
        if len(power) > MAX_BUFFERED_RIDE_SAMPLES:
            return JSONResponse({"error": "ride is too long"}, status_code=413)

        try:
            started_at = _dt.datetime.fromisoformat(str(body.get("started_at")))
        except (TypeError, ValueError):
            return JSONResponse(
                {"error": "started_at must be an ISO timestamp"}, status_code=400
            )
        if started_at.tzinfo is not None:
            # The app is naive-UTC end to end so an in-app ride and Zwift's
            # .fit for it land on the same instant; normalise rather than
            # storing an offset nothing else here understands.
            started_at = started_at.astimezone(_dt.timezone.utc).replace(tzinfo=None)

        workout_id = body.get("workout_id")
        if workout_id is not None:
            try:
                workout_id = int(workout_id)
            except (TypeError, ValueError):
                return JSONResponse({"error": "workout_id must be an integer"},
                                    status_code=400)
            # Scoped, so a connector cannot attach a ride to a workout that is
            # not its own user's.
            if db.get_plan_workout(device["user_id"], workout_id) is None:
                workout_id = None

        try:
            duration_s = int(body.get("duration_s") or len(power))
        except (TypeError, ValueError):
            return JSONResponse({"error": "duration_s must be an integer"},
                                status_code=400)

        activity_id, _record = await asyncio.to_thread(
            importer.save_ride_record,
            device["user_id"],
            started_at,
            duration_s,
            {
                "power": power,
                "cadence": samples.get("cadence") or [],
                "heartrate": samples.get("heartrate") or [],
            },
            str(body.get("name") or "Ride"),
            float(body.get("ftp") or importer.current_ftp(device["user_id"])),
            workout_id,
        )
        _log.info(
            "connector '%s' uploaded a buffered ride -> activity %s",
            device["label"], activity_id,
        )
        # activity_id is None when the ride is already stored - the connector
        # retries until it gets an answer, so a duplicate upload must read as
        # success rather than driving another retry.
        return JSONResponse(
            {"activity_id": activity_id, "duplicate": activity_id is None}
        )

    @app.post("/api/connector/session")
    async def connector_session_mint(request: Request):
        """Trade a device token for a single-use ticket that opens a session.

        The tray app shows this server's own web UI in a window. That UI is
        session-cookie authenticated and the connector holds no cookie, so
        something has to bridge the two - and it must not be "type your
        password into a window a tray icon opened", which is a habit worth not
        teaching.

        Bearer-authenticated exactly like /api/connector/ride above, and for
        the same reason: this is the connector talking. Deliberately HTTP
        rather than a call over the open WebSocket, because the connector never
        sends requests on that socket (see connector_ws) - the server owns
        every decision, and this route keeps it that way.

        Only the ticket is returned, never a URL. The server has no reliable
        idea which address reaches it from the connector's machine
        (_feed_base_url exists because that question is genuinely hard); the
        connector knows, because it dialled in on it.
        """
        header = request.headers.get("authorization") or ""
        scheme, _, token = header.partition(" ")
        device = None
        if scheme.lower() == "bearer" and token:
            device = connectorauth.device_for_token(token.strip())
        if device is None:
            app.state.connector_failures.record_failure()
            return JSONResponse({"error": "unauthorized"}, status_code=401)

        ticket = app.state.connector_tickets.mint(
            device["user_id"], device["username"], device["device_id"]
        )
        _log.info(
            "connector '%s' took a session ticket", device["label"]
        )
        response = JSONResponse(
            {"ticket": ticket, "expires_in": connectorsession.TICKET_TTL_S}
        )
        # The body is a live credential for the next minute.
        response.headers["Cache-Control"] = "private, no-store"
        return response

    @app.get("/connector/session")
    def connector_session_redeem(request: Request, token: str = ""):
        """Spend a ticket and become logged in.

        The parameter is named ``token`` on purpose, and renaming it would be a
        security regression: uvicorn's access logger writes the whole request
        target, and calendarfeed's redaction filter scrubs query parameters
        whose name starts with "token". Called anything else, every ticket this
        route ever receives would be written to the access log in plaintext.

        A bad ticket redirects to /login rather than explaining itself. There
        is nothing useful to tell a caller who did not have one, and the
        rider's own failure case - a ticket that sat too long - is fixed by
        double-clicking again.

        The session is stamped with where it came from, and that stamp is what
        keeps this route from being an escalation with no way back. A device
        token is accepted as already compromised - that is the whole premise of
        the Revoke button - so the session it opens must not be able to mint a
        credential that outlives the device. See _from_connector and the
        settings routes that consult it.
        """
        claim = app.state.connector_tickets.redeem(token)
        if claim is None:
            app.state.connector_failures.record_failure()
            return RedirectResponse("/login", status_code=303)
        request.session["user_id"] = claim["user_id"]
        request.session["username"] = claim["username"]
        request.session[SESSION_VIA] = VIA_CONNECTOR
        request.session[SESSION_DEVICE_ID] = claim["device_id"]
        _log.info(
            "a connector window opened a session for user %s", claim["user_id"]
        )
        response = RedirectResponse("/", status_code=303)
        response.headers["Cache-Control"] = "private, no-store"
        return response

    @app.post("/api/ftp/ramp-test/accept")
    def api_ramp_test_accept(
        request: Request, activity_id: int = Body(..., embed=True)
    ):
        """Make an accepted ramp-test result the rider's FTP.

        The number is RECOMPUTED here from the stored ride rather than taken
        from the request. The client is not the authority on a value that
        becomes a scoring basis, and recomputing costs one read of a stream we
        already have.

        This deliberately does NOT rescore prior activities.
        ``ftp_history`` is dated, so a row dated the test day changes nothing
        that came before it, and that default stands: rewriting a rider's
        recorded IF/TSS is a separate, explicit action
        (``ftp_rescore.rescore_imported_activities``), never a side effect of
        accepting a test.
        """
        uid = _uid(request)
        act = db.get_activity(uid, activity_id)
        if act is None:
            return JSONResponse({"error": "activity not found"}, status_code=404)
        # Routing, not authorization: the session's name is how a stored ride
        # is recognized as the declared test, and it keeps this route from
        # computing a "best minute x 0.75" out of an ordinary ride. It is not a
        # trust boundary and is not relied on as one - what gates the write is
        # the rider pressing accept on a value ``is_plausible_ftp`` admitted.
        # (Uploads are forced onto their .fit filename and in-app rides are
        # named by the planner, so the name is not freely chosen in practice.)
        if not ramp_test_mod.is_declared_ftp_test(act):
            return JSONResponse(
                {"error": "that ride was not a ramp test"}, status_code=400
            )
        streams = act.get("streams") or {}
        power = streams.get("power") or []
        result = ramp_test_mod.evaluate(
            power,
            # The PRESCRIBED window in workout seconds. Passing a sample
            # count here read the wrong part of any ride not recorded at
            # exactly 1 Hz - and the live loop's floor makes that the
            # normal case, so an accepted FTP came out below the one the
            # rider had just been shown.
            ramp_test_prescribed_window(),
            importer.current_ftp(uid),
            act.get("duration_s"),
            path=config.db_path(),
        )
        if not result["offer"]:
            return JSONResponse(
                {"error": result["message"], "result": result}, status_code=400
            )
        # The ride is named by its UTC date (see importer.save_ride_record), so
        # the entry is dated the same day the ride is filed under. Anything
        # else would put the test and its result on different days.
        started = parse_naive(act.get("start_time"))
        date_iso = (started or utc_now()).date().isoformat()
        # replace_existing: an estimate written for that date (ftp_backfill
        # fills every date) would otherwise make this an INSERT OR IGNORE that
        # silently does nothing. A confirmed test outranks whatever is already
        # sitting on that date - including a manual entry, which is why the
        # row being replaced is reported back rather than quietly dropped.
        replaced = db.ftp_entry_on(uid, date_iso, path=config.db_path())
        db.add_ftp_entry(uid, date_iso, result["ftp"], source=ramp_test_mod.SOURCE,
                         path=config.db_path(), replace_existing=True)
        # user_settings.ftp outranks ftp_history in current_ftp(), so leaving a
        # typed override in place would accept the test and change nothing the
        # rider can see. Accepting a measured FTP is the rider replacing their
        # own earlier statement, so the statement goes.
        previous_override = db.get_user_settings(uid).get("ftp")
        if previous_override is not None:
            db.set_user_ftp_override(uid, None, path=config.db_path())
        importer.profile_store.refresh(uid)
        effective = importer.current_ftp(uid)
        return JSONResponse({
            "ftp": result["ftp"],
            "date": date_iso,
            "source": ramp_test_mod.SOURCE,
            "activity_id": activity_id,
            "effective_ftp": round(float(effective), 1),
            "completed_ramp": result["completed_ramp"],
            "cleared_override": (
                round(float(previous_override), 1)
                if previous_override is not None else None
            ),
            "replaced": replaced,
        })

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

    @app.post("/api/activity/{activity_id}/weight")
    def api_activity_weight(
        request: Request,
        activity_id: int,
        weight_kg: object = Body(None, embed=True),
        date: Optional[str] = Body(None, embed=True),
    ):
        """Store a manual weigh-in; defaults to the ride's own local date.

        The date is the ride's local calendar day unless the rider posts one
        explicitly - the same "weight as of that day" resolution then applies
        to every record on that date, not just this activity.
        """
        uid = _uid(request)
        act = db.get_activity(uid, activity_id)
        if act is None:
            return JSONResponse({"error": "activity not found"}, status_code=404)
        parsed = parse_weight_input(weight_kg)
        if parsed.kg is None:
            return JSONResponse(
                {"error": parsed.error or "weight not saved"}, status_code=400
            )
        settings = db.get_user_settings(uid)
        tz = settings.get("timezone")
        if date in (None, ""):
            start = parse_naive(act.get("start_time"))
            date_iso = (
                to_user_timezone(start, tz).date().isoformat()
                if start is not None
                else local_today(tz).isoformat()
            )
        else:
            date_iso, date_error = _weight_log_date(date, tz)
            if date_error:
                return JSONResponse({"error": date_error}, status_code=400)
        db.record_weight(uid, date_iso, parsed.kg, "manual")
        return JSONResponse(
            {"date": date_iso, "weight_kg": parsed.kg, "source": "manual"}
        )



    @app.post("/activities/rescan")
    def rescan(request: Request, activities_dir: str = Form("")):
        """Start an asynchronous rescan and return immediately.

        The scan runs in a background daemon thread; the client polls
        GET /api/scan/status for live progress. A rescan already in flight for
        this user returns 409 with the running status (no second scan starts).
        """
        uid = _uid(request)
        posted = (activities_dir or "").strip()
        # Persist a typed directory as the user's activities_dir setting -
        # but only once it has passed the same containment check POST
        # /settings applies. This route used to save it unchecked, which meant
        # the Activities page was a way around the validation on the Settings
        # page for the very same field.
        if posted:
            # require_exists=False: a folder that is not there is answered by
            # the scan status ("exists": false), which is the more useful
            # response and what the page already renders. Containment under a
            # trusted root is the part that must not be skippable.
            clean, error = _validate_dir(
                posted, uid, require_exists=False, scope="activities"
            )
            if error:
                return JSONResponse({"error": error}, status_code=400)
            posted = clean or posted
            db.save_user_settings(uid, {"activities_dir": posted})
        started = _start_user_scan(uid, directory=posted or None)
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

    def _export_error(exc: paths.ExportTargetUnavailable) -> dict:
        """Render-ready form of a refusal from the .zwo writers.

        The explicit export buttons used to be incapable of failing: they went
        through a resolver that fell back to a literal "me" folder, so they
        always reported success, sometimes for a folder Zwift never reads
        (issue #44). Now they share the automatic sweep's resolver, so they can
        also come back with "there is nowhere to write" - and the page says so
        in the SAME words the sweep already uses (plan.html's export_alert
        macro), because the whole point of the fix is that the two paths stop
        disagreeing about where a user's workouts go.

        ``detail`` is only carried for 'blocked', the one reason where a folder
        WAS configured and was refused: the user needs to know which value of
        theirs was rejected and why. For 'choose'/'missing' nothing was
        determined, so there is nothing specific to quote.
        """
        _log.info("export refused (%s): %s", exc.reason, exc.detail or exc)
        return {"reason": exc.reason, "detail": exc.detail if exc.refused else ""}

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
            export_error=None,
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
        backend = get_backend(uid)
        try:
            target, reason = backend.resolve_export_dir(
                settings.get("zwift_id"), settings.get("workouts_dir")
            )
        except BackendUnavailable:
            # A new plan is still created and downloadable; only the automatic
            # write into the Zwift folder is deferred.
            return {"auto_export": None, "auto_export_reason": "offline"}
        if not target:
            return {
                "auto_export": None,
                "auto_export_reason": reason,  # 'choose' | 'missing'
                "zwift_candidates": discover(uid, "zwift_id_candidates"),
            }
        workouts = db.plan_workouts_for_plan(uid, plan_id, include_zwo=True)
        try:
            result = backend.apply_exports(
                ExportManifest(
                    zwift_id=settings.get("zwift_id"),
                    override=settings.get("workouts_dir"),
                    write=[
                        {"date": w["date"], "name": w["name"],
                         "zwo": w["zwo_or_segments"]}
                        for w in workouts
                    ],
                )
            )
        except paths.ExportTargetUnavailable as e:
            # The plan's rows are already committed at this point, so this may
            # not escape: it would 500 the response to a plan the user now
            # owns and cannot see. It is a RuntimeError on purpose, so the
            # OSError branch below does not cover it.
            _log.warning("plan auto-export refused (%s): %s", e.reason, e.detail or e)
            return {
                "auto_export": None,
                "auto_export_reason": e.reason,
                "auto_export_detail": e.detail if e.refused else "",
                "zwift_candidates": paths.candidate_zwift_ids(),
            }
        except OSError as e:
            # A real I/O failure (permissions, full disk, folder yanked). The
            # reason used to be f"error: {e}", which matched no branch in
            # plan.html's export_alert macro, so the plan card rendered NOTHING
            # and the user was told their workouts had not been exported by
            # being told nothing at all. It is now a reason the macro knows,
            # with the OS message carried separately as the detail.
            _log.warning("plan auto-export failed: %s", e)
            return {"auto_export": None, "auto_export_reason": "error",
                    "auto_export_detail": str(e)}
        if result.get("status") != "ok":
            return {"auto_export": None,
                    "auto_export_reason": result["reason"] or result.get("status", "error"),
                    "auto_export_detail": ""}
        return {
            "auto_export": {
                "count": result["exported"],
                "directory": result["directory"],
                "reason": reason,
            },
            "auto_export_reason": None,
            "auto_export_detail": "",
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
        export_error = None
        if workouts:
            result = get_backend(uid).apply_exports(
                ExportManifest(
                    zwift_id=settings.get("zwift_id"),
                    override=settings.get("workouts_dir"),
                    write=[
                        {"date": w["date"], "name": w["name"],
                         "zwo": w["zwo_or_segments"]}
                        for w in workouts
                    ],
                    resolution="direct",
                )
            )
            if result.get("status") == "ok":
                exported = {"count": result["exported"], "directory": result["directory"]}
            else:
                export_error = {"reason": result["reason"] or result.get("status", "error"),
                                "detail": ""}
        summary = _plan_summary(uid, plan_id)
        return templates.TemplateResponse(
            request,
            "plan.html",
            _generate_ctx(request, mode="plan", plan=summary, exported=exported,
                          export_error=export_error),
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
        export_error = None
        summary = None
        if w:
            settings = db.get_user_settings(uid)
            result = get_backend(uid).apply_exports(
                ExportManifest(
                    zwift_id=settings.get("zwift_id"),
                    override=settings.get("workouts_dir"),
                    write=[{"date": w["date"], "name": w["name"],
                            "zwo": w["zwo_or_segments"]}],
                    resolution="direct",
                )
            )
            if result.get("status") == "ok":
                exported = {"count": result["exported"], "directory": result["directory"]}
            else:
                export_error = {"reason": result["reason"] or result.get("status", "error"),
                                "detail": ""}
            summary = _plan_summary(uid, w["plan_id"])
        return templates.TemplateResponse(
            request,
            "plan.html",
            _generate_ctx(request, mode="plan", plan=summary, exported=exported,
                          export_error=export_error),
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
        adjustment_id: Optional[int] = None,
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
        activities_by_date: dict = {}
        for w in db.plan_workouts_for_month(uid, y, m):
            wd = dict(w)
            adjustment_state = w.get("adjustment_state")
            wd["adjustment_cancelled"] = adjustment_state in {
                "ooto_canceled", "displaced",
            }
            wd["adjustment_replacement"] = adjustment_state in {
                "rescheduled", "rebalanced",
            }
            wd["skipped"] = (
                _in_ooto(w["date"])
                and not w.get("completed_activity_id")
            )
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
        for activity in db.activities_for_month_unlinked(uid, y, m):
            activity = dict(activity)
            activity.update({
                "date": activity["start_time"][:10],
                "activity": True,
                "url": f"/activity/{activity['id']}",
            })
            activities_by_date.setdefault(activity["date"], []).append(activity)

        # Which block of the active plan's arc each day belongs to, so a day
        # cell can say "build" rather than leaving the rider to count weeks.
        # Empty for a plan with no goal, which is every plan that predates them.
        active = db.get_active_plan(uid) if uid is not None else None
        adjustment = None
        if adjustment_id is not None:
            adjustment = db.get_ooto_adjustment(uid, adjustment_id)
            if adjustment and adjustment.get("status") != "pending":
                adjustment = None
        if adjustment is None:
            pending = db.list_pending_ooto_adjustments(uid)
            if pending:
                adjustment = pending[0]
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
                        "activities": activities_by_date.get(iso, []),
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
                ooto_adjustment=_ooto_adjustment_view(adjustment),
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
        try:
            status = exporter.sync_plan_exports(uid)["status"]
        except paths.ExportTargetUnavailable as e:
            # sync_plan_exports() already turns a refusal into a status; this
            # is the backstop, because this route has no other error handling
            # and a refusal is not an OSError anything upstream would catch.
            # The reason vocabulary is a fixed set of bare words, so it is safe
            # to put straight in the redirect's query string.
            status = _export_error(e)["reason"]
        return RedirectResponse(
            url=f"/calendar?exported={status}", status_code=303
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
                ooto_id = db.add_ooto_range(uid, start, end, note.strip() or None)
                adjustment_id = None
                plan = db.get_active_plan(uid)
                if plan is not None:
                    ooto = next(
                        (row for row in db.list_ooto_ranges(uid)
                         if row["id"] == ooto_id),
                        None,
                    )
                    if ooto is not None:
                        try:
                            proposal = _ooto_proposal_for(uid, plan, ooto)
                            if proposal.get("affected"):
                                adjustment_id = db.create_ooto_adjustment(
                                    uid, plan["id"], ooto_id,
                                    ooto["start_date"], ooto["end_date"], proposal,
                                )
                        except Exception:
                            _log.warning(
                                "OOTO adjustment proposal failed for user %s",
                                uid, exc_info=True,
                            )
                # Keep the Zwift folder in sync (drop newly-skipped .zwo).
                try:
                    exporter.sync_plan_exports(uid)
                except Exception:
                    _log.warning("export sync after OOTO add failed", exc_info=True)
                if adjustment_id is not None:
                    return RedirectResponse(
                        url=f"/calendar?adjustment_id={adjustment_id}",
                        status_code=303,
                    )
            except ValueError:
                pass
        return RedirectResponse(url="/calendar", status_code=303)

    @app.post("/ooto/{ooto_id}/delete")
    def ooto_delete(request: Request, ooto_id: int):
        uid = _uid(request)
        # Read the files the revert is about to orphan BEFORE deleting: a
        # rescheduled row is deleted and a rebalanced row is renamed back, so
        # afterwards nothing remembers the .zwo either of them wrote. Pruned
        # before the sync, which then re-writes the restored plan on top.
        orphans = db.ooto_range_revert_orphans(uid, ooto_id)
        db.delete_ooto_range(uid, ooto_id)
        try:
            for orphan in orphans:
                adaptmod.reexport_workout(
                    uid, orphan["date"], orphan["name"], None,
                )
            exporter.sync_plan_exports(uid)  # re-export days that are back in
        except Exception:
            _log.warning("export sync after OOTO delete failed", exc_info=True)
        return RedirectResponse(url="/calendar", status_code=303)

    @app.post("/ooto-adjustment/{adjustment_id}/confirm")
    def ooto_adjustment_confirm(
        request: Request, adjustment_id: int, option: str = Form(...),
    ):
        uid = _uid(request)
        result = db.apply_ooto_adjustment(
            uid, adjustment_id, option, now=utc_today()
        )
        if result.get("status") == "applied":
            try:
                # Prune the .zwo of any session this adjustment renamed BEFORE
                # the sync writes its replacement: the manifest is built from
                # stored rows and cannot know the name that just went away.
                for renamed in result.get("renamed") or ():
                    adaptmod.reexport_workout(
                        uid, renamed["date"], renamed["old_name"], None,
                    )
                exporter.sync_plan_exports(uid)
            except Exception:
                _log.warning(
                    "export sync after OOTO adjustment failed", exc_info=True
                )
            flash = "OOTO adjustment applied."
        elif result.get("status") == "stale":
            flash = "That OOTO adjustment is stale; the plan changed underneath it."
        elif result.get("status") == "already_resolved":
            # A double-submit lands here. Deliberately NOT treated as "applied":
            # re-running the export sync for a row nothing touched is churn, and
            # telling the rider it just applied would be a lie.
            flash = (
                "That OOTO adjustment was already "
                f"{result.get('resolution') or 'resolved'}."
            )
        else:
            flash = "The OOTO adjustment could not be applied."
        return RedirectResponse(
            url="/calendar?flash=" + _url.quote(flash), status_code=303
        )

    @app.post("/ooto-adjustment/{adjustment_id}/dismiss")
    def ooto_adjustment_dismiss(request: Request, adjustment_id: int):
        uid = _uid(request)
        db.set_ooto_adjustment_status(uid, adjustment_id, "dismissed")
        return RedirectResponse(
            url="/calendar?flash="
            + _url.quote("OOTO workouts will remain skipped."),
            status_code=303,
        )

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
        result = get_backend(uid).apply_exports(
            ExportManifest(
                zwift_id=settings.get("zwift_id"),
                override=settings.get("workouts_dir"),
                write=[{"date": scheduled, "name": last["name"], "zwo": last["zwo"]}],
                resolution="direct",
            )
        )
        if result.get("status") != "ok":
            return templates.TemplateResponse(
                request,
                "plan.html",
                _generate_ctx(request, mode="workout",
                              export_error={"reason": result["reason"] or result.get("status", "error"),
                                            "detail": ""}),
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
                      calendar_feed_url: Optional[str] = None,
                      ftp_message: Optional[str] = None,
                      ftp_confirm_required: bool = False,
                      ftp_form_value: Optional[str] = None,
                      weight_message: Optional[str] = None,
                      weight_form_value: Optional[str] = None,
                      connector_message: Optional[str] = None,
                      connector_new_token: Optional[str] = None,
                      connector_new_label: Optional[str] = None,
                      refusal_message: Optional[str] = None,
                      llm_message: Optional[str] = None) -> dict:
        settings = db.get_user_settings(uid)
        # LLM refinement (app-level). The page shows the EFFECTIVE endpoint
        # (an env var wins silently) and the STORED model: blank falls back to
        # the per-provider default at runtime, so echoing the effective model
        # here would re-store it and make "clear the saved model" unreachable.
        llm_cfg = config.load_config()
        llm_endpoint_raw = (llm_cfg.llm_endpoint or "").strip() or "anthropic"
        llm_endpoint_norm = config.normalize_llm_endpoint(llm_endpoint_raw)
        if llm_endpoint_norm in config.LLM_DEFAULT_MODELS:
            llm_endpoint_display, llm_custom_url_display = llm_endpoint_norm, ""
        else:
            # A URL (valid or not): the custom option, with the raw value in
            # the URL field so a broken one is visible and fixable.
            llm_endpoint_display, llm_custom_url_display = (
                "custom", llm_endpoint_raw,
            )
        return _ctx(
            request,
            settings=settings,
            # A rejected FTP is echoed back in the field so the rider can see and
            # correct what they typed, rather than the stored value silently
            # replacing it.
            ftp_form_value=(
                _ftp_field_value(settings.get("ftp"))
                if ftp_form_value is None else ftp_form_value
            ),
            ftp_message=ftp_message,
            ftp_confirm_required=ftp_confirm_required,
            ftp_min=round(FTP_INPUT_MIN_WATTS),
            ftp_max=round(FTP_INPUT_MAX_WATTS),
            # A refused weight is echoed back in the field, the FTP way: the
            # rider sees and corrects what they typed rather than the stored
            # value silently replacing it.
            weight_message=weight_message,
            weight_form_value=(
                _weight_field_value(settings.get("weight_kg"))
                if weight_form_value is None else weight_form_value
            ),
            weight_min=round(WEIGHT_INPUT_MIN_KG),
            weight_max=round(WEIGHT_INPUT_MAX_KG),
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
            api_key_set=bool(llm_cfg.api_key),
            llm_endpoint=llm_endpoint_display,
            llm_custom_url=llm_custom_url_display,
            llm_model=(llm_cfg.llm_model or "").strip(),
            llm_message=llm_message,
            saved=saved,
            zwift_candidates=discover(uid, "zwift_id_candidates"),
            watch_default=discover(uid, "default_activities_dir"),
            # Rendered as a banner: the Settings page is where someone goes
            # to find out why their connector is not working, so it must
            # load without one.
            connector_offline=is_offline(uid),
            # Same contract as the calendar token: the list never contains a
            # secret, and connector_new_token is only ever passed in by the
            # route that just minted it - never read back out of the database.
            connector_devices=connectorauth.list_devices(uid),
            connector_message=connector_message,
            connector_new_token=connector_new_token,
            connector_new_label=connector_new_label,
            # Rendered as an alert at the top of the page, separately from the
            # per-section messages: this one is about the session, not about
            # the field the rider filled in.
            refusal_message=refusal_message,
            zwift_creds_saved=credstore.credentials_saved(uid),
            zwift_cred_backend=credstore.storage_backend(),
            cred_message=cred_message,
            backups=backup.list_backups(),
            backup_message=backup_message,
            dir_message=dir_message,
            restore_cmd=_restore_command(),
        )

    # What a connector-opened session is refused, and why it is exactly this
    # list. A device token is the credential this design accepts as already
    # compromised - docs/windows-security.md points at the Revoke button as the
    # answer to a stolen laptop - so the session it opens must not be able to
    # leave a NEW credential behind that revoking the device does not reach.
    # Three routes could:
    #
    #   POST /settings/connector       a second device token, of the attacker's
    #                                  own, surviving revocation of the first
    #   POST /settings/calendar-feed   the whole training calendar re-pointed at
    #                                  a URL only the attacker holds
    #   POST /settings (LLM fields)    the app-global - not per-user - LLM
    #                                  settings: swapping the KEY sends every
    #                                  coaching request, prompts included, to
    #                                  an account someone else owns, and
    #                                  repointing the ENDPOINT at a base URL
    #                                  the attacker runs hands them that key
    #                                  and those prompts without their ever
    #                                  having to know either
    #
    # Revoking is deliberately NOT on the list: it is the way out, and the rider
    # who has lost a laptop may well be looking at the tray window of the
    # machine still in front of them. Nor is the rest of POST /settings -
    # pointing the app at the right folders is what the window is for.
    _CONNECTOR_REFUSAL = (
        "This window was opened by a connector device, so it cannot issue or "
        "replace credentials, or change the app-wide LLM settings. Sign in "
        "with your password first."
    )

    def _refuse_connector_session(request: Request, uid: int) -> Response:
        """The refusal, rendered as the page the rider was already looking at.

        403 rather than a redirect or a bare error: the caller asked for
        something this session is not allowed to do, and the page says what to
        do instead. A 500 would be the wrong answer to a request that is
        perfectly well formed.
        """
        return templates.TemplateResponse(
            request, "settings.html",
            _settings_ctx(request, uid, False, refusal_message=_CONNECTOR_REFUSAL),
            status_code=403,
        )

    def _validate_dir(
        value: str, uid: Optional[int] = None, require_exists: bool = True,
        scope: str = "",
    ) -> "tuple[Optional[str], Optional[str]]":
        """Validate a user-supplied folder path against the trusted roots.

        Delegated to the backend because the check has to run on the machine
        that owns the path: in a server/client install these are the *client's*
        folders, and measuring them against the container's home directory
        would reject every legitimate answer.

        ``scope`` names the field, so the machine that owns the path answers
        with the rule that will govern it in use. Without it a split install
        accepts an activities folder here and then declines to scan it, which
        is a saved setting that does nothing.
        """
        return get_backend(uid).validate_dir(
            value, require_exists=require_exists, scope=scope
        )

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
        zwift_email: str = Form(""),
        zwift_password: str = Form(""),
        weight_kg: str = Form(""),
        timezone: str = Form(""),
        confirm_low_ftp: str = Form(""),
        llm_endpoint: str = Form(""),
        llm_custom_url: str = Form(""),
        llm_model: str = Form(""),
        api_key: str = Form(""),
        anthropic_api_key: str = Form(""),
    ):
        uid = _uid(request)
        # The FTP field is a SCORING BASIS, so it gets the same policy as every
        # other place a rider types one (wattracker.ftp_input, issue #64). A
        # rejected value is reported and left unsaved; the rest of the form is
        # saved regardless, so a typo in one field does not discard the others.
        ftp_message: Optional[str] = None
        ftp_confirm_required = False
        ftp_value: Optional[float] = None
        if (ftp or "").strip():
            parsed = parse_ftp_input(ftp, confirmed=_checked(confirm_low_ftp))
            ftp_value = parsed.watts
            if parsed.error:
                ftp_message = "FTP not saved. " + parsed.error
            ftp_confirm_required = parsed.needs_confirmation
        # A picked player folder (radio) wins over the free-text field.
        chosen_zwift_id = (zwift_id_choice or "").strip() or zwift_id
        # Weight follows the same single-policy shape as FTP: parse_weight_input
        # is the only gate, a rejected value is reported and left unsaved, and
        # the rest of the form is saved regardless. An accepted value becomes a
        # manual log for the rider's local today, not the legacy scalar.
        weight_message: Optional[str] = None
        weight_val: Optional[float] = None
        if (weight_kg or "").strip():
            parsed_weight = parse_weight_input(weight_kg)
            weight_val = parsed_weight.kg
            if parsed_weight.error:
                weight_message = "Weight not saved. " + parsed_weight.error
        # Confine user-supplied folders to existing directories under $HOME; a
        # rejected folder is dropped from the update (existing value kept).
        dir_msgs: List[str] = []
        clean_activities, act_err = _validate_dir(
            activities_dir, uid, scope="activities"
        )
        if act_err:
            dir_msgs.append(act_err)
            clean_activities = ""  # don't persist an invalid path
        clean_workouts, wk_err = _validate_dir(
            workouts_dir, uid, scope="workouts"
        )
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
                "ftp": ftp_value,
                "zwift_id": chosen_zwift_id,
                "activities_dir": clean_activities,
                "workouts_dir": clean_workouts,
                "timezone": clean_timezone,
            },
        )
        # A manual FTP entry records a source='manual' row for today (per user).
        if ftp_value is not None:
            db.add_ftp_entry(uid, utc_today().isoformat(), ftp_value, "manual")
        # A typed weight is a measurement: a manual log for the rider's local
        # today. record_weight re-syncs the legacy scalar to it (a fresh manual
        # log is always the latest-dated row), so scalar readers stay current.
        if weight_val is not None:
            tz = db.get_user_settings(uid).get("timezone")
            db.record_weight(uid, local_today(tz).isoformat(), weight_val,
                             "manual")
        try:
            profile_store.refresh(uid)
        except Exception:
            _log.warning("profile refresh after settings save failed", exc_info=True)
        # LLM refinement settings are app-level (shared). The key is blank =
        # keep (it is never displayed); the model is blank = clear the stored
        # value (it is always shown, and blank falls back to the provider
        # default at runtime). Environment variables (API_KEY, LLM_ENDPOINT,
        # LLM_MODEL) still override stored values; a conflict resolves
        # silently in their favour, exactly as WATTRACKER_SECRET does.
        llm_msgs: List[str] = []
        endpoint_choice = (llm_endpoint or "").strip().lower()
        custom_url = (llm_custom_url or "").strip()
        endpoint_to_save: Optional[str] = None
        url_to_save: Optional[str] = None
        endpoint_ok = False
        if endpoint_choice == "custom":
            # The selector holds the URL itself when custom: the URL is
            # required and validated; named providers ignore the field.
            url_to_save = config.normalize_llm_endpoint(custom_url)
            if url_to_save is None:
                llm_msgs.append(
                    "Custom LLM endpoint needs a valid http:// or https:// "
                    "base URL (e.g. http://localhost:11434/v1)."
                )
                url_to_save = None
            else:
                endpoint_ok = True
        elif endpoint_choice:
            if endpoint_choice in ("anthropic", "openai", "openrouter"):
                endpoint_to_save = endpoint_choice
                endpoint_ok = True
            else:
                llm_msgs.append(
                    f"Unknown LLM provider '{llm_endpoint.strip()}' - use "
                    "anthropic, openai, openrouter, or custom."
                )
        model_val = (llm_model or "").strip()
        model_rejected = len(model_val) > 200
        if model_rejected:
            llm_msgs.append("LLM model must be at most 200 characters.")
            model_val = ""
        # api_key wins over the legacy alias if both are posted.
        key_val = (api_key or "").strip() or (anthropic_api_key or "").strip()
        # The model field is always part of the form, but blank clears the
        # stored value (falling back to the provider default) only when this
        # save actually carried an endpoint selection and the field was
        # genuinely blank - a REJECTED model must be reported and left
        # unsaved, not treated as a clear that wipes the previously working
        # model.
        clear_model = endpoint_ok and not model_val and not model_rejected
        # Shared is also what makes this whole group - endpoint, custom URL,
        # model and key alike - the part of this route a connector-opened
        # session may not touch. A session that swapped the KEY would point
        # every coaching request this server makes, prompt contents and all,
        # at an account somebody else owns, and revoking the device would not
        # undo it. A settable ENDPOINT is the same threat, larger: a base URL
        # the attacker controls is handed the shared key on the first
        # refinement call and every rider's prompt payload after that, by a
        # server that otherwise looks like it is working, and revoking the
        # device does not undo that either. The model comes with them because
        # it is written by the same call and decides whether the layer runs at
        # all. The rest of this route stays open on purpose; pointing the app
        # at the right folders is exactly what the tray window exists for.
        # See _CONNECTOR_REFUSAL.
        #
        # Refused means asked to CHANGE the group, not merely to post it: the
        # LLM fields share one form with the folders, so a connector window
        # sends back the provider and model this page just rendered on every
        # save. Reading that echo as an attempt would 403 the folder save the
        # window exists for while preventing nothing. A connector session
        # writes none of this group either way - the write below is skipped
        # whether or not anything differed - so the only question here is
        # whether the rider gets told, and a save that asked for something
        # other than what it was shown has to be told.
        refusal = False
        if _from_connector(request):
            # Answered below with the same 403 the other two refusals use, and
            # deliberately not by falling through to the ordinary "Settings
            # saved." render: a page that says both that it saved and that it
            # refused describes neither outcome, and 200 tells a script the
            # write went through. The other fields on this form are still
            # written first - only the app-global LLM group is off limits. The
            # per-field LLM complaints go with it: this request was refused,
            # not evaluated, and one answer beats two.
            stored = config.load_config()
            stored_endpoint = (stored.llm_endpoint or "").strip() or "anthropic"
            stored_model = (stored.llm_model or "").strip()
            # What set_llm_settings would put in llm_endpoint (None = leave
            # it alone), compared against what the page rendered - which is
            # the effective endpoint, so an unset one reads as "anthropic".
            endpoint_to_write = url_to_save or endpoint_to_save
            refusal = (
                # A rejected value is still an attempt to change the group.
                bool(llm_msgs)
                # Blank = keep, so any key at all was typed into this window.
                or bool(key_val)
                or (endpoint_to_write is not None
                    and endpoint_to_write != stored_endpoint)
                or (bool(model_val) and model_val != stored_model)
                or (clear_model and bool(stored_model))
            )
            if refusal:
                llm_msgs = []
        elif endpoint_to_save or url_to_save or model_val or key_val:
            config.set_llm_settings(
                endpoint=endpoint_to_save,
                custom_url=url_to_save,
                model=model_val or None,
                api_key=key_val or None,
                clear_model=clear_model,
            )
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
            _settings_ctx(request, uid, not refusal, cred_message=cred_message,
                          dir_message="; ".join(dir_msgs) or None,
                          ftp_message=ftp_message,
                          ftp_confirm_required=ftp_confirm_required,
                          ftp_form_value=ftp if ftp_message else None,
                          weight_message=weight_message,
                          weight_form_value=weight_kg if weight_message else None,
                          refusal_message=_CONNECTOR_REFUSAL if refusal else None,
                          llm_message="; ".join(llm_msgs) or None),
            status_code=403 if refusal else 200,
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
        if _from_connector(request):
            # Rotating from here would hand the attacker the only copy of the
            # new link and leave the rider's calendar app silently stale - a
            # compromise that looks like a sync bug. See _CONNECTOR_REFUSAL.
            return _refuse_connector_session(request, uid)
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

    @app.post("/settings/connector", response_class=HTMLResponse)
    def settings_connector_pair(request: Request, label: str = Form("")):
        """Pair a new connector machine and show its token exactly once.

        Same shape as the calendar-feed action: session-authenticated,
        same-origin checked, and the plaintext is rendered into this one
        response and then discarded - only the hash is stored, so this is the
        only moment it can be copied.
        """
        if not _same_origin_or_absent(request):
            return PlainTextResponse("Origin not allowed", status_code=403)
        uid = _uid(request)
        if _from_connector(request):
            # The blocker the pre-merge review of this branch found. Pairing
            # from inside a connector-opened window mints a token that survives
            # revoking the device that opened the window, so the owner does the
            # one thing the docs tell them to do about a stolen laptop and the
            # thief keeps a permanent credential - under a label they chose.
            return _refuse_connector_session(request, uid)
        clean = connectorauth.clean_label(label)
        minted = connectorauth.generate_token(uid, clean)
        if minted is None:
            return templates.TemplateResponse(
                request, "settings.html",
                _settings_ctx(request, uid, False,
                              connector_message="Could not pair the device."),
                status_code=500,
            )
        _device_id, token = minted
        response = templates.TemplateResponse(
            request, "settings.html",
            _settings_ctx(
                request, uid, False,
                connector_message=(
                    "Device paired. Copy the token now - it is not shown again."
                ),
                connector_new_token=token,
                connector_new_label=clean,
            ),
        )
        # This page body contains the plaintext token exactly once.
        response.headers["Cache-Control"] = "private, no-store"
        return response

    @app.post("/settings/connector/{device_id}/revoke", response_class=HTMLResponse)
    def settings_connector_revoke(request: Request, device_id: int):
        if not _same_origin_or_absent(request):
            return PlainTextResponse("Origin not allowed", status_code=403)
        uid = _uid(request)
        # Scoped by uid inside the delete, so a guessed id cannot unpair
        # someone else's machine. A miss is reported the same as a hit would
        # be if it were someone else's - there is nothing to learn either way.
        revoked = connectorauth.revoke(uid, device_id)
        if revoked:
            # Deleting the row settles the next connection; this settles the
            # one that is open. Without it a revoked machine keeps serving RPC
            # over its existing socket until the server restarts, while the
            # page tells the owner the token no longer works.
            connectorhub.close_device(uid, device_id)
            # And this settles a ticket already in flight. Small window - a
            # minute at most - but "revoked" must not still be able to open a
            # logged-in window.
            app.state.connector_tickets.revoke_device(device_id)
        return templates.TemplateResponse(
            request, "settings.html",
            _settings_ctx(
                request, uid, False,
                connector_message=(
                    "Device revoked. Its token no longer works."
                    if revoked else "No such device."
                ),
            ),
        )

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
            can_show_wkg=data["can_show_wkg"],
            rider_id=zid if zid.isdigit() else "",
            saved_zwift_id=zid,
            workouts_root=discover(uid, "workouts_root"),
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

    def _validate_just_ride(wtype, minutes, variant=None):
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
        try:
            selected_variant = validate_variant(kind, variant)
        except ValueError as e:
            raise ValueError(str(e))
        return kind, mins, selected_variant

    def _ride_session(uid, workout_id=None, wtype=None, minutes=None, variant=None):
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
            kind, mins, selected_variant = _validate_just_ride(wtype, minutes, variant)
            s = build_workout(kind, mins, selected_variant, profile=profile)
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

    def _ramp_test_result(controller) -> Optional[dict]:
        """The ramp-test result for a finished ride, or None if it was not one.

        The window comes from the PRESCRIBED session, not from the shape of
        the recording: the rider declared the test by selecting it, so there is
        nothing to detect. Returns None for every other kind of ride, and for a
        test that recorded nothing.
        """
        window = ramp_test_window(controller.session)
        if window is None or not controller.has_started:
            return None
        power = controller.recorded_power()
        if not power:
            return None
        try:
            result = ramp_test_mod.evaluate(
                power, window, controller.ftp, int(controller.elapsed),
                path=config.db_path(),
            )
        except Exception:
            # Offering a result must never be what breaks the end of a ride.
            # The ride is already saved; the rider can accept from the stored
            # activity, which recomputes this same number.
            _log.warning("could not compute a ramp-test result", exc_info=True)
            return None
        result["activity_id"] = controller.activity_id
        return result

    def _watts(fraction, ftp: float) -> Optional[int]:
        return present.watts(fraction, ftp)

    def _fmt_clock(seconds: int) -> str:
        return present.fmt_clock(seconds)

    @app.get("/ride/workout/preview")
    def ride_workout_preview(request: Request, type: str = "", minutes: str = "",
                             variant: Optional[str] = None):
        uid = _uid(request)
        try:
            kind, mins, selected_variant = _validate_just_ride(type, minutes, variant)
            rider_profile = profile_store.for_user(uid)
            session = build_workout(kind, mins, selected_variant,
                                    profile=rider_profile)
        except (ValueError, OverflowError) as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        ftp = importer.current_ftp(uid)
        payload = _ride_workout_payload(session, ftp)
        info = workout_type_info(kind) or {}
        info["low_watts"] = _watts(info.get("low"), ftp)
        info["high_watts"] = _watts(info.get("high"), ftp)
        info["work_watts"] = _watts(info.get("work"), ftp)
        payload.update({
            "variant": selected_variant,
            "variant_options": list(VARIANTS[kind]),
            "description": session.description,
            "workout_type": session.workout_type,
            "estimated_tss": session.estimated_tss,
            "type_info": info,
            "segments": present.segment_rows(session, ftp),
        })
        all_variants = {}
        for option in VARIANTS[kind]:
            candidate = build_workout(kind, mins, option, profile=rider_profile)
            candidate_payload = _ride_workout_payload(candidate, ftp)
            # Keyed by the candidate's OWN length, not the length asked for:
            # the client looks this up by the duration it was served
            # (duration_s / 60), and a measurement protocol is emitted at the
            # protocol's length rather than the requested one. For every
            # training type the two are the same number.
            all_variants[option] = {
                str(round(candidate_payload["duration_s"] / 60)): {
                    "name": candidate.name,
                    "description": candidate.description,
                    "estimated_tss": candidate.estimated_tss,
                    "duration_s": candidate_payload["duration_s"],
                    "profile": candidate_payload["profile"],
                }
            }
        payload["variant_profiles"] = all_variants
        return JSONResponse(payload)

    @app.websocket("/connector/ws")
    async def connector_ws(websocket: WebSocket):
        """A connector machine attaching itself to this server.

        Authenticated by a per-device bearer token, not a session cookie - the
        connector is not a browser and has no login. Note this handler is
        reached without AuthMiddleware running at all: Starlette's
        BaseHTTPMiddleware only wraps http scopes, so every websocket route in
        this app does its own auth (the ride socket reads the session; this one
        reads the header).

        No Origin check here, unlike /ride/ws. That check exists to stop a
        malicious *web page* from driving a socket with the user's ambient
        cookie; this endpoint ignores cookies entirely and demands a secret the
        browser does not have, so an Origin would add nothing - and a native
        client has no Origin to send in the first place.
        """
        header = websocket.headers.get("authorization") or ""
        scheme, _, token = header.partition(" ")
        device = None
        if scheme.lower() == "bearer" and token:
            device = connectorauth.device_for_token(token.strip())
        if device is None:
            count = app.state.connector_failures.record_failure()
            _log.warning(
                "rejected connector token (%d rejected since start)", count
            )
            # Refused before accept(), so nothing is ever sent to a caller that
            # did not prove it holds a token. 1008 = policy violation.
            await websocket.close(code=1008)
            return

        await websocket.accept()
        peer = rpc.RpcPeer(_ConnectorSocket(websocket))

        async def _hang_up(code: int = 1000) -> None:
            try:
                await websocket.close(code=code)
            except Exception:
                pass  # already gone; nothing to do

        session = connectorhub.ConnectorSession(
            user_id=device["user_id"],
            device_id=device["device_id"],
            label=device["label"],
            peer=peer,
            loop=asyncio.get_running_loop(),
            closer=_hang_up,
        )
        displaced = connectorhub.register(session)
        _log.info(
            "connector '%s' attached for user %s%s",
            session.label, session.user_id,
            " (replacing an earlier one)" if displaced else "",
        )
        try:
            await websocket.send_json(
                {"event": "hello", "protocol": rpc.PROTOCOL_VERSION,
                 "device": session.label}
            )
            while True:
                message = rpc.decode(await websocket.receive_text())
                if peer.resolve(message):
                    continue
                # Connector -> server events. The connector never sends
                # requests: the server owns the database and therefore owns
                # every decision. Unknown events are ignored rather than
                # fatal, so an older server tolerates a newer connector.
                event = message.get("event")
                if event:
                    await _handle_connector_event(session, event, message)
        except (WebSocketDisconnect, rpc.ProtocolError) as exc:
            if isinstance(exc, rpc.ProtocolError):
                _log.warning("connector %s protocol error: %s", session.label, exc)
        except Exception:
            _log.warning(
                "connector %s failed", session.label, exc_info=True
            )
        finally:
            connectorhub.unregister(session)
            _log.info("connector '%s' detached", session.label)

    async def _handle_connector_event(session, event: str, message: dict) -> None:
        """Route one connector-originated event.

        Only ride telemetry so far, and it is fire-and-forget by design: a
        dropped frame costs one second of a chart, whereas acknowledging every
        sample would put a round trip in the middle of a 1 Hz loop.
        """
        if event == "ble.sample":
            sink = session.ble_sink
            if sink is not None:
                sink.update(
                    power=message.get("power"),
                    cadence=message.get("cadence"),
                    hr=message.get("hr"),
                    # The connector's own index for this sample. What makes a
                    # reconnect ask for exactly the seconds it missed.
                    index=message.get("n"),
                )
            return
        _log.debug("ignoring unknown connector event %s", event)

    @app.get("/ride", response_class=HTMLResponse)
    async def ride_page(request: Request, workout_id: Optional[int] = None):
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
        available, reason = await _ble_available(uid)
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
    async def ride_status(request: Request):
        available, reason = await _ble_available(_uid(request))
        return JSONResponse({"available": available, "reason": reason})

    @app.post("/ride/scan")
    async def ride_scan(request: Request):
        uid = _uid(request)
        available, reason = await _ble_available(uid)
        if not available:
            return JSONResponse(
                {"available": False, "reason": reason, "devices": []}
            )
        try:
            session = _ble_session(uid)
            found = (
                await bledevices.scan() if session is None
                else await remote_ble.scan(session)
            )
            _remember_scanned_names(uid, found)
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

    # The friendly names the device picker last showed, per user and address.
    # The ride socket only ever receives addresses back, and connect_sensors'
    # failures name a device by whatever it was given - so this is what turns a
    # failed selection back into "KICKR CORE" for the rider. Names only, kept
    # in memory and bounded; nothing here is authoritative for a ride.
    _scanned_names: dict = {}

    def _remember_scanned_names(uid: Optional[int], devices) -> None:
        for device in devices or []:
            if not isinstance(device, dict):
                continue
            address = str(device.get("address") or "")
            name = str(device.get("name") or "")
            if not address or not name or name == address:
                continue
            key = (uid, address)
            _scanned_names.pop(key, None)
            _scanned_names[key] = name
        while len(_scanned_names) > _MAX_REMEMBERED_SENSOR_NAMES:
            _scanned_names.pop(next(iter(_scanned_names)))

    def _scanned_names_for(uid: Optional[int]) -> dict:
        return {
            address: name
            for (owner, address), name in _scanned_names.items()
            if owner == uid
        }

    async def _ble_available(uid: Optional[int]) -> "tuple[bool, str]":
        """Whether a BLE radio is usable, here or on the connector's machine."""
        if backend_mode() == "local":
            return bledevices.bluetooth_available()
        session = connectorhub.get(uid)
        if session is None:
            return False, (
                "No connector is attached. Start the wattracker connector on "
                "the machine where Zwift and your trainer are."
            )
        return await remote_ble.bluetooth_available(session)

    def _ble_session(uid: Optional[int]):
        """The connector to ride through, or None in local mode."""
        return None if backend_mode() == "local" else connectorhub.require(uid)

    def _ws_origin_ok(websocket: WebSocket) -> bool:
        """Allow only same-origin browsers; a cross-site page always sends an
        Origin that won't match. A missing Origin (native BLE/CLI clients that
        aren't browsers) is allowed.

        The configured public hosts count as same-origin: reached over a LAN
        name, the ride page's own Origin *is* that name, so an allowlist of
        just localhost would refuse the very page this server served. The
        names come from the same validated setting that feeds the Host
        allowlist, so this cannot be widened independently of that.
        """
        origin = websocket.headers.get("origin")
        if not origin:
            return True
        try:
            host = _url.urlparse(origin).hostname
        except ValueError:
            return False
        if host in _ALLOWED_WS_ORIGIN_HOSTS:
            return True
        return any(
            host == IPv6TrustedHostMiddleware._host_only(public)
            for public in config.public_hosts()
        )

    def _selected_ble_addresses(params) -> Optional[dict]:
        """Parse an explicit, bounded sensor selection from WS query params."""
        if params.get("selected") != "1":
            return None

        power = params.getlist("power")
        hr = params.getlist("hr")
        trainer = params.getlist("trainer")
        cadence = params.getlist("cadence")
        if len(power) > _MAX_SELECTED_POWER_SOURCES:
            raise ValueError(
                f"Select at most {_MAX_SELECTED_POWER_SOURCES} power sensors."
            )
        if len(hr) > 1 or len(trainer) > 1 or len(cadence) > 1:
            raise ValueError(
                "Select at most one heart-rate monitor, one trainer, and one cadence sensor."
            )

        selected = {"power": [], "hr": [], "trainer": [], "cadence": []}
        for role, addresses in (
            ("power", power), ("hr", hr), ("trainer", trainer),
            ("cadence", cadence),
        ):
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

    def _connection_has_power(conn: Optional[dict]) -> bool:
        """Do not mistake the cadence-only legacy power alias for watts."""
        conn = conn or {}
        power = conn.get("power_source")
        return power is not None and power is not conn.get("cadence_source")

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
        except ConnectorUnavailable:
            # Transport failure — do not report as ERG-disabled. Let the
            # per-tick caller distinguish "we could not ask" from "the trainer
            # said no" so it skips this tick rather than counting a failure.
            raise
        except Exception as exc:
            available, current = _connection_erg_state(conn)
            return available, False if enabled else current, str(exc)
        available, actual = _connection_erg_state(conn)
        # Generic trainers may not expose state; successful commands are the
        # authoritative result for those backwards-compatible implementations.
        if not hasattr(trainer, "erg_enabled"):
            actual = enabled
        return available, actual, None

    def _connector_live(conn: Optional[dict]) -> bool:
        """Is the machine holding the radio still reachable?

        Always true in local mode, where the radio is this process's own.
        """
        if not isinstance(conn, remote_ble.RemoteConnection):
            return True
        return conn.live_session is not None

    async def _resume_after_offline(
        websocket: WebSocket,
        conn: "remote_ble.RemoteConnection",
        controller: RideController,
        offline_s: float,
    ) -> bool:
        """Take the ride back over, replaying the seconds we were not here for.

        The connector kept sampling into its own buffer throughout, so the
        missing seconds exist - they just have not been through the state
        machine. Ticking them in one at a time is what makes the saved
        activity identical to one that never dropped: elapsed advances,
        pause and resume land where the rider actually stopped and started,
        and the samples go in in order rather than leaving a hole.

        Returns False when there is no ride left to carry on with, because the
        connector ended it while we were away.
        """
        still_riding = True
        try:
            rows, still_riding = await remote_ble.resume_ride(conn)
        except Exception as exc:
            # Do not fail the ride over a failed catch-up. Riding on with a
            # gap is strictly better than ending a workout the rider is still
            # in the middle of.
            _log.warning("could not replay the missed ride samples: %s", exc)
            rows = []
        for power, cadence, hr in rows:
            controller.tick(
                power=int(power or 0), cadence=cadence, hr=hr, dt=1
            )
        await websocket.send_json({
            "status": "connector_resumed",
            "offline_s": round(offline_s, 1),
            "replayed": len(rows),
            "riding": still_riding,
            "message": (
                f"Connector back after {int(offline_s)}s"
                + (f"; recovered {len(rows)} seconds of riding." if rows
                   else ".")
            ),
        })
        return still_riding

    def _connector_is_buffering(conn: Optional[dict]) -> bool:
        """Has the connector demonstrably been recording this ride?

        Every sample it sends carries its index in its own buffer, so an index
        having arrived is proof there is a file on the far end holding the
        ride. If none ever did - the buffer failed to open, or it is an older
        connector - then handing it the record would hand it to nobody.
        """
        return (
            isinstance(conn, remote_ble.RemoteConnection)
            and conn.sink.index is not None
        )

    async def _defer_ride_to_connector(
        websocket: WebSocket, controller: RideController,
        conn: Optional[dict], offline_s: float,
    ) -> bool:
        """End a ride whose connector is not here, and save nothing.

        The connector still holds the whole ride - every second of it,
        including the ones this end never saw - and uploads it the moment it
        can reach us again. Writing our own truncated copy first would not
        merely be worse data: the dedup hash is over (start, duration), so the
        short row and the complete one differ and both land, leaving one ride
        stored as two activities.

        Every way a ride can end while the connector is away goes through
        here - the wait timing out, the rider pressing stop, and the connector
        having ended the ride itself - because they would all otherwise
        produce that second row.

        Returns whether the record was handed over, which callers keep: it is
        what stops the ``finally`` from telling a connector that reconnects in
        the meantime to throw the buffer away. That would lose the ride
        outright - and so, for the same reason, would deferring to a connector
        that turns out not to be buffering at all.
        """
        if not _connector_is_buffering(conn):
            _log.warning(
                "the connector never reported buffering this ride; saving "
                "what reached us rather than deferring to a file that may "
                "not exist"
            )
            await websocket.send_json({
                "status": "connector_lost",
                "offline_s": round(offline_s, 1),
                "buffered": False,
                "message": (
                    "The connector is not reachable and never reported "
                    "recording locally. The ride was saved here, as far as it "
                    "got."
                ),
            })
            return False
        controller.autosave = False
        _log.warning(
            "ending a ride with no connector attached (away %.0fs); it will "
            "arrive as a buffered upload when the connector reconnects",
            offline_s,
        )
        await websocket.send_json({
            "status": "connector_lost",
            "offline_s": round(offline_s, 1),
            "buffered": True,
            "message": (
                "The connector is not reachable. The ride was recorded on "
                "your PC and will appear in Activities once it reconnects."
            ),
        })
        return True

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
        # Revocation has to be enforced here, not upstream. AuthMiddleware
        # terminates a revoked connector session on every HTTP request, but it
        # is a BaseHTTPMiddleware and never runs for a websocket scope, so
        # without this line a revoked laptop keeps a working ride socket after
        # the browser half has been cut off - and drives whichever connector is
        # attached now, because _ble_session resolves by user_id alone. The
        # session cannot be cleared from here the way the middleware clears it
        # (there is no response to carry a new cookie), so the socket simply
        # refuses; the next HTTP request is what empties the cookie.
        if not _connector_session_still_paired(websocket):
            _log.info("a ride socket was refused because its device was revoked")
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
                variant=params.get("variant"),
            )
        except (ValueError, OverflowError) as e:
            await websocket.send_json({"status": "error", "error": str(e)})
            await websocket.close()
            return
        ftp = importer.current_ftp(uid)
        workout_payload = _ride_workout_payload(session, ftp, ride_name)
        await websocket.send_json({"status": "workout", "workout": workout_payload})
        available, reason = (False, "") if sim else await _ble_available(uid)

        # Without a simulation request and without hardware, report the
        # unavailable state (page still works) and close cleanly.
        if not sim and not available:
            await websocket.send_json(
                {
                    "status": "unavailable",
                    "ble_available": available,
                    "reason": reason,
                    "message": (
                        "Bluetooth riding needs a connector running on the "
                        "machine with your trainer. Use Simulate to preview "
                        "the live screen."
                        if backend_mode() == "server" else
                        "Bluetooth riding needs an adapter and `pip install "
                        ".[ble]`. Use Simulate to preview the live screen."
                    ),
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
            # True once the ride's record has been handed to the connector's
            # buffer. Keeps the cleanup below from telling it to discard the
            # only copy.
            deferred = False
            try:
                try:
                    ble_session = _ble_session(uid)
                    if ble_session is not None:
                        conn = await remote_ble.connect_sensors(
                            ble_session,
                            selected=selected,
                            # So a ride survives losing us mid-way: the
                            # connector keeps recording against this identity
                            # and uploads it once it can reach us again.
                            ride={
                                "started_at": utc_now().isoformat(),
                                "name": session.name,
                                "ftp": float(ftp),
                                "workout_id": selected_workout_id,
                            },
                            # By user, not the session object we connected
                            # through: a connector that drops and comes back
                            # is a *new* session, and a ride that outlives the
                            # socket has to find the new one.
                            resolve_session=lambda: connectorhub.get(uid),
                        )
                    elif selected is None:
                        conn = await bledevices.connect_sensors()
                    else:
                        conn = await bledevices.connect_sensors(selected=selected)
                except Exception as e:  # no adapter, scan failure, ...
                    await websocket.send_json({"status": "error", "error": str(e)})
                    return
                if not _connection_has_power(conn) and not conn["trainer"]:
                    details = " ".join(
                        _rider_facing_sensor_warnings(conn, _scanned_names_for(uid))
                    )
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
                     # Which selected sensors did not bind, in rider language.
                     # The raw BLE reason stays in the log for debugging.
                     "warnings": _rider_facing_sensor_warnings(
                         conn, _scanned_names_for(uid)
                     ),
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
                            if isinstance(conn, remote_ble.RemoteConnection):
                                await remote_ble.disconnect_sensor(conn, address)
                            else:
                                await bledevices.disconnect_sensor(conn, address)
                            if controller is not None:
                                controller.update_sources(
                                    trainer=conn.get("trainer"),
                                    power_source=conn.get("power_source"),
                                    cadence_source=conn.get("cadence_source"),
                                    hr_source=conn.get("hr_source"),
                                )
                            ending_session = not (
                                _connection_has_power(conn) or conn.get("trainer")
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
                    if action == "adjust_intensity":
                        delta = message.get("delta")
                        if type(delta) is not int or not (-50 <= delta <= 50):
                            await websocket.send_json(
                                {
                                    "status": "error",
                                    "error": "Invalid intensity delta.",
                                }
                            )
                            return None
                        # A ramp test refuses the nudge outright (the protocol
                        # IS the measurement). Say so, so the page can explain
                        # the dead key instead of showing a stuck +0%.
                        locked = bool(getattr(controller, "intensity_locked", False))
                        bias = controller.adjust_intensity_bias(delta)
                        # Answer at once: the badge should track the key, not
                        # wait for the next 1 Hz state frame. The trainer
                        # setpoint follows on the next poll tick.
                        reply = {"status": "intensity", "bias": bias}
                        if locked:
                            reply["locked"] = True
                        await websocket.send_json(reply)
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
                    cadence_source=conn.get("cadence_source"),
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
                offline_s = 0.0
                resumed = False
                revoked = False
                while controller.status != "finished":
                    # Re-asked every tick, not just at the handshake. See
                    # _end_revoked_ride: without this a socket opened one
                    # second before Revoke keeps riding, and keeps driving
                    # whichever connector is attached now. ~1 Hz is the
                    # cadence this loop already runs at, and the lookup is
                    # skipped entirely for a password session.
                    if not _connector_session_still_paired(websocket):
                        _log.info(
                            "a live ride socket was closed because its device "
                            "was revoked"
                        )
                        revoked = True
                        break
                    tick_started = _ride_loop_time()
                    poll_now = tick_started
                    if action_queue is not None:
                        while not action_queue.empty():
                            outcome = await _handle_action(action_queue.get_nowait())
                            if outcome == "stop":
                                if not _connector_live(conn):
                                    deferred = await _defer_ride_to_connector(
                                        websocket, controller, conn, offline_s
                                    )
                                controller.stop()
                                break
                    if controller.status == "finished":
                        break

                    # The connector is the radio. Losing it is not the rider
                    # stopping, so the controller is *frozen* rather than
                    # polled: ticking it against a silent sink would fabricate
                    # zero-power seconds, pause the workout and start the
                    # inactivity clock on a rider who is still pedalling. The
                    # real seconds are on the connector's disk and get replayed
                    # when it comes back.
                    if not _connector_live(conn):
                        if offline_s == 0.0:
                            _log.warning(
                                "connector went away mid-ride for user %s; "
                                "holding the ride open", uid,
                            )
                            await websocket.send_json({
                                "status": "connector_offline",
                                "message": (
                                    "Lost the connector. Your PC is still "
                                    "recording and the trainer is holding its "
                                    "target - reconnecting."
                                ),
                            })
                        offline_s += RIDE_POLL_INTERVAL_S
                        if offline_s >= CONNECTOR_OFFLINE_TIMEOUT_S:
                            deferred = await _defer_ride_to_connector(
                                websocket, controller, conn, offline_s
                            )
                            controller.stop()
                            break
                        await websocket.send_json({
                            **controller.state(),
                            "connector_offline": True,
                            "offline_s": round(offline_s, 1),
                        })
                        await _ride_sleep(
                            max(0.0,
                                RIDE_POLL_INTERVAL_S
                                - (_ride_loop_time() - tick_started))
                        )
                        continue

                    if offline_s:
                        still_riding = await _resume_after_offline(
                            websocket, conn, controller, offline_s
                        )
                        if not still_riding:
                            # The connector gave up on this ride before we
                            # came back - the rider stopped for long enough
                            # with nobody driving. Its buffer is the record,
                            # exactly as when we are the ones who give up.
                            deferred = await _defer_ride_to_connector(
                                websocket, controller, conn, offline_s
                            )
                            controller.stop()
                            break
                        offline_s = 0.0
                        # The trainer has been holding one target throughout,
                        # and may well have dropped out of ERG when the FTMS
                        # writes stopped. Re-arm rather than nudge.
                        # The replay advanced the controller using recorded
                        # one-second samples; start a fresh live-clock interval
                        # so the outage is not counted a second time.
                        poll_now = _ride_loop_time()
                        controller.sync_poll_clock(poll_now)
                        resumed = True

                    previous_status = controller.status
                    controller.poll(
                        now=poll_now,
                        minimum_dt=1.0,
                        # One late tick is ordinary; a gap of more than
                        # two cadences is a stall nobody measured.
                        maximum_dt=2.0 * RIDE_POLL_INTERVAL_S,
                    )
                    if (
                        controller.erg_enabled
                        and controller.status in
                        ("running", "cooldown", "finished")
                    ):
                        try:
                            (
                                command_available,
                                command_enabled,
                                command_error,
                            ) = await _set_connection_erg(
                                conn,
                                True,
                                controller.current_target,
                                force_rearm=(
                                    erg_failures > 0
                                    or resumed
                                    or (previous_status == "paused"
                                        and controller.status == "running")
                                ),
                            )
                        except ConnectorUnavailable:
                            # Transport failure — skip this tick's ERG command
                            # so the next iteration's _connector_live check
                            # transitions to the freeze path. Do not count as
                            # a trainer refusal and do not touch erg_enabled.
                            resumed = False
                        else:
                            resumed = False
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
                if revoked:
                    # No closing state frame: a revoked socket gets the
                    # refusal and nothing else. The finally block below
                    # releases the radio and closes.
                    await _end_revoked_ride(websocket, controller)
                else:
                    final_state = controller.state()
                    # The one place a ramp test's result can be delivered: the
                    # rider is still on the bike and the number is about to
                    # decide every workout they are prescribed from here on.
                    # It is only ever OFFERED - see the accept route, which is
                    # the only thing that writes.
                    ramp_result = _ramp_test_result(controller)
                    if ramp_result is not None:
                        final_state["ramp_test"] = ramp_result
                    await websocket.send_json(final_state)
            except BaseException as exc:
                abnormal_cleanup = True
                # Client closed the socket or BLE failed mid-ride: stop cleanly
                # once a ride actually started. An idle controller must not
                # create a zero-duration activity.
                try:
                    if controller is not None and not _connector_live(conn):
                        # Same reasoning as _defer_ride_to_connector: the
                        # connector holds a complete copy and will upload it,
                        # and a truncated row here would land beside it rather
                        # than dedupe against it. No frame is sent - the socket
                        # is the thing that just failed.
                        if _connector_is_buffering(conn):
                            controller.autosave = False
                            deferred = True
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
                if isinstance(conn, remote_ble.RemoteConnection):
                    # The radio is on the other machine, so releasing it is an
                    # explicit request rather than a local disconnect. The ride
                    # page reopens after a short delay expecting a free
                    # adapter, and with a network hop in the middle that delay
                    # is no longer enough on its own - so ask, and wait.
                    #
                    # Reaching the connector here is also what tells it the
                    # ride ended cleanly, so it can drop its buffer. If it is
                    # unreachable this call simply fails, the buffer survives,
                    # and the ride arrives as an upload instead - which is
                    # exactly the outcome that case wants.
                    live = conn.live_session
                    if live is not None:
                        try:
                            live.ble_sink = None
                            await live.call(
                                "ble.release", {"discard_buffer": not deferred}
                            )
                        except BaseException:
                            pass
                try:
                    await websocket.close()
                except BaseException:
                    pass
            return

        # Simulated ride: pedal at a steady wattage, compressing time so the
        # whole session streams quickly. (Real hardware uses monotonic loop
        # intervals; connector replay uses one-second recorded samples.)
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
                # The same per-tick revocation check the hardware loop makes,
                # and it has to be here too: /ride/ws?sim=1 is reachable on the
                # same session, streams from the same route, and is what the
                # reviewer used to receive frames after a successful revoke.
                if not _connector_session_still_paired(websocket):
                    _log.info(
                        "a live ride socket was closed because its device "
                        "was revoked"
                    )
                    await _end_revoked_ride(websocket, controller)
                    return
                controller.tick(power=pedal, dt=step_dt)
                await websocket.send_json(controller.state())
                frames += 1
                await asyncio.sleep(0.01)
            if controller.status != "finished":
                controller.stop()
            # No ramp-test result here, deliberately. Simulate is a preview of
            # the ride screen at a fixed wattage; a number produced by nobody
            # pedalling must never be put in front of the rider as their FTP.
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

    @app.post("/api/plan/workout/{workout_id}/completion")
    @app.post("/api/plan/workout/{workout_id}/complete")
    def api_plan_workout_complete(
        request: Request, workout_id: int, completed: Optional[bool] = Body(None, embed=True)
    ):
        """Manually link the best same-day, unused activity to a workout.

        Repeating the action for an already completed workout is an idempotent
        success and returns its existing activity link.
        """
        uid = _uid(request)
        if completed is False:
            result = db.set_plan_workout_completion(uid, workout_id, False)
            if result == "not_found":
                return JSONResponse({"error": "workout not found"}, status_code=404)
            workout = db.get_plan_workout(uid, workout_id)
            return JSONResponse({
                "id": workout_id,
                "status": "incomplete",
                "completed": False,
                "activity_id": workout.get("completed_activity_id") if workout else None,
            })
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
                "completed": bool(workout and workout.get("completed_activity_id")),
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

        completed = w.get("completed_activity_id") is not None
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
                "prev_name": w.get("prev_name"),
                "prev_type": w.get("prev_type"),
                "prev_duration_s": w.get("prev_duration_s"),
                "prev_tss": w.get("prev_tss"),
                "rpe": w.get("rpe"),
                "too_hard": w.get("rpe") == 10,
                "ftp_feedback_applied": bool(w.get("feedback_applied")),
                "completed": completed,
                "completion_verified": completion_verified,
                "rpe_eligible": completion_verified,
                "can_mark_complete": (
                    not completed
                    and w["date"] <= utc_today().isoformat()
                ),
                "can_toggle_completion": (
                    completed
                    or w["date"] <= utc_today().isoformat()
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
