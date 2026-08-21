"""Shared pytest fixtures: isolate config + database in a temp directory."""
import os
import sys

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
    # Ensure no ambient config leaks in.
    monkeypatch.setenv("WATTRACKER_SECRET", "test-secret-key")
    # Keep the suite deterministic: no background scan task, and Zwift
    # player-folder detection looks at an isolated (empty) root, never the
    # machine's real Zwift install.
    monkeypatch.setenv("WATTRACKER_AUTO_SCAN", "0")
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


def redirect_home(monkeypatch, path) -> None:
    """Point ``os.path.expanduser("~")`` at *path*, on every platform.

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
