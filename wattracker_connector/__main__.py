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
import logging
import logging.handlers
import os
import queue
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
        root.addHandler(handler)
    if sys.stderr is not None:
        stream = _ConnectorStreamHandler()
        stream.setFormatter(formatter)
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
# binary that autostarts is a gadget worth not handing out. These are exactly
# the two optional halves the spec collects best-effort.
_SMOKE_IMPORTABLE = ("bleak", "webviewpy")

# One connector per logon session. "Local\" is the right scope and "Global\"
# would be the wrong one: two riders signed in to the same machine are two
# riders with two trainers. The server's one-connector-per-*account* rule is a
# different rule, enforced at the other end, and shows up here as _Replaced.
_MUTEX_NAME = r"Local\wattracker-connector"
_ERROR_ALREADY_EXISTS = 183

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
        self._notify = notify

    def open(self, url: str) -> None:
        self._requests.put(url)

    def present(self) -> bool:
        """Whether a window is up. Safe from any thread."""
        with self._lock:
            return self._window is not None

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
            self._notify("wattracker", f"{exc} Opening your browser instead.")
            window_module.open_in_browser(url)
            return
        with self._lock:
            self._window = window
        try:
            window.run()
        finally:
            with self._lock:
                self._window = None
            try:
                window.destroy()
            except Exception:
                log.debug("could not destroy the window", exc_info=True)


def _run_with_tray(connector: Connector, settings: dict) -> int:
    """Start the three threads, and take them down in the right order."""
    global _instance_handle
    from . import autostart, tray_win32, webview as window_module

    may_run, _instance_handle = _claim_single_instance()
    if not may_run:
        # Distinct from the server's one-per-account rule: this one is about
        # two copies on one desktop, and the running one is perfectly good.
        log.info("a connector is already running in this session; exiting")
        tray_win32.signal_existing_instance()
        return 0

    # Only ever repoints an entry the rider already asked for, at the one path
    # that is known to be right: the executable currently running.
    autostart.refresh()

    windows = _WindowLoop(notify=lambda *a, **k: tray.notify(*a, **k))

    def _open_window() -> None:
        """The tray's Open, on one of its workers, so it may take its time."""
        if windows.present():
            windows.focus()
            return
        try:
            url = window_module.session_url(settings["server"], settings["token"])
        except window_module.WindowUnavailable as exc:
            # A revoked device and an unreachable server are different
            # problems with different fixes, and the message already says
            # which one this is. Showing it is the whole point of a tray.
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
    connector_thread = _ConnectorThread(connector)

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

    if args.smoke_import:
        name = args.smoke_import
        if name not in _SMOKE_IMPORTABLE:
            log.error("--smoke-import only accepts %s", ", ".join(_SMOKE_IMPORTABLE))
            return 2
        try:
            __import__(name)
        except Exception:
            # Logged, not printed: the frozen build this exists for is
            # windowed, so the log file is where the smoke test reads it.
            log.error("could not import %s", name, exc_info=True)
            return 1
        log.info("%s imported successfully", name)
        return 0

    stored = load()
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
    }
    missing = [k for k in ("server", "token") if not settings[k]]
    if missing:
        # Named explicitly rather than "invalid configuration": the first-run
        # experience is someone pasting a token, and they should be told which
        # half they left out.
        message = (
            f"Missing: {', '.join(missing)}. Pair a device on the server's "
            "Settings page, then run:\n"
            "  wattracker-connector --server http://SERVER:8000 --token TOKEN --save"
        )
        print(message, file=sys.stderr)
        # Logged as well as printed, because the build this matters most for
        # is the windowed one, where ``print`` to a stream that is None is a
        # silent no-op and the log file is the only place anyone can read it.
        log.error("%s", message)
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
    )
    if _tray_wanted(args):
        if os.name != "nt":
            print(
                "--tray needs Windows; run with --headless on this machine.",
                file=sys.stderr,
            )
            return 2
        return _run_with_tray(connector, settings)

    try:
        asyncio.run(connector.run_forever())
    except KeyboardInterrupt:
        log.info("stopping")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
