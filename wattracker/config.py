"""App-level configuration: session secret and ANTHROPIC_API_KEY.

Per-user settings (FTP override, ZwiftID, folder paths) live in the database,
scoped by user - see ``db.get_user_settings`` / ``db.save_user_settings``.

Values resolve from environment variables first, then an optional JSON config
file (config.json in the app data dir).
"""
from __future__ import annotations

import json
import os
import secrets
from dataclasses import dataclass
from typing import Optional


def app_data_dir() -> str:
    """Directory for wattracker's own data (db + config.json)."""
    override = os.environ.get("WATTRACKER_DATA_DIR")
    if override:
        os.makedirs(override, exist_ok=True)
        return override
    base = os.path.join(os.path.expanduser("~"), ".wattracker")
    os.makedirs(base, exist_ok=True)
    return base


def config_path() -> str:
    return os.path.join(app_data_dir(), "config.json")


def db_path() -> str:
    override = os.environ.get("WATTRACKER_DB")
    if override:
        return override
    return os.path.join(app_data_dir(), "wattracker.db")


@dataclass
class Config:
    """App-level (not per-user) configuration."""

    anthropic_api_key: Optional[str] = None


def _load_json() -> dict:
    path = config_path()
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f) or {}
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_json(data: dict) -> None:
    with open(config_path(), "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def load_config() -> Config:
    """Load app-level config, env overriding the JSON file."""
    data = _load_json()
    return Config(
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY")
        or data.get("anthropic_api_key"),
    )


def set_anthropic_api_key(key: str) -> None:
    """Persist an app-level Anthropic API key to config.json."""
    if not key:
        return
    data = _load_json()
    data["anthropic_api_key"] = key
    _save_json(data)


def anthropic_api_key_set() -> bool:
    return bool(load_config().anthropic_api_key)


def auto_scan_enabled() -> bool:
    """Whether the background daily activity scan runs (WATTRACKER_AUTO_SCAN).

    Defaults to on; set WATTRACKER_AUTO_SCAN=0 to disable (used by the tests to
    keep the suite deterministic).
    """
    return os.environ.get("WATTRACKER_AUTO_SCAN", "1") not in ("0", "false", "no")


def session_secret() -> str:
    """Return the signed-cookie session secret, generating + persisting once.

    Priority: WATTRACKER_SECRET env var -> config.json -> freshly generated.
    """
    env = os.environ.get("WATTRACKER_SECRET")
    if env:
        return env
    data = _load_json()
    secret = data.get("session_secret")
    if secret:
        return secret
    secret = secrets.token_hex(32)
    data["session_secret"] = secret
    _save_json(data)
    return secret
