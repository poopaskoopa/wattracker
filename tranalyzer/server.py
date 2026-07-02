"""FastAPI application: auth + per-user dashboard, activities, generate, settings."""
from __future__ import annotations

import asyncio
import calendar as _cal
import datetime as _dt
import io
import os
import zipfile
from contextlib import asynccontextmanager
from typing import List, Optional

from fastapi import FastAPI, Form, Request, UploadFile, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware

from . import auth, config, db, paths
from .analysis import pipeline
from .ble import devices as bledevices
from .ble.runner import RideController, flatten_session
from .ingest import importer
from .prescribe import plan as planmod
from .prescribe import zwo
from .prescribe import llm
from .prescribe.planner import build_workout, plan_workout

DAY_LABELS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

# Real-hardware ride loop cadence (seconds); module-level so tests can shrink it.
RIDE_POLL_INTERVAL_S = 1.0


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
        yield

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
        state = pipeline.build_state(_uid(request))
        return templates.TemplateResponse(
            request, "dashboard.html", _ctx(request, state=state.to_dict())
        )

    def _activities_context(request: Request, scan: Optional[dict] = None) -> dict:
        uid = _uid(request)
        settings = db.get_user_settings(uid)
        candidates = paths.annotated_candidates()
        saved = settings.get("activities_dir")
        prefill = saved or (candidates[0]["path"] if candidates else "")
        return _ctx(
            request,
            activities=db.list_activities(uid),
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

    @app.post("/activities/rescan", response_class=HTMLResponse)
    def rescan(request: Request, activities_dir: str = Form("")):
        uid = _uid(request)
        posted = (activities_dir or "").strip()
        # Persist a typed directory as the user's activities_dir setting.
        if posted:
            db.save_user_settings(uid, {"activities_dir": posted})
        result = importer.scan_activities(uid, directory=posted or None)
        directory = result.get("directory")
        scan = {
            "directory": directory,
            "exists": bool(directory and os.path.isdir(directory)),
            "found": result.get("found", 0),
            "imported": result.get("imported", 0),
            "skipped": result.get("skipped", 0),
        }
        return templates.TemplateResponse(
            request, "activities.html", _activities_context(request, scan=scan)
        )

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
        }

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

    @app.get("/generate", response_class=HTMLResponse)
    def generate_form(request: Request):
        return templates.TemplateResponse(
            request, "generate.html", _generate_ctx(request)
        )

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
            "generate.html",
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
        }
        try:
            generated = planmod.generate_plan(
                name, start, weeks, day_ints, hours_per_week, hit_days_per_week,
                hard_days=hard_ints or None,
            )
            plan_id = db.create_plan(
                uid, name or "Training Plan", generated["start_date"], generated["weeks"]
            )
            for w in generated["workouts"]:
                zwo_str = zwo.zwo_string(w["session"])
                db.add_plan_workout(
                    plan_id, uid, w["date"], w["name"], w["type"],
                    w["duration_s"], w["tss"], zwo_str,
                )
            summary = _plan_summary(uid, plan_id)
            summary["polarized_hard_fraction"] = generated["polarized_hard_fraction"]
            summary["weekly"] = generated["weekly"]
        except ValueError as e:
            plan_error = str(e)

        return templates.TemplateResponse(
            request,
            "generate.html",
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
            "generate.html",
            _generate_ctx(request, mode="plan", plan=summary, exported=exported),
        )

    @app.get("/plan/{plan_id}/download.zip")
    def plan_download_zip(request: Request, plan_id: int):
        uid = _uid(request)
        workouts = db.plan_workouts_for_plan(uid, plan_id, include_zwo=True)
        if not workouts:
            return RedirectResponse(url="/generate", status_code=303)
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
            "generate.html",
            _generate_ctx(request, mode="plan", plan=summary, exported=exported),
        )

    @app.get("/plan/workout/{workout_id}/download")
    def plan_workout_download(request: Request, workout_id: int):
        w = db.get_plan_workout(_uid(request), workout_id)
        if not w:
            return RedirectResponse(url="/generate", status_code=303)
        fname = zwo.plan_filename(w["date"], w["name"])
        return Response(
            content=w["zwo_or_segments"],
            media_type="application/xml",
            headers={"Content-Disposition": f'attachment; filename="{fname}"'},
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

        by_date: dict = {}
        for w in db.plan_workouts_for_month(uid, y, m):
            by_date.setdefault(w["date"], []).append(w)

        cal = _cal.Calendar(firstweekday=0)  # Monday
        weeks = []
        for week in cal.monthdatescalendar(y, m):
            row = []
            for d in week:
                row.append(
                    {
                        "date": d.isoformat(),
                        "day": d.day,
                        "in_month": d.month == m,
                        "workouts": by_date.get(d.isoformat(), []),
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
            ),
        )

    @app.post("/generate/export")
    def generate_export(request: Request):
        uid = _uid(request)
        last = app.state.last.get(uid)
        if not last:
            return RedirectResponse(url="/generate", status_code=303)
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
            "generate.html",
            _generate_ctx(request, mode="workout", exported_path=path),
        )

    @app.get("/generate/download")
    def generate_download(request: Request):
        last = app.state.last.get(_uid(request))
        if not last:
            return RedirectResponse(url="/generate", status_code=303)
        zwo_str, name = last
        fname = (name or "workout").replace(" ", "_")
        return Response(
            content=zwo_str,
            media_type="application/xml",
            headers={"Content-Disposition": f'attachment; filename="{fname}.zwo"'},
        )

    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request):
        uid = _uid(request)
        return templates.TemplateResponse(
            request,
            "settings.html",
            _ctx(
                request,
                settings=db.get_user_settings(uid),
                current_ftp=round(importer.current_ftp(uid), 1),
                api_key_set=config.anthropic_api_key_set(),
                saved=False,
            ),
        )

    @app.post("/settings", response_class=HTMLResponse)
    def settings_save(
        request: Request,
        ftp: str = Form(""),
        zwift_id: str = Form(""),
        activities_dir: str = Form(""),
        workouts_dir: str = Form(""),
        anthropic_api_key: str = Form(""),
    ):
        uid = _uid(request)
        db.save_user_settings(
            uid,
            {
                "ftp": ftp,
                "zwift_id": zwift_id,
                "activities_dir": activities_dir,
                "workouts_dir": workouts_dir,
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
        return templates.TemplateResponse(
            request,
            "settings.html",
            _ctx(
                request,
                settings=db.get_user_settings(uid),
                current_ftp=round(importer.current_ftp(uid), 1),
                api_key_set=config.anthropic_api_key_set(),
                saved=True,
            ),
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
                return build_workout(w["type"], max(1, w["duration_s"] / 60)), w["name"]
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
        session = build_workout(w["type"], max(1, w["duration_s"] / 60))

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
                "ftp": round(ftp, 1),
                "description": session.description,
                "total_duration": total_s,
                "segments": segments,
                "profile": profile,
            }
        )

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
