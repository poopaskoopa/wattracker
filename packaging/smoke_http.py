"""HTTP assertions shared by the installed-wheel and frozen-app smoke tests.

Both smoke tests answer the same question - "does this build actually serve the
UI?" - so the checks live here once. Stdlib only: the frozen smoke test runs
under whatever python3 is available, not inside the project venv.

Imported by sibling scripts in packaging/, which python puts on sys.path[0]
automatically when they are invoked by path.
"""
from __future__ import annotations

import http.cookiejar
import socket
import time
import urllib.parse
import urllib.request

VENDORED_CHART_ASSETS = (
    "chart.umd.min.js",
    "chartjs-plugin-zoom.umd.min.js",
)


def free_loopback_port() -> int:
    """A port the kernel just handed out, so a smoke run never collides with a
    wattracker the developer already has running on the default 8000."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def build_opener() -> urllib.request.OpenerDirector:
    """An opener with a cookie jar, so the session survives register -> settings."""
    return urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
    )


def request(opener, url, data=None, timeout=10):
    encoded = urllib.parse.urlencode(data).encode() if data else None
    return opener.open(url, encoded, timeout=timeout)


def get_text(opener, url, timeout=10) -> str:
    response = request(opener, url, timeout=timeout)
    if response.status != 200:
        raise AssertionError(f"{url} returned HTTP {response.status}")
    return response.read().decode("utf-8", "replace")


def wait_until_serving(opener, base, timeout=90, still_running=None) -> None:
    """Poll /login until it answers, or raise.

    ``still_running`` is an optional callable; when it returns False the wait
    aborts immediately rather than burning the full timeout on a process that
    has already died.
    """
    deadline = time.time() + timeout
    while True:
        if still_running is not None and not still_running():
            raise RuntimeError("the application exited before it began serving")
        try:
            if request(opener, base + "/login", timeout=2).status == 200:
                return
        except OSError:
            pass
        if time.time() >= deadline:
            raise RuntimeError(f"{base}/login did not answer within {timeout}s")
        time.sleep(0.25)


def assert_ui_renders(opener, base) -> None:
    """Assert the UI is really rendered, not merely that a port answers.

    A 200 proves uvicorn is up; it does not prove the Jinja templates and the
    static tree survived packaging. So this checks the rendered login markup,
    the stylesheet, and the vendored chart bundles - the three things a
    missing-datas packaging bug takes out.
    """
    login = get_text(opener, base + "/login")
    for marker in ('<form method="post" action="/login"', 'name="username"', "/static/style.css"):
        if marker not in login:
            raise AssertionError(f"login page did not render expected markup: {marker!r}")

    css = get_text(opener, base + "/static/style.css")
    if "{" not in css or len(css) < 1000:
        raise AssertionError("style.css did not serve real CSS")

    for asset in VENDORED_CHART_ASSETS:
        response = request(opener, base + "/static/vendor/" + asset)
        if response.status != 200 or len(response.read()) < 1000:
            raise AssertionError(f"vendored chart asset smoke failed: {asset}")

    register = get_text(opener, base + "/register")
    if 'name="password"' not in register:
        raise AssertionError("register page did not render its form")


def register_user(opener, base, username="smokeuser", password="password123") -> None:
    request(opener, base + "/register", {"username": username, "password": password})


def assert_credential_backend(opener, base, expected) -> None:
    """Assert the settings page reports the expected credential backend.

    This is how the frozen build proves its platform keyring backend was
    actually packaged: keyring picks its backend by runtime discovery, so a
    missing hidden import degrades silently to the file-key fallback instead of
    raising. Reading the reported name writes nothing to the system vault.
    """
    settings = get_text(opener, base + "/settings")
    if expected not in settings:
        raise AssertionError(
            f"settings page did not report the {expected!r} credential backend"
        )
