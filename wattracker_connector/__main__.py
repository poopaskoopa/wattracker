"""Entry point for the connector, with and without a desktop behind it.

One process, three threads, and the split between them is forced rather than
chosen:

    main       the window, when there is one. ``webview_run`` is a native
               event loop that wants the main thread - on macOS it is not
               negotiable, and there is no reason to keep two stories.
    tray       the notification icon, its hidden window and its message pump.
               Win32 gives every thread its own queue, which is what makes
               this legal.
    connector  ``asyncio.run(connector.run_forever())``, exactly the loop the
               headless run uses. The tray wraps this ``Connector``; it does
               not reimplement any of it.

``--headless`` keeps the old shape - no tray, no window, the run loop on the
main thread - and is what the packaging smoke test and any service manager
drive.

    wattracker-connector --server http://192.168.1.10:8000 --token ... --save
    wattracker-connector                # reuses the saved settings
"""
from __future__ import annotations

import argparse
import asyncio
import importlib
import logging
import logging.handlers
import os
import queue
import re
import sys
import threading
from typing import Callable, List, Optional

from wattracker.config import _restrict

from .client import Connector, ConnectorStatus
from .config import config_path, load, log_path, save
from .handlers import ConnectorConfig

log = logging.getLogger(__name__)

# Small enough that a rider can send one, large enough to hold the reconnect
# history that makes a flaky link diagnosable.
_LOG_MAX_BYTES = 512 * 1024
_LOG_BACKUPS = 2

# Exact secret values to scrub from every log line, registered at startup by
# whoever learns one. Written once in main() before any thread starts and only
# read afterwards, which is why it needs no lock.
_SECRETS: "set[str]" = set()

# Below this, a "secret" is a substring of ordinary words and scrubbing it would
# shred the log rather than protect anything. Real device tokens are 43 chars
# (secrets.token_urlsafe(32)), so nothing legitimate is turned away.
_MIN_SECRET_LEN = 16


def redact_secret(value: object) -> None:
    """Register a credential so it never appears in the log in any spelling.

    The ``Bearer`` pattern below only catches a token spelled the way an HTTP
    header spells it. Register the value itself and a bare one - in a settings
    dump, a dict repr, an exception message - is caught too.
    """
    if isinstance(value, str) and len(value) >= _MIN_SECRET_LEN:
        _SECRETS.add(value)


def forget_secrets() -> None:
    """Drop the registry. For tests; the connector registers once and runs."""
    _SECRETS.clear()


class _SecretRedactingFilter(logging.Filter):
    """Keep the device token out of the connector log.

    Why this exists: ``-v`` sets the root logger to DEBUG, and ``websockets``
    logs every handshake header at that level - ``"> Authorization: Bearer
    <token>"``, one line per connection attempt. That used to be transient
    stderr on a developer's console. It now lands in a rotating file the rider
    is asked for whenever the link misbehaves, so "send me your connector.log"
    would hand over a live credential; on this branch that credential opens a
    web session, so it would hand over the account.

    Redacting rather than silencing ``websockets`` on purpose. The handshake
    lines are exactly what ``-v`` is for - a wrong Host or a 403 from the
    server is diagnosed from them - and a level cut would take the diagnosis
    away along with the leak. Only the credential is removed; the header is
    still visibly there.

    This is installed on the HANDLERS, never on a logger. A filter on the
    connector's root logger would not see this record at all: logger filters
    run only for records logged through that logger, and ``websockets.client``
    is a different logger that merely propagates to the same handlers.
    """

    # Anchored on the scheme name so it cannot chew through unrelated text, and
    # bounded to the character set a credential can actually use (RFC 6750
    # token68 plus the base64url alphabet token_urlsafe emits). The floor of 8
    # is deliberately low enough to over-match the odd line of prose - "bearer
    # authentication failed" comes out redacted - because the two errors are
    # not symmetrical: over-matching costs a word in a log, under-matching
    # writes a live credential to a file the rider is asked to email.
    _BEARER = re.compile(r"(?i:bearer)\s+[A-Za-z0-9._~+/=-]{8,}")

    @classmethod
    def redact(cls, value: str) -> str:
        text = cls._BEARER.sub("Bearer [REDACTED]", value)
        for secret in _SECRETS:
            if secret in text:
                text = text.replace(secret, "[REDACTED]")
        return text

    def _scrub(self, value: object) -> object:
        # Non-strings are rendered by the formatter, so they have to be
        # rendered here too or a token inside a dict/repr walks straight past.
        # Only a value that actually changed is replaced, so "%d" and friends
        # keep their argument's type on the overwhelmingly common path.
        if isinstance(value, str):
            return self.redact(value)
        try:
            text = str(value)
        except Exception:
            # It cannot be rendered here, so it cannot be rendered into the log
            # either - nothing can leak through a __str__ that raises.
            return value
        cleaned = self.redact(text)
        return cleaned if cleaned != text else value

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            # _scrub, not an isinstance(str) guard: getMessage() renders a
            # non-string msg with str(), so a dict or an object whose repr
            # carries the header used to walk straight past a check that only
            # looked at strings - while the same value passed as an *argument*
            # was scrubbed. One rule for both.
            record.msg = self._scrub(record.msg)
            if isinstance(record.args, tuple):
                record.args = tuple(self._scrub(a) for a in record.args)
            elif isinstance(record.args, dict):
                record.args = {k: self._scrub(v) for k, v in record.args.items()}
            # Exceptions are rendered by the formatter, from exc_info, long
            # after this filter has run - so scrubbing msg and args does not
            # touch them. A library that raises with the header in the message
            # (or attaches the request to the exception) would write the
            # credential to the file verbatim. Render the traceback here and
            # hand the formatter the redacted text instead: logging caches
            # exc_text and will not re-render it.
            if record.exc_info:
                if record.exc_text is None:
                    record.exc_text = logging.Formatter().formatException(
                        record.exc_info
                    )
                record.exc_text = self.redact(record.exc_text)
                record.exc_info = None
            elif record.exc_text:
                record.exc_text = self.redact(record.exc_text)
            if record.stack_info:
                record.stack_info = self.redact(record.stack_info)
        except Exception:
            # Logging must never be what breaks the connector, and a record
            # that could not be scrubbed is still better dropped than emitted:
            # blank the message rather than let an unredacted one through.
            record.msg = "a log line could not be redacted and was dropped"
            record.args = ()
            # Dropped means dropped: a traceback that survived the failure
            # above is exactly the thing most likely to be carrying the
            # credential.
            record.exc_info = None
            record.exc_text = None
            record.stack_info = None
        return True


class _ConnectorHandler:
    """Marks the handlers this module installed, so a second call replaces them."""


class _OwnerOnlyRotatingFileHandler(
    _ConnectorHandler, logging.handlers.RotatingFileHandler
):
    """A rotating handler whose files are owner-only, rotations included.

    Restricting once after construction would protect today's file and none of
    the ones rotation creates later, so the lockdown belongs on the open path
    rather than beside the call that first opens it.
    """

    def _open(self):
        stream = super()._open()
        _restrict(self.baseFilename, 0o600, is_dir=False)
        return stream


class _ConnectorStreamHandler(_ConnectorHandler, logging.StreamHandler):
    """A plain stderr handler, tagged so it can be replaced rather than stacked."""


def _configure_logging(verbose: bool) -> None:
    """Log to a file always, and to stderr only when there is one.

    The frozen tray build is windowed: it has no stderr at all, and on Windows
    writing to that closed handle raises rather than being discarded. So the
    file is the primary destination and the stream handler is the conditional
    one - which also leaves the pip-installed console script behaving exactly
    as it did.

    Calling this twice replaces its own handlers rather than stacking a second
    copy of each, so a second call cannot start double-logging every line.

    Every handler installed here carries _SecretRedactingFilter. ``verbose``
    turns on DEBUG for the whole process, which is what makes ``websockets``
    print the Authorization header of each handshake; the filter is what keeps
    that switch from writing the device token into a file that persists.
    """
    root = logging.getLogger()
    for existing in [h for h in root.handlers if isinstance(h, _ConnectorHandler)]:
        root.removeHandler(existing)
        existing.close()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    try:
        handler = _OwnerOnlyRotatingFileHandler(
            log_path(), maxBytes=_LOG_MAX_BYTES, backupCount=_LOG_BACKUPS,
            encoding="utf-8",
        )
    except OSError:
        # An unwritable config dir must not stop the connector running; it
        # only costs the log. Reported through whatever stderr exists.
        handler = None
        print("could not open the connector log file", file=sys.stderr)
    if handler is not None:
        handler.setFormatter(formatter)
        handler.addFilter(_SecretRedactingFilter())
        root.addHandler(handler)
    if sys.stderr is not None:
        stream = _ConnectorStreamHandler()
        stream.setFormatter(formatter)
        stream.addFilter(_SecretRedactingFilter())
        root.addHandler(stream)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wattracker-connector",
        description=(
            "Give a wattracker server access to this machine's Zwift folders "
            "and Bluetooth trainer."
        ),
    )
    parser.add_argument("--server", help="Server base URL, e.g. http://192.168.1.10:8000")
    parser.add_argument(
        "--token",
        help=(
            "Device token from the server's Settings page. Pass it once with "
            "--save and omit it afterwards: an argument is visible to every "
            "process on this machine (ps / Task Manager) and lands in shell "
            "history, whereas the saved config file is written 0600"
        ),
    )
    parser.add_argument("--activities-dir", help="Override the Zwift Activities folder")
    parser.add_argument("--workouts-dir", help="Override the Zwift Workouts folder")
    parser.add_argument(
        "--scan-interval", type=float, metavar="SECONDS",
        help=(
            "How often to check the Activities folder for a finished ride, in "
            "seconds (default 60, minimum 5). A .fit has to sit unchanged for "
            "a minute before it counts as finished, so a shorter interval "
            "brings the report forward but does not shorten that minute. Use "
            "0 to stop watching, which leaves the server's daily sweep as the "
            "only thing that imports rides"
        ),
    )
    parser.add_argument(
        "--save", action="store_true",
        help="Write these settings to the config file and use them from now on",
    )
    parser.add_argument(
        "--show-config", action="store_true",
        help="Print the saved settings (token redacted) and exit",
    )
    parser.add_argument(
        "--headless", action="store_true",
        help=(
            "Run without the tray icon even when this is the frozen build. "
            "The frozen executable otherwise puts itself in the notification "
            "area; this is how a service manager, a terminal or the packaging "
            "smoke test drives the same connector without a desktop"
        ),
    )
    parser.add_argument(
        "--tray", action="store_true",
        help=(
            "Run in the notification area. Implied by the packaged Windows "
            "executable, which is what it is for; --headless overrides it"
        ),
    )
    parser.add_argument(
        "--smoke-import", metavar="MODULE",
        help=(
            "Import MODULE and exit 0 if it worked, 1 if it did not. For "
            "packaging/smoke_frozen_connector.py: bleak and webviewpy are both "
            "resolved at runtime, so PyInstaller cannot see them statically "
            "and a build that dropped one starts perfectly and is simply "
            "missing a feature. A frozen windowed binary has no other way to "
            "be asked a question"
        ),
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser


# What --smoke-import will import. An allowlist rather than "whatever you
# named": this flag ships to riders, and an arbitrary-import switch on a
# binary that autostarts is a gadget worth not handing out. These are the
# optional halves the spec collects best-effort, plus the one module that
# distinguishes "bleak is packaged" from "bleak can reach a radio": bleak
# resolves its backend in the constructors, not at import, so the top-level
# package imports perfectly in a build whose winrt backend never made it in.
_SMOKE_IMPORTABLE = ("bleak", "bleak.backends.winrt.client", "webviewpy")

# One connector per logon session. "Local\" is the right scope and "Global\"
# would be the wrong one: two riders signed in to the same machine are two
# riders with two trainers. The server's one-connector-per-*account* rule is a
# different rule, enforced at the other end, and shows up here as _Replaced.
_MUTEX_NAME = r"Local\wattracker-connector"

_ERROR_ALREADY_EXISTS = 183

# MB_ICONERROR, plus the two flags that stop the dialog opening behind
# whatever the rider was looking at when they double-clicked us. MB_OK is 0x0
# and is left out rather than written as a term that changes nothing.
_MB_STARTUP_ERROR = 0x10 | 0x10000 | 0x40000


def _fatal(message: str) -> None:
    """Say why we are not starting, somewhere the rider can actually see it.

    The frozen build is windowed, so ``print`` to a stderr that is None is a
    silent no-op and the log file is somewhere they have to be told about -
    by an icon that, in exactly this situation, never appears. The result is
    an executable that flashes and vanishes, which reads as a broken download
    rather than as a connector that has not been paired yet.

    So: the log always, stderr when there is one, and a message box only when
    there is not - a console run and the packaging smoke test both have a
    stream to read and must not be left waiting on a modal dialog.
    """
    log.error("%s", message)
    if sys.stderr is not None:
        print(message, file=sys.stderr)
        return
    # print(..., file=None) means sys.stdout, not "nowhere", so the guard above
    # is what keeps the two destinations from being decided by accident.
    if os.name != "nt":
        return
    try:
        import ctypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.MessageBoxW(None, message, "wattracker connector",
                           _MB_STARTUP_ERROR)
    except Exception:
        # A connector that cannot even complain is still allowed to exit.
        log.warning("could not show the startup error", exc_info=True)


# How long the connector is given to notice a stop before it is cancelled, and
# how long the whole shutdown may take. Both are short: this is a rider who has
# clicked Quit and is watching an icon they expect to disappear.
_STOP_GRACE_S = 3.0
_JOIN_TIMEOUT_S = 10.0
_TRAY_JOIN_TIMEOUT_S = 5.0

# Held for the life of the process: Windows releases a named mutex when its
# last handle closes, so a garbage-collected handle would make every launch
# believe it was the only one.
_instance_handle = None


def _tray_wanted(args) -> bool:
    """Whether this run puts an icon in the notification area.

    ``--headless`` wins over everything, including an explicit ``--tray``: the
    packaging smoke test drives the frozen binary that way and a tray would
    leave it waiting for a rider who is not there. Otherwise the frozen build
    implies it, because a windowed executable with no icon is a process a rider
    can neither see nor stop.
    """
    if args.headless:
        return False
    if args.tray:
        return True
    return bool(getattr(sys, "frozen", False))


def _setup_wanted(args) -> bool:
    """Whether an unpaired run may ask for the pairing, rather than explain it.

    The same test as the tray, and for the same reason: this is the build that
    a rider double-clicks, so it is the build with a desktop to ask on and no
    console to be told on. ``--headless`` excludes itself, which is what keeps
    packaging/smoke_frozen_connector.py's unpaired check reading an exit code
    instead of waiting on a dialog nobody is there to dismiss.
    """
    return _tray_wanted(args) and os.name == "nt"


def _pair_interactively(settings: dict):
    """Run the setup window. Returns ``(settings_or_None, was_asked)``.

    The second half of that pair is what separates a rider who closed the
    window from a desktop that could not show one, and they want opposite
    endings: the first has just been told what the connector needs and chose
    not to give it, so telling them again in a second dialog is nagging; the
    second has been told nothing at all, and falls back to the message an
    unpaired connector has always printed.

    Never fatal on its own either way. A window that will not open is a reason
    to explain, not a reason to crash.
    """
    from . import setup_win32

    try:
        return setup_win32.prompt_for_settings(settings), True
    except setup_win32.SetupUnavailable as exc:
        log.warning("could not open the setup window: %s", exc)
    except Exception:
        log.exception("the setup window failed")
    return None, False


def _claim_single_instance(name: str = _MUTEX_NAME):
    """Take the named mutex. Returns (may_run, handle).

    A second launch is the ordinary consequence of a desktop shortcut and an
    autostart entry both existing, so it is answered rather than treated as an
    error: the running icon says hello and this process goes away.

    ``name`` is an argument only so a test can claim something that is not the
    real connector's mutex; nothing else should pass it.
    """
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.CreateMutexW.argtypes = [
        ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p
    ]
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel32.CreateMutexW(None, False, name)
    error = ctypes.get_last_error()
    if not handle:
        # Whatever went wrong here, refusing to start over it would be worse
        # than the duplicate it was guarding against.
        log.warning("could not claim the single-instance mutex (error %d)", error)
        return True, None
    if error == _ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(ctypes.c_void_p(handle))
        return False, None
    return True, handle


def _claim_or_signal() -> bool:
    """Take the single-instance mutex, or give the running connector the click.

    Called before the pairing window rather than on the way into the tray, and
    that ordering is the whole point. A desktop shortcut and a hurried second
    double-click is the ordinary way a second launch happens - the mutex
    comment says so - and on a never-paired exe the old ordering put a setup
    dialog on the screen first and discovered the redundancy afterwards. Two
    identical windows asking for the same token, one of which is about to
    exit whatever the rider types into it.
    """
    global _instance_handle

    from . import tray_win32

    may_run, _instance_handle = _claim_single_instance()
    if may_run:
        return True
    # Distinct from the server's one-per-account rule: this one is about two
    # copies on one desktop, and the running one is perfectly good.
    log.info("a connector is already running in this session; exiting")
    tray_win32.signal_existing_instance()
    return False


class _ConnectorThread:
    """The connector's thread, and a stop that actually stops it.

    ``Connector.stop()`` sets an ``asyncio.Event``, which is neither safe to
    touch from another thread nor - on its own - sufficient: the run loop
    spends most of its life awaiting a frame on a socket that nobody is about
    to send anything on, and setting a flag does not interrupt a read. So the
    flag is set from inside the loop, given a moment to be noticed, and the
    task is cancelled if it is not.

    The radio is released here in that case, deliberately. ``run_forever``
    releases it on its way out, and a task that was cancelled never reaches its
    own last line - which would leave a trainer held in ERG by a process that
    has already gone.
    """

    def __init__(self, connector: Connector) -> None:
        self._connector = connector
        self._stop_requested = threading.Event()
        self.thread = threading.Thread(
            target=self._run, name="connector", daemon=True
        )

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self._stop_requested.set()
        self.thread.join(timeout=_JOIN_TIMEOUT_S)
        if self.thread.is_alive():
            log.warning("the connector thread did not finish; exiting anyway")

    def _run(self) -> None:
        try:
            asyncio.run(self._drive())
        except Exception:
            log.exception("the connector thread stopped unexpectedly")
        finally:
            # Whatever happened, nothing further is going to reconnect, and
            # the tray draws that differently from "trying".
            self._connector.status.stopped = True

    async def _drive(self) -> None:
        runner = asyncio.create_task(self._connector.run_forever())
        # A thread rather than call_soon_threadsafe: this is running before
        # anyone can ask for a stop, so there is no window in which a Quit
        # arrives to a loop that has no way to hear it yet.
        await asyncio.to_thread(self._stop_requested.wait)
        log.info("stopping the connector")
        self._connector.stop()
        _done, pending = await asyncio.wait({runner}, timeout=_STOP_GRACE_S)
        if pending:
            log.info("the connector was mid-read; cancelling it")
            runner.cancel()
            await asyncio.gather(runner, return_exceptions=True)
            await self._connector.ble.teardown()
            return
        try:
            failure = runner.exception()
        except asyncio.CancelledError:
            return
        if failure is not None:
            log.error("the connector loop ended with %r", failure)


class _WindowLoop:
    """Runs the rider's window on the main thread, one at a time.

    The tray asks for a window from its own thread and this loop opens it here,
    because the native loop wants to be the main one. Requests arrive on a
    queue; when a window is up, the main thread is inside its loop and not
    reading the queue at all, which is exactly why :meth:`present` exists - the
    caller checks before it mints a ticket, rather than queueing a second
    window that would open with an expired one an hour later.
    """

    _QUIT = object()

    def __init__(self, notify: Callable[..., None]) -> None:
        self._requests: "queue.Queue" = queue.Queue()
        self._lock = threading.Lock()
        self._window = None
        # Held between "a worker decided to open a window" and "that window is
        # up or has failed". present() alone cannot cover that gap - see claim().
        self._opening = False
        self._notify = notify

    def open(self, url: str) -> None:
        self._requests.put(url)

    def present(self) -> bool:
        """Whether a window is up. Safe from any thread."""
        with self._lock:
            return self._window is not None

    def claim(self) -> bool:
        """Take the right to open the one window. False if someone has it.

        present() is not enough on its own, and the gap is not theoretical.
        Minting a ticket is a network round trip, and _window is only set at
        the far end of it, inside _show on the main thread. Two Opens close
        together - two double-clicks, which is what a tray icon invites - both
        see present() False, and both mint. TicketStore.mint replaces a
        device's outstanding ticket, so the first ticket is dead before its
        window ever redeems it: window one lands on the login page, and window
        two only appears once window one is closed. That is precisely the
        outcome the ticket exists to prevent, produced by clicking twice.

        So the claim is taken *before* the mint, and released by whoever
        finishes the attempt.
        """
        with self._lock:
            if self._window is not None or self._opening:
                return False
            self._opening = True
            return True

    def release(self) -> None:
        """Give the claim back after an attempt that never reached a window."""
        with self._lock:
            self._opening = False

    def focus(self) -> None:
        """Bring the open window forward, best effort, from any thread.

        Windows may refuse to raise a window on behalf of a process that does
        not own the foreground, in which case the taskbar button flashes
        instead. That is the OS being deliberate, not a failure worth
        reporting to a rider.
        """
        with self._lock:
            window = self._window
        if window is None:
            return
        try:
            import ctypes

            handle = window.get_window()
            ctypes.WinDLL("user32").SetForegroundWindow(
                ctypes.c_void_p(getattr(handle, "value", handle))
            )
        except Exception:
            log.debug("could not raise the open window", exc_info=True)

    def quit(self) -> None:
        """End the loop, closing any open window first. Safe from any thread."""
        with self._lock:
            window = self._window
        if window is not None:
            try:
                # The library's documented cross-thread call, and the reason
                # this three-thread split is legal rather than lucky.
                window.terminate()
            except Exception:
                log.warning("could not terminate the window", exc_info=True)
        self._requests.put(self._QUIT)

    def run(self) -> None:
        while True:
            request = self._requests.get()
            if request is self._QUIT:
                return
            self._show(request)

    def _show(self, url: str) -> None:
        from . import webview as window_module

        try:
            window = window_module.open_window(url)
        except window_module.WindowUnavailable as exc:
            # No WebView2 runtime, or no engine at all. The ticket is
            # single-use and expires in a minute, so handing this same URL to
            # the rider's own browser is the same credential in a different
            # window, not a wider one.
            log.warning("opening a window failed: %s", exc)
            with self._lock:
                self._opening = False
            self._notify("wattracker", f"{exc} Opening your browser instead.")
            window_module.open_in_browser(url)
            return
        with self._lock:
            self._window = window
            self._opening = False
        try:
            window.run()
        finally:
            with self._lock:
                self._window = None
                self._opening = False
            try:
                window.destroy()
            except Exception:
                log.debug("could not destroy the window", exc_info=True)


def _run_with_tray(connector: Connector, settings: dict) -> int:
    """Start the three threads, and take them down in the right order.

    The single-instance mutex is *not* claimed here; main() has already done
    it, before any window went on the screen. See _claim_or_signal.
    """
    from . import autostart, tray_win32, webview as window_module

    # Only ever repoints an entry the rider already asked for, at the one path
    # that is known to be right: the executable currently running.
    autostart.refresh()

    # Bound before the callables that close over them. Both of these used to be
    # assigned below their own uses - legal, because nothing calls a tray
    # callback until the pump is running, but it makes the construction order
    # load-bearing and silent about it: reorder two lines and the failure is a
    # NameError inside a tray worker, nowhere near the edit. The one remaining
    # forward reference is `windows`, which is genuine - the tray's callbacks
    # need it and it needs the tray's notify - and it is bound two lines later.
    connector_thread = _ConnectorThread(connector)

    def _open_window() -> None:
        """The tray's Open, on one of its workers, so it may take its time."""
        if not windows.claim():
            # Either a window is up or another worker is already minting for
            # one. Both mean this click is answered by the window that is
            # coming, and no second ticket is spent invalidating the first.
            windows.focus()
            return
        try:
            url = window_module.session_url(settings["server"], settings["token"])
        except window_module.WindowUnavailable as exc:
            # A revoked device and an unreachable server are different
            # problems with different fixes, and the message already says
            # which one this is. Showing it is the whole point of a tray.
            windows.release()
            log.warning("could not open a session: %s", exc)
            tray.notify("wattracker", str(exc), level="warning")
            return
        windows.open(url)

    def _quit() -> None:
        """The order in the plan: connector, then the window, then the pump."""
        log.info("quit requested from the tray")
        connector_thread.stop()
        windows.quit()

    tray = tray_win32.TrayIcon(
        status=connector.status, on_open=_open_window, on_quit=_quit
    )
    windows = _WindowLoop(notify=tray.notify)

    def _pump() -> None:
        try:
            tray.run()
        except Exception:
            log.exception("the tray stopped unexpectedly")
        finally:
            # Never leave the main thread waiting on a queue nothing can fill.
            windows.quit()

    tray_thread = threading.Thread(target=_pump, name="tray", daemon=True)
    connector_thread.start()
    tray_thread.start()
    try:
        windows.run()
    except KeyboardInterrupt:
        pass
    finally:
        # Unconditional, and idempotent: Quit has already done this, but a tray
        # that died on its own has not, and the connector thread parks a worker
        # on that event. Interpreter shutdown joins pool threads, so leaving one
        # blocked here is a process that quits and then does not exit.
        connector_thread.stop()
        tray.stop()
        tray_thread.join(timeout=_TRAY_JOIN_TIMEOUT_S)
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    args = _parser().parse_args(argv)
    _configure_logging(args.verbose)
    # Registered as early as each one is known, not once at the end: a token is
    # only protected from the lines logged after it is registered.
    redact_secret(args.token)

    if args.smoke_import:
        name = args.smoke_import
        if name not in _SMOKE_IMPORTABLE:
            log.error("--smoke-import only accepts %s", ", ".join(_SMOKE_IMPORTABLE))
            return 2
        try:
            module = importlib.import_module(name)
        except Exception:
            # Logged, not printed: the frozen build this exists for is
            # windowed, so the log file is where the smoke test reads it.
            log.error("could not import %s", name, exc_info=True)
            return 1
        if name == "webviewpy" and not getattr(
            module, "is_webviewlibrary_load_ok", False
        ):
            # Importing webviewpy proves nothing about the window. Its module
            # level declare_library_path(None, False) swallows a failed CDLL
            # and only sets this flag; the first Webview() then raises
            # "webview library not loaded", which webview.py reports to the
            # rider as a missing WebView2 runtime. So a build that packaged
            # the DLL to the wrong path imports green here and opens a browser
            # tab for every window, blaming the machine. Ask the flag.
            log.error(
                "%s imported but its native library did not load; the frozen "
                "build has webview.dll somewhere it does not look", name,
            )
            return 1
        log.info("%s imported successfully", name)
        return 0

    stored = load()
    redact_secret(stored.get("token"))
    if args.show_config:
        from .config import describe

        print(f"config file: {config_path()}")
        for key, value in sorted(describe(stored).items()):
            print(f"  {key}: {value}")
        return 0

    settings = {
        "server": args.server or stored.get("server"),
        "token": args.token or stored.get("token"),
        "activities_dir": args.activities_dir or stored.get("activities_dir"),
        "workouts_dir": args.workouts_dir or stored.get("workouts_dir"),
        # `is not None`, not `or`: 0 is the value that turns the watcher off,
        # and `or` would read it as "unset" and hand back the stored interval -
        # so --scan-interval 0 --save would appear to work and change nothing.
        "scan_interval": (
            args.scan_interval if args.scan_interval is not None
            else stored.get("scan_interval")
        ),
    }
    if _tray_wanted(args):
        if os.name != "nt":
            _fatal("--tray needs Windows; run with --headless on this machine.")
            return 2
        if not _claim_or_signal():
            return 0

    missing = [k for k in ("server", "token") if not settings[k]]
    if missing and _setup_wanted(args):
        paired, asked = _pair_interactively(settings)
        if paired:
            settings.update(paired)
            redact_secret(settings["token"])
            save(settings)
            log.info("paired from the setup window; saved to %s", config_path())
            missing = []
        elif asked:
            # They saw the two questions and closed the window. Nothing to add.
            log.info("the setup window was cancelled; nothing was saved")
            return 2
    if missing:
        # Named explicitly rather than "invalid configuration": the first-run
        # experience is someone pasting a token, and they should be told which
        # half they left out.
        message = (
            f"Missing: {', '.join(missing)}. Pair a device on the server's "
            "Settings page, then run:\n"
            "  wattracker-connector --server http://SERVER:8000 --token TOKEN --save"
        )
        _fatal(message)
        return 2

    if args.save:
        save(settings)
        print(f"Saved to {config_path()}")

    connector = Connector(
        server_url=settings["server"],
        token=settings["token"],
        config=ConnectorConfig(
            activities_dir=settings["activities_dir"],
            workouts_dir=settings["workouts_dir"],
        ),
        status=ConnectorStatus(),
        scan_interval=settings["scan_interval"],
    )
    if _tray_wanted(args):
        # Windows-ness and the single instance were both settled above, before
        # anything was drawn on the screen.
        return _run_with_tray(connector, settings)

    try:
        asyncio.run(connector.run_forever())
    except KeyboardInterrupt:
        log.info("stopping")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
