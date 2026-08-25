"""First-run pairing, asked for in a window rather than in an argv.

The frozen connector is a single file a rider downloads and double-clicks.
Until this existed, a copy that had never been paired read its empty config,
printed the pairing instructions to a stderr that a windowed build does not
have, and exited 2 - so what the rider saw was an executable that flashes and
vanishes. The instructions were correct and unreachable: they named a command
line, on a binary nobody starts from a command line.

So the same two questions are asked in a window. It is a plain top-level window
with child controls rather than a resource dialog, because a resource template
is a blob of packed bytes built at runtime and this is the same thing in
readable form - and because the tray already builds a window this way, so there
is one story about how this program talks to Win32 rather than two.

Deliberately *not* shown for ``--headless``. The packaging smoke test drives an
unpaired binary that way and reads its exit code; a modal dialog there is a
hang, which is precisely the failure mode that script exists to catch.

Imports on any OS and refuses on use, the shape ``tray_win32`` and ``webview``
both take, so the Linux suite can hold this module's structure even though it
can never run it.
"""
from __future__ import annotations

import ctypes
import logging
import os
from typing import Optional

log = logging.getLogger(__name__)


class SetupUnavailable(Exception):
    """There is no desktop to ask on."""


# --------------------------------------------------------------- Win32 types
# Spelled out rather than taken from ctypes.wintypes, which does not import on
# Linux - and this module must. Same sizes wintypes would give.
_DWORD = ctypes.c_uint32
_UINT = ctypes.c_uint32
_INT = ctypes.c_int32
_LONG = ctypes.c_int32
_HANDLE = ctypes.c_void_p
_WPARAM = ctypes.c_size_t
_LPARAM = ctypes.c_ssize_t
_LRESULT = ctypes.c_ssize_t


class _POINT(ctypes.Structure):
    _fields_ = [("x", _LONG), ("y", _LONG)]


class _RECT(ctypes.Structure):
    _fields_ = [("left", _LONG), ("top", _LONG),
                ("right", _LONG), ("bottom", _LONG)]


class _MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", _HANDLE), ("message", _UINT), ("wParam", _WPARAM),
        ("lParam", _LPARAM), ("time", _DWORD), ("pt", _POINT),
    ]


class _WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", _UINT), ("style", _UINT),
        # A raw pointer, so the structure stays definable off Windows: the
        # WNDPROC prototype needs WINFUNCTYPE, which only exists there.
        ("lpfnWndProc", ctypes.c_void_p),
        ("cbClsExtra", _INT), ("cbWndExtra", _INT),
        ("hInstance", _HANDLE), ("hIcon", _HANDLE), ("hCursor", _HANDLE),
        ("hbrBackground", _HANDLE),
        ("lpszMenuName", ctypes.c_wchar_p), ("lpszClassName", ctypes.c_wchar_p),
        ("hIconSm", _HANDLE),
    ]


# ----------------------------------------------------------- Win32 constants
_WS_OVERLAPPED, _WS_CAPTION, _WS_SYSMENU = 0x00000000, 0x00C00000, 0x00080000
_WS_CHILD, _WS_VISIBLE, _WS_TABSTOP = 0x40000000, 0x10000000, 0x00010000
_WS_EX_CLIENTEDGE, _WS_EX_APPWINDOW = 0x00000200, 0x00040000
_ES_AUTOHSCROLL = 0x0080
_BS_DEFPUSHBUTTON = 0x0001
_SS_LEFT = 0x0000

_WM_DESTROY, _WM_CLOSE, _WM_COMMAND = 0x0002, 0x0010, 0x0111
_WM_SETFONT, _WM_SETICON = 0x0030, 0x0080
_SW_SHOWNORMAL = 1
_ICON_SMALL, _ICON_BIG = 0, 1
_IMAGE_ICON = 1
_LR_LOADFROMFILE = 0x0010
_IDC_ARROW = 32512
_COLOR_BTNFACE = 15
_SM_CXSCREEN, _SM_CYSCREEN = 0, 1
_ERROR_CLASS_ALREADY_EXISTS = 1410

_MB_OK, _MB_ICONWARNING = 0x0, 0x30

# Control identifiers. IDOK and IDCANCEL keep their standard values so
# IsDialogMessageW turns Enter and Escape into them for free.
_ID_OK, _ID_CANCEL = 1, 2
_ID_SERVER, _ID_TOKEN = 100, 101

CLASS_NAME = "wattracker_connector_setup"
_TITLE = "wattracker connector - pairing"

# Client-area layout at 96 DPI, in the order the controls are created. Laid out
# by hand because the alternative is a dialog resource template, which is the
# same numbers with a packing step in front of them.
_WIDTH, _HEIGHT = 430, 232
_MARGIN = 16
_FIELD_W = _WIDTH - 2 * _MARGIN

_USER32 = None
_CLASS_WNDPROC = None
# hwnd -> the _Setup that owns it. A window class carries the procedure it was
# registered with, so the procedure belongs to the module and the window is
# looked up here; this is the same arrangement tray_win32 documents at length.
_INSTANCES: dict = {}


def _user32():
    """user32 with the argument types that keep 64-bit handles intact.

    Without these ctypes assumes every argument and return value is a C int and
    silently truncates handles to 32 bits, which produces windows that cannot
    be found and controls that never appear, with no error anywhere.
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
    user32.RegisterClassExW.argtypes = [ctypes.POINTER(_WNDCLASSEXW)]
    user32.DestroyWindow.argtypes = [_HANDLE]
    user32.ShowWindow.argtypes = [_HANDLE, _INT]
    user32.UpdateWindow.argtypes = [_HANDLE]
    user32.SetForegroundWindow.argtypes = [_HANDLE]
    user32.SetFocus.argtypes = [_HANDLE]
    user32.GetMessageW.argtypes = [ctypes.POINTER(_MSG), _HANDLE, _UINT, _UINT]
    user32.TranslateMessage.argtypes = [ctypes.POINTER(_MSG)]
    user32.DispatchMessageW.argtypes = [ctypes.POINTER(_MSG)]
    user32.DispatchMessageW.restype = _LRESULT
    user32.IsDialogMessageW.argtypes = [_HANDLE, ctypes.POINTER(_MSG)]
    user32.SendMessageW.restype = _LRESULT
    user32.SendMessageW.argtypes = [_HANDLE, _UINT, _WPARAM, _LPARAM]
    user32.GetWindowTextLengthW.argtypes = [_HANDLE]
    user32.GetWindowTextLengthW.restype = _INT
    user32.GetWindowTextW.argtypes = [_HANDLE, ctypes.c_wchar_p, _INT]
    user32.GetWindowTextW.restype = _INT
    user32.SetWindowTextW.argtypes = [_HANDLE, ctypes.c_wchar_p]
    user32.AdjustWindowRect.argtypes = [ctypes.POINTER(_RECT), _DWORD, _INT]
    user32.GetSystemMetrics.argtypes = [_INT]
    user32.GetSystemMetrics.restype = _INT
    user32.LoadCursorW.restype = _HANDLE
    user32.LoadCursorW.argtypes = [_HANDLE, ctypes.c_void_p]
    user32.LoadImageW.restype = _HANDLE
    user32.LoadImageW.argtypes = [_HANDLE, ctypes.c_void_p, _UINT,
                                  _INT, _INT, _UINT]
    user32.MessageBoxW.argtypes = [_HANDLE, ctypes.c_wchar_p,
                                   ctypes.c_wchar_p, _UINT]
    user32.PostQuitMessage.argtypes = [_INT]
    _USER32 = user32
    return user32


def _dispatch_to_instance(hwnd, message, wparam, lparam):
    setup = _INSTANCES.get(hwnd)
    if setup is None:
        # Messages sent while the window is still being created arrive before
        # it can be registered here, and messages after it is destroyed arrive
        # after it has been removed. Both belong to the default handler.
        return _user32().DefWindowProcW(_HANDLE(hwnd), message, wparam, lparam)
    return setup._handle_message(hwnd, message, wparam, lparam)


def _gui_font():
    """Segoe UI at the size the rest of Windows uses, or the stock font.

    DEFAULT_GUI_FONT is the one-liner, and it is Tahoma 8 - a dialog that looks
    like it came from a different decade than the tray beside it. Falling back
    to it is still better than no font at all, which is the system font: bold,
    fixed and unreadable at this size.
    """
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    gdi32.CreateFontW.restype = _HANDLE
    gdi32.CreateFontW.argtypes = [
        _INT, _INT, _INT, _INT, _INT, _DWORD, _DWORD, _DWORD,
        _DWORD, _DWORD, _DWORD, _DWORD, _DWORD, ctypes.c_wchar_p,
    ]
    gdi32.GetStockObject.restype = _HANDLE
    gdi32.GetStockObject.argtypes = [_INT]
    # -12 is 9pt at 96 DPI; 400 is FW_NORMAL, 1 DEFAULT_CHARSET, 5 CLEARTYPE.
    font = gdi32.CreateFontW(-12, 0, 0, 0, 400, 0, 0, 0, 1, 0, 0, 5, 0,
                             "Segoe UI")
    if not font:
        font = gdi32.GetStockObject(17)  # DEFAULT_GUI_FONT
    return font


class _Setup:
    """One window, its controls, and whatever the rider typed into them."""

    def __init__(self, initial: Optional[dict] = None) -> None:
        self.result: Optional[dict] = None
        self._initial = initial or {}
        self._hwnd = None
        self._server = None
        self._token = None
        self._font = None

    # ------------------------------------------------------------- building
    def _child(self, class_name, text, style, x, y, width, height,
               control_id=0, ex_style=0):
        user32 = _user32()
        hwnd = user32.CreateWindowExW(
            ex_style, class_name, text, _WS_CHILD | _WS_VISIBLE | style,
            x, y, width, height, _HANDLE(self._hwnd),
            _HANDLE(control_id), None, None,
        )
        if not hwnd:
            raise SetupUnavailable(
                f"could not create the {class_name} control "
                f"(error {ctypes.get_last_error()})"
            )
        if self._font:
            user32.SendMessageW(_HANDLE(hwnd), _WM_SETFONT,
                                _WPARAM(self._font), 1)
        return hwnd

    def _register_class(self) -> None:
        global _CLASS_WNDPROC
        user32 = _user32()
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetModuleHandleW.restype = _HANDLE
        kernel32.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]

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
        window_class.style = 0
        window_class.lpfnWndProc = ctypes.cast(_CLASS_WNDPROC, ctypes.c_void_p)
        window_class.hInstance = kernel32.GetModuleHandleW(None)
        window_class.hCursor = user32.LoadCursorW(None, ctypes.c_void_p(_IDC_ARROW))
        # +1 because the system colour constants are one less than the brush
        # handles this field wants. Without it the dialog is transparent black.
        window_class.hbrBackground = _HANDLE(_COLOR_BTNFACE + 1)
        window_class.lpszClassName = CLASS_NAME
        if not user32.RegisterClassExW(ctypes.byref(window_class)):
            error = ctypes.get_last_error()
            if error != _ERROR_CLASS_ALREADY_EXISTS:
                raise SetupUnavailable(
                    f"could not register the setup window class (error {error})"
                )

    def _create(self) -> None:
        user32 = _user32()
        self._register_class()
        self._font = _gui_font()

        style = _WS_OVERLAPPED | _WS_CAPTION | _WS_SYSMENU
        # The style is applied to the frame, so the numbers in the layout above
        # stay client-area numbers rather than silently losing a title bar.
        frame = _RECT(0, 0, _WIDTH, _HEIGHT)
        user32.AdjustWindowRect(ctypes.byref(frame), style, 0)
        width = frame.right - frame.left
        height = frame.bottom - frame.top
        x = max(0, (user32.GetSystemMetrics(_SM_CXSCREEN) - width) // 2)
        y = max(0, (user32.GetSystemMetrics(_SM_CYSCREEN) - height) // 3)

        self._hwnd = user32.CreateWindowExW(
            _WS_EX_APPWINDOW, CLASS_NAME, _TITLE, style,
            x, y, width, height, None, None, None, None,
        )
        if not self._hwnd:
            raise SetupUnavailable(
                f"could not create the setup window "
                f"(error {ctypes.get_last_error()})"
            )
        _INSTANCES[self._hwnd] = self
        self._set_icon()

        row = _MARGIN
        self._child("STATIC", "Server address", _SS_LEFT,
                    _MARGIN, row, _FIELD_W, 18)
        row += 20
        self._server = self._child(
            "EDIT", str(self._initial.get("server") or ""),
            _ES_AUTOHSCROLL | _WS_TABSTOP,
            _MARGIN, row, _FIELD_W, 24, _ID_SERVER, _WS_EX_CLIENTEDGE,
        )
        row += 28
        self._child("STATIC", "For example  http://192.168.1.10:8000", _SS_LEFT,
                    _MARGIN, row, _FIELD_W, 18)
        row += 30
        self._child("STATIC", "Device token", _SS_LEFT,
                    _MARGIN, row, _FIELD_W, 18)
        row += 20
        self._token = self._child(
            "EDIT", str(self._initial.get("token") or ""),
            _ES_AUTOHSCROLL | _WS_TABSTOP,
            _MARGIN, row, _FIELD_W, 24, _ID_TOKEN, _WS_EX_CLIENTEDGE,
        )
        row += 28
        self._child(
            "STATIC",
            "Pair a device on the server's Settings page, then paste it here.",
            _SS_LEFT, _MARGIN, row, _FIELD_W, 18,
        )

        button_y = _HEIGHT - _MARGIN - 28
        self._child("BUTTON", "Save and connect", _BS_DEFPUSHBUTTON | _WS_TABSTOP,
                    _WIDTH - _MARGIN - 226, button_y, 140, 28, _ID_OK)
        self._child("BUTTON", "Cancel", _WS_TABSTOP,
                    _WIDTH - _MARGIN - 80, button_y, 80, 28, _ID_CANCEL)

        user32.ShowWindow(_HANDLE(self._hwnd), _SW_SHOWNORMAL)
        user32.UpdateWindow(_HANDLE(self._hwnd))
        user32.SetForegroundWindow(_HANDLE(self._hwnd))
        user32.SetFocus(_HANDLE(self._server))

    def _set_icon(self) -> None:
        """The tray's icon on the window, so Alt-Tab says who is asking.

        Best-effort: an unpaired connector with a generic icon is a cosmetic
        problem, and refusing to ask for a token over one would not be.
        """
        try:
            from .tray_win32 import icon_path

            path = icon_path()
            if not os.path.exists(path):
                return
            user32 = _user32()
            icon = user32.LoadImageW(None, path, _IMAGE_ICON, 0, 0,
                                     _LR_LOADFROMFILE)
            if icon:
                for which in (_ICON_SMALL, _ICON_BIG):
                    user32.SendMessageW(_HANDLE(self._hwnd), _WM_SETICON,
                                        _WPARAM(which), _LPARAM(icon))
        except Exception:
            log.debug("could not put an icon on the setup window", exc_info=True)

    # ------------------------------------------------------------- messages
    def _text(self, control) -> str:
        user32 = _user32()
        length = user32.GetWindowTextLengthW(_HANDLE(control))
        if length <= 0:
            return ""
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(_HANDLE(control), buffer, length + 1)
        return buffer.value.strip()

    def _complain(self, text: str) -> None:
        _user32().MessageBoxW(_HANDLE(self._hwnd), text, _TITLE,
                              _MB_OK | _MB_ICONWARNING)

    def _accept(self) -> None:
        """Validate what is in the boxes, and keep the window open if it is wrong.

        The URL is checked with the connector's own parser rather than a second
        opinion written here, so what this dialog accepts and what the client
        can dial are the same set by construction.
        """
        server = self._text(self._server)
        token = self._text(self._token)
        if not server or not token:
            self._complain("Both the server address and the device token are "
                           "needed.")
            _user32().SetFocus(_HANDLE(self._server if not server
                                       else self._token))
            return
        from .client import websocket_url

        try:
            websocket_url(server)
        except ValueError as exc:
            self._complain(f"That server address will not work: {exc}")
            _user32().SetFocus(_HANDLE(self._server))
            return
        self.result = {"server": server, "token": token}
        _user32().DestroyWindow(_HANDLE(self._hwnd))

    def _handle_message(self, hwnd, message, wparam, lparam):
        user32 = _user32()
        if message == _WM_COMMAND:
            command = wparam & 0xFFFF
            if command == _ID_OK:
                self._accept()
                return 0
            if command == _ID_CANCEL:
                user32.DestroyWindow(_HANDLE(hwnd))
                return 0
        elif message == _WM_CLOSE:
            user32.DestroyWindow(_HANDLE(hwnd))
            return 0
        elif message == _WM_DESTROY:
            _INSTANCES.pop(hwnd, None)
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(_HANDLE(hwnd), message, wparam, lparam)

    # ------------------------------------------------------------- the loop
    def run(self) -> Optional[dict]:
        user32 = _user32()
        self._create()
        message = _MSG()
        while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
            # IsDialogMessageW is what makes Tab walk the fields and Enter and
            # Escape mean the two buttons - all of it free, and none of it
            # present in a plain window without this call.
            if not user32.IsDialogMessageW(_HANDLE(self._hwnd),
                                           ctypes.byref(message)):
                user32.TranslateMessage(ctypes.byref(message))
                user32.DispatchMessageW(ctypes.byref(message))
        return self.result


def prompt_for_settings(initial: Optional[dict] = None) -> Optional[dict]:
    """Ask for a server and a token. ``None`` if the rider cancelled.

    Returns only the two fields it asked about, so a caller merging this into
    saved settings cannot lose the directory overrides it did not ask for.
    """
    if os.name != "nt":
        raise SetupUnavailable("the setup window needs Windows")
    return _Setup(initial).run()
