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


# The heading of the banner wattracker.setuptoken prints at startup. The token
# itself is the first non-empty line after it - the same rule start.sh applies
# to the log it captures, and the same rule an operator applies by eye.
SETUP_TOKEN_BANNER = "wattracker setup token"


def setup_token_from(text: str):
    """The setup token in captured server output, or None."""
    lines = (text or "").splitlines()
    for index, line in enumerate(lines):
        if SETUP_TOKEN_BANNER in line:
            for candidate in lines[index + 1:]:
                candidate = candidate.strip()
                if candidate:
                    return candidate
    return None


def read_setup_token(stream_path, timeout=30, still_running=None) -> str:
    """Wait for the first-account setup token in the server's captured stdout.

    A smoke test has to do what an operator does: the first account on an
    install must present the one-time token the server prints while its
    database is empty (wattracker/setuptoken.py), so a build that cannot be
    registered against is a build whose first run does not work. Reading it out
    of the process's own output - rather than reaching into the app for it - is
    what makes that a real end-to-end check rather than a rehearsal.

    Polled rather than read once, even though the banner is printed during
    lifespan startup and therefore before the socket accepts anything: the
    caller redirected the child's stdout to a file, and waiting a moment for
    bytes to land is cheaper than a flaky smoke run. ``still_running`` aborts
    early on a process that has already died, exactly as wait_until_serving
    does.
    """
    deadline = time.time() + timeout
    while True:
        try:
            with open(stream_path, "r", encoding="utf-8", errors="replace") as handle:
                token = setup_token_from(handle.read())
        except OSError:
            token = None
        if token:
            return token
        if still_running is not None and not still_running():
            raise RuntimeError(
                "the application exited before printing a setup token"
            )
        if time.time() >= deadline:
            raise RuntimeError(
                f"no setup token appeared in {stream_path} within {timeout}s; "
                "a fresh install prints one at startup and cannot be "
                "registered against without it"
            )
        time.sleep(0.25)


def register_user(
    opener, base, setup_token, username="smokeuser", password="password123"
) -> None:
    """Create the FIRST account on a freshly packaged install.

    ``setup_token`` is required rather than optional on purpose. Every caller
    here is registering into an empty database, which is precisely the case the
    token governs, and a default of "" would turn a caller that forgot it into
    a 403 at run time in CI instead of a missing argument at the call site.

    The body is checked, not just the status: a validation failure re-renders
    the form with 200, so "it did not raise" is not "an account exists".
    """
    body = request(
        opener,
        base + "/register",
        {"username": username, "password": password, "setup_token": setup_token},
    ).read().decode("utf-8", "replace")
    if 'action="/register"' in body:
        raise AssertionError(
            "registration came back as the sign-up form again, so no account "
            "was created"
        )


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
