"""Backend selection.

``WATTRACKER_MODE`` picks which machine the Zwift folders and BLE hardware
live on:

  local  (default) - this machine, exactly as the app has always worked
  server           - a connector process elsewhere owns them (see remote.py)

The default is deliberately ``local``: the all-in-one install is still a
supported, first-class deployment, and nothing about running from source
should require thinking about connectors.
"""
from __future__ import annotations

import os
from typing import Optional

from .base import (
    ActivityFile,
    ActivityListing,
    Backend,
    BackendUnavailable,
    ExportManifest,
)
from .local import LocalBackend

__all__ = [
    "ActivityFile",
    "ActivityListing",
    "Backend",
    "BackendUnavailable",
    "ExportManifest",
    "LocalBackend",
    "discover",
    "get_backend",
    "is_offline",
    "mode",
]

_LOCAL = LocalBackend()

# Sentinels a page can render when the connector is offline: the discovery
# calls all answer "nothing found here", which is exactly what the templates
# already handle for a machine with no Zwift install.
_OFFLINE_DISCOVERY = {
    "activity_candidates": [],
    "zwift_id_candidates": [],
    "default_activities_dir": None,
    "workouts_root": None,
}


def discover(user_id: Optional[int], what: str):
    """A discovery call that degrades instead of raising when offline.

    Page rendering must not depend on a connector being attached: the Settings
    page is exactly where someone goes to find out *why* their connector is
    not working, so it has to load without one.
    """
    if what not in _OFFLINE_DISCOVERY:
        raise KeyError(f"not a discovery call: {what}")
    try:
        return getattr(get_backend(user_id), what)()
    except BackendUnavailable:
        return _OFFLINE_DISCOVERY[what]


def is_offline(user_id: Optional[int] = None) -> bool:
    """True when running in server mode with no connector attached."""
    if mode() == "local":
        return False
    from .. import connectorhub

    return not connectorhub.is_attached(user_id)


def mode() -> str:
    """The configured deployment mode (``local`` or ``server``)."""
    raw = os.environ.get("WATTRACKER_MODE", "local").strip().lower() or "local"
    if raw not in ("local", "server"):
        raise ValueError("WATTRACKER_MODE must be 'local' or 'server'")
    return raw


def get_backend(user_id: Optional[int] = None) -> Backend:
    """The backend serving ``user_id``'s machine.

    In local mode every user shares the one machine the app runs on, so
    ``user_id`` is ignored. In server mode each user has their own connector
    and the id selects it - hence the parameter exists from the start, so call
    sites are already written the right way round.
    """
    if mode() == "local":
        return _LOCAL
    from .remote import get_remote_backend

    return get_remote_backend(user_id)
