"""App-level configuration: session secret and ANTHROPIC_API_KEY.

Per-user settings (FTP override, ZwiftID, folder paths) live in the database,
scoped by user - see ``db.get_user_settings`` / ``db.save_user_settings``.

Values resolve from environment variables first, then an optional JSON config
file (config.json in the app data dir).
"""
from __future__ import annotations

import getpass
import json
import logging
import os
import secrets
import subprocess
from dataclasses import dataclass
from typing import Optional

_log = logging.getLogger(__name__)


def _restrict_windows_acl(path: str, is_dir: bool) -> None:
    """Best-effort owner-only NTFS ACL on Windows.

    POSIX modes are inert on Windows: ``os.chmod`` only toggles the read-only
    attribute and sets no ACL. So when the data dir is relocated off the user
    profile (WATTRACKER_DATA_DIR / WATTRACKER_DB) onto a volume whose inherited
    ACL grants e.g. ``Users:(R)``, another local standard account could read the
    session secret, Anthropic key and password hashes. Reset inheritance and
    grant full control to the current user only.

    Done via ``icacls`` (shipped with every supported Windows; no extra
    dependency, and cleaner than hand-building a SID/DACL through ctypes). The
    argv is passed as a list (no ``shell=True``) so a data-dir path containing
    spaces stays a single argument and nothing is shell-interpreted. Best-effort:
    it must never crash the app, mirroring the chmod-can-fail contract.
    """
    try:
        user = getpass.getuser()
    except Exception:  # pragma: no cover - getuser is extremely robust
        user = os.environ.get("USERNAME", "")
    if not user:
        return
    # Dirs get object+container inheritance so new children (db, -wal, -shm,
    # backups) are owner-only too; files just get full control.
    grant = f"{user}:(OI)(CI)F" if is_dir else f"{user}:F"
    try:
        subprocess.run(
            ["icacls", path, "/inheritance:r", "/grant:r", grant],
            check=True,
            capture_output=True,
        )
    except (subprocess.SubprocessError, OSError, ImportError):
        _log.debug("could not set owner-only ACL on %s", path, exc_info=True)


def _restrict(path: str, mode: int, *, is_dir: Optional[bool] = None) -> None:
    """Best-effort owner-only lockdown of the data dir/files.

    On POSIX this is a plain ``chmod`` (makedirs()'s mode is umask-masked, and
    some filesystems don't honour it, so set it explicitly). On Windows POSIX
    modes are meaningless, so enforce an owner-only ACL instead (see
    ``_restrict_windows_acl``). Never let an unsupported-FS failure crash the
    app. ``is_dir`` is inferred from the path when not given.
    """
    try:
        if not os.path.exists(path):
            return
        if os.name == "nt":
            if is_dir is None:
                is_dir = os.path.isdir(path)
            _restrict_windows_acl(path, is_dir)
        else:
            os.chmod(path, mode)
    except OSError:
        _log.debug("could not restrict %s to %o", path, mode, exc_info=True)


def app_data_dir() -> str:
    """Directory for wattracker's own data (db + config.json), owner-only (0700)."""
    override = os.environ.get("WATTRACKER_DATA_DIR")
    base = override or os.path.join(os.path.expanduser("~"), ".wattracker")
    os.makedirs(base, mode=0o700, exist_ok=True)
    _restrict(base, 0o700)
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
        # Self-heal permissions on installs created before perms were tightened:
        # config.json can hold the session secret and Anthropic key.
        _restrict(path, 0o600)
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f) or {}
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_json(data: dict) -> None:
    path = config_path()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    # config.json can hold the session secret and the Anthropic API key - keep
    # it owner-only.
    _restrict(path, 0o600)


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


def server_host() -> str:
    """Validated loopback bind host (Windows v1 is intentionally local-only)."""
    raw = os.environ.get("WATTRACKER_HOST", "127.0.0.1").strip()
    if raw.startswith("[") and raw.endswith("]"):
        raw = raw[1:-1]
    if not raw:
        raise ValueError("WATTRACKER_HOST must be a loopback host")
    if raw.lower() == "localhost":
        return "localhost"
    if raw in ("127.0.0.1", "::1"):
        return raw
    raise ValueError("WATTRACKER_HOST must be loopback-only (127.0.0.1, localhost, or ::1)")


def server_port() -> int:
    raw = os.environ.get("WATTRACKER_PORT", "8000").strip()
    try:
        port = int(raw)
    except ValueError as exc:
        raise ValueError("WATTRACKER_PORT must be an integer from 1 to 65535") from exc
    if not 1 <= port <= 65535:
        raise ValueError("WATTRACKER_PORT must be an integer from 1 to 65535")
    return port


def open_browser_enabled() -> bool:
    raw = os.environ.get("WATTRACKER_OPEN_BROWSER", "1").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    raise ValueError("WATTRACKER_OPEN_BROWSER must be a boolean")


def browser_url(host: Optional[str] = None, port: Optional[int] = None) -> str:
    host = server_host() if host is None else host
    port = server_port() if port is None else port
    display_host = f"[{host}]" if ":" in host and not host.startswith("[") else host
    return f"http://{display_host}:{port}"


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
