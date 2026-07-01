"""FastAPI application: auth + per-user dashboard, activities, generate, settings."""
from __future__ import annotations

import datetime as _dt
import os
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware

from . import auth, config, db, paths
from .analysis import pipeline
from .ingest import importer
from .prescribe import zwo
from .prescribe import llm
from .prescribe.planner import plan_workout

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

    @app.get("/generate", response_class=HTMLResponse)
    def generate_form(request: Request):
        return templates.TemplateResponse(
            request,
            "generate.html",
            _ctx(request, session=None, error=None, duration=60),
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
            _ctx(request, session=session_dict, error=error, duration=duration_min),
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
            _ctx(request, session=None, error=None, exported_path=path, duration=60),
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

    # --------------------------------------------------------- JSON API
    @app.get("/api/state")
    def api_state(request: Request):
        return JSONResponse(pipeline.build_state(_uid(request)).to_dict())

    @app.get("/api/load")
    def api_load(request: Request):
        return JSONResponse(pipeline.load_series(_uid(request)))

    @app.get("/api/curve")
    def api_curve(request: Request):
        return JSONResponse(pipeline.curve_points(_uid(request)))

    @app.get("/api/activities")
    def api_activities(request: Request):
        return JSONResponse(db.list_activities(_uid(request)))

    @app.get("/api/ftp")
    def api_ftp(request: Request):
        return JSONResponse(db.ftp_history_list(_uid(request)))

    return app


app = create_app()
