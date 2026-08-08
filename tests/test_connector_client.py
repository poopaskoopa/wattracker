"""The connector half: URL handling, and the import weight that must not grow.

The import-weight test is the load-bearing one. The connector is frozen into a
Windows executable, and the moment something drags numpy/pandas/scipy/fastapi
in, that executable roughly quadruples and PyInstaller's exclude list silently
stops holding. Catching it here is much cheaper than catching it on a build
machine.
"""
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
# Everything the frozen connector must NOT pull in. PyInstaller's spec excludes
# these; if an import sneaks back the exclude list stops matching reality and
# the build either bloats or breaks at runtime.
FORBIDDEN = [
    "numpy", "pandas", "scipy", "fastapi", "starlette", "uvicorn",
    "anthropic", "jinja2", "matplotlib", "fitdecode", "keyring",
    "wattracker.db", "wattracker.server", "wattracker.ingest",
]


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
