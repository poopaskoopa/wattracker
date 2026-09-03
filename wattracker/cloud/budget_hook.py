"""Authenticated HTTP hooks for durable budget kill-switch actions."""
from __future__ import annotations

import hmac

from fastapi import FastAPI, HTTPException, Request

from .limits import clear_kill_switch, disable_public_api, disable_writes


_TOKEN_HEADER = "X-Wattracker-Budget-Token"


def _safe_compare(left: str, right: str) -> bool:
    try:
        return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))
    except (AttributeError, TypeError, ValueError, UnicodeEncodeError):
        return False


def create_budget_hook_app(
    backend: object,
    *,
    expected_token: str,
    platform_authenticated: bool = False,
) -> FastAPI:
    """Create the externally-hosted budget callback application.

    ``platform_authenticated`` is a deployment seam, not a request input.  It
    is enabled only by the Azure Functions entry point, whose host enforces
    ``AuthLevel.FUNCTION`` before dispatching to ASGI.  The default remains an
    app-level header check for direct hosts and tests.  In particular, the
    ``code`` query parameter is never compared with ``expected_token``: in a
    Functions deployment it is the host key, not this app's token.
    """
    if not isinstance(expected_token, str) or not expected_token:
        raise ValueError("expected_token is required")
    if not isinstance(platform_authenticated, bool):
        raise ValueError("platform_authenticated must be a boolean")

    app = FastAPI(title="Wattracker budget hook", docs_url=None, redoc_url=None)

    def authenticate_header(request: Request) -> None:
        header_token = request.headers.get(_TOKEN_HEADER)
        if header_token is None or not _safe_compare(header_token, expected_token):
            raise HTTPException(status_code=401, detail="unauthorized")

    def authenticate(request: Request) -> None:
        if not platform_authenticated:
            authenticate_header(request)

    async def apply(request: Request, action) -> dict[str, str]:
        authenticate(request)
        try:
            action(backend)
        except Exception as exc:
            raise HTTPException(status_code=503, detail="budget hook unavailable") from exc
        return {"status": "ok"}

    @app.post("/budget/disable-writes")
    async def disable_writes_hook(request: Request) -> dict[str, str]:
        return await apply(
            request, lambda durable_backend: disable_writes(
                durable_backend, reason="budget 80%"
            )
        )

    @app.post("/budget/disable-public-api")
    async def disable_public_api_hook(request: Request) -> dict[str, str]:
        return await apply(
            request, lambda durable_backend: disable_public_api(
                durable_backend, reason="budget 100%"
            )
        )

    @app.post("/budget/clear")
    async def clear_hook(request: Request) -> dict[str, str]:
        # Clearing is deliberately never covered by the platform-authenticated
        # seam.  A leaked Functions callback URL contains only the host key and
        # must not become an operator credential.
        authenticate_header(request)
        try:
            clear_kill_switch(backend, reason="operator clear")
        except Exception as exc:
            raise HTTPException(status_code=503, detail="budget hook unavailable") from exc
        return {"status": "ok"}

    return app
