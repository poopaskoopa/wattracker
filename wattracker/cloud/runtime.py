"""Container entry point for the isolated Azure read and sync services.

The ordinary desktop process never imports this module.  Container images run
it explicitly after deployment injects the server secret, operator token, and
storage account name through managed configuration.
"""
from __future__ import annotations

import base64
import os
from typing import Iterable

from .api import CloudConfig, CloudState, create_cloud_app
from .security import AzureTableSecurityStateBackend
from .storage import AzureTenantStore


def _required(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def _server_secret() -> bytes:
    encoded = _required("WATTRACKER_CLOUD_SERVER_SECRET")
    try:
        secret = base64.b64decode(encoded.encode("ascii"), validate=True)
    except (UnicodeEncodeError, ValueError):
        secret = b""
    if len(secret) < 32:
        raise RuntimeError("WATTRACKER_CLOUD_SERVER_SECRET must be base64 256-bit material")
    return secret


def _origins() -> tuple[str, ...]:
    values: Iterable[str] = (
        value.strip()
        for value in os.environ.get("WATTRACKER_ALLOWED_ORIGINS", "").split(",")
    )
    return tuple(value for value in values if value)


def create_runtime_app():
    """Build the production app with persistent Azure-backed object storage."""
    config = CloudConfig(
        server_secret=_server_secret(),
        operator_token=_required("WATTRACKER_CLOUD_OPERATOR_TOKEN"),
        plane=os.environ.get("WATTRACKER_CLOUD_PLANE", "read"),
        allowed_origins=_origins(),
        apim_proof_header="X-APIM-Request-Proof",
        apim_proof_value=_required("WATTRACKER_APIM_PROOF_VALUE"),
    )
    account_name = _required("WATTRACKER_STORAGE_ACCOUNT_NAME")
    store = AzureTenantStore.from_managed_identity(account_name)
    security_backend = AzureTableSecurityStateBackend.from_managed_identity(
        account_name
    )
    security_backend.verify_access(writable=config.plane in {"all", "read"})
    state = CloudState.create(
        config,
        store=store,
        security_backend=security_backend,
        require_persistent_security=True,
    )
    return create_cloud_app(config, state=state)


def main() -> None:
    import uvicorn

    uvicorn.run(
        create_runtime_app(),
        host=os.environ.get("WATTRACKER_CLOUD_HOST", "0.0.0.0"),
        port=int(os.environ.get("WATTRACKER_CLOUD_PORT", "8000")),
        access_log=False,
    )


if __name__ == "__main__":
    main()
