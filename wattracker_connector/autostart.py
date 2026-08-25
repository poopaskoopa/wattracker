"""Start with Windows: one per-user registry value, and nothing else.

Every other way of arranging this asks for elevation - HKLM's Run key, a
service, a scheduled task - and ``docs/windows-security.md`` promises
everywhere else that the connector never does. HKCU's Run key is the one a
rider can set, read and remove without an administrator, which is what makes it
the right one here even though it only fires at logon.

Two rules that are easy to get wrong, both of which fail silently:

**Only the frozen executable may register.** Pointing Run at a venv's
``python.exe -m wattracker_connector`` works right up until the venv is moved,
rebuilt or its interpreter upgraded - after which Windows starts nothing at
every logon and there is nothing anywhere to say why. So a non-frozen process
refuses instead, and the tray greys the menu item out and says so.

**Nothing is written unless the rider asks for it.** Importing this module
touches no registry key, and startup writes only through :func:`refresh`, which
rewrites a value that is already there and never creates one.

Importable on any OS: ``winreg`` is imported inside the functions that need it,
so the Linux suite can hold the shape of this even though it can never run it.
"""
from __future__ import annotations

import logging
import os
import sys
from typing import Optional

log = logging.getLogger(__name__)

# HKEY_CURRENT_USER, and deliberately nothing else. Named here once so the
# test that forbids HKLM has a single thing to read.
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"

# The value name is the identity of this entry: rewriting the same name is what
# makes enable() idempotent and what lets a moved executable be healed rather
# than duplicated. It must not carry a version.
VALUE_NAME = "wattracker-connector"


class AutostartUnavailable(Exception):
    """Autostart cannot be arranged here, with a reason worth showing a rider."""


def is_frozen() -> bool:
    """True in the PyInstaller build, false in a checkout or a venv."""
    return bool(getattr(sys, "frozen", False))


def supported() -> bool:
    """Whether this process is one that may register itself at all."""
    return os.name == "nt" and is_frozen()


def command() -> str:
    """The value data: this executable's absolute path, quoted.

    Quoted because ``C:\\Program Files\\...`` is the ordinary case and the Run
    key splits an unquoted path on its first space, which turns a working entry
    into one that launches ``C:\\Program``.
    """
    return f'"{os.path.abspath(sys.executable)}"'


def _open_run_key(write: bool):
    """The HKCU Run key, opened for reading or for writing.

    Raises AutostartUnavailable rather than ImportError off Windows, so the
    caller has one exception to catch for "this machine cannot do that".
    """
    try:
        import winreg
    except ImportError as exc:  # every OS that is not Windows
        raise AutostartUnavailable(
            "Starting with Windows is a Windows feature."
        ) from exc
    access = winreg.KEY_SET_VALUE if write else winreg.KEY_READ
    # Created if absent, which it is on a stripped image. Opening for read with
    # CreateKey would be a write, so only the write path may create it.
    if write:
        return winreg, winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, access)
    return winreg, winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, access)


def registered_command() -> Optional[str]:
    """What the Run key currently points at for us, or None if nothing does."""
    try:
        winreg, key = _open_run_key(write=False)
    except (AutostartUnavailable, OSError):
        return None
    try:
        with key:
            value, _kind = winreg.QueryValueEx(key, VALUE_NAME)
    except OSError:  # no such value - the ordinary "not enabled" case
        return None
    return value if isinstance(value, str) else None


def enabled() -> bool:
    """Whether Windows will start this connector at the next logon.

    A read, everywhere. Off Windows it is simply False rather than an error:
    the caller is a menu asking whether to draw a tick.
    """
    return registered_command() is not None


def _same_target(left: str, right: str) -> bool:
    """Whether two Run values name the same executable.

    Compared as paths, not as strings: quoting and case are both free
    variations on Windows, and treating ``"C:\\x\\Y.exe"`` and ``c:\\x\\y.exe``
    as different targets would rewrite the registry on every single launch.
    """
    def _normalise(value: str) -> str:
        return os.path.normcase(os.path.normpath(value.strip().strip('"')))

    return _normalise(left) == _normalise(right)


def enable() -> None:
    """Register this executable to start at logon. Idempotent.

    Refuses when this is not the frozen build, because the alternative is an
    entry that points at an interpreter and a working directory which will not
    both still be there in a month.
    """
    if os.name != "nt":
        raise AutostartUnavailable("Starting with Windows is a Windows feature.")
    if not is_frozen():
        raise AutostartUnavailable(
            "Only the packaged WattrackerConnector.exe can start with Windows. "
            "This is running from a Python environment, and an entry pointing "
            "at one stops working as soon as it moves."
        )
    winreg, key = _open_run_key(write=True)
    with key:
        winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, command())
    log.info("registered %s to start with Windows", VALUE_NAME)


def disable() -> None:
    """Remove the entry. Idempotent, but only about the entry being absent.

    "Already gone" is exactly FileNotFoundError, and only that. A bare OSError
    also covers the failures that mean the opposite - access denied, a hive
    held by another process, a policy-locked Run key - and swallowing those
    made the tray lie: _toggle_autostart shows the warning balloon by catching
    what this raises, so with nothing ever raised the rider was told "It will
    no longer start with Windows." over an entry that was still there and
    would still run at the next logon. A toggle that fails silently in the
    direction of *more* startup is the failure this module exists to prevent.

    AutostartUnavailable is still swallowed, because that one really is the
    idempotent case: a machine that cannot autostart at all has nothing to
    remove.
    """
    try:
        winreg, key = _open_run_key(write=True)
    except AutostartUnavailable:
        return
    try:
        with key:
            winreg.DeleteValue(key, VALUE_NAME)
    except FileNotFoundError:
        return  # no such value: already in the desired state
    log.info("removed %s from the Windows startup entries", VALUE_NAME)


def refresh() -> bool:
    """Re-point an existing entry at where this executable actually is.

    The rider ticked the box once, then moved the exe out of Downloads into
    somewhere they meant to keep it. Nothing tells Windows that, so the entry
    keeps naming a file that is no longer there and autostart quietly stops -
    the failure being invisible is the whole problem with it. Called at startup,
    where the running executable's path is the one fact that settles it.

    Never creates an entry: if the rider has not asked for autostart, this does
    nothing at all. Returns True if it healed one.
    """
    if not supported():
        return False
    current = registered_command()
    if current is None or _same_target(current, command()):
        return False
    try:
        enable()
    except (AutostartUnavailable, OSError):
        log.warning("could not update the Windows startup entry", exc_info=True)
        return False
    log.info(
        "the startup entry pointed at %s; repointed it at this executable",
        current,
    )
    return True
