"""Optional, server-mediated cloud synchronization.

The local Wattracker application does not import this package during normal
startup.  Deployments that want the cloud read/write planes construct the
standalone ASGI app from :mod:`wattracker.cloud.api`.
"""

from .api import CloudConfig, CloudState, create_cloud_app
from .client import CloudSyncClient, SyncCredentials, SyncResult, https_transport
from .credentials import CloudCredentialStore, KeyringBackend
from .models import CloudObject, SyncBatch
from .storage import AzureTenantStore, MemoryTenantStore
from .snapshot import (
    profile_batch,
    profile_object,
    snapshot_batch,
    snapshot_convergence,
    snapshot_counts,
    snapshot_digest,
    snapshot_objects,
)
from .security import (
    AzureTableSecurityStateBackend,
    MemorySecurityStateBackend,
    SecurityStateUnavailable,
)
# The operator surface for the budget kill switch.  #169's CLI is the intended
# caller; these take a state backend rather than a running app so the switch
# can be thrown and cleared while every replica is scaled to zero.
from .limits import (
    KILL_SWITCH_TTL_SECONDS,
    KillSwitchState,
    KillSwitchUnavailable,
    clear_kill_switch,
    disable_public_api,
    disable_writes,
    read_kill_switch,
    set_kill_switch,
)

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
    "SecurityStateUnavailable",
    "snapshot_batch",
    "snapshot_convergence",
    "snapshot_counts",
    "snapshot_digest",
    "snapshot_objects",
]
