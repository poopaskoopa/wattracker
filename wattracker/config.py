"""App-level configuration: session secret and LLM settings.

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
from urllib.parse import urlsplit

_log = logging.getLogger(__name__)


def _icacls_path() -> str:
    r"""Absolute path to the system ``icacls.exe``.

    WHY absolute, and why this is a security control rather than a tidiness
    preference: ``subprocess.run`` with a list and no ``shell=True`` reaches
    ``CreateProcessW`` with ``lpApplicationName=NULL``, and in that mode
    Windows resolves a bare program name through a documented search order
    that begins with **the directory of the calling executable and then the
    current working directory - both BEFORE System32**. The connector ships as
    a portable .exe a rider drops in Downloads or on a USB stick, and
    ``_restrict`` is on the very first code path its ``__main__`` reaches, so
    an ``icacls.exe`` planted next to that .exe (or in whatever directory the
    process happens to be started from) would be executed as the rider on
    every launch, every config save and every log rotation. ``CREATE_NO_WINDOW``
    plus ``capture_output=True`` means the rider would never see it run. That
    is arbitrary code execution obtained by dropping one file beside a
    download - no elevation, no exploit.

    Naming the full path removes the search entirely: ``CreateProcessW`` opens
    exactly that file or fails. ``%SystemRoot%`` is read from the environment
    (Windows always sets it) with ``C:\Windows`` as the fallback for the
    pathological case where it is missing or empty - a wrong guess there
    degrades to the same best-effort no-op as a missing icacls, which the
    caller already swallows, whereas trusting a bare name degrades to running
    an attacker's binary.
    """
    system_root = os.environ.get("SystemRoot") or r"C:\Windows"
    return os.path.join(system_root, "System32", "icacls.exe")


#: Well-known SIDs whose presence in a path's DACL means the path is ALREADY
#: reachable by principals other than its owner, in the numerical ``*``-prefixed
#: form icacls documents. Numerical and not friendly names because the friendly
#: names are LOCALISED (``BUILTIN\Users`` is ``VORDEFINIERT\Benutzer`` on a
#: German install) and this app ships to whatever Windows a rider owns.
_FOREIGN_SIDS = (
    "*S-1-5-32-545",  # BUILTIN\Users
    "*S-1-1-0",       # Everyone
    "*S-1-5-11",      # NT AUTHORITY\Authenticated Users
)

#: Seconds any single icacls spawn may take before it is killed.
#:
#: WHY a timeout at all: db.py supports a WATTRACKER_DB on a network mount, and
#: icacls on a stalled mount blocks forever. Without this a hang in the SECOND
#: spawn leaves the path at its parent's inherited ACL PERMANENTLY and says
#: nothing, because the only warning lives in an ``except`` branch that a hang
#: never reaches. ``subprocess.TimeoutExpired`` subclasses ``SubprocessError``
#: (checked against this interpreter, not assumed), so the handlers already
#: written below route a timeout to exactly the same place a non-zero exit goes.
#: 15s is far past any local-disk icacls and far short of a mount's own timeout.
_ACL_SPAWN_TIMEOUT = 15


def _acl_needs_reset(icacls: str, path: str, spawn: dict) -> bool:
    r"""Does ``path`` already carry a DACL ace for a foreign principal?

    WHY this gate exists. ``/reset`` replaces the ACL with the parent's default
    INHERITED one, so between the reset spawn and the grant spawn the path is
    exactly as permissive as its parent. Windows evaluates a DACL once, at
    ``CreateFile`` time, so a handle another local account opens inside that
    window keeps its access for as long as it holds the handle - re-locking the
    path afterwards does not revoke it. For ``~/.wattracker`` that window is
    harmless (the parent is the user profile, owner-only already), and so is it
    for the db/-wal/-shm under a data dir ``app_data_dir()`` has just locked.
    But ``db_path()`` returns a ``WATTRACKER_DB`` override VERBATIM and never
    calls ``app_data_dir()``, and nothing restricts that override's parent: for
    ``WATTRACKER_DB=D:\shared\wt.db`` the file and its two sidecars would each
    take that volume's inheritable default - plausibly ``Users:(RX)`` and
    ``Authenticated Users:(M)`` on a stock non-system NTFS root - at every
    single app start. Those files were never widened before the reset landed,
    so an unconditional reset is a straight regression on the one deployment
    shape this whole function exists to protect.

    Probing first removes that. The reset now only ever runs on a path that is
    ALREADY exposed to one of ``_FOREIGN_SIDS``, so the window it opens can
    only re-expose what was exposed anyway, and on the clean path - which is
    every install that has not been relocated - the reset stops running at all.

    COST, stated plainly because it is paid on every call: a clean path spends
    one spawn per SID (three, all missing) plus the grant; an exposed one stops
    at the first hit and adds the reset. That is more spawns than the two this
    function used, and ``_restrict`` runs on every ``app_data_dir()``. A single
    ``/findsid`` naming all three SIDs would collapse it, but the reference's
    grammar admits only one ``/findsid Sid`` per invocation and an undocumented
    repetition that silently kept just the last SID would be a false negative -
    the one direction this must not fail in - so the SIDs are walked separately
    until a real Windows box says otherwise.

    WHAT SIGNAL IS READ, and why not the exit code. ``/findsid`` is a
    REPORTING verb: finding nothing is not a failure, so icacls exits 0 either
    way. The answer is on stdout.

    MEASURED on Windows 11 build 26100, because this shape was reasoned wrong
    once and the gate it feeds never fired: a hit and a miss BOTH print two
    non-blank lines - "SID Found: <path>." or "No files with a matching SID was
    found", each above the summary. So the line count separates nothing and the
    echoed path is the only tell; a count outside 1-2 lines is a shape this
    probe has never seen, and resets.

    The echo is read rather than the miss wording, which is localised. It is
    trusted only for an ASCII path: icacls writes the OEM codepage and
    ``text=True`` decodes the ANSI one, so outside ASCII a mangled echo would
    read as a clean miss - the one direction this must not fail in. A
    non-ASCII path resets without probing.

    FAIL TOWARD RESETTING. Every other outcome - a raise, a timeout, a non-zero
    exit, anything on stderr, output of an unexpected shape, stdout that was not
    captured at all - returns True. A probe that cannot answer must never be the
    reason the explicit foreign ace this function exists to clear is left in
    place; resetting is what the code did before the probe, so True is never
    worse than the status quo. Probing stops at the first SID answering True.

    VERIFIED ON WINDOWS 11 (build 26100): the two shapes above, exit 0 and an
    empty stderr for both, and that ``/findsid`` matches an INHERITED ace as
    well as an explicit one - so a relocated db under a permissive parent
    probes as exposed, which is what the reset is for. The SIDs are still
    walked in separate spawns: the reference admits one ``/findsid Sid`` per
    invocation. No ``/T``: only this path.
    """
    if not path.isascii():
        # The echo is the only tell and it is unreadable across the OEM/ANSI
        # codepage gap. Reset rather than misread a mangled one as clean.
        return True
    for sid in _FOREIGN_SIDS:
        try:
            proc = subprocess.run([icacls, path, "/findsid", sid], **spawn)
        except (subprocess.SubprocessError, OSError, ImportError):
            _log.debug("could not probe the acl on %s", path, exc_info=True)
            return True
        out = getattr(proc, "stdout", None)
        err = getattr(proc, "stderr", None)
        if not isinstance(out, str) or err:
            return True  # nothing readable, or icacls complained: inconclusive
        lines = [ln for ln in out.splitlines() if ln.strip()]
        if not 1 <= len(lines) <= 2:
            return True  # not a shape this probe has ever seen
        if any(path.casefold() in ln.casefold() for ln in lines):
            return True  # the path was echoed back: this sid is on its DACL
    return False


def _restrict_windows_acl(path: str, is_dir: bool) -> None:
    """Best-effort owner-only NTFS ACL on Windows.

    POSIX modes are inert on Windows: ``os.chmod`` only toggles the read-only
    attribute and sets no ACL. So when the data dir is relocated off the user
    profile (WATTRACKER_DATA_DIR / WATTRACKER_DB) onto a volume whose ACL grants
    e.g. ``Users:(R)``, another local standard account could read the session
    secret, LLM API key and password hashes. Reset inheritance and grant full
    control to the current user only.

    Two icacls calls, because one cannot do it and icacls rejects the flags
    together. ``/inheritance:r`` removes only INHERITED aces, and ``/grant:r``
    replaces aces only for the user it names, so an EXPLICIT ace for anyone
    else survives both - verified on Windows 11, where a directory carrying an
    explicit ``Users:(OI)(CI)(R)`` kept it, and the ``wattracker.db`` created
    afterwards inherited it as ``Users:(I)(R)``. That is the exposure this
    function exists to close, so ``/reset`` drops the explicit aces first and
    the grant then re-narrows what /reset widened.

    The reset is GATED on a probe, and the gate is a security control rather
    than an optimisation: an unconditional ``/reset`` briefly restores the
    parent's ACL on a path whose parent nothing else restricts, which is a
    regression on a relocated ``WATTRACKER_DB``. ``_acl_needs_reset`` carries
    the whole argument, the stdout signal it reads, and the rule that a probe
    which cannot answer resets anyway.

    Which is why the ORDER of the two failures is not symmetric. Between the
    calls the path carries whatever its parent grants; a failed reset leaves
    the old behavior intact and is unremarkable, but a reset that succeeds
    followed by a grant that does not ends WIDER than it started, and that one
    is worth saying out loud. ``check=True`` is what makes that warning
    reachable at all: without it a non-zero grant raises nothing, the reset
    stands unnarrowed, and the path is left open in silence.

    Done via ``icacls`` (shipped with every supported Windows; no extra
    dependency, and cleaner than hand-building a SID/DACL through ctypes). The
    argv is passed as a list (no ``shell=True``) so a data-dir path containing
    spaces stays a single argument and nothing is shell-interpreted. Best-effort:
    it must never crash the app, mirroring the chmod-can-fail contract.

    Every spawn carries ``timeout=`` - see ``_ACL_SPAWN_TIMEOUT`` for why a hang
    is the one failure the warning below could not otherwise reach.

    CREATE_NO_WINDOW because the connector's frozen build is windowed and has
    no console of its own: without it Windows gives every ``icacls`` child a
    brand new console, and the rider watches half a dozen of them flash open
    and shut each time the tray starts. Fetched with ``getattr`` because the
    flag exists only on Windows, and the tests reach this function by
    monkeypatching ``os.name`` on machines where it does not.

    The executable is named by ABSOLUTE path, and that is a security control
    rather than tidiness - see ``_icacls_path``.
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
    icacls = _icacls_path()
    spawn = {
        "check": True,
        "capture_output": True,
        "text": True,
        # A non-ASCII path under a mismatched console codepage must degrade to
        # replacement characters, never to a UnicodeDecodeError that would read
        # as a probe failure for the wrong reason.
        "errors": "replace",
        "timeout": _ACL_SPAWN_TIMEOUT,
        "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0),
    }
    reset = False
    if _acl_needs_reset(icacls, path, spawn):
        try:
            subprocess.run([icacls, path, "/reset"], **spawn)
            reset = True
        except (subprocess.SubprocessError, OSError, ImportError):
            # Nothing is lost by carrying on: without the reset this is exactly
            # what the function did before, so the grant is still worth trying.
            _log.debug("could not clear explicit aces on %s", path, exc_info=True)
    try:
        subprocess.run(
            [icacls, path, "/inheritance:r", "/grant:r", grant], **spawn
        )
    except (subprocess.SubprocessError, OSError, ImportError):
        if reset:
            _log.warning(
                "cleared the acl on %s but could not re-apply the owner-only "
                "grant - it now carries whatever its parent grants", path,
                exc_info=True,
            )
        else:
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

    api_key: Optional[str] = None
    llm_endpoint: Optional[str] = None
    llm_model: Optional[str] = None


def _load_json() -> dict:
    path = config_path()
    if os.path.exists(path):
        # Self-heal permissions on installs created before perms were tightened:
        # config.json can hold the session secret and LLM API key.
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
    # config.json can hold the session secret and the LLM API key - keep
    # it owner-only.
    _restrict(path, 0o600)


#: config.json fields already reported as malformed, so a hand-edited junk
#: value warns once per process instead of on every request.
_warned_malformed: set = set()


def _as_optional_str(value, field: str) -> Optional[str]:
    """A config.json value that must be a string; anything else is absent.

    Hand-editing is the only source of a non-string (every writer stores
    strings), and a junk value must degrade to "unset", never crash a
    request: llm_settings() runs on every /generate, so a bare number under
    llm_endpoint would 500 the whole planner without this.
    """
    if isinstance(value, str) and value:
        return value
    if value is not None:
        if field not in _warned_malformed:
            _warned_malformed.add(field)
            _log.warning(
                "ignoring malformed %s in config.json (expected a string, "
                "got %s)", field, type(value).__name__,
            )
    return None


def load_config() -> Config:
    """Load app-level config, env overriding the JSON file, per field.

    The key resolves API_KEY -> config.json api_key -> ANTHROPIC_API_KEY ->
    config.json anthropic_api_key (the legacy name, kept as a lowest-priority
    fallback so existing installs work unchanged).
    """
    data = _load_json()
    return Config(
        api_key=os.environ.get("API_KEY")
        or _as_optional_str(data.get("api_key"), "api_key")
        or os.environ.get("ANTHROPIC_API_KEY")
        or _as_optional_str(data.get("anthropic_api_key"), "anthropic_api_key"),
        llm_endpoint=os.environ.get("LLM_ENDPOINT")
        or _as_optional_str(data.get("llm_endpoint"), "llm_endpoint"),
        llm_model=os.environ.get("LLM_MODEL")
        or _as_optional_str(data.get("llm_model"), "llm_model"),
    )


#: Default model per named LLM endpoint. A custom base URL has no default:
#: the model is required, and without one the LLM layer is disabled.
LLM_DEFAULT_MODELS = {
    "anthropic": "claude-sonnet-5",
    "openai": "gpt-5.6-luna",
    "openrouter": "google/gemini-3.7-flash",
}

#: The one named OpenAI-compatible endpoint whose host differs from the openai
#: SDK's built-in default (https://api.openai.com/v1).
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


def _normalize_llm_url(raw: str) -> Optional[str]:
    """Strictly validate a custom OpenAI-compatible base URL, or return None.

    http:// or https:// with a host; no credentials, whitespace or fragment -
    the same refuse-don't-sanitise posture as ``public_host()``. A bare
    host[:port] gets ``/v1`` appended (the vLLM / LM Studio / OpenRouter
    convention; Ollama accepts either), and a trailing slash is stripped from
    a URL that has a path, which is used as-is.
    """
    if any(ch.isspace() for ch in raw):
        return None
    parts = urlsplit(raw)
    if parts.scheme not in ("http", "https"):
        return None
    if not parts.hostname:
        return None
    if parts.username is not None or parts.password is not None:
        return None
    if parts.fragment:
        return None
    try:
        port = parts.port
    except ValueError:
        return None
    if port is not None and not 1 <= port <= 65535:
        return None
    path = parts.path.rstrip("/")
    if not path:
        path = "/v1"
    url = f"{parts.scheme}://{parts.netloc}{path}"
    if parts.query:
        url += f"?{parts.query}"
    return url


def normalize_llm_endpoint(raw: str) -> Optional[str]:
    """Validate an LLM_ENDPOINT / config.json ``llm_endpoint`` value.

    Returns the lowercased endpoint keyword for the named providers, the
    normalised base URL for a custom OpenAI-compatible server, or None when
    the value is unusable. The literal ``custom`` is NOT a value: the selector
    holds the URL itself, and a hand-edited config.json with it is treated as
    invalid (the LLM layer is disabled with a warning).
    """
    value = (raw or "").strip()
    lowered = value.lower()
    if lowered in LLM_DEFAULT_MODELS:
        return lowered
    if lowered == "custom":
        return None
    return _normalize_llm_url(value)


@dataclass
class LlmSettings:
    """The resolved LLM configuration for one call, when the layer runs.

    ``endpoint`` is the keyword for a named provider or the normalised base
    URL for a custom one; ``base_url`` is what the OpenAI-compatible client
    is pointed at (None for openai: the SDK default already is the right URL).
    """

    endpoint: str
    base_url: Optional[str]
    api_key: Optional[str]
    model: Optional[str]


def llm_settings() -> Optional[LlmSettings]:
    """Resolve the effective LLM settings, or None when the layer is skipped.

    None exactly when one of these holds: the endpoint value is invalid (not
    a keyword, not a valid http/https URL - including the literal "custom");
    no model resolves (a custom URL without LLM_MODEL / a stored model -
    named endpoints always have a default); or a named endpoint has no API
    key. A custom URL may go keyless (local servers). In every other case the
    LLM layer is attempted, and any failure mid-call degrades to the plan
    returned unchanged.
    """
    cfg = load_config()
    endpoint = normalize_llm_endpoint(cfg.llm_endpoint or "anthropic")
    if endpoint is None:
        _log.warning(
            "LLM refinement disabled: unusable LLM_ENDPOINT %r (expected "
            "'anthropic', 'openai', 'openrouter', or an http/https base URL)",
            cfg.llm_endpoint,
        )
        return None
    model = (cfg.llm_model or "").strip() or LLM_DEFAULT_MODELS.get(endpoint)
    if not model:
        _log.warning(
            "LLM refinement disabled: no model configured for custom endpoint "
            "%s (set LLM_MODEL)", endpoint,
        )
        return None
    api_key = cfg.api_key
    if endpoint in LLM_DEFAULT_MODELS and not api_key:
        return None
    if endpoint == "openrouter":
        base_url = OPENROUTER_BASE_URL
    elif endpoint in LLM_DEFAULT_MODELS:
        base_url = None
    else:
        base_url = endpoint
    return LlmSettings(
        endpoint=endpoint, base_url=base_url, api_key=api_key, model=model
    )


def set_llm_settings(
    endpoint: Optional[str] = None,
    custom_url: Optional[str] = None,
    model: Optional[str] = None,
    api_key: Optional[str] = None,
    clear_model: bool = False,
) -> None:
    """Persist LLM settings to config.json.

    Whichever non-empty values are given are written under ``llm_endpoint`` /
    ``llm_model`` / ``api_key``; a custom base URL is stored *in*
    ``llm_endpoint`` (the field holds the URL when the provider is custom -
    the same value the LLM_ENDPOINT env var holds). Plain empty values are
    skipped, so a partial save never clobbers the other fields. An empty model
    plus ``clear_model`` deletes the stored ``llm_model`` (the model then
    falls back to the per-endpoint default at runtime). Writing ``api_key``
    removes the legacy ``anthropic_api_key`` entry.
    """
    endpoint = (endpoint or "").strip().lower() or None
    if endpoint == "custom":
        # The literal is not a value; without a URL there is nothing to store.
        endpoint = None
    custom_url = (custom_url or "").strip() or None
    model = (model or "").strip()
    api_key = (api_key or "").strip() or None
    data = _load_json()
    changed = False
    if endpoint or custom_url:
        new_value = custom_url or endpoint
        old_value = data.get("llm_endpoint")
        if old_value != new_value:
            # App-level and shared: any authenticated user can re-point
            # EVERY user's LLM traffic, and a stored URL/key is invisible
            # after the fact - so the change is logged loudly.
            _log.warning(
                "LLM endpoint changed from %r to %r (app-level setting, "
                "shared across all users)", old_value, new_value,
            )
        data["llm_endpoint"] = new_value
        changed = True
    if model:
        data["llm_model"] = model
        changed = True
    elif clear_model and "llm_model" in data:
        del data["llm_model"]
        changed = True
    if api_key:
        data["api_key"] = api_key
        data.pop("anthropic_api_key", None)
        changed = True
    if changed:
        _save_json(data)


def set_anthropic_api_key(key: str) -> None:
    """Deprecated alias for ``set_llm_settings(api_key=key)``."""
    set_llm_settings(api_key=key)


def anthropic_api_key_set() -> bool:
    """Deprecated alias: whether the effective LLM API key is set."""
    return bool(load_config().api_key)


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


def allow_registration() -> bool:
    """Whether ``POST /register`` may create an ADDITIONAL account.

    The first account is not governed by this: every install bootstraps by
    registering, and a server with no users has nothing to protect yet. Once a
    user exists, an open /register is a hole rather than a feature, because
    registration is unauthenticated and an account is not a harmless thing to
    hold on this app:

    * the LLM settings are app-global, not per-user, so any account can point
      the endpoint at a host it controls and collect the rider's stored API key
      and every prompt payload sent afterwards; and
    * ``_promote_to_password_session`` clears the ``via=connector`` marker when
      a password is proven, so a connector session that registers a throwaway
      account sheds the marker and walks past the /settings refusal that exists
      to stop exactly that.

    Neither is reachable from outside while the server is bound to loopback,
    which is why this was survivable until LAN binding became a documented
    option. docs/windows-security.md has listed "registration policy" as an
    unbuilt prerequisite for that bind since it was written; this is it.

    Deliberately the same shape as ``allow_non_loopback``: a separate explicit
    variable, off by default, parsed identically, so a rider who has learned
    one has learned both and neither can be turned on by accident.
    """
    raw = os.environ.get("WATTRACKER_ALLOW_REGISTRATION", "").strip().lower()
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
