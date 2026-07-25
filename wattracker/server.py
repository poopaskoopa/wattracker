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

from . import auth, backup, config, credstore, db, exporter, paths, races
from .analysis import pipeline, zones
from .ble import devices as bledevices
from .ble.runner import RideController, flatten_session
from .ingest import importer
from .prescribe import adapt as adaptmod
from .prescribe import plan as planmod
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

DAY_LABELS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

# Real-hardware ride loop cadence (seconds); module-level so tests can shrink it.
RIDE_POLL_INTERVAL_S = 1.0
RIDE_INACTIVITY_TIMEOUT_S = 300.0


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
            "started_at": _dt.datetime.now().isoformat(timespec="seconds"),
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
                st["finished_at"] = _dt.datetime.now().isoformat(
                    timespec="seconds"
                )

    threading.Thread(target=_run, daemon=True).start()
    return snapshot


def run_daily_maintenance() -> dict:
    """One synchronous pass of all daily jobs: import scan (FTP re-eval +
    completion matching inside), then per-user plan adaptation and race-result
    refresh. Each stage is fault-isolated per user.
    """
    totals = importer.run_auto_scan()
    totals["adapted"] = 0
    totals["races"] = 0
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
        try:
            state = pipeline.build_state(uid)
            summary = adaptmod.apply_adaptations(uid, state)
            totals["adapted"] += summary.get("adjusted", 0)
        except Exception:
            _log.warning("plan adaptation failed for user %s", uid, exc_info=True)
        try:
            races.refresh_race_results(uid, respect_backoff=True)
            totals["races"] += 1
        except Exception:
            _log.warning("race refresh failed for user %s", uid, exc_info=True)
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
    today = today or _dt.date.today()
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
_EXEMPT = ("/login", "/register")
_EXEMPT_PREFIXES = ("/static", "/favicon")

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
    return kw


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
    # Per-user cache of the last generated .zwo (avoids cross-user bleed).
    app.state.last = {}
    # In-process brute-force throttle for /login (per lowercased username).
    app.state.login_throttle = auth.LoginThrottle()

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
    app.add_middleware(
        IPv6TrustedHostMiddleware,
        allowed_hosts=["localhost", "127.0.0.1", "[::1]", "::1", "testserver"],
        www_redirect=False,
    )

    # -------------------------------------------------------------- auth
    @app.get("/register", response_class=HTMLResponse)
    def register_form(request: Request):
        if _uid(request):
            return RedirectResponse("/", status_code=303)
        return templates.TemplateResponse(
            request, "register.html", {"request": request, "error": None}
        )

    @app.post("/register", response_class=HTMLResponse)
    def register_submit(
        request: Request,
        username: str = Form(...),
        password: str = Form(...),
    ):
        username = (username or "").strip()
        err = auth.validate_credentials(username, password)
        if not err:
            user_id = db.create_user(username, auth.hash_password(password))
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
        uname = (username or "").strip()
        throttle = app.state.login_throttle
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
        if user:
            ok = auth.verify_password(password, user["password_hash"])
        else:
            # Spend the same scrypt time as a real verify so a missing username
            # isn't distinguishable by timing.
            auth.dummy_verify(password)
            ok = False
        if not ok:
            throttle.record_failure(uname)
            return templates.TemplateResponse(
                request,
                "login.html",
                {"request": request, "error": "Invalid username or password."},
            )
        throttle.record_success(uname)
        # Transparent upgrade: re-hash legacy/low-cost hashes at the current cost.
        if auth.needs_rehash(user["password_hash"]):
            try:
                db.set_password_hash(user["username"], auth.hash_password(password))
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
        for a in activities:
            a["duration_fmt"] = races.format_duration(a.get("duration_s"))
        return _ctx(
            request,
            activities=activities,
            scan=scan,
            candidates=candidates,
            saved_dir=saved,
            prefill_dir=prefill,
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
                manual_ftp=settings.get("ftp"),
                manual_hr_max=settings.get("hr_max"),
                error=error,
                saved=request.query_params.get("saved"),
            ),
        )

    @app.get("/profile", response_class=HTMLResponse)
    def profile_page(request: Request):
        return _profile_response(request)

    @app.post("/profile/ftp", response_class=HTMLResponse)
    def profile_ftp_save(
        request: Request,
        ftp: str = Form(""),
        action: str = Form("save"),
    ):
        uid = _uid(request)
        if action == "reset":
            db.set_user_ftp_override(uid, None)
            return RedirectResponse("/profile?saved=ftp", status_code=303)
        try:
            value = int(ftp.strip())
        except (TypeError, ValueError):
            return _profile_response(request, "FTP must be a whole number from 1 to 2000 W.")
        if not 1 <= value <= 2000:
            return _profile_response(request, "FTP must be a whole number from 1 to 2000 W.")
        db.set_user_ftp_override(uid, value)
        return RedirectResponse("/profile?saved=ftp", status_code=303)

    @app.post("/profile/hr-max", response_class=HTMLResponse)
    def profile_hr_max_save(
        request: Request,
        hr_max: str = Form(""),
        action: str = Form("save"),
    ):
        uid = _uid(request)
        if action == "reset":
            db.set_user_hr_max(uid, None)
            return RedirectResponse("/profile?saved=1", status_code=303)
        try:
            value = int(hr_max.strip())
        except (TypeError, ValueError):
            return _profile_response(request, "HRmax must be a whole number from 80 to 230 bpm.")
        if not 80 <= value <= 230:
            return _profile_response(request, "HRmax must be a whole number from 80 to 230 bpm.")
        db.set_user_hr_max(uid, value)
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
        posted = (activities_dir or "").strip()
        # Persist a typed directory as the user's activities_dir setting.
        if posted:
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
        }

    def _plan_management(uid: Optional[int]) -> dict:
        """Summarize the user's plans for the management section.

        current: the plan whose date range (start_date .. +weeks*7d) covers
        today; if none covers today, the most recent plan flagged in_effect=False;
        None only when the user has no plans. others: every other plan, newest
        first. Each entry carries name, model, dates, end_date, and progress
        (completed/total workouts).
        """
        if uid is None:
            return {"current": None, "others": []}
        today = _dt.date.today()
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
            })
        if not entries:
            return {"current": None, "others": []}
        current = next((e for e in entries if e["covers_today"]), None)
        if current is not None:
            current["in_effect"] = True
        else:
            # Most recent plan (list is created DESC) but flag not-in-effect.
            current = entries[0]
            current["in_effect"] = False
        others = [e for e in entries if e["id"] != current["id"]]
        return {"current": current, "others": others}

    def _generate_ctx(request: Request, **kw) -> dict:
        base = dict(
            session=None,
            error=None,
            duration=60,
            mode="workout",
            plan=None,
            plan_error=None,
            plan_defaults=_plan_defaults(),
            day_labels=DAY_LABELS,
            exported=None,
            exported_path=None,
            scheduled_date=_dt.date.today().isoformat(),
            flash=None,
            plan_mgmt=_plan_management(_uid(request)),
        )
        base.update(kw)
        return _ctx(request, **base)

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
            session = plan_workout(state, duration_min)
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
    ):
        uid = _uid(request)
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
            "model": model,
        }
        try:
            generated = planmod.generate_plan(
                name, start, weeks, day_ints, hours_per_week, hit_days_per_week,
                hard_days=hard_ints or None, model=model,
            )
            plan_id = db.create_plan(
                uid, name or "Training Plan", generated["start_date"],
                generated["weeks"], model=generated["model"],
            )
            for w in generated["workouts"]:
                zwo_str = zwo.zwo_string(w["session"])
                db.add_plan_workout(
                    plan_id, uid, w["date"], w["name"], w["type"],
                    w["duration_s"], w["tss"], zwo_str,
                    variant=w.get("variant"),
                )
            # Match any already-imported activities against the new plan's
            # workouts now - the gated rescan path only matches when NEW files
            # import, so a plan created after its rides were imported would
            # otherwise never be marked completed until a future import.
            importer.match_plan_completions(uid)
            summary = _plan_summary(uid, plan_id)
            summary["polarized_hard_fraction"] = generated["polarized_hard_fraction"]
            summary["weekly"] = generated["weekly"]
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

    @app.get("/calendar", response_class=HTMLResponse)
    def calendar_view(
        request: Request,
        year: Optional[int] = None,
        month: Optional[int] = None,
    ):
        uid = _uid(request)
        today = _dt.date.today()
        y = year or today.year
        m = month or today.month
        # Normalise month into 1..12 (defensive).
        if m < 1:
            y, m = y - 1, 12
        elif m > 12:
            y, m = y + 1, 1

        ooto_ranges = db.list_ooto_ranges(uid)

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
                        "workouts": by_date.get(iso, []),
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
                export_result=request.query_params.get("exported"),
            ),
        )

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
                      dir_message: Optional[str] = None) -> dict:
        settings = db.get_user_settings(uid)
        return _ctx(
            request,
            settings=settings,
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
        """Validate a user-supplied folder path.

        Returns (clean_path, error). A folder is accepted only when it exists,
        is a directory, and its real path remains under the user's home,
        OS-discovered Documents/Zwift roots, or a process-owner environment
        override. This admits redirected Windows Known Folders and OneDrive/UNC
        roots without permitting arbitrary web-supplied system paths or symlink
        escapes. Empty means "unchanged" (clean_path="", error=None).
        """
        raw = (value or "").strip()
        if not raw:
            return "", None
        expanded = os.path.realpath(os.path.abspath(os.path.expanduser(raw)))
        if not os.path.isdir(expanded):
            return None, f"Folder not found or not a directory: {raw}"
        allowed = False
        for root in paths.trusted_storage_roots():
            resolved_root = os.path.realpath(os.path.abspath(os.path.expanduser(root)))
            try:
                if os.path.commonpath([expanded, resolved_root]) == resolved_root:
                    allowed = True
                    break
            except ValueError:
                continue  # Different Windows drives or UNC shares.
        if not allowed:
            return None, (
                "Folder must be inside your home directory or a configured "
                f"Zwift data directory: {raw}"
            )
        return expanded, None

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
        db.save_user_settings(
            uid,
            {
                "ftp": ftp,
                "zwift_id": chosen_zwift_id,
                "activities_dir": clean_activities,
                "workouts_dir": clean_workouts,
                "weight_kg": weight_val,
            },
        )
        # A manual FTP entry records a source='manual' row for today (per user).
        if ftp not in (None, ""):
            try:
                watts = float(ftp)
                if watts > 0:
                    db.add_ftp_entry(uid, _dt.date.today().isoformat(), watts, "manual")
            except ValueError:
                pass
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
        today = _dt.date.today().isoformat()
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
        if workout_id:
            w = db.get_plan_workout(uid, int(workout_id))
            if w:
                return build_workout(w["type"], max(1, w["duration_s"] / 60),
                                     w.get("variant")), w["name"], w["id"]
            raise ValueError("workout not found")
        if wtype:
            kind, mins = _validate_just_ride(wtype, minutes)
            s = build_workout(kind, mins)
            return s, s.name, None
        s = build_workout("endurance", 45)
        return s, s.name, None

    def _ride_workout_payload(session, ftp: float, name: Optional[str] = None) -> dict:
        blocks, total_s = flatten_session(session)
        profile = []
        for start, end, kind, value in blocks:
            lo, hi = (value, value) if kind == "const" else value
            profile.append(
                {
                    "start": start,
                    "end": end,
                    "watts_start": int(round(lo * ftp)),
                    "watts_end": int(round(hi * ftp)),
                }
            )
        return {
            "name": name or session.name,
            "duration_s": total_s,
            "ftp": round(ftp, 1),
            "profile": profile,
        }

    def _watts(fraction, ftp: float) -> Optional[int]:
        return None if fraction is None else int(round(float(fraction) * ftp))

    def _preview_segments(session, ftp: float) -> List[dict]:
        """Human-readable per-segment breakdown with watt ranges."""
        rows: List[dict] = []
        for seg in session.segments:
            if seg.kind == "intervals" and seg.repeat:
                on_s = int(seg.on_duration or 0)
                off_s = int(seg.off_duration or 0)
                rows.append({
                    "label": (
                        f"{seg.repeat} x {_fmt_clock(on_s)} on / "
                        f"{_fmt_clock(off_s)} easy"
                    ),
                    "duration_s": seg.duration,
                    "watts_low": _watts(seg.off_power, ftp),
                    "watts_high": _watts(seg.on_power, ftp),
                    "on_watts": _watts(seg.on_power, ftp),
                    "off_watts": _watts(seg.off_power, ftp),
                    "text": seg.text,
                })
                continue
            if seg.kind in ("warmup", "cooldown"):
                lo = _watts(seg.power_low, ftp)
                hi = _watts(seg.power_high, ftp)
                if lo is not None and hi is not None and lo > hi:
                    lo, hi = hi, lo
                label = "Warmup ramp" if seg.kind == "warmup" else "Cooldown"
            else:
                lo = hi = _watts(seg.power, ftp)
                label = "Steady block" if seg.kind == "steadystate" else seg.kind
            rows.append({
                "label": label,
                "duration_s": seg.duration,
                "watts_low": lo,
                "watts_high": hi,
                "on_watts": None,
                "off_watts": None,
                "text": seg.text,
            })
        return rows

    def _fmt_clock(seconds: int) -> str:
        seconds = int(seconds or 0)
        if seconds < 60:
            return f"{seconds}s"
        minutes, secs = divmod(seconds, 60)
        return f"{minutes}min" if not secs else f"{minutes}min {secs}s"

    @app.get("/ride/workout/preview")
    def ride_workout_preview(request: Request, type: str = "", minutes: str = ""):
        uid = _uid(request)
        try:
            kind, mins = _validate_just_ride(type, minutes)
            session = build_workout(kind, mins)
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
            "segments": _preview_segments(session, ftp),
        })
        return JSONResponse(payload)

    @app.get("/ride", response_class=HTMLResponse)
    def ride_page(request: Request):
        uid = _uid(request)
        available, reason = bledevices.bluetooth_available()
        return templates.TemplateResponse(
            request,
            "ride.html",
            _ctx(
                request,
                ble_available=available,
                ble_reason=reason,
                workouts=_upcoming_plan_workouts(uid),
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
                    nonlocal controller
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
                            available_now, enabled_now = _connection_erg_state(conn)
                            await websocket.send_json(
                                {
                                    "status": "device_disconnected",
                                    "address": address,
                                    "devices": conn.get("names", {}),
                                    "erg_available": available_now,
                                    "erg_enabled": enabled_now,
                                    "message": "Device disconnected.",
                                }
                            )
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
                if initial_erg_enabled:
                    (
                        initial_erg_available,
                        initial_erg_enabled,
                        initial_erg_error,
                    ) = await _set_connection_erg(
                        conn, True, controller.current_target
                    )
                    controller.erg_available = initial_erg_available
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
                        and controller.status in ("running", "finished")
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
                                previous_status == "paused"
                                and controller.status == "running"
                            ),
                        )
                        controller.erg_available = command_available
                        controller.set_erg_enabled(
                            command_enabled, command_trainer=False
                        )
                        if command_error:
                            await websocket.send_json(
                                {
                                    "status": "erg",
                                    "available": command_available,
                                    "enabled": False,
                                    "error": command_error,
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
            while controller.status != "finished" and frames < max_frames:
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
    def _watts(frac: Optional[float], ftp: float) -> Optional[int]:
        return int(round(frac * ftp)) if frac is not None else None

    @app.post("/api/plan/workout/{workout_id}/reconcile")
    def api_plan_workout_reconcile(request: Request, workout_id: int):
        """Reconcile one non-future workout against existing activity data."""
        uid = _uid(request)
        workout = db.get_plan_workout(uid, workout_id)
        if not workout:
            return JSONResponse({"error": "workout not found"}, status_code=404)

        today = _dt.date.today()
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

        Segments are reconstructed deterministically via ``build_workout`` (the
        stored type/duration fully determine the session, matching the stored
        .zwo), so no extra persistence is needed.
        """
        uid = _uid(request)
        w = db.get_plan_workout(uid, workout_id)
        if not w:
            return JSONResponse({"error": "workout not found"}, status_code=404)
        completion_verified = importer.plan_workout_completion_verified(uid, w)
        ftp = importer.current_ftp(uid)
        session = build_workout(w["type"], max(1, w["duration_s"] / 60),
                                w.get("variant"))

        segments = []
        for seg in session.segments:
            d = seg.to_dict()
            d["watts"] = _watts(seg.power, ftp)
            d["watts_low"] = _watts(seg.power_low, ftp)
            d["watts_high"] = _watts(seg.power_high, ftp)
            d["watts_on"] = _watts(seg.on_power, ftp)
            d["watts_off"] = _watts(seg.off_power, ftp)
            segments.append(d)

        # Flattened timeline (intervals expanded, ramps kept) for the chart.
        blocks, total_s = flatten_session(session)
        profile = []
        for (s, e, kind, val) in blocks:
            lo, hi = (val, val) if kind == "const" else val
            profile.append(
                {
                    "start": s,
                    "end": e,
                    "watts_start": int(round(lo * ftp)),
                    "watts_end": int(round(hi * ftp)),
                }
            )

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
                    and w["date"] <= _dt.date.today().isoformat()
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
