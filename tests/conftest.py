"""Shared pytest fixtures: isolate config + database in a temp directory."""
import functools
import os
import sys
import tempfile

import pytest

# Resolved ONCE at import time, before any test redirects HOME below. This is
# the only thing outside the sandbox the suite is still allowed to look at: the
# playwright browser download, which is a tool dependency and not app data.
# Pinning it explicitly keeps the DOM smoke tests running instead of silently
# turning into skips once HOME no longer points at the developer's home.
_REAL_HOME = os.path.expanduser("~")
if sys.platform == "darwin":
    _PLAYWRIGHT_CACHE = os.path.join(_REAL_HOME, "Library", "Caches", "ms-playwright")
elif sys.platform.startswith("win"):
    _PLAYWRIGHT_CACHE = os.path.join(_REAL_HOME, "AppData", "Local", "ms-playwright")
else:
    _PLAYWRIGHT_CACHE = os.path.join(_REAL_HOME, ".cache", "ms-playwright")


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    """Point the app's data dir and DB at a fresh temp location per test."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    # The developer's real home is not part of any test's fixture. Point HOME
    # (and the Windows equivalent) at a sandbox directory so expanduser("~") -
    # which paths.trusted_storage_roots() and the Documents/Zwift discovery are
    # built on - can only ever resolve inside tmp_path. Without this, a test
    # that stores a directory setting is implicitly asserting against whatever
    # is in the real ~/Documents/Zwift.
    #
    # It is deliberately a SUBDIRECTORY of tmp_path, not tmp_path itself: the
    # confinement tests plant their "outside" directories as tmp_path siblings
    # and must keep being rejected. Making the whole sandbox trusted would
    # quietly turn those into passing no-ops.
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("ONEDRIVE", raising=False)
    # Redirecting HOME also hides playwright's browser cache from it; point at
    # the real one so the DOM smoke tests keep running rather than skipping.
    if not os.environ.get("PLAYWRIGHT_BROWSERS_PATH"):
        monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", _PLAYWRIGHT_CACHE)
    monkeypatch.setenv("WATTRACKER_DATA_DIR", str(data_dir))
    monkeypatch.setenv("WATTRACKER_DB", str(tmp_path / "test.db"))
    # The connector keeps its own directory, and redirecting HOME does not
    # move it on Windows: wattracker_connector.config.config_dir reads
    # LOCALAPPDATA there, which nothing above touches. So a test that starts a
    # ride through the connector's real handlers - conftest_connector's
    # attach_connector builds a BleState with the default buffer - wrote its
    # ride-buffer.jsonl into the rider's own directory, where the next real
    # connect would find it and upload it as their ride. On POSIX the same
    # path lands under the redirected HOME, which is why CI never saw it.
    monkeypatch.setenv(
        "WATTRACKER_CONNECTOR_DIR", str(tmp_path / "connector-config")
    )
    # Ensure no ambient config leaks in.
    monkeypatch.setenv("WATTRACKER_SECRET", "test-secret-key")
    # Keep the suite deterministic: no background scan task, and Zwift
    # player-folder detection looks at an isolated (empty) root, never the
    # machine's real Zwift install.
    monkeypatch.setenv("WATTRACKER_AUTO_SCAN", "0")
    # Registration policy: the first account is always allowed, but a SECOND
    # one now needs WATTRACKER_ALLOW_REGISTRATION (config.allow_registration).
    # A large number of tests legitimately register a second rider through the
    # route to exercise per-user scoping - "bob" in test_backup_route.py, the
    # second accounts in test_auth.py and test_security_fixes.py - and their
    # subject is isolation between accounts, not the sign-up policy. Opting in
    # here keeps that intent intact and keeps the policy in ONE place, so the
    # tests that DO cover it (test_registration_policy.py) are the ones that
    # opt back out with monkeypatch.delenv.
    monkeypatch.setenv("WATTRACKER_ALLOW_REGISTRATION", "1")
    zwift_root = tmp_path / "ZwiftWorkouts"
    zwift_root.mkdir()
    monkeypatch.setenv("WATTRACKER_ZWIFT_WORKOUTS_ROOT", str(zwift_root))
    # Credential storage: force the encrypted file-key backend so tests never
    # touch the developer's real macOS Keychain.
    monkeypatch.setenv("WATTRACKER_KEYRING", "0")
    for key in (
        "WATTRACKER_FTP",
        "WATTRACKER_ZWIFT_ID",
        "WATTRACKER_ACTIVITIES_DIR",
        "WATTRACKER_WORKOUTS_DIR",
        # The default posture is loopback-only; a developer's real tailnet
        # settings must never leak in and quietly widen the Host allowlist
        # under test.
        "WATTRACKER_PUBLIC_HOST",
        "WATTRACKER_PUBLIC_SCHEME",
        # LLM settings: a developer's real keys/endpoints must never leak in
        # and call a live provider from a test.
        "API_KEY",
        "LLM_ENDPOINT",
        "LLM_MODEL",
        # Kept for the legacy-fallback tests, which set it deliberately.
        "ANTHROPIC_API_KEY",
    ):
        monkeypatch.delenv(key, raising=False)
    yield
    # Nothing this fixture sets is scoped to a thread: WATTRACKER_DB and
    # WATTRACKER_DATA_DIR are process-global, and a rescan re-reads both on
    # every database call it makes. So a scan thread that outlives its test
    # does not stop - it starts writing into the NEXT test's sandbox, and CI
    # duly produced a pre-migration backup inside a test that never migrated
    # anything, plus tables vanishing mid-test. create_app()'s shutdown now
    # joins those threads (server.wait_for_scans); this fails the test that
    # leaked one instead of letting the damage land on an innocent test later
    # in the run.
    server = sys.modules.get("wattracker.server")
    if server is not None:
        stragglers = [t.name for t in server.live_scan_threads()]
        assert not stragglers, (
            f"activity scan(s) still running after the test: {stragglers}. "
            "Drive the scan to completion (poll /api/scan/status) or let the "
            "app's TestClient close before the test ends."
        )


@pytest.fixture()
def home_dir(tmp_path):
    """The sandboxed HOME (see isolated_env).

    Use this for any directory a test stores in user_settings and expects the
    app to ACTUALLY USE: activities_dir/workouts_dir are confined to the
    trusted roots on read as well as on write, so a bare tmp_path sibling is
    refused - exactly as the Settings form would refuse it. Tests that want a
    path to be REJECTED should keep using a tmp_path sibling.
    """
    return tmp_path / "home"


@pytest.fixture()
def user_id():
    """Create a test user and return its id."""
    from wattracker import auth, db

    db.init_db()
    return db.create_user("tester", auth.hash_password("password123"))


@pytest.fixture(autouse=True)
def cheap_scrypt(monkeypatch):
    """Drop scrypt to a low cost for the whole suite.

    The production hash is ~128 MiB of memory and ~0.25s of CPU per call, and
    the suite runs it thousands of times - every /register, every login, and
    every `user_id` fixture. What the tests exercise is the hashing CONTRACT
    (format string, verify roundtrip, legacy format, rehash upgrade, timing
    equalization), and all of that is independent of the absolute cost level.
    A cheap but genuine scrypt (n=2**12) keeps every code path identical at
    ~0.007s and ~4 MiB. ``_LEGACY_N`` is lowered to stay below ``_N`` so the
    legacy-format tests keep meaning "an old cheap hash needs a rehash".
    """
    from wattracker import auth as auth_module

    monkeypatch.setattr(auth_module, "_N", 2 ** 12)
    monkeypatch.setattr(auth_module, "_LEGACY_N", 2 ** 10)
    # _DUMMY_HASH is built at import time at the production cost; rebuild it
    # cheaply so the failed-login timing-equalization verify stays cheap too.
    monkeypatch.setattr(
        auth_module,
        "_DUMMY_HASH",
        auth_module.hash_password("wattracker::no-such-user"),
    )


@functools.lru_cache(maxsize=1)
def symlinks_supported() -> bool:
    """True when this process is actually allowed to create a symlink.

    Windows gates ``CreateSymbolicLink`` behind SeCreateSymbolicLinkPrivilege,
    which a standard account only holds with Developer Mode enabled. Without
    it every ``Path.symlink_to`` raises ``OSError`` WinError 1314, and a test
    that plants a symlink fails for a reason that has nothing to do with the
    behaviour it covers.

    This probes rather than branching on ``os.name`` on purpose: the symlink
    containment checks are worth running on a Windows box that HAS the
    privilege, and a blanket POSIX-only skip would claim the behaviour is
    untestable here when it is only unprivileged.
    """
    with tempfile.TemporaryDirectory() as probe:
        link = os.path.join(probe, "link")
        try:
            os.symlink(os.path.join(probe, "target"), link, target_is_directory=True)
        except (OSError, NotImplementedError, AttributeError):
            return False
    return True


#: Reason string shared by every test skipped for the privilege above, so the
#: skip report names the machine setting rather than the platform.
requires_symlinks = pytest.mark.skipif(
    not symlinks_supported(),
    reason="creating symlinks needs Developer Mode (SeCreateSymbolicLinkPrivilege) on Windows",
)


def redirect_home(monkeypatch, path) -> None:
    r"""Point ``os.path.expanduser("~")`` at *path*, on every platform.

    Setting ``HOME`` alone does nothing on Windows: ``ntpath.expanduser``
    reads ``USERPROFILE`` and never consults ``HOME``. A test that redirects
    the home directory into a temp dir therefore silently keeps the real one,
    and since pytest's ``tmp_path`` lives under
    ``%USERPROFILE%\AppData\Local\Temp``, any folder the test builds to
    stand for "outside the home" is in fact inside the trusted root. The
    containment assertions then pass a folder they were written to refuse -
    the test reports success while testing nothing.
    """
    path = str(path)
    monkeypatch.setenv("HOME", path)
    monkeypatch.setenv("USERPROFILE", path)


def _receive_until(ws, predicate, description, cap=200):
    """Receive JSON frames until `predicate` matches one, or fail cleanly.

    A regression that stops a code path from ever sending the awaited frame
    would otherwise hang the loop (and the test run) forever, since the
    websocket has no server-side timeout here. Shared across test modules
    that drive `/ride/ws` (and other websocket endpoints) so every wait-for-
    a-frame loop in the suite gets the same bounded behaviour.
    """
    for _ in range(cap):
        message = ws.receive_json()
        if predicate(message):
            return message
    pytest.fail(f"never received {description} after {cap} frames")


try:  # pragma: no cover - starlette ships with fastapi, so this always binds
    from starlette.websockets import WebSocketDisconnect as _WebSocketDisconnect
except ImportError:  # pragma: no cover - suite is skipped wholesale without it
    _WebSocketDisconnect = None

# What starlette's TestClient actually raises out of `receive_json()` when the
# server closes the socket. Determined empirically against the installed
# starlette (1.6.0) by draining a real `/ride/ws` simulation to completion and
# printing the exception's MRO, not read off the docs: `receive_json()` calls
# `_raise_on_close()`, which turns a `websocket.close` ASGI message into
# `WebSocketDisconnect` - and nothing else surfaced.
#
# WHY this is worth narrowing from the bare `except Exception` it shipped as:
# with `raise_server_exceptions=True` (the default our TestClient fixtures use)
# an unhandled exception inside the endpoint propagates out of this very call.
# A bare `except Exception` swallowed it and returned the frames collected so
# far, so a ride handler that crashed halfway looked exactly like one that
# finished and closed cleanly - the crash was invisible and the test went
# green on a truncated frame list. Naming the disconnect explicitly means a
# server-side crash now escapes this helper and fails the test that caused it.
#
# A tuple (rather than the bare class) so a future starlette that raises an
# additional close-path exception can be accommodated by extending it here,
# once, rather than by widening the catch back out at the call site.
_WS_CLOSED = (_WebSocketDisconnect,) if _WebSocketDisconnect is not None else ()


def _drain_until_close(ws, cap=2000):
    """Collect frames until the server closes the socket, or fail cleanly.

    Mirrors `_receive_until`'s reasoning: without a cap, a regression that
    stops the server from ever closing the socket would hang this loop (and
    the test run) forever instead of failing the one test that hit it.

    Only the close is caught (see `_WS_CLOSED`); anything else the endpoint
    raises propagates so a crashed handler cannot masquerade as a clean close.
    """
    frames = []
    for _ in range(cap):
        try:
            frames.append(ws.receive_json())
        except _WS_CLOSED:
            return frames
    pytest.fail(f"socket never closed after {cap} frames")
