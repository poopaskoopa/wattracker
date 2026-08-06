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
import re
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


def allow_non_loopback() -> bool:
    """Whether a non-loopback bind is permitted (WATTRACKER_ALLOW_NON_LOOPBACK).

    Off by default, and deliberately a separate variable from WATTRACKER_HOST
    rather than something inferred from it. Binding beyond loopback is the one
    change that turns this from a personal app into a networked service, and
    every other control here - the Host allowlist, the WebSocket origin check,
    a session cookie with no Secure flag - was written on the assumption that
    it never happens. Requiring a second, explicit opt-in means it cannot be
    done by fat-fingering a host, and it makes the container image the only
    thing in the tree that asks for it.
    """
    raw = os.environ.get("WATTRACKER_ALLOW_NON_LOOPBACK", "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def server_host() -> str:
    """Validated bind host. Loopback-only unless explicitly opted out of."""
    raw = os.environ.get("WATTRACKER_HOST", "127.0.0.1").strip()
    if raw.startswith("[") and raw.endswith("]"):
        raw = raw[1:-1]
    if not raw:
        raise ValueError("WATTRACKER_HOST must be a loopback host")
    if raw.lower() == "localhost":
        return "localhost"
    if raw in ("127.0.0.1", "::1"):
        return raw
    if allow_non_loopback():
        return raw
    raise ValueError(
        "WATTRACKER_HOST must be loopback-only (127.0.0.1, localhost, or ::1). "
        "To bind an interface reachable from the network - which is what the "
        "container image does - also set WATTRACKER_ALLOW_NON_LOOPBACK=1, and "
        "read the exposure note in the README first."
    )


def server_port() -> int:
    raw = os.environ.get("WATTRACKER_PORT", "8000").strip()
    try:
        port = int(raw)
    except ValueError as exc:
        raise ValueError("WATTRACKER_PORT must be an integer from 1 to 65535") from exc
    if not 1 <= port <= 65535:
        raise ValueError("WATTRACKER_PORT must be an integer from 1 to 65535")
    return port


# One DNS label: letters/digits/hyphen, 1-63 characters, no leading or
# trailing hyphen. Applied to an already-lowercased, ASCII-only label.
_DNS_LABEL_RE = re.compile(r"\A[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")

# Characters that would give a bare hostname structure it must not have here -
# a path, query, fragment, userinfo, an IPv6 literal, quoting or percent
# escaping - plus the wildcard. Any of them means the value is not the single
# exact hostname this setting is documented to hold, so it is refused outright
# rather than sanitised into something the operator did not write.
_PUBLIC_HOST_FORBIDDEN = frozenset("/\\?#@*[]\"'%,;&=<>{}|^`~+$!()")


def _validate_public_host(raw: str) -> Optional[str]:
    """Validate one external hostname with an optional port. See public_host."""
    raw = (raw or "").strip()
    if not raw:
        return None
    err = ValueError(
        "WATTRACKER_PUBLIC_HOST must be a bare hostname with an optional port "
        "(e.g. 'laptop.tail1234.ts.net' or 'laptop.tail1234.ts.net:8443') - no "
        "scheme, path, credentials, whitespace or wildcard"
    )
    # Printable ASCII only: this rejects control characters, embedded
    # whitespace, and non-ASCII lookalikes (a Unicode digit passes str.isdigit
    # and would sneak through the port check below; an IDN must be given in
    # punycode).
    if any(not ("\x21" <= ch <= "\x7e") for ch in raw):
        raise err
    if any(ch in _PUBLIC_HOST_FORBIDDEN for ch in raw):
        raise err

    host = raw.lower()
    port: Optional[str] = None
    if ":" in host:
        host, _, port = host.rpartition(":")
        # A second colon means an IPv6 literal or a mangled value, not
        # "host:port"; either way it is not what this setting accepts.
        if not host or ":" in host or not port.isdigit():
            raise err
        if not 1 <= int(port) <= 65535:
            raise err

    if len(host) > 253:
        raise err
    # A trailing dot yields an empty final label and is rejected here: the
    # allowlist match is exact, so the value must be written the same way a
    # client will send it in the Host header.
    if any(not _DNS_LABEL_RE.match(label) for label in host.split(".")):
        raise err
    return f"{host}:{port}" if port else host


def public_host() -> Optional[str]:
    """Validated external hostname this app is reached by (WATTRACKER_PUBLIC_HOST).

    Unset or empty -> None, and behaviour is exactly the default posture: the
    Host allowlist stays loopback-only and calendar links are minted from the
    request's own base URL. Set, it names the one extra host the server will
    answer to (a ``tailscale serve`` tailnet name, say) and the host calendar
    subscription links are built from.

    This value is appended to a security allowlist, so it is the one place a
    typo could widen exposure: it accepts an exact DNS hostname with an
    optional ``:port`` and nothing else, and anything else raises. A wildcard
    in any form is rejected - the allowlist does no pattern matching and must
    never be given a value that looks like it does. The hostname is lowercased
    because DNS names are case-insensitive while the allowlist comparison is
    not; normalising here is what stops ``Foo.ts.net`` from either bypassing or
    silently missing the entry.
    """
    return _validate_public_host(os.environ.get("WATTRACKER_PUBLIC_HOST", ""))


def public_hosts() -> "list[str]":
    """Every external name this app answers to (WATTRACKER_PUBLIC_HOSTS).

    ``public_host`` accepts exactly one name, which is enough for a single
    ``tailscale serve`` hostname but not for a LAN, where the same server is
    legitimately reached as an IP, a short hostname and a .local name at the
    same time. This is the comma-separated form; each value goes through the
    identical, already-strict ``public_host`` validator, so widening the count
    does not widen what any single entry may be. Duplicates and the value of
    WATTRACKER_PUBLIC_HOST itself are folded in, order preserved.
    """
    out: "list[str]" = []
    single = public_host()
    if single:
        out.append(single)
    for part in os.environ.get("WATTRACKER_PUBLIC_HOSTS", "").split(","):
        validated = _validate_public_host(part)
        if validated and validated not in out:
            out.append(validated)
    return out


def cookie_secure() -> bool:
    """Whether the session cookie carries the Secure flag.

    Off by default because the app speaks plain http; turn it on the moment
    something terminates TLS in front (tailscale serve, Caddy), or the cookie
    will travel in the clear to anyone on the same network.
    """
    raw = os.environ.get("WATTRACKER_COOKIE_SECURE", "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def public_scheme() -> str:
    """Scheme for links that name ``public_host`` (WATTRACKER_PUBLIC_SCHEME).

    Defaults to https because ``tailscale serve`` terminates TLS: the app
    itself still only ever speaks plain http on loopback, so this describes how
    the outside world reaches it, not how it is served. Only http or https are
    accepted; the escape hatch exists for anyone fronting it without TLS.
    """
    raw = os.environ.get("WATTRACKER_PUBLIC_SCHEME", "").strip().lower() or "https"
    if raw not in ("http", "https"):
        raise ValueError("WATTRACKER_PUBLIC_SCHEME must be 'http' or 'https'")
    return raw


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
