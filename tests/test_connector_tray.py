"""The tray icon, its menu, and the three threads behind it.

Split by what each platform can honestly say:

* **Anywhere** - the module imports, refuses to construct off Windows, holds
  its structure layouts, and reads ``ConnectorStatus`` rather than keeping its
  own copy of anything. Same bargain ``tests/test_connector_webview.py``
  strikes: the native half cannot be exercised on the suite's CI, so what
  surrounds it is pinned instead.
* **On Windows** - a real hidden window, a real icon in the notification area,
  a real menu built and read back through Win32. This is the machine the
  connector ships to, and none of it is reachable from CI, so when the suite
  runs here it runs for real.

The thread wiring below the tray - the connector thread's stop, the window
loop, the single-instance mutex - is testable everywhere and tested everywhere,
because that is where a shutdown that leaves a trainer held would come from.
"""
import asyncio
import ctypes
import os
import subprocess
import sys
import textwrap
import threading
import time
import types

import pytest

from wattracker_connector import setup_win32, tray_win32, webview
from wattracker_connector.__main__ import (
    _MUTEX_NAME,
    _claim_single_instance,
    _ConnectorThread,
    _parser,
    _setup_wanted,
    _tray_wanted,
    _WindowLoop,
)
from wattracker_connector.client import ConnectorStatus

WINDOWS = os.name == "nt"
windows_only = pytest.mark.skipif(not WINDOWS, reason="Win32 lives on Windows")


def _has_an_interactive_desktop():
    """Is this process in a session that owns a notification area?

    Session 0 is the services session and has no shell, so Shell_NotifyIcon
    has nowhere to put an icon and refuses. The self-hosted CI runner is a
    service account and lands there, where the three tests below fail on an
    environment that cannot host a tray icon rather than on anything about the
    tray. A developer's session, and a runner configured to log on
    interactively, are non-zero and run them for real.

    Session id rather than FindWindowW("Shell_TrayWnd"): a packaged terminal
    cannot see windows owned by processes outside its container, so the window
    lookup reports no shell in sessions where the icon demonstrably works.
    """
    if not WINDOWS:
        return False
    from ctypes import wintypes

    kernel32 = ctypes.windll.kernel32
    session = wintypes.DWORD()
    if not kernel32.ProcessIdToSessionId(
        kernel32.GetCurrentProcessId(), ctypes.byref(session)
    ):
        return False
    return session.value != 0


needs_notification_area = pytest.mark.skipif(
    not _has_an_interactive_desktop(),
    reason="session 0 has no shell, so there is no notification area",
)


def _status(**fields) -> ConnectorStatus:
    status = ConnectorStatus()
    status.server_url = "http://192.168.1.10:8000"
    for name, value in fields.items():
        setattr(status, name, value)
    return status


@pytest.fixture()
def tray():
    """A real TrayIcon, constructed but not pumping.

    Construction loads the DLLs and nothing else; the window, the icon and the
    timer all belong to whichever thread calls run(). So everything that does
    not need a pump can be asked here, quickly and without a desktop.
    """
    if not WINDOWS:
        pytest.skip("Win32 lives on Windows")
    return tray_win32.TrayIcon(
        status=_status(connected=True), on_open=lambda: None, on_quit=lambda: None
    )


# ------------------------------------------------------------- anywhere at all
def test_it_imports_off_windows_and_refuses_only_on_construction():
    """The pattern webview.py already follows, so the Linux suite can hold it."""
    assert tray_win32.TrayIcon is not None
    assert tray_win32.icon_path().endswith(os.path.join("static", "favicon.ico"))


@pytest.mark.skipif(WINDOWS, reason="the point is the other platforms")
def test_constructing_off_windows_says_what_to_do_instead():
    with pytest.raises(tray_win32.TrayUnavailable) as excinfo:
        tray_win32.TrayIcon(status=_status(), on_open=lambda: None,
                            on_quit=lambda: None)
    assert "headless" in str(excinfo.value)


# szTip + szInfo + szInfoTitle, the only fields declared as inline arrays of
# characters rather than as pointers or integers.
_INLINE_WCHARS = 128 + 256 + 64


@pytest.mark.skipif(
    ctypes.sizeof(ctypes.c_void_p) != 8, reason="the shipped build is 64-bit"
)
def test_the_shell_structure_is_the_size_the_shell_expects():
    """cbSize is how the shell decides which version of the struct it was given.

    The failure a wrong layout causes is Shell_NotifyIcon quietly returning
    false rather than anything that names a field, so the layout is pinned here
    instead.

    The number checked is the one *Windows* will compute, which is not the one
    this process measures unless it is running there: ``ctypes.c_wchar`` is two
    bytes on Windows and four on every other platform ctypes runs on, so the
    three inline character arrays make the struct 896 bytes wider here than it
    is on the machine the shell lives on. Derived rather than skipped off
    Windows, because the CI that actually runs this suite is macOS - and a
    guard that only runs on the developer's own box is the one that let the
    literal 976 through in the first place.

    Size alone is a weaker check than it looks: everything here is padded to
    eight bytes, so widening ``uID`` to a handle or moving ``uFlags`` past
    ``hIcon`` both leave the total untouched. The offsets below are what
    actually catch those, and they are literals rather than derived because
    every field named sits ahead of the first character array, where no
    ``wchar_t`` has yet been counted and Windows and POSIX still agree.
    """
    measured = ctypes.sizeof(tray_win32._NOTIFYICONDATAW)
    as_windows_sees_it = measured - _INLINE_WCHARS * (
        ctypes.sizeof(ctypes.c_wchar) - 2
    )
    assert as_windows_sees_it == 976
    # No inline arrays in this one, so its size is the same everywhere.
    assert ctypes.sizeof(tray_win32._WNDCLASSEXW) == 80

    data = tray_win32._NOTIFYICONDATAW
    assert [
        (name, getattr(data, name).offset)
        for name in (
            "cbSize", "hWnd", "uID", "uFlags", "uCallbackMessage", "hIcon",
        )
    ] == [
        ("cbSize", 0), ("hWnd", 8), ("uID", 16), ("uFlags", 20),
        ("uCallbackMessage", 24), ("hIcon", 32),
    ]


def test_the_tray_reads_connector_status_and_keeps_no_copy():
    """One writer, one reader, and no second opinion about being connected.

    Called unbound so this runs on the platforms that cannot construct one:
    what is being asserted is that every drawn state comes out of the status
    object, which is exactly what an unbound call can show.
    """
    status = _status(connected=True)
    snapshot = tray_win32.TrayIcon._snapshot(types.SimpleNamespace(_status=status))
    assert snapshot[0] == "connected"

    status.connected = False
    snapshot = tray_win32.TrayIcon._snapshot(types.SimpleNamespace(_status=status))
    assert snapshot[0] == "offline"

    # Stopped outranks both: "not connected and still trying" and "not
    # connected and never again" are the two the rider most needs told apart.
    status.stopped = True
    status.stopped_reason = "Another connector has taken this account over."
    snapshot = tray_win32.TrayIcon._snapshot(types.SimpleNamespace(_status=status))
    assert snapshot[0] == "stopped"
    assert snapshot[4] == "Another connector has taken this account over."


def test_the_tray_never_imports_the_connector_itself():
    """It shows a status object. It does not own, start or stop a Connector."""
    source = (
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "wattracker_connector", "tray_win32.py")
    )
    with open(source, encoding="utf-8") as handle:
        text = handle.read()
    assert "from .client import" not in text
    assert "Connector(" not in text


def test_no_native_window_library_is_pulled_in_by_the_tray():
    """The tray may open a window; importing it must not load one.

    A subprocess for the reason tests/test_connector_client.py gives: this
    interpreter has imported half the world already.
    """
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(
            """
            import sys
            import wattracker_connector.tray_win32
            print("webviewpy" in sys.modules)
            """
        )],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "False"


# ------------------------------------------------------------- the icon states
@windows_only
def test_the_tooltip_names_the_server_it_is_talking_to(tray):
    assert tray._tooltip() == "wattracker - connected to http://192.168.1.10:8000"
    assert len(tray._tooltip()) <= 127  # szTip, including its NUL

    tray._status.connected = False
    assert "not connected" in tray._tooltip()

    tray._status.stopped = True
    assert "stopped" in tray._tooltip()


@windows_only
def test_a_very_long_server_url_cannot_overrun_the_tooltip(tray):
    tray._status.server_url = "http://" + ("a" * 300) + ":8000"
    assert len(tray._tooltip()) <= 127


@windows_only
def test_being_replaced_changes_the_icon_and_balloons_the_reason(tray):
    """A dead icon and no explanation is what this exists to prevent.

    ``run_forever`` deliberately stops instead of reconnecting when another
    connector takes the account over, so this is the only place the rider is
    ever told.
    """
    tray._refresh(force=True)
    assert tray._state == "connected"

    tray._status.connected = False
    tray._status.stopped = True
    tray._status.stopped_reason = "Another connector has taken this account over."
    tray._refresh()

    assert tray._state == "stopped"
    assert tray._icons["stopped"] != tray._icons["connected"]
    title, text, level = tray._balloons.pop()
    assert level == "error"
    assert "taken this account over" in text
    assert "stopped" in title.lower()


@windows_only
def test_a_stop_with_no_reason_still_says_something_useful(tray):
    tray._refresh(force=True)
    tray._status.stopped = True
    tray._refresh()

    _title, text, _level = tray._balloons.pop()
    assert "log" in text.lower()


@windows_only
def test_a_reconnect_does_not_balloon_every_time(tray):
    """The icon changes on a dropped socket; a balloon for each would be noise."""
    tray._refresh(force=True)
    for _ in range(3):
        tray._status.connected = False
        tray._refresh()
        tray._status.connected = True
        tray._refresh()

    assert not tray._balloons


@windows_only
def test_an_unreachable_server_balloons_once_and_keeps_trying(tray):
    """The server being switched off is the case the tooltip alone cannot carry.

    A rider whose server has gone sees an icon that looks exactly like an icon
    that is still starting up. Saying it once is the difference; saying it on
    every poll would be a notification every two seconds all night.
    """
    tray._status.connected = False
    tray._status.last_error = "timed out during opening handshake"
    for _ in range(5):
        tray._refresh()

    assert len(tray._balloons) == 1
    _title, text, level = tray._balloons.pop()
    assert level == "warning"
    assert "192.168.1.10:8000" in text
    assert "timed out" in text
    # Still trying is the promise the text makes; the state has to match it.
    assert tray._state == "offline"


@windows_only
def test_the_first_poll_before_any_attempt_says_nothing(tray):
    """Offline with no error yet is a connector that has only just started."""
    tray._status.connected = False
    tray._refresh(force=True)

    assert not tray._balloons


@windows_only
def test_a_server_that_comes_back_and_goes_again_is_announced_twice(tray):
    """One balloon per outage, not one per process."""
    tray._status.connected = False
    tray._status.last_error = "connection refused"
    tray._refresh()
    assert len(tray._balloons) == 1

    tray._status.connected = True
    tray._refresh()
    tray._status.connected = False
    tray._refresh()

    assert len(tray._balloons) == 2


# ------------------------------------------------------- one window at a time
def test_two_opens_in_flight_mint_only_one_ticket():
    """The gap present() cannot cover, which is where the ticket dies.

    Minting is a network round trip and _window is only set at the far end of
    it, so two Opens close together both see present() False, both mint, and
    TicketStore.mint replaces the first ticket with the second. Window one
    then redeems a dead ticket and lands on the login page - the exact outcome
    the ticket exists to prevent, produced by double-clicking.
    """
    loop = _WindowLoop(notify=lambda *a, **k: None)

    assert loop.claim() is True     # first worker takes it
    assert loop.claim() is False    # second worker, still before any window
    assert loop.present() is False  # and no window exists yet, which is the gap


def test_a_failed_attempt_gives_the_claim_back():
    """A mint that raised must not wedge the tray's Open for the session."""
    loop = _WindowLoop(notify=lambda *a, **k: None)

    assert loop.claim() is True
    loop.release()
    assert loop.claim() is True


# ------------------------------------------------------------------- pairing
def test_the_setup_window_imports_anywhere_and_refuses_elsewhere(monkeypatch):
    """Same bargain the tray strikes: importable on the suite's CI, refuses there."""
    monkeypatch.setattr(os, "name", "posix")
    with pytest.raises(setup_win32.SetupUnavailable):
        setup_win32.prompt_for_settings({})


def test_headless_never_opens_the_setup_window():
    """A dialog here would hang packaging/smoke_frozen_connector.py.

    That script runs an unpaired binary with --headless and reads its exit
    code; a modal window waiting for a click is exactly the hang it exists to
    turn into a failure, and it would be doing it to itself.
    """
    assert _setup_wanted(_parser().parse_args(["--headless"])) is False
    assert _setup_wanted(_parser().parse_args(["--headless", "--tray"])) is False


@windows_only
def test_an_explicit_tray_run_may_ask_for_the_pairing():
    assert _setup_wanted(_parser().parse_args(["--tray"])) is True


# -------------------------------------------------------------------- the menu
def _menu_items(user32, menu):
    """Read a live HMENU back: (text, checked, greyed) per item."""
    user32.GetMenuItemCount.argtypes = [ctypes.c_void_p]
    user32.GetMenuStringW.argtypes = [
        ctypes.c_void_p, ctypes.c_uint, ctypes.c_wchar_p, ctypes.c_int,
        ctypes.c_uint,
    ]
    user32.GetMenuState.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint]
    items = []
    buffer = ctypes.create_unicode_buffer(256)
    for index in range(user32.GetMenuItemCount(ctypes.c_void_p(menu))):
        user32.GetMenuStringW(
            ctypes.c_void_p(menu), index, buffer, len(buffer), 0x400  # MF_BYPOSITION
        )
        state = user32.GetMenuState(ctypes.c_void_p(menu), index, 0x400)
        items.append((buffer.value, bool(state & 0x008), bool(state & 0x001)))
    return items


@windows_only
def test_the_menu_offers_what_the_rider_came_for(tray):
    """Built through Win32 and read back through Win32, not from a Python list."""
    tray._status.last_connected_at = "2026-08-14T14:32:07"
    menu = tray._user32.CreatePopupMenu()
    try:
        tray._build_menu(menu)
        items = _menu_items(tray._user32, menu)
    finally:
        tray._user32.DestroyMenu(ctypes.c_void_p(menu))

    labels = [text for text, _checked, _greyed in items]
    assert "Connected to http://192.168.1.10:8000" in labels
    assert "Since 14:32" in labels
    assert "&Open wattracker" in labels
    assert "Open &log" in labels
    assert "Open &config folder" in labels
    assert "&Quit" in labels
    # The status line is shown, not offered: clicking it must do nothing.
    assert dict((text, greyed) for text, _c, greyed in items)[
        "Connected to http://192.168.1.10:8000"
    ]


@windows_only
def test_the_menu_says_why_autostart_is_not_on_offer(tray):
    """Running from a checkout, the box cannot be ticked. Say so, don't hide it."""
    menu = tray._user32.CreatePopupMenu()
    try:
        tray._build_menu(menu)
        items = _menu_items(tray._user32, menu)
    finally:
        tray._user32.DestroyMenu(ctypes.c_void_p(menu))

    startup = [item for item in items if item[0].startswith("Start with Windows")]
    assert startup, [text for text, _c, _g in items]
    label, _checked, greyed = startup[0]
    assert greyed and "packaged" in label


@windows_only
def test_a_disconnected_menu_shows_the_last_error(tray):
    tray._status.connected = False
    tray._status.last_error = "  [Errno 111] Connection\n refused  "
    menu = tray._user32.CreatePopupMenu()
    try:
        tray._build_menu(menu)
        labels = [text for text, _c, _g in _menu_items(tray._user32, menu)]
    finally:
        tray._user32.DestroyMenu(ctypes.c_void_p(menu))

    assert "Not connected to http://192.168.1.10:8000" in labels
    # Collapsed onto one line: a menu is not a log viewer, and the log is one
    # click below this.
    assert "[Errno 111] Connection refused" in labels


# -------------------------------------------------------- a real icon, pumping
@windows_only
@needs_notification_area
def test_the_icon_actually_goes_into_the_notification_area():
    """The end-to-end Win32 path: class, window, icon, pump, and back out.

    Everything above tests a piece; this is the only thing that proves the
    pieces add up to an icon. It runs a real pump on a real hidden top-level
    window - which is also what makes the icon survive explorer restarting.
    """
    tray = tray_win32.TrayIcon(
        status=_status(connected=True), on_open=lambda: None, on_quit=lambda: None
    )
    thread = threading.Thread(target=tray.run, name="tray-test", daemon=True)
    thread.start()
    try:
        deadline = time.time() + 15
        while time.time() < deadline and not tray._added:
            time.sleep(0.05)
        assert tray._added, "Shell_NotifyIcon refused to add the icon"
        assert tray._hwnd
        # Found by class name, which is how a second launch reaches the first.
        assert tray._user32.FindWindowW(tray_win32.CLASS_NAME, None)
    finally:
        tray.stop()
        thread.join(timeout=10)
    assert not thread.is_alive(), "the pump did not exit"
    assert not tray._added, "the icon was left behind in the notification area"


@windows_only
@needs_notification_area
def test_the_icon_comes_back_when_explorer_restarts():
    """The message really is sent to us, and we really do re-add the icon.

    ``taskkill /f /im explorer.exe`` is the honest version of this and is on
    the manual checklist. This is the part that can be automated: the same
    registered broadcast, posted to our own window, which is exactly what the
    shell sends - and it only arrives at all because the window is top-level
    rather than message-only.
    """
    tray = tray_win32.TrayIcon(
        status=_status(connected=True), on_open=lambda: None, on_quit=lambda: None
    )
    thread = threading.Thread(target=tray.run, name="tray-test", daemon=True)
    thread.start()
    try:
        deadline = time.time() + 15
        while time.time() < deadline and not tray._added:
            time.sleep(0.05)
        assert tray._added

        # What explorer's restart looks like from here: the shell has genuinely
        # forgotten the icon - not just this process's opinion of it, which is
        # why the icon is removed for real - and the window is told afterwards.
        tray._shell32.Shell_NotifyIconW(2, ctypes.byref(tray._data(0)))  # NIM_DELETE
        tray._added = False
        tray._user32.PostMessageW(
            ctypes.c_void_p(tray._hwnd), tray._taskbar_created, 0, 0
        )
        deadline = time.time() + 5
        while time.time() < deadline and not tray._added:
            time.sleep(0.05)
        assert tray._added, "the icon did not come back after TaskbarCreated"
    finally:
        tray.stop()
        thread.join(timeout=10)


@windows_only
@needs_notification_area
def test_a_second_launch_is_told_where_the_first_one_is():
    """The single-instance path, end to end: find the window, post the message."""
    tray = tray_win32.TrayIcon(
        status=_status(connected=True), on_open=lambda: None, on_quit=lambda: None
    )
    thread = threading.Thread(target=tray.run, name="tray-test", daemon=True)
    thread.start()
    try:
        deadline = time.time() + 15
        while time.time() < deadline and not tray._added:
            time.sleep(0.05)
        assert tray._added

        assert tray_win32.signal_existing_instance() is True
        # The balloon is queued by the pump on receiving it, then drained; a
        # short wait rather than a sleep, because this crosses two threads.
        deadline = time.time() + 5
        while time.time() < deadline and not tray._shown:
            time.sleep(0.05)
        assert tray._shown, "the running tray ignored the second launch"
    finally:
        tray.stop()
        thread.join(timeout=10)


# --------------------------------------------------------------- --tray itself
def test_headless_always_wins():
    """The packaging smoke test drives the frozen binary that way."""
    assert _tray_wanted(_parser().parse_args(["--headless", "--tray"])) is False
    assert _tray_wanted(_parser().parse_args(["--headless"])) is False


def test_the_frozen_build_puts_itself_in_the_tray(monkeypatch):
    """A windowed exe with no icon is a process a rider cannot see or stop."""
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert _tray_wanted(_parser().parse_args([])) is True
    monkeypatch.delattr(sys, "frozen", raising=False)
    assert _tray_wanted(_parser().parse_args([])) is False
    assert _tray_wanted(_parser().parse_args(["--tray"])) is True


def test_the_new_flag_does_not_overlap_the_ones_that_were_there_first():
    options = _parser()._option_string_actions
    assert {"--tray", "--headless", "--smoke-import"} <= set(options)


# ------------------------------------------------------------ one per session
def test_the_mutex_is_scoped_to_this_logon_session():
    """Global\\ would make two signed-in riders fight over one connector."""
    assert _MUTEX_NAME.startswith("Local\\")


@windows_only
def test_a_second_claim_on_the_same_name_is_refused():
    """Not the real name: the rider may have the real connector running."""
    name = r"Local\wattracker-connector-test-" + str(os.getpid())
    first, handle = _claim_single_instance(name)
    assert first is True
    try:
        second, other = _claim_single_instance(name)
        assert second is False
        assert other is None
    finally:
        ctypes.WinDLL("kernel32").CloseHandle(ctypes.c_void_p(handle))


# ---------------------------------------------------------- the window loop
class _FakeWindow:
    """Stands in for the native window: blocks in run() until terminated."""

    def __init__(self) -> None:
        self._closed = threading.Event()
        self.destroyed = False
        self.ran = threading.Event()

    def run(self):
        self.ran.set()
        # Long enough that a Quit which fails to terminate the window hangs
        # rather than being rescued by a timeout, which is what it would do to
        # a rider: the icon disappears and the process never exits.
        self._closed.wait(120)

    def terminate(self):
        self._closed.set()

    def destroy(self):
        self.destroyed = True

    def get_window(self):
        return 0


def test_the_window_runs_on_the_loops_thread_and_quit_closes_it(monkeypatch):
    """Quit has to reach a window that is inside a native loop of its own.

    webview_terminate is the documented cross-thread call, and it is the whole
    reason the window may be on one thread while the tray is on another.
    """
    window = _FakeWindow()
    monkeypatch.setattr(webview, "open_window", lambda url: window)
    loop = _WindowLoop(notify=lambda *a, **k: None)
    loop.open("http://192.168.1.10:8000/connector/session?token=x")
    thread = threading.Thread(target=loop.run, daemon=True)
    thread.start()

    assert window.ran.wait(10)
    assert loop.present() is True

    loop.quit()
    thread.join(timeout=15)
    assert not thread.is_alive(), "the main loop did not come back from the window"
    assert window.destroyed
    assert loop.present() is False


def test_a_machine_with_no_webview_gets_its_browser(monkeypatch):
    """Same one-shot ticket, a different window - not a wider credential."""
    opened = []
    monkeypatch.setattr(
        webview, "open_window",
        lambda url: (_ for _ in ()).throw(
            webview.WindowUnavailable("This machine has no WebView2 runtime.")
        ),
    )
    monkeypatch.setattr(webview, "open_in_browser", opened.append)
    said = []
    loop = _WindowLoop(notify=lambda *a, **k: said.append(a))

    loop.open("http://host:8000/connector/session?token=abc")
    loop.quit()
    loop.run()

    assert opened == ["http://host:8000/connector/session?token=abc"]
    assert said and "WebView2" in said[0][1]


# ------------------------------------------------------- stopping the connector
class _StubConnector:
    """A connector whose loop does what the real one does: waits on a socket."""

    def __init__(self, notices_stop: bool) -> None:
        self.status = ConnectorStatus()
        self.ble = types.SimpleNamespace(teardown=self._teardown)
        self.stopped = threading.Event()
        self.torn_down = 0
        self._notices = notices_stop

    def stop(self) -> None:
        self.stopped.set()

    async def _teardown(self) -> None:
        self.torn_down += 1

    async def run_forever(self) -> None:
        if self._notices:
            while not self.stopped.is_set():
                await asyncio.sleep(0.01)
            await self.ble.teardown()   # what the real run_forever does last
            return
        # The case that matters: blocked awaiting a frame that is never coming,
        # where setting a flag changes nothing at all.
        await asyncio.Event().wait()


def test_quitting_stops_a_connector_that_is_watching_for_it():
    connector = _StubConnector(notices_stop=True)
    thread = _ConnectorThread(connector)
    thread.start()

    started = time.time()
    thread.stop()

    assert not thread.thread.is_alive()
    assert connector.stopped.is_set()
    assert connector.torn_down == 1, "the radio was released once, on the way out"
    assert time.time() - started < 3.0, "it waited out the grace period needlessly"


def test_quitting_releases_the_trainer_even_when_the_loop_is_mid_read(monkeypatch):
    """The realistic quit: the run loop is blocked on a socket read.

    A flag does not interrupt a read, so the task is cancelled - and a
    cancelled task never reaches run_forever's last line, which is where the
    trainer is released. Without the teardown here, Quit leaves a rider's
    trainer holding its last ERG target.
    """
    monkeypatch.setattr("wattracker_connector.__main__._STOP_GRACE_S", 0.2)
    connector = _StubConnector(notices_stop=False)
    thread = _ConnectorThread(connector)
    thread.start()
    time.sleep(0.2)

    thread.stop()

    assert not thread.thread.is_alive(), "the connector thread outlived the quit"
    assert connector.stopped.is_set()
    assert connector.torn_down == 1, "the trainer was left held by a cancelled task"
    assert connector.status.stopped is True


def test_a_connector_that_dies_on_its_own_is_reported_as_stopped():
    """The tray draws "stopped" differently from "trying", so it must be true."""

    class _Dies(_StubConnector):
        async def run_forever(self):
            raise RuntimeError("the loop fell over")

    connector = _Dies(notices_stop=True)
    thread = _ConnectorThread(connector)
    thread.start()
    thread.stop()

    assert connector.status.stopped is True
