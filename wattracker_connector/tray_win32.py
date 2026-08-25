"""The notification-area icon, hand-rolled over Win32 with ctypes.

``Shell_NotifyIconW`` needs a window to send its callback messages to, so this
creates a hidden one and pumps its queue on a thread of its own. Win32 gives
every thread its own message queue and the icon belongs to whichever thread
owns its window, so the tray does not have to be the main thread - which it
cannot be, because the OS WebView wants that one.

No new dependency, which is the entire point: a tray built out of ctypes costs
nothing in the artifact, and the artifact is a single file a rider downloads.

Four things here are load-bearing rather than decorative.

**The hidden window is top-level, not message-only.** ``HWND_MESSAGE`` would be
the cheaper, more obvious choice and it is wrong: broadcast messages do not
reach message-only windows, and ``TaskbarCreated`` - the message that says
explorer restarted and took our icon with it - is a broadcast. An icon that
never comes back is the classic way a hand-rolled tray is quietly broken.

**Nothing slow happens on the pump.** Minting a session ticket is a network
round trip with a fifteen-second timeout; quitting stops a connector that may
be mid-ride. Both are handed to short-lived workers, and what they have to say
comes back as a posted message. A tray that stalls its pump is one Windows
eventually replaces with a ghost.

**It owns no connector state.** Everything it draws is read from the
``ConnectorStatus`` the connector thread writes - plain attributes, one writer,
one reader - so there is no second copy of "are we connected" to disagree with
the first.

**It imports on any OS and refuses only on construction.** The same shape
``webview.py`` uses, so the Linux suite can hold this module's structure even
though it can never run it: no ``winreg``, no ``ctypes.WINFUNCTYPE`` and no DLL
handle is touched at import time.
"""
from __future__ import annotations

import ctypes
import logging
import os
import sys
import threading
from collections import deque
from typing import Callable, Optional

from . import autostart
from .config import config_dir, log_path

log = logging.getLogger(__name__)


class TrayUnavailable(Exception):
    """There is no notification area to put an icon in."""


# --------------------------------------------------------------- Win32 types
# Spelled out in plain ctypes types rather than ctypes.wintypes, which does not
# import at all on Linux - and this module must. The sizes are the same ones
# wintypes would give.
_DWORD = ctypes.c_uint32
_UINT = ctypes.c_uint32
_INT = ctypes.c_int32
_LONG = ctypes.c_int32
_WORD = ctypes.c_uint16
_BYTE = ctypes.c_uint8
_HANDLE = ctypes.c_void_p
_WPARAM = ctypes.c_size_t
_LPARAM = ctypes.c_ssize_t
_LRESULT = ctypes.c_ssize_t


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", _DWORD), ("Data2", _WORD), ("Data3", _WORD),
        ("Data4", _BYTE * 8),
    ]


class _POINT(ctypes.Structure):
    _fields_ = [("x", _LONG), ("y", _LONG)]


class _MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", _HANDLE), ("message", _UINT), ("wParam", _WPARAM),
        ("lParam", _LPARAM), ("time", _DWORD), ("pt", _POINT),
    ]


class _WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", _UINT), ("style", _UINT),
        # Held as a raw pointer so this structure stays definable off Windows:
        # the WNDPROC prototype needs WINFUNCTYPE, which only exists there.
        ("lpfnWndProc", ctypes.c_void_p),
        ("cbClsExtra", _INT), ("cbWndExtra", _INT),
        ("hInstance", _HANDLE), ("hIcon", _HANDLE), ("hCursor", _HANDLE),
        ("hbrBackground", _HANDLE),
        ("lpszMenuName", ctypes.c_wchar_p), ("lpszClassName", ctypes.c_wchar_p),
        ("hIconSm", _HANDLE),
    ]


class _NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", _DWORD), ("hWnd", _HANDLE), ("uID", _UINT),
        ("uFlags", _UINT), ("uCallbackMessage", _UINT), ("hIcon", _HANDLE),
        ("szTip", ctypes.c_wchar * 128),
        ("dwState", _DWORD), ("dwStateMask", _DWORD),
        ("szInfo", ctypes.c_wchar * 256),
        ("uVersion", _UINT),
        ("szInfoTitle", ctypes.c_wchar * 64),
        ("dwInfoFlags", _DWORD), ("guidItem", _GUID), ("hBalloonIcon", _HANDLE),
    ]


# ----------------------------------------------------------- Win32 constants
_NIM_ADD, _NIM_MODIFY, _NIM_DELETE = 0, 1, 2
_NIF_MESSAGE, _NIF_ICON, _NIF_TIP, _NIF_INFO = 0x01, 0x02, 0x04, 0x10
_NIIF_INFO, _NIIF_WARNING, _NIIF_ERROR = 0x01, 0x02, 0x03

_WM_DESTROY = 0x0002
_WM_CLOSE = 0x0010
_WM_COMMAND = 0x0111
_WM_TIMER = 0x0113
_WM_LBUTTONDBLCLK = 0x0203
_WM_RBUTTONUP = 0x0205
_WM_CONTEXTMENU = 0x007B
_WM_NULL = 0x0000

# Our own messages. WM_APP is the range reserved for exactly this.
_WM_APP = 0x8000
_WM_TRAY_CALLBACK = _WM_APP + 1   # the shell telling us about our icon
_WM_TRAY_BALLOON = _WM_APP + 2    # "a worker left you something to show"
_WM_TRAY_STOP = _WM_APP + 3       # "take the icon down and let the pump exit"

_MF_STRING, _MF_GRAYED, _MF_CHECKED, _MF_SEPARATOR = 0x000, 0x001, 0x008, 0x800
_TPM_RIGHTBUTTON, _TPM_RETURNCMD, _TPM_NONOTIFY = 0x0002, 0x0100, 0x0080

_IMAGE_ICON = 1
_LR_LOADFROMFILE, _LR_SHARED = 0x0010, 0x8000
_IDI_ERROR, _IDI_WARNING = 32513, 32515
_SM_CXSMICON, _SM_CYSMICON = 49, 50

_IDC_ARROW = 32512
_ERROR_CLASS_ALREADY_EXISTS = 1410

# Menu command identifiers. Only their distinctness matters.
_ID_STATUS, _ID_DETAIL = 1, 2
_ID_OPEN, _ID_LOG, _ID_CONFIG, _ID_AUTOSTART, _ID_QUIT = 3, 4, 5, 6, 7

# The window class doubles as the way a second launch finds the first one, so
# it is a fixed public-ish name rather than something generated per process.
CLASS_NAME = "wattracker_connector_tray"
_WINDOW_TITLE = "wattracker connector"

# Sent by the shell to every top-level window when explorer restarts.
_TASKBAR_CREATED = "TaskbarCreated"
# Posted by a second launch of the connector to the first one's window.
SHOW_MESSAGE = "wattracker-connector.show"

# How often the icon re-reads ConnectorStatus. Fast enough that a rider who
# glances at the tray after plugging the network back in sees it go green while
# they are still looking, cheap enough to be free: it is six attribute reads.
_POLL_MS = 2000

# The shell is not always listening at logon - autostart puts us there before
# explorer has finished creating the notification area. TaskbarCreated covers
# the restart case, but not this one, so the first add is retried.
_ADD_ATTEMPTS = 30
_ADD_RETRY_MS = 1000

_STATE_CONNECTED, _STATE_OFFLINE, _STATE_STOPPED = "connected", "offline", "stopped"


def icon_path() -> str:
    """The app's own favicon, in the checkout or unpacked beside the exe.

    The spec bundles it to ``wattracker/web/static``; PyInstaller's onefile
    build unpacks that under ``sys._MEIPASS``. Outside the frozen build the
    package sits next to ``wattracker`` either way, installed or in a checkout.
    """
    base = getattr(sys, "_MEIPASS", None)
    if not base:
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, "wattracker", "web", "static", "favicon.ico")


# A window class belongs to the process, and it carries the window procedure
# that was registered with it - so a second TrayIcon in one process would get
# the first one's procedure, and send it messages about a window it has never
# heard of. The class therefore gets one procedure that belongs to nobody, and
# each window is looked up here. This is the ordinary Win32 arrangement; the
# alternative (a class name per instance) would break the second-launch signal,
# which finds the running connector precisely by class name.
_INSTANCES: dict = {}
_CLASS_WNDPROC = None
_USER32 = None
_SHELL32 = None


def _dispatch_to_instance(hwnd, message, wparam, lparam):
    tray = _INSTANCES.get(hwnd)
    if tray is None:
        # Messages sent while the window is still being created arrive before
        # it can be registered here, and messages after it is destroyed arrive
        # after it has been removed. Both belong to the default handler.
        return _user32().DefWindowProcW(_HANDLE(hwnd), message, wparam, lparam)
    return tray._handle_message(hwnd, message, wparam, lparam)


def _user32():
    """user32 with the argument types that keep 64-bit handles intact.

    Without these, ctypes assumes every argument and return value is a C int
    and silently truncates handles to 32 bits - which on this machine means
    windows that cannot be found and icons that never appear, with no error
    anywhere.
    """
    global _USER32
    if _USER32 is not None:
        return _USER32
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.CreateWindowExW.restype = _HANDLE
    user32.CreateWindowExW.argtypes = [
        _DWORD, ctypes.c_wchar_p, ctypes.c_wchar_p, _DWORD,
        _INT, _INT, _INT, _INT, _HANDLE, _HANDLE, _HANDLE, ctypes.c_void_p,
    ]
    user32.DefWindowProcW.restype = _LRESULT
    user32.DefWindowProcW.argtypes = [_HANDLE, _UINT, _WPARAM, _LPARAM]
    user32.DestroyWindow.argtypes = [_HANDLE]
    user32.GetMessageW.argtypes = [ctypes.POINTER(_MSG), _HANDLE, _UINT, _UINT]
    user32.TranslateMessage.argtypes = [ctypes.POINTER(_MSG)]
    user32.DispatchMessageW.argtypes = [ctypes.POINTER(_MSG)]
    user32.DispatchMessageW.restype = _LRESULT
    user32.PostMessageW.argtypes = [_HANDLE, _UINT, _WPARAM, _LPARAM]
    user32.SetTimer.argtypes = [_HANDLE, _WPARAM, _UINT, ctypes.c_void_p]
    user32.SetTimer.restype = _WPARAM
    user32.KillTimer.argtypes = [_HANDLE, _WPARAM]
    user32.CreatePopupMenu.restype = _HANDLE
    user32.AppendMenuW.argtypes = [_HANDLE, _UINT, _WPARAM, ctypes.c_wchar_p]
    user32.DestroyMenu.argtypes = [_HANDLE]
    user32.SetMenuDefaultItem.argtypes = [_HANDLE, _UINT, _UINT]
    user32.TrackPopupMenu.restype = _INT
    user32.TrackPopupMenu.argtypes = [
        _HANDLE, _UINT, _INT, _INT, _INT, _HANDLE, ctypes.c_void_p,
    ]
    user32.SetForegroundWindow.argtypes = [_HANDLE]
    user32.GetCursorPos.argtypes = [ctypes.POINTER(_POINT)]
    user32.LoadImageW.restype = _HANDLE
    user32.LoadImageW.argtypes = [
        _HANDLE, ctypes.c_void_p, _UINT, _INT, _INT, _UINT,
    ]
    user32.LoadCursorW.restype = _HANDLE
    user32.LoadCursorW.argtypes = [_HANDLE, ctypes.c_void_p]
    user32.DestroyIcon.argtypes = [_HANDLE]
    user32.RegisterWindowMessageW.restype = _UINT
    user32.RegisterWindowMessageW.argtypes = [ctypes.c_wchar_p]
    user32.RegisterClassExW.argtypes = [ctypes.POINTER(_WNDCLASSEXW)]
    user32.FindWindowW.restype = _HANDLE
    user32.FindWindowW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p]
    user32.GetSystemMetrics.argtypes = [_INT]
    user32.GetSystemMetrics.restype = _INT
    _USER32 = user32
    return user32


def _shell32():
    global _SHELL32
    if _SHELL32 is not None:
        return _SHELL32
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    shell32.Shell_NotifyIconW.argtypes = [_DWORD, ctypes.POINTER(_NOTIFYICONDATAW)]
    shell32.Shell_NotifyIconW.restype = ctypes.c_int
    _SHELL32 = shell32
    return shell32


def signal_existing_instance() -> bool:
    """Tell a connector already running in this session to show itself.

    The second launch's whole job: find the first one's hidden window by its
    class - unique to us, and the reason the class name is fixed - and post it a
    registered message. Registered messages are the documented way to send
    something of your own to a window in another process without inventing a
    number that collides with somebody else's.
    """
    if os.name != "nt":
        return False
    try:
        user32 = _user32()
        hwnd = user32.FindWindowW(CLASS_NAME, None)
        if not hwnd:
            return False
        message = user32.RegisterWindowMessageW(SHOW_MESSAGE)
        return bool(user32.PostMessageW(_HANDLE(hwnd), message, 0, 0))
    except Exception:
        log.warning("could not signal the running connector", exc_info=True)
        return False


class TrayIcon:
    """The icon, its menu, and the pump that serves both.

    ``status`` is read and never written. ``on_open`` and ``on_quit`` are
    called on worker threads, so they may block; anything they want the rider
    to see comes back through :meth:`notify`, which is safe from any thread.
    """

    def __init__(
        self,
        status,
        on_open: Callable[[], None],
        on_quit: Callable[[], None],
    ) -> None:
        if os.name != "nt":
            # Construction is where this fails, deliberately: importing must
            # work everywhere so the structure above can be tested off Windows.
            raise TrayUnavailable(
                "The notification area is a Windows feature; run with "
                "--headless on this machine."
            )
        self._status = status
        self._on_open = on_open
        self._on_quit = on_quit
        self._user32 = _user32()
        self._shell32 = _shell32()
        self._lock = threading.Lock()
        self._balloons: deque = deque()
        self._hwnd: Optional[int] = None
        self._icons: dict = {}
        self._state: Optional[str] = None
        # One balloon per outage, not one per poll. Cleared again by a
        # successful connection, so the *next* outage is announced too.
        self._warned_unreachable = False
        self._added = False
        self._add_attempts = 0
        self._quitting = False
        # How many times a second launch has announced itself. A test hook -
        # read by tests/test_connector_tray.py - because posting a registered
        # message to another process leaves nothing else to assert on.
        self._shown = 0
        self._taskbar_created = self._user32.RegisterWindowMessageW(_TASKBAR_CREATED)
        self._show_message = self._user32.RegisterWindowMessageW(SHOW_MESSAGE)
        # Icon handles are the process's, not a thread's, so they are loaded
        # here rather than on the pump - which also means the state the icon
        # will show can be asked for without a desktop to show it on.
        self._load_icons()

    # ------------------------------------------------------------- the pump
    def run(self) -> None:
        """Create the window and the icon, then serve messages until stopped.

        Everything below this line happens on the calling thread: a window
        belongs to the thread that created it, and only that thread's pump
        will ever be handed its messages.
        """
        self._register_class()
        self._create_window()
        self._refresh(force=True)
        self._add_icon()
        self._user32.SetTimer(_HANDLE(self._hwnd), 1, _POLL_MS, None)
        message = _MSG()
        while True:
            result = self._user32.GetMessageW(ctypes.byref(message), None, 0, 0)
            if result <= 0:  # 0 is WM_QUIT, -1 is an error we cannot pump past
                break
            self._user32.TranslateMessage(ctypes.byref(message))
            self._user32.DispatchMessageW(ctypes.byref(message))
        log.info("tray pump finished")

    def stop(self) -> None:
        """Take the icon down and let the pump return. Safe from any thread."""
        hwnd = self._hwnd
        if hwnd:
            self._user32.PostMessageW(_HANDLE(hwnd), _WM_TRAY_STOP, 0, 0)

    def notify(self, title: str, text: str, level: str = "info") -> None:
        """Show a balloon. Safe from any thread, which is why it is queued.

        Shell_NotifyIcon has to be called from the thread that owns the icon's
        window, so a caller on another thread leaves the message here and posts
        a nudge; the pump picks it up.
        """
        with self._lock:
            self._balloons.append((title, text, level))
        hwnd = self._hwnd
        if hwnd:
            self._user32.PostMessageW(_HANDLE(hwnd), _WM_TRAY_BALLOON, 0, 0)

    # ------------------------------------------------------ window and class
    def _register_class(self) -> None:
        global _CLASS_WNDPROC
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetModuleHandleW.restype = _HANDLE
        kernel32.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]
        instance = kernel32.GetModuleHandleW(None)

        if _CLASS_WNDPROC is None:
            # Built once and kept for the life of the process: the class holds
            # a raw pointer to it, and a garbage-collected callback would leave
            # Windows calling into freed memory.
            prototype = ctypes.WINFUNCTYPE(
                _LRESULT, _HANDLE, _UINT, _WPARAM, _LPARAM
            )
            _CLASS_WNDPROC = prototype(_dispatch_to_instance)

        window_class = _WNDCLASSEXW()
        window_class.cbSize = ctypes.sizeof(_WNDCLASSEXW)
        window_class.lpfnWndProc = ctypes.cast(_CLASS_WNDPROC, ctypes.c_void_p)
        window_class.hInstance = instance
        window_class.hCursor = self._user32.LoadCursorW(
            None, ctypes.c_void_p(_IDC_ARROW)
        )
        window_class.lpszClassName = CLASS_NAME
        self._instance = instance
        atom = self._user32.RegisterClassExW(ctypes.byref(window_class))
        if not atom and ctypes.get_last_error() != _ERROR_CLASS_ALREADY_EXISTS:
            raise TrayUnavailable(
                f"could not register the tray window class "
                f"({ctypes.get_last_error()})"
            )
        # Already registered is success: a class belongs to the process, not to
        # the window, so a tray taken down and put back up again in one process
        # finds its own class still there.

    def _create_window(self) -> None:
        # Top-level and never shown, rather than HWND_MESSAGE: only a
        # top-level window is sent TaskbarCreated when explorer restarts, and
        # without that the icon goes away for good the first time it does.
        hwnd = self._user32.CreateWindowExW(
            0, CLASS_NAME, _WINDOW_TITLE, 0, 0, 0, 0, 0,
            None, None, self._instance, None,
        )
        if not hwnd:
            raise TrayUnavailable(
                f"could not create the tray window ({ctypes.get_last_error()})"
            )
        self._hwnd = hwnd
        _INSTANCES[hwnd] = self

    # ---------------------------------------------------------------- icons
    def _load_icons(self) -> None:
        """One icon per state, at the size the notification area asks for."""
        width = self._user32.GetSystemMetrics(_SM_CXSMICON) or 16
        height = self._user32.GetSystemMetrics(_SM_CYSMICON) or 16
        path = icon_path()
        connected = self._user32.LoadImageW(
            None, ctypes.cast(ctypes.c_wchar_p(path), ctypes.c_void_p),
            _IMAGE_ICON, width, height, _LR_LOADFROMFILE,
        )
        if not connected:
            # Not fatal: an icon nobody can identify is better than no tray.
            log.warning("could not load %s; falling back to a system icon", path)
            connected = self._system_icon(_IDI_WARNING, width, height)
        self._icons = {
            _STATE_CONNECTED: connected,
            # Distinct at a glance, and free: the shell's own warning and error
            # icons rather than a second and third artwork file to keep in step
            # with the first.
            _STATE_OFFLINE: self._system_icon(_IDI_WARNING, width, height),
            _STATE_STOPPED: self._system_icon(_IDI_ERROR, width, height),
        }
        self._loaded_icon = connected

    def _system_icon(self, ident: int, width: int, height: int):
        return self._user32.LoadImageW(
            None, ctypes.c_void_p(ident), _IMAGE_ICON, width, height, _LR_SHARED
        )

    # --------------------------------------------------------- the icon data
    def _data(self, flags: int) -> _NOTIFYICONDATAW:
        data = _NOTIFYICONDATAW()
        data.cbSize = ctypes.sizeof(_NOTIFYICONDATAW)
        data.hWnd = self._hwnd
        data.uID = 1
        data.uFlags = flags
        data.uCallbackMessage = _WM_TRAY_CALLBACK
        return data

    def _add_icon(self) -> None:
        data = self._data(_NIF_MESSAGE | _NIF_ICON | _NIF_TIP)
        data.hIcon = self._icons.get(self._state)
        data.szTip = self._tooltip()
        self._added = bool(self._shell32.Shell_NotifyIconW(_NIM_ADD, ctypes.byref(data)))
        if self._added:
            # Worth a line: the log is the only thing a frozen build can say,
            # and "did the icon come back" is otherwise answerable only by
            # somebody looking at the screen.
            log.info("tray icon added")
            self._drain_balloons()
            return
        # At logon the shell may not have created the notification area yet.
        # TaskbarCreated does not cover that case - it is only sent when
        # explorer *re*starts - so the first add is retried on the timer.
        self._add_attempts += 1
        if self._add_attempts <= _ADD_ATTEMPTS:
            log.debug(
                "the shell would not take the tray icon (attempt %d); retrying",
                self._add_attempts,
            )
            self._user32.SetTimer(_HANDLE(self._hwnd), 2, _ADD_RETRY_MS, None)
        else:
            log.error("gave up adding the tray icon after %d attempts",
                      self._add_attempts)

    def _remove_icon(self) -> None:
        if self._added:
            data = self._data(0)
            self._shell32.Shell_NotifyIconW(_NIM_DELETE, ctypes.byref(data))
            self._added = False
        icon = getattr(self, "_loaded_icon", None)
        if icon:
            self._user32.DestroyIcon(_HANDLE(icon))
            self._loaded_icon = None

    # --------------------------------------------------------------- status
    def _snapshot(self):
        """Everything drawn, read from ConnectorStatus in one go.

        One read per attribute, into locals: the connector thread writes these
        while this one reads them, and re-reading ``connected`` between the
        icon and the tooltip is how they end up disagreeing.
        """
        status = self._status
        stopped = bool(getattr(status, "stopped", False))
        connected = bool(status.connected)
        if stopped:
            state = _STATE_STOPPED
        elif connected:
            state = _STATE_CONNECTED
        else:
            state = _STATE_OFFLINE
        return (
            state,
            status.server_url or "the server",
            status.last_error,
            status.last_connected_at,
            getattr(status, "stopped_reason", None),
        )

    def _tooltip(self) -> str:
        state, server, _error, _since, _reason = self._snapshot()
        if state == _STATE_CONNECTED:
            text = f"wattracker - connected to {server}"
        elif state == _STATE_STOPPED:
            text = "wattracker - stopped, not reconnecting"
        else:
            text = f"wattracker - not connected to {server}"
        return text[:127]  # szTip is 128 wide characters including the NUL

    def _refresh(self, force: bool = False) -> None:
        """Re-read the status and repaint the icon if it changed."""
        state, _server, error, _since, _reason = self._snapshot()
        # Checked before the unchanged-state return below, and deliberately:
        # a connector that cannot reach its server sits in OFFLINE from its
        # first poll to its last, so the state never *changes* and an
        # announcement hung off a transition would never fire. What changes is
        # last_error, which is set once the first attempt has actually failed -
        # which is why this cannot simply run when the icon is added.
        if state == _STATE_CONNECTED:
            self._warned_unreachable = False
        elif state == _STATE_OFFLINE and error and not self._warned_unreachable:
            self._warned_unreachable = True
            self._balloon_unreachable(error)
        if state == self._state and not force:
            return
        previous, self._state = self._state, state
        if not force and self._added:
            data = self._data(_NIF_ICON | _NIF_TIP)
            data.hIcon = self._icons.get(state)
            data.szTip = self._tooltip()
            self._shell32.Shell_NotifyIconW(_NIM_MODIFY, ctypes.byref(data))
        if state == _STATE_STOPPED and previous not in (None, _STATE_STOPPED):
            self._balloon_stopped()

    def _balloon_unreachable(self, error: str) -> None:
        """Say the server is not answering, once, and keep trying anyway.

        A server that is rebooting, a laptop that has just woken and a server
        that has been switched off for good all look identical from here, and
        only the last one wants the rider's attention. So this says it once and
        the connector carries on reconnecting: the icon and its tooltip are
        what report the state after that, and a balloon per retry would be a
        notification every few seconds all night.
        """
        if self._quitting:
            return
        server = self._snapshot()[1]
        self.notify(
            "wattracker connector",
            f"Cannot reach {server}: {error}. Still trying - check that the "
            "server is running and on the same network.",
            level="warning",
        )

    def _balloon_stopped(self) -> None:
        """Say why we stopped, because a dead icon explains nothing.

        The case this exists for is another connector taking the account over:
        ``run_forever`` deliberately does not reconnect, so without this the
        rider sees an icon that is simply there and a server that no longer
        knows about their trainer.
        """
        if self._quitting:
            return
        reason = self._snapshot()[4] or (
            "The connector has stopped and will not reconnect. See the log for "
            "what happened."
        )
        self.notify("wattracker connector stopped", reason, level="error")

    # ---------------------------------------------------------- the balloons
    def _drain_balloons(self) -> None:
        while True:
            with self._lock:
                if not self._balloons:
                    return
                title, text, level = self._balloons.popleft()
            self._show_balloon(title, text, level)

    def _show_balloon(self, title: str, text: str, level: str) -> None:
        if not self._added:
            return
        data = self._data(_NIF_INFO)
        data.szInfoTitle = title[:63]
        data.szInfo = text[:255]
        data.dwInfoFlags = {
            "info": _NIIF_INFO, "warning": _NIIF_WARNING, "error": _NIIF_ERROR,
        }.get(level, _NIIF_INFO)
        self._shell32.Shell_NotifyIconW(_NIM_MODIFY, ctypes.byref(data))

    # --------------------------------------------------------------- the menu
    def _show_menu(self) -> None:
        menu = self._user32.CreatePopupMenu()
        if not menu:
            return
        chosen = 0
        try:
            self._build_menu(menu)
            point = _POINT()
            self._user32.GetCursorPos(ctypes.byref(point))
            # Both of these are the documented dance rather than superstition:
            # without the foreground call the menu will not close when the
            # rider clicks elsewhere, and without the trailing post it can be
            # left drawn after it has already been dismissed.
            self._user32.SetForegroundWindow(_HANDLE(self._hwnd))
            chosen = self._user32.TrackPopupMenu(
                _HANDLE(menu),
                _TPM_RIGHTBUTTON | _TPM_RETURNCMD | _TPM_NONOTIFY,
                point.x, point.y, 0, _HANDLE(self._hwnd), None,
            )
            self._user32.PostMessageW(_HANDLE(self._hwnd), _WM_NULL, 0, 0)
        finally:
            self._user32.DestroyMenu(_HANDLE(menu))
        if chosen:
            self._invoke(chosen)

    def _build_menu(self, menu) -> None:
        state, server, error, since, reason = self._snapshot()
        if state == _STATE_CONNECTED:
            headline = f"Connected to {server}"
            detail = f"Since {_clock(since)}" if since else None
        elif state == _STATE_STOPPED:
            headline = "Stopped - not reconnecting"
            detail = _shorten(reason or error)
        else:
            headline = f"Not connected to {server}"
            detail = _shorten(error) if error else "Reconnecting..."

        append = self._user32.AppendMenuW
        # Greyed rather than absent: the status is the first thing the rider
        # opened this menu to find out, and a disabled item still shows it.
        append(_HANDLE(menu), _MF_STRING | _MF_GRAYED, _ID_STATUS, headline)
        if detail:
            append(_HANDLE(menu), _MF_STRING | _MF_GRAYED, _ID_DETAIL, detail)
        append(_HANDLE(menu), _MF_SEPARATOR, 0, None)
        append(_HANDLE(menu), _MF_STRING, _ID_OPEN, "&Open wattracker")
        append(_HANDLE(menu), _MF_STRING, _ID_LOG, "Open &log")
        append(_HANDLE(menu), _MF_STRING, _ID_CONFIG, "Open &config folder")
        append(_HANDLE(menu), _MF_SEPARATOR, 0, None)
        if autostart.supported():
            flags = _MF_STRING | (_MF_CHECKED if autostart.enabled() else 0)
            append(_HANDLE(menu), flags, _ID_AUTOSTART, "&Start with Windows")
        else:
            # Named rather than hidden: a rider looking for the setting should
            # find out why it is not on offer, not conclude it does not exist.
            append(
                _HANDLE(menu), _MF_STRING | _MF_GRAYED, _ID_AUTOSTART,
                "Start with Windows (packaged build only)",
            )
        append(_HANDLE(menu), _MF_SEPARATOR, 0, None)
        append(_HANDLE(menu), _MF_STRING, _ID_QUIT, "&Quit")
        # Bold, and what a double-click does. The two must agree.
        self._user32.SetMenuDefaultItem(_HANDLE(menu), _ID_OPEN, 0)

    def _invoke(self, command: int) -> None:
        if command == _ID_OPEN:
            self._open_window()
        elif command == _ID_LOG:
            self._reveal(log_path(), "log")
        elif command == _ID_CONFIG:
            self._reveal(config_dir(), "config folder")
        elif command == _ID_AUTOSTART:
            self._toggle_autostart()
        elif command == _ID_QUIT:
            self._quit()

    # ------------------------------------------------------------ the actions
    def _open_window(self) -> None:
        # On a worker: this mints a ticket over the network, which is a fifteen
        # second timeout against an unreachable server, and the pump has to
        # keep answering the shell throughout.
        self._worker("open", self._on_open)

    def _quit(self) -> None:
        if self._quitting:
            return
        self._quitting = True
        self._worker("quit", self._on_quit)

    def _toggle_autostart(self) -> None:
        try:
            if autostart.enabled():
                autostart.disable()
                self.notify(
                    "wattracker connector",
                    "It will no longer start with Windows.",
                )
            else:
                autostart.enable()
                self.notify(
                    "wattracker connector",
                    "It will start automatically when you sign in.",
                )
        except (autostart.AutostartUnavailable, OSError) as exc:
            log.warning("could not change the startup entry: %s", exc)
            self.notify("wattracker connector", str(exc), level="warning")

    def _reveal(self, path: str, what: str) -> None:
        try:
            os.startfile(path)  # noqa: S606 - the rider asked for this
        except OSError as exc:
            log.warning("could not open the %s: %s", what, exc)
            self.notify(
                "wattracker connector",
                f"Could not open the {what}: {exc}",
                level="warning",
            )

    def _worker(self, name: str, action: Callable[[], None]) -> None:
        def _run() -> None:
            try:
                action()
            except Exception as exc:  # a menu click must never kill the tray
                log.exception("the tray's %s action failed", name)
                self.notify("wattracker connector", str(exc), level="warning")

        threading.Thread(target=_run, name=f"tray-{name}", daemon=True).start()

    # ------------------------------------------------------------ the wndproc
    def _handle_message(self, hwnd, message, wparam, lparam):
        try:
            return self._dispatch(hwnd, message, wparam, lparam)
        except Exception:
            # An exception raised through a ctypes callback has nowhere to go
            # and takes the pump with it, which would leave a dead icon.
            log.exception("tray message %s failed", message)
            return 0

    def _dispatch(self, hwnd, message, wparam, lparam):
        if message == _WM_TRAY_CALLBACK:
            event = lparam & 0xFFFF
            if event == _WM_LBUTTONDBLCLK:
                self._open_window()
            elif event in (_WM_RBUTTONUP, _WM_CONTEXTMENU):
                self._show_menu()
            return 0
        if message == _WM_TIMER:
            if wparam == 2:  # the retry timer, not the poll
                self._user32.KillTimer(_HANDLE(self._hwnd), 2)
                if not self._added:
                    self._add_icon()
                return 0
            self._refresh()
            return 0
        if message == _WM_TRAY_BALLOON:
            self._drain_balloons()
            return 0
        if message == self._taskbar_created:
            # explorer restarted and took the notification area with it. The
            # icon is gone, not hidden; adding it again is the only way back.
            log.info("explorer restarted; adding the tray icon again")
            self._added = False
            self._add_attempts = 0
            self._add_icon()
            return 0
        if message == self._show_message:
            self._shown += 1
            self.notify(
                "wattracker connector",
                "It is already running - this icon is it.",
            )
            return 0
        if message == _WM_TRAY_STOP or message == _WM_CLOSE:
            self._user32.KillTimer(_HANDLE(self._hwnd), 1)
            self._user32.DestroyWindow(_HANDLE(self._hwnd))
            return 0
        if message == _WM_DESTROY:
            self._remove_icon()
            _INSTANCES.pop(self._hwnd, None)
            self._user32.PostQuitMessage(0)
            return 0
        return self._user32.DefWindowProcW(
            _HANDLE(hwnd), message, wparam, lparam
        )


def _clock(timestamp: Optional[str]) -> str:
    """The time out of an ISO timestamp, for a menu line that has one line."""
    if not timestamp:
        return "?"
    _date, _sep, rest = timestamp.partition("T")
    return rest[:5] or timestamp


def _shorten(text: Optional[str], limit: int = 60) -> Optional[str]:
    """A menu is not a log viewer; the log file is one click below this."""
    if not text:
        return None
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"
