"""Run after wheel installation from a directory outside the checkout."""
from __future__ import annotations

import http.cookiejar
import importlib.resources
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request

import wattracker


VENDORED_CHART_ASSETS = (
    "chart.umd.min.js",
    "chartjs-plugin-zoom.umd.min.js",
)


def request(opener, url, data=None):
    encoded = urllib.parse.urlencode(data).encode() if data else None
    return opener.open(url, encoded, timeout=2)


with tempfile.TemporaryDirectory() as temporary:
    temp = Path(temporary)
    source_package = Path(__file__).resolve().parent.parent / "wattracker"
    if Path(wattracker.__file__).resolve().parent == source_package.resolve():
        raise RuntimeError("wattracker imported from the source checkout")
    static_root = importlib.resources.files("wattracker").joinpath(
        "web", "static", "vendor"
    )
    for asset in VENDORED_CHART_ASSETS:
        resource = static_root.joinpath(asset)
        if not resource.is_file() or len(resource.read_bytes()) < 1000:
            raise RuntimeError(f"installed package is missing vendored asset: {asset}")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = os.environ.copy()
    env.update(WATTRACKER_DATA_DIR=str(temp / "data"), WATTRACKER_PORT=str(port), WATTRACKER_OPEN_BROWSER="0", WATTRACKER_AUTO_SCAN="0")
    proc = subprocess.Popen([sys.executable, "-m", "wattracker"], cwd=temp, env=env)
    base = f"http://127.0.0.1:{port}"
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    try:
        deadline = time.time() + 20
        while True:
            try:
                if request(opener, base + "/login").status == 200:
                    break
            except OSError:
                if time.time() >= deadline:
                    raise
                time.sleep(0.25)
        assert request(opener, base + "/static/style.css").status == 200
        for asset in VENDORED_CHART_ASSETS:
            response = request(opener, base + "/static/vendor/" + asset)
            assert response.status == 200
            assert len(response.read()) > 1000
        assert request(opener, base + "/register").status == 200
        request(opener, base + "/register", {"username": "smokeuser", "password": "password123"})
        assert request(opener, base + "/settings/backup", {"smoke": "1"}).status == 200
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
