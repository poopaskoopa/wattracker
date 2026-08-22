"""The tray's window: how it gets a session, and how it fails.

The window itself cannot be tested here - it is a native WebView on a machine
with a desktop - so what is pinned instead is everything around it: the ticket
exchange against a real server, the URL that is built from it, the failure
messages a rider would have to act on, and the rule that none of this drags a
native library into a headless connector's import graph.
"""
import subprocess
import sys
import textwrap

import pytest

pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from wattracker import connectorauth, db  # noqa: E402
from wattracker.server import create_app  # noqa: E402
from wattracker_connector import webview  # noqa: E402


@pytest.fixture()
def client():
    with TestClient(create_app()) as c:
        yield c


def _paired(client, username="rider", label="Zwift PC"):
    client.post("/register", data={"username": username, "password": "password123"})
    uid = db.get_user_by_username(username)["id"]
    device_id, token = connectorauth.generate_token(uid, label)
    return uid, device_id, token


class _FakeResponse:
    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeOpener:
    """Stands in for urllib, recording what the connector asked for."""

    def __init__(self, payload=b'{"ticket": "abc123", "expires_in": 60.0}',
                 error=None):
        self._payload = payload
        self._error = error
        self.requests = []

    def open(self, request, timeout=None):
        self.requests.append(request)
        if self._error is not None:
            raise self._error
        return _FakeResponse(self._payload)


@pytest.fixture()
def opener(monkeypatch):
    fake = _FakeOpener()
    monkeypatch.setattr(webview, "_no_redirect_opener", lambda: fake)
    return fake


# ------------------------------------------------------------- the URL
def test_the_url_is_built_from_the_connectors_own_server_url(opener):
    """The server returns a ticket, never a URL - it cannot know the address."""
    url = webview.session_url("http://192.168.1.10:8000", "device-token")

    assert url == "http://192.168.1.10:8000/connector/session?token=abc123"
    request = opener.requests[0]
    assert request.full_url == "http://192.168.1.10:8000/api/connector/session"
    assert request.get_header("Authorization") == "Bearer device-token"
    assert request.get_method() == "POST"


def test_a_trailing_slash_does_not_double_up(opener):
    url = webview.session_url("http://host:8000/", "t")
    assert url == "http://host:8000/connector/session?token=abc123"


def test_the_ticket_is_percent_encoded(monkeypatch):
    """token_urlsafe cannot emit these, but a URL builder must not assume it."""
    fake = _FakeOpener(payload=b'{"ticket": "a+b/c=d&e"}')
    monkeypatch.setattr(webview, "_no_redirect_opener", lambda: fake)

    url = webview.session_url("http://host:8000", "t")
    assert url.endswith("?token=a%2Bb%2Fc%3Dd%26e")


def test_the_parameter_is_named_token_not_ticket(opener):
    """Renaming it would put a live credential in the server's access log.

    calendarfeed's redaction filter keys on parameter names beginning "token";
    the server side pins the route's own parameter name, and this pins the one
    the connector sends.
    """
    assert "?token=" in webview.session_url("http://host:8000", "t")


# --------------------------------------------------------- the failures
def test_a_revoked_device_is_told_to_pair_again(monkeypatch):
    import urllib.error

    fake = _FakeOpener(error=urllib.error.HTTPError(
        "http://host:8000/api/connector/session", 401, "Unauthorized", {}, None
    ))
    monkeypatch.setattr(webview, "_no_redirect_opener", lambda: fake)

    with pytest.raises(webview.WindowUnavailable) as excinfo:
        webview.session_url("http://host:8000", "revoked")
    assert "no longer paired" in str(excinfo.value)


def test_an_unreachable_server_names_itself(monkeypatch):
    fake = _FakeOpener(error=OSError("connection refused"))
    monkeypatch.setattr(webview, "_no_redirect_opener", lambda: fake)

    with pytest.raises(webview.WindowUnavailable) as excinfo:
        webview.session_url("http://192.168.1.10:8000", "t")
    assert "192.168.1.10:8000" in str(excinfo.value)


def test_a_server_that_answers_without_a_ticket_is_refused(monkeypatch):
    for payload in (b"{}", b'{"ticket": ""}', b'{"ticket": 7}'):
        fake = _FakeOpener(payload=payload)
        monkeypatch.setattr(webview, "_no_redirect_opener", lambda: fake)
        with pytest.raises(webview.WindowUnavailable):
            webview.session_url("http://host:8000", "t")


def test_the_mint_refuses_to_follow_a_redirect():
    """The device token must not be replayed onto a host a 302 names.

    Same property tests/test_connector_client.py pins for the ride upload, and
    the same opener - this asserts the window's mint actually uses it.
    """
    opener = webview._no_redirect_opener()
    handler = next(
        h for h in opener.handlers
        if type(h).__name__ == "_Refuse"
    )
    assert handler.redirect_request(None, None, 302, "", {}, "http://evil") is None


# ------------------------------------------------- against a real server
def test_the_url_opens_a_real_session_on_a_real_server(client, monkeypatch):
    """End to end, minus the native window: mint here, redeem there."""
    _uid, _device_id, token = _paired(client)

    class _RealOpener:
        def open(self, request, timeout=None):
            response = client.post(
                "/api/connector/session",
                headers={"Authorization": request.get_header("Authorization")},
            )
            return _FakeResponse(response.content)

    monkeypatch.setattr(webview, "_no_redirect_opener", lambda: _RealOpener())

    url = webview.session_url("http://testserver", token)
    path = url[len("http://testserver"):]

    with TestClient(client.app) as window:
        landing = window.get(path, follow_redirects=False)
        assert landing.status_code == 303
        assert window.get("/settings", follow_redirects=False).status_code == 200


# ------------------------------------------------------- the import rule
def test_the_window_is_importable_without_webviewpy():
    """It is an optional extra; a headless connector must not need it."""
    assert webview.session_url is not None
    assert webview.WindowUnavailable is not None


def test_opening_without_webviewpy_says_so_rather_than_crashing(monkeypatch):
    """The rider gets the browser fallback, not a traceback."""
    monkeypatch.setitem(sys.modules, "webviewpy", None)

    with pytest.raises(webview.WindowUnavailable) as excinfo:
        webview.open_window("http://host:8000/")
    assert "No embedded browser" in str(excinfo.value)


@pytest.mark.parametrize(
    "module",
    ["wattracker_connector.client", "wattracker_connector.__main__",
     "wattracker_connector.webview"],
)
def test_no_native_window_library_is_pulled_into_the_import_graph(module):
    """The tray may load it; the connector core may not - and nor may this.

    A subprocess for the same reason tests/test_connector_client.py uses one:
    this interpreter has imported half the world already.
    """
    script = textwrap.dedent(
        f"""
        import sys
        import {module}
        print("webviewpy" in sys.modules)
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=120
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "False", (
        f"{module} imported webviewpy at module scope; it must be imported "
        "inside the function that opens the window."
    )
