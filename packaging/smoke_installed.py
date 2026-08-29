"""Run after wheel installation from a directory outside the checkout."""
from __future__ import annotations

import importlib.resources
import os
from pathlib import Path
import subprocess
import sys
import tempfile

import smoke_http

import wattracker


with tempfile.TemporaryDirectory() as temporary:
    temp = Path(temporary)
    source_package = Path(__file__).resolve().parent.parent / "wattracker"
    if Path(wattracker.__file__).resolve().parent == source_package.resolve():
        raise RuntimeError("wattracker imported from the source checkout")
    static_root = importlib.resources.files("wattracker").joinpath(
        "web", "static", "vendor"
    )
    for asset in smoke_http.VENDORED_CHART_ASSETS:
        resource = static_root.joinpath(asset)
        if not resource.is_file() or len(resource.read_bytes()) < 1000:
            raise RuntimeError(f"installed package is missing vendored asset: {asset}")
    port = smoke_http.free_loopback_port()
    env = os.environ.copy()
    env.update(WATTRACKER_DATA_DIR=str(temp / "data"), WATTRACKER_PORT=str(port), WATTRACKER_OPEN_BROWSER="0", WATTRACKER_AUTO_SCAN="0")
    # The child's output is captured to a file rather than inherited, because
    # the first account on a fresh install has to present the one-time setup
    # token the server prints at startup (wattracker/setuptoken.py), and this
    # script is the operator here: it has to read what an operator would read.
    # The file is echoed at the end so the CI log keeps everything it had when
    # stdout went straight through - including that banner, which is what shows
    # a packaged build prints the token at all.
    output_path = temp / "server-output.txt"
    proc = None
    base = f"http://127.0.0.1:{port}"
    opener = smoke_http.build_opener()
    try:
        with open(output_path, "wb") as captured:
            proc = subprocess.Popen(
                [sys.executable, "-m", "wattracker"],
                cwd=temp,
                env=env,
                stdout=captured,
                stderr=subprocess.STDOUT,
            )
        smoke_http.wait_until_serving(
            opener, base, timeout=20, still_running=lambda: proc.poll() is None
        )
        smoke_http.assert_ui_renders(opener, base)
        token = smoke_http.read_setup_token(
            output_path, still_running=lambda: proc.poll() is None
        )
        smoke_http.register_user(opener, base, token)
        assert smoke_http.request(opener, base + "/settings/backup", {"smoke": "1"}).status == 200
    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        print("=== installed wattracker output ===")
        try:
            print(output_path.read_text(errors="replace") or "<empty>")
        except OSError as exc:
            print(f"<unavailable: {exc}>")
