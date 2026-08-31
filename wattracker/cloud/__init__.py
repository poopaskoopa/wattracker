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
    "create_cloud_app",
    "https_transport",
    "KeyringBackend",
    "MemorySecurityStateBackend",
    "profile_batch",
    "profile_object",
    "SecurityStateUnavailable",
    "snapshot_batch",
    "snapshot_convergence",
    "snapshot_counts",
    "snapshot_digest",
    "snapshot_objects",
]
