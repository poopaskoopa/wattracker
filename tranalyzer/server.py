"""FastAPI application: auth + per-user dashboard, activities, generate, settings."""
from __future__ import annotations

import asyncio
import calendar as _cal
import datetime as _dt
import io
import logging
import os
import threading
import urllib.parse as _url
import zipfile
from contextlib import asynccontextmanager
from typing import List, Optional

from fastapi import Body, FastAPI, Form, Request, UploadFile, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware

from . import auth, config, credstore, db, exporter, paths, races
from .analysis import pipeline
from .ble import devices as bledevices
from .ble.runner import RideController, flatten_session
from .ingest import importer
from .prescribe import adapt as adaptmod
from .prescribe import plan as planmod
from .prescribe import zwo
from .prescribe import llm
from .prescribe.planner import build_workout, plan_workout

DAY_LABELS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

# Real-hardware ride loop cadence (seconds); module-level so tests can shrink it.
RIDE_POLL_INTERVAL_S = 1.0

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

# Paths served without authentication.
_EXEMPT = ("/login", "/register")
_EXEMPT_PREFIXES = ("/static", "/docs", "/openapi", "/redoc", "/favicon")


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

    app = FastAPI(title="TRanalyzer", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")
    # Per-user cache of the last generated .zwo (avoids cross-user bleed).
    app.state.last = {}

    # SessionMiddleware must be OUTER (added last) so request.session is
    # populated before AuthMiddleware runs.
    app.add_middleware(AuthMiddleware)
    app.add_middleware(
        SessionMiddleware,
        secret_key=config.session_secret(),
        session_cookie="tranalyzer_session",
        same_site="lax",
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
        user = db.get_user_by_username((username or "").strip())
        if not user or not auth.verify_password(password, user["password_hash"]):
            return templates.TemplateResponse(
                request,
                "login.html",
                {"request": request, "error": "Invalid username or password."},
            )
        request.session["user_id"] = user["id"]
        request.session["username"] = user["username"]
        return RedirectResponse("/", status_code=303)

    @app.get("/logout")
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
        detail = pipeline.activity_detail(_uid(request), activity_id)
        if not detail:
            return JSONResponse({"error": "not found"}, status_code=404)
        return JSONResponse(detail)

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
        content = await file.read()
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
    def plan_page(request: Request):
        return templates.TemplateResponse(
            request, "plan.html",
            _generate_ctx(request, flash=request.query_params.get("flash")),
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
            app.state.last[uid] = (zwo_str, session.name)
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
    def generate_export(request: Request):
        uid = _uid(request)
        last = app.state.last.get(uid)
        if not last:
            return RedirectResponse(url="/plan", status_code=303)
        zwo_str, name = last
        settings = db.get_user_settings(uid)
        path = zwo.write_to_zwift(
            zwo_str,
            settings.get("zwift_id") or "me",
            name=name or "TRanalyzer_Workout",
            workouts_override=settings.get("workouts_dir"),
        )
        return templates.TemplateResponse(
            request,
            "plan.html",
            _generate_ctx(request, mode="workout", exported_path=path),
        )

    @app.get("/generate/download")
    def generate_download(request: Request):
        last = app.state.last.get(_uid(request))
        if not last:
            return RedirectResponse(url="/plan", status_code=303)
        zwo_str, name = last
        fname = (name or "workout").replace(" ", "_")
        return Response(
            content=zwo_str,
            media_type="application/xml",
            headers={"Content-Disposition": f'attachment; filename="{fname}.zwo"'},
        )

    def _settings_ctx(request: Request, uid: int, saved: bool,
                      cred_message: Optional[str] = None) -> dict:
        settings = db.get_user_settings(uid)
        return _ctx(
            request,
            settings=settings,
            current_ftp=round(importer.current_ftp(uid), 1),
            api_key_set=config.anthropic_api_key_set(),
            saved=saved,
            zwift_candidates=paths.candidate_zwift_ids(),
            watch_default=paths.activities_dir(),
            zwift_creds_saved=credstore.credentials_saved(uid),
            zwift_cred_backend=credstore.storage_backend(),
            cred_message=cred_message,
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
        db.save_user_settings(
            uid,
            {
                "ftp": ftp,
                "zwift_id": chosen_zwift_id,
                "activities_dir": activities_dir,
                "workouts_dir": workouts_dir,
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
            backend = credstore.save_zwift_credentials(
                uid, zwift_email, zwift_password
            )
            db.clear_race_auth_failure(uid)
            cred_message = f"Zwift credentials saved ({backend})."
        elif zwift_email.strip() or zwift_password:
            cred_message = ("Zwift credentials NOT saved - both email and "
                            "password are needed.")
        return templates.TemplateResponse(
            request, "settings.html",
            _settings_ctx(request, uid, True, cred_message=cred_message),
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
        except Exception as e:  # never let a refresh kill the page
            _log.warning("race refresh failed", exc_info=True)
            refreshed = {"source": None, "count": 0, "error": str(e)}
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

    def _ride_session(uid, workout_id=None, wtype=None, minutes=None):
        """Build the Session to ride, from a plan workout or an ad-hoc type/duration."""
        if workout_id:
            w = db.get_plan_workout(uid, int(workout_id))
            if w:
                return build_workout(w["type"], max(1, w["duration_s"] / 60),
                                     w.get("variant")), w["name"]
        if wtype:
            try:
                mins = float(minutes) if minutes else 45
            except (TypeError, ValueError):
                mins = 45
            try:
                s = build_workout(wtype, max(20, mins))
                return s, s.name
            except ValueError:
                pass
        s = build_workout("endurance", 45)
        return s, s.name

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
                ftp=round(importer.current_ftp(uid), 0),
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

    @app.websocket("/ride/ws")
    async def ride_ws(websocket: WebSocket):
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
        session, _name = _ride_session(
            uid,
            workout_id=params.get("workout_id"),
            wtype=params.get("type"),
            minutes=params.get("minutes"),
        )
        ftp = importer.current_ftp(uid)
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
            try:
                try:
                    conn = await bledevices.connect_sensors()
                except Exception as e:  # no adapter, scan failure, ...
                    await websocket.send_json({"status": "error", "error": str(e)})
                    return
                if not conn["power_source"] and not conn["trainer"]:
                    await websocket.send_json(
                        {
                            "status": "error",
                            "error": "No power meter or FTMS trainer found. "
                            "Scan first, or use Simulate.",
                        }
                    )
                    return
                controller = RideController(
                    session,
                    ftp,
                    trainer=conn["trainer"],
                    power_source=conn["power_source"],
                    hr_source=conn["hr_source"],
                    user_id=uid,
                    autosave=True,
                )
                await websocket.send_json(
                    {"status": "connected", "devices": conn["names"],
                     "erg": conn["trainer"] is not None}
                )
                while controller.status != "finished":
                    controller.poll(dt=1)
                    await websocket.send_json(controller.state())
                    await asyncio.sleep(RIDE_POLL_INTERVAL_S)
                await websocket.send_json(controller.state())
            except Exception:
                # Client closed the socket or BLE failed mid-ride: stop cleanly
                # (stop() zeroes the ERG target and saves the ride).
                try:
                    if conn is not None and "controller" in locals():
                        controller.stop()
                except Exception:
                    pass
            finally:
                for client in (conn or {}).get("clients", []):
                    try:
                        await client.disconnect()
                    except Exception:
                        pass
                try:
                    await websocket.close()
                except Exception:
                    pass
            return

        # Simulated ride: pedal at a steady wattage, compressing time so the
        # whole session streams quickly. (Real hardware ticks at dt=1 in real
        # time from a connected power source.)
        trainer = bledevices.SimulatedTrainer()
        controller = RideController(
            session, ftp, trainer=trainer, user_id=uid, autosave=True
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
                "completed": w.get("completed_activity_id") is not None,
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
        if w.get("completed_activity_id") is None:
            return JSONResponse(
                {"error": "only completed workouts can be graded"}, status_code=400
            )
        db.set_plan_workout_rpe(uid, workout_id, rpe_val)
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
