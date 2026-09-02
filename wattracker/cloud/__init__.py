"""Optional, server-mediated cloud synchronization.

The local Wattracker application does not import this package during normal
startup.  Deployments that want the cloud read/write planes construct the
standalone ASGI app from :mod:`wattracker.cloud.api`.

Exports are resolved lazily.  The Azure budget Function packages only the
cloud control modules, and importing ``wattracker.cloud.budget_hook`` must not
pull the desktop snapshot stack (NumPy, SciPy, or the local database) into its
deployment package.
"""

from importlib import import_module
from typing import Final


_EXPORTS: Final[dict[str, tuple[str, str]]] = {
    "CloudConfig": (".api", "CloudConfig"),
    "CloudState": (".api", "CloudState"),
    "create_cloud_app": (".api", "create_cloud_app"),
    "CloudSyncClient": (".client", "CloudSyncClient"),
    "SyncCredentials": (".client", "SyncCredentials"),
    "SyncResult": (".client", "SyncResult"),
    "https_transport": (".client", "https_transport"),
    "CloudCredentialStore": (".credentials", "CloudCredentialStore"),
    "KeyringBackend": (".credentials", "KeyringBackend"),
    "CloudObject": (".models", "CloudObject"),
    "SyncBatch": (".models", "SyncBatch"),
    "AzureTenantStore": (".storage", "AzureTenantStore"),
    "MemoryTenantStore": (".storage", "MemoryTenantStore"),
    "AzureTableSecurityStateBackend": (
        ".security", "AzureTableSecurityStateBackend"
    ),
    "MemorySecurityStateBackend": (".security", "MemorySecurityStateBackend"),
    "SecurityStateUnavailable": (".security", "SecurityStateUnavailable"),
    "KILL_SWITCH_TTL_SECONDS": (".limits", "KILL_SWITCH_TTL_SECONDS"),
    "KillSwitchState": (".limits", "KillSwitchState"),
    "KillSwitchUnavailable": (".limits", "KillSwitchUnavailable"),
    "clear_kill_switch": (".limits", "clear_kill_switch"),
    "disable_public_api": (".limits", "disable_public_api"),
    "disable_writes": (".limits", "disable_writes"),
    "read_kill_switch": (".limits", "read_kill_switch"),
    "set_kill_switch": (".limits", "set_kill_switch"),
    "clear_snapshot_publication": (".snapshot", "clear_snapshot_publication"),
    "commit_snapshot_batch": (".snapshot", "commit_snapshot_batch"),
    "profile_batch": (".snapshot", "profile_batch"),
    "profile_object": (".snapshot", "profile_object"),
    "reset_snapshot_publication": (".snapshot", "reset_snapshot_publication"),
    "snapshot_batch": (".snapshot", "snapshot_batch"),
    "snapshot_convergence": (".snapshot", "snapshot_convergence"),
    "snapshot_counts": (".snapshot", "snapshot_counts"),
    "snapshot_digest": (".snapshot", "snapshot_digest"),
    "snapshot_objects": (".snapshot", "snapshot_objects"),
}


def __getattr__(name: str):
    try:
        module_name, attribute_name = _EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from exc
    value = getattr(import_module(module_name, __name__), attribute_name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))

__all__ = [
    "CloudConfig",
    "CloudObject",
    "CloudCredentialStore",
    "CloudState",
    "CloudSyncClient",
    "AzureTenantStore",
    "AzureTableSecurityStateBackend",
    "MemoryTenantStore",
    "SyncBatch",
    "SyncCredentials",
    "SyncResult",
    "clear_kill_switch",
    "create_cloud_app",
    "disable_public_api",
    "disable_writes",
    "clear_snapshot_publication",
    "commit_snapshot_batch",
    "https_transport",
    "KeyringBackend",
    "KILL_SWITCH_TTL_SECONDS",
    "KillSwitchState",
    "KillSwitchUnavailable",
    "MemorySecurityStateBackend",
    "read_kill_switch",
    "set_kill_switch",
    "profile_batch",
    "profile_object",
    "reset_snapshot_publication",
    "SecurityStateUnavailable",
    "snapshot_batch",
    "snapshot_convergence",
    "snapshot_counts",
    "snapshot_digest",
    "snapshot_objects",
]
