"""The window behind the tray icon: this server's own UI, already logged in.

Double-clicking the tray icon should show the rider their training - not a
login form. The connector cannot present a session cookie (it has a device
token) and must not present a password prompt, so it trades the token for a
one-minute single-use ticket and navigates to a URL that spends it. The server
half is ``wattracker.connectorsession``; this is the half that asks.

Two things are deliberate here and easy to undo by accident:

**The URL is built from the connector's own ``server_url``.** The server
returns only the ticket, because it has no reliable idea which address reaches
it from this machine. This side does, by construction: it is the address the
connector dialled.

**webviewpy is imported inside the function that opens the window.** The
connector's smallness is an enforced property (tests/test_connector_client.py),
and the window is the tray's business, not the connector core's. Importing at
module scope would drag a native library into every headless run. This module
must stay importable with webviewpy absent, on any OS.
"""
from __future__ import annotations

import json
import logging
import os
from urllib.parse import quote

from .buffer import _no_redirect_opener
from .config import config_dir

log = logging.getLogger(__name__)

# The window is a view onto a server that may be a NAS on the far side of the
# house. Long enough to survive that, short enough that a double-click on an
# unreachable server gives up while the rider is still looking at the tray.
_MINT_TIMEOUT_S = 15.0

_TITLE = "wattracker"
_WIDTH = 1100
_HEIGHT = 800


class WindowUnavailable(Exception):
    """The window could not be shown, with a reason worth showing a rider."""


def session_url(server_url: str, token: str) -> str:
    """Trade the device token for a ticket, and return the URL that spends it.

    Raises WindowUnavailable with something a rider can act on: a revoked
    device and an unreachable server are different problems with different
    fixes, and "could not open the window" tells them neither.
    """
    import urllib.error
    import urllib.request

    endpoint = server_url.rstrip("/") + "/api/connector/session"
    request = urllib.request.Request(
        endpoint,
        data=b"",
        headers={"Authorization": f"Bearer {token}"},
        method="POST",
    )
    try:
        # The same refuse-to-redirect opener the ride upload uses, for the
        # same reason: urllib replays Authorization onto whatever host a 302
        # names, which would hand the device token to it in plaintext.
        with _no_redirect_opener().open(request, timeout=_MINT_TIMEOUT_S) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise WindowUnavailable(
                "This machine is no longer paired with the server. Pair it "
                "again on the server's Settings page."
            ) from exc
        raise WindowUnavailable(
            f"The server refused to open a session ({exc.code})."
        ) from exc
    except Exception as exc:
        raise WindowUnavailable(
            f"Could not reach {server_url}: {exc}"
        ) from exc

    ticket = (body or {}).get("ticket")
    if not isinstance(ticket, str) or not ticket:
        raise WindowUnavailable("The server did not issue a session ticket.")
    # Named "token" because the server's redaction of its own access log keys
    # on that name - see wattracker/connectorsession.py. Not a free choice.
    return (
        server_url.rstrip("/")
        + "/connector/session?token="
        + quote(ticket, safe="")
    )


def _prepare_webview_environment() -> None:
    """Keep WebView2's profile out of the folder the exe was dropped into.

    WebView2 defaults its user-data folder to one beside the executable, which
    for a single portable file means a directory quietly appearing next to it -
    on a USB stick, in Downloads, wherever it was put. Point it at the config
    directory instead, which is already created owner-only.

    Set before the window is created, because the runtime reads it when the
    environment is built. Harmless everywhere else: no other engine looks at
    it.
    """
    os.environ.setdefault(
        "WEBVIEW2_USER_DATA_FOLDER", os.path.join(config_dir(), "webview")
    )


def open_window(url: str):
    """Open the OS WebView on ``url`` and run its loop until it is closed.

    Blocks, and wants to be the main thread - the underlying ``webview_run``
    is a native event loop, unconditionally main-thread on macOS. The tray and
    the connector run on their own threads for exactly this reason.

    Returns the live window so the caller can navigate or terminate it from
    another thread; raises WindowUnavailable when there is no usable engine,
    which is the caller's cue to fall back to a browser.
    """
    try:
        from webviewpy import Webview, webview_exception
    except Exception as exc:  # not installed, or its native will not load
        raise WindowUnavailable(
            "No embedded browser is available on this machine."
        ) from exc

    _prepare_webview_environment()
    try:
        # Constructing is what loads the native and builds the WebView2
        # environment, so this is where a missing runtime surfaces.
        window = Webview()
        window.set_title(_TITLE)
        window.set_size(_WIDTH, _HEIGHT)
        window.navigate(url)
    except webview_exception as exc:
        # Overwhelmingly this is a missing WebView2 runtime. It is inbox on
        # Windows 11 and current Windows 10, so it means an old or stripped
        # machine rather than a mistake the rider made.
        raise WindowUnavailable(
            "This machine has no WebView2 runtime, so the window cannot open."
        ) from exc
    return window


def open_in_browser(url: str) -> None:
    """The fallback: the rider's own browser, same ticket, same result.

    A ticket is single-use and expires in a minute, so this is not a lesser
    credential being handed to a wider audience - it is the same one-shot URL
    in a different window.
    """
    import webbrowser

    webbrowser.open(url)
