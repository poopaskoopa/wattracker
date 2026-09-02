"""Azure Functions entry point for the external budget callback."""
from __future__ import annotations

import os

import azure.functions as func

from wattracker.cloud.budget_hook import create_budget_hook_app
from wattracker.cloud.security import AzureTableSecurityStateBackend


def _required(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


backend = AzureTableSecurityStateBackend.from_managed_identity(
    _required("WATTRACKER_STORAGE_ACCOUNT_NAME"), table_name="CloudControl"
)
app = func.AsgiFunctionApp(
    app=create_budget_hook_app(
        backend,
        expected_token=_required("WATTRACKER_BUDGET_HOOK_TOKEN"),
        platform_authenticated=True,
    ),
    http_auth_level=func.AuthLevel.FUNCTION,
)
