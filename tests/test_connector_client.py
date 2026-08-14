"""The connector half: URL handling, and the import weight that must not grow.

The import-weight test is the load-bearing one. The connector is frozen into a
Windows executable, and the moment something drags numpy/pandas/scipy/fastapi
in, that executable roughly quadruples and PyInstaller's exclude list silently
stops holding. Catching it here is much cheaper than catching it on a build
machine.
"""
import importlib.util
import pathlib
import subprocess
import sys
import textwrap

import pytest

from wattracker_connector.client import websocket_url


# ------------------------------------------------------------- server URL
@pytest.mark.parametrize(
    "given,expected",
    [
        ("http://192.168.1.10:8000", "ws://192.168.1.10:8000/connector/ws"),
        ("https://wattracker.example", "wss://wattracker.example/connector/ws"),
        ("http://host:8000/", "ws://host:8000/connector/ws"),
        # A path on the base URL is dropped: the endpoint is fixed.
        ("http://host:8000/settings", "ws://host:8000/connector/ws"),
        ("ws://host:8000", "ws://host:8000/connector/ws"),
        ("  http://host:8000  ", "ws://host:8000/connector/ws"),
    ],
)
def test_websocket_url(given, expected):
    assert websocket_url(given) == expected


@pytest.mark.parametrize(
    "bad", ["", "host:8000", "/connector/ws", "ftp://host", "not a url"]
)
def test_websocket_url_rejects_unusable_values(bad):
    with pytest.raises(ValueError):
        websocket_url(bad)


# ---------------------------------------------------------- import weight
# Everything the frozen connector must NOT pull in. packaging/
# wattracker-connector.spec passes this same list to PyInstaller's excludes; if
# an import sneaks back, the exclude list stops matching reality and the build
# either bloats or breaks at runtime.
#
# Loaded from the one definition rather than repeated here, and loaded by path
# because the directory is named "packaging" - so is an installed PyPI
# distribution, and a plain import would find that one instead.
_EXCLUDES_PATH = (
    pathlib.Path(__file__).parents[1] / "packaging" / "_connector_excludes.py"
)
_EXCLUDES_SPEC = importlib.util.spec_from_file_location(
    "_wattracker_connector_excludes", _EXCLUDES_PATH
)
_EXCLUDES = importlib.util.module_from_spec(_EXCLUDES_SPEC)
_EXCLUDES_SPEC.loader.exec_module(_EXCLUDES)
FORBIDDEN = _EXCLUDES.FORBIDDEN


def _import_check(module: str) -> subprocess.CompletedProcess:
    """Import `module` in a clean interpreter and report forbidden modules.

    A subprocess is the only honest way to measure this: pytest has already
    imported half the world into *this* interpreter, so an in-process check
    would pass no matter how heavy the connector became.
    """
    script = textwrap.dedent(
        f"""
        import sys
        import {module}
        leaked = sorted(
            name for name in {FORBIDDEN!r}
            if name in sys.modules
        )
        print(",".join(leaked))
        """
    )
    return subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True, text=True, timeout=120,
    )


@pytest.mark.parametrize(
    "module",
    [
        "wattracker_connector",
        "wattracker_connector.handlers",
        "wattracker_connector.client",
        # The offline ride fallback runs this exact class in the connector, so
        # it has to stay as light as the rest.
        "wattracker.ble.runner",
        "wattracker.paths",
        "wattracker.rpc",
        "wattracker.prescribe.zwo",
    ],
)
def test_connector_modules_stay_lightweight(module):
    result = _import_check(module)
    assert result.returncode == 0, (
        f"importing {module} failed:\n{result.stderr}"
    )
    leaked = [name for name in result.stdout.strip().split(",") if name]
    assert leaked == [], (
        f"{module} pulled in modules the frozen connector excludes: {leaked}. "
        "Make the offending import lazy (see RideController._save)."
    )


# -------------------------------------------- the token must not be forwarded
def test_the_buffered_ride_upload_does_not_follow_redirects(tmp_path):
    """urllib replays Authorization on a redirect, to any host it names.

    So a server answering this POST with a 302 elsewhere harvests the device
    token in plaintext. There is no legitimate reason for the paired server to
    redirect an API POST, and a 3xx is a retryable code, so refusing to follow
    keeps the buffered ride rather than discarding it.
    """
    import http.server
    import json
    import threading

    from wattracker_connector import buffer as buffer_mod

    token = "a" * 43
    # Every request that carried the token, wherever it landed. A redirect
    # that is followed shows up here as a second entry on the OTHER server.
    harvested = []

    def _handler(name):
        class Handler(http.server.BaseHTTPRequestHandler):
            def _record_and_reply(self):
                if self.headers.get("Authorization") == f"Bearer {token}":
                    harvested.append(name)
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                if name == "server" and self.path.endswith("/api/connector/ride"):
                    self.send_response(302)
                    self.send_header("Location", attacker_url[0] + "/harvest")
                    self.end_headers()
                    return
                body = json.dumps({"activity_id": 1}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_POST = _record_and_reply
            do_GET = _record_and_reply

            def log_message(self, *a):
                pass

        return Handler

    attacker_url = [""]
    attacker = http.server.HTTPServer(("127.0.0.1", 0), _handler("attacker"))
    attacker_url[0] = f"http://127.0.0.1:{attacker.server_port}"
    server = http.server.HTTPServer(("127.0.0.1", 0), _handler("server"))
    for srv in (attacker, server):
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        store = buffer_mod.RideBuffer(str(tmp_path / "ride.json"))
        store.start("2026-06-01T10:00:00", "Ride", 250.0, None)
        store.append(power=200)
        store.finish()

        result = buffer_mod.upload_pending(
            f"http://127.0.0.1:{server.server_port}", token, store
        )
    finally:
        server.shutdown()
        attacker.shutdown()

    # The token reached the paired server and nowhere else.
    assert harvested == ["server"]
    assert "attacker" not in harvested
    # A 3xx is not a definite answer, so the ride is still there for next time.
    assert result is None
    assert store.load() is not None


# ----------------------------------------------------- what a session records
def test_a_connection_records_the_moment_it_started(monkeypatch):
    """The tray's "Since 14:32" line, and the only place it is ever written.

    ConnectorStatus is the connector's whole public face - one thread writes
    it, another draws it - so a field nobody fills is a menu line that reads
    "Since ?" forever, on a machine with no console to ask instead.
    """
    import asyncio
    import json

    from wattracker import rpc
    from wattracker_connector import client as clientmod
    from wattracker_connector.handlers import ConnectorConfig

    monkeypatch.setattr(clientmod, "upload_pending", lambda *a, **k: None)
    connector = clientmod.Connector(
        server_url="http://server.invalid:8000", token="t",
        config=ConnectorConfig(activities_dir=None, workouts_dir=None),
    )
    during = {}

    class _Connection:
        greeted = False

        async def send(self, text):
            pass

        async def recv(self):
            if not self.greeted:
                self.greeted = True
                return json.dumps(
                    {"event": "hello", "protocol": rpc.PROTOCOL_VERSION}
                )
            during["connected"] = connector.status.connected
            during["since"] = connector.status.last_connected_at
            raise RuntimeError("the socket went away")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    class _Websockets:
        def connect(self, url, **kwargs):
            return _Connection()

    with pytest.raises(RuntimeError):
        asyncio.run(
            connector._connected_session(_Websockets(), "ws://host:8000/connector/ws")
        )

    assert during["connected"] is True
    assert during["since"], "nothing recorded when the connection came up"
    # An ISO local timestamp: the tray shows the time out of it and nothing else.
    assert during["since"][:2] == "20" and "T" in during["since"]
    # And the session ending puts it back, without losing when it started.
    assert connector.status.connected is False
    assert connector.status.last_connected_at == during["since"]
