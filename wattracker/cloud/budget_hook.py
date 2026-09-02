"""Authenticated HTTP hooks for durable budget kill-switch actions."""
from __future__ import annotations

import hmac

from fastapi import FastAPI, HTTPException, Request

from .limits import disable_public_api, disable_writes


_TOKEN_HEADER = "X-Wattracker-Budget-Token"


def _safe_compare(left: str, right: str) -> bool:
    try:
        return hmac.compare_digest(left, right)
    except (TypeError, ValueError, UnicodeEncodeError):
        return False


def create_budget_hook_app(backend: object, *, expected_token: str) -> FastAPI:
    """Create the small externally-hosted budget callback application."""
    if not isinstance(expected_token, str) or not expected_token:
        raise ValueError("expected_token is required")

    app = FastAPI(title="Wattracker budget hook", docs_url=None, redoc_url=None)

    def authenticate(request: Request) -> None:
        query_token = request.query_params.get("code")
        header_token = request.headers.get(_TOKEN_HEADER)
        if (
            query_token is not None
            and header_token is not None
            and not _safe_compare(query_token, header_token)
        ):
            raise HTTPException(status_code=401, detail="unauthorized")
        supplied = query_token if query_token is not None else header_token
        if supplied is None or not _safe_compare(supplied, expected_token):
            raise HTTPException(status_code=401, detail="unauthorized")

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

    return app
