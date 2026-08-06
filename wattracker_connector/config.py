"""Where the connector keeps its settings, and how they are protected.

The device token is a credential, so this file gets the same owner-only
treatment the main app gives its data directory - the POSIX chmod on Unix, and
an actual NTFS ACL on Windows, where a chmod would be inert. That helper
already exists in ``wattracker.config``; reusing it keeps one definition of
"owner-only" rather than a second, subtly different one here.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Optional

from wattracker.config import _restrict

log = logging.getLogger(__name__)

_DIR_NAME = "wattracker-connector"


def config_dir() -> str:
    """Per-user config directory, created owner-only.

    ``WATTRACKER_CONNECTOR_DIR`` overrides it, which is what the tests use and
    what lets someone run two connectors against two servers from one account.
    """
    override = os.environ.get("WATTRACKER_CONNECTOR_DIR")
    if override:
        base = override
    elif os.name == "nt":
        root = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        base = os.path.join(root, _DIR_NAME)
    else:
        root = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
            os.path.expanduser("~"), ".config"
        )
        base = os.path.join(root, _DIR_NAME)
    os.makedirs(base, mode=0o700, exist_ok=True)
    _restrict(base, 0o700)
    return base


def config_path() -> str:
    return os.path.join(config_dir(), "connector.json")


def load() -> dict:
    path = config_path()
    if not os.path.exists(path):
        return {}
    # Self-heal permissions on files written before this was tightened - the
    # token lives in here.
    _restrict(path, 0o600)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle) or {}
    except (json.JSONDecodeError, OSError):
        log.warning("could not read %s; treating it as empty", path)
        return {}


def save(data: dict) -> None:
    path = config_path()
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2)
    _restrict(path, 0o600)


def describe(data: Optional[dict] = None) -> dict:
    """Config with the token redacted, for logging and the settings dialog."""
    data = load() if data is None else data
    shown = dict(data)
    if shown.get("token"):
        shown["token"] = "(set)"
    return shown
