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
