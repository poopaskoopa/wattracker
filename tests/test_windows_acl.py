"""Windows owner-only ACL enforcement in ``config._restrict``.

POSIX ``chmod`` is inert on Windows (it only flips the read-only bit and sets no
ACL), so a data dir relocated onto a volume whose inherited ACL grants other
local accounts read access would leak the session secret and password hashes.
``config._restrict`` therefore applies an owner-only ACL via ``icacls`` on
Windows. These tests are platform-neutral: the Windows branch is exercised on
macOS/Linux by monkeypatching ``os.name`` and the ``icacls`` subprocess call.
"""
import getpass
import os
import subprocess

from wattracker import config


def _capture_run(monkeypatch):
    """Patch getpass + subprocess.run; return the list that records run() argv."""
    calls = []

    def fake_run(argv, *args, **kwargs):
        calls.append((argv, args, kwargs))
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(getpass, "getuser", lambda: "alice")
    monkeypatch.setattr(subprocess, "run", fake_run)
    return calls


# ----------------------------------------------------------------- POSIX branch
def test_posix_uses_chmod_and_never_touches_acl(tmp_path, monkeypatch):
    """On non-Windows, _restrict chmods and never spawns icacls / the ACL helper."""
    monkeypatch.setattr(os, "name", "posix")

    chmods = []
    monkeypatch.setattr(os, "chmod", lambda p, m: chmods.append((p, m)))

    acl_calls = []
    monkeypatch.setattr(
        config, "_restrict_windows_acl", lambda p, d: acl_calls.append((p, d))
    )
    run_calls = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: run_calls.append(a))

    f = tmp_path / "config.json"
    f.write_text("{}")
    config._restrict(str(f), 0o600)

    assert chmods == [(str(f), 0o600)]
    assert acl_calls == []  # ACL helper untouched
    assert run_calls == []  # no subprocess spawned


# --------------------------------------------------------------- Windows branch
def test_windows_file_builds_owner_only_icacls_argv(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "name", "nt")
    calls = _capture_run(monkeypatch)

    f = tmp_path / "wattracker.db"
    f.write_text("x")
    config._restrict(str(f), 0o600, is_dir=False)

    assert len(calls) == 1
    argv = calls[0][0]
    assert argv == ["icacls", str(f), "/inheritance:r", "/grant:r", "alice:F"]
    # never shell=True
    assert calls[0][2].get("shell", False) is False


def test_windows_icacls_gets_no_console_of_its_own(tmp_path, monkeypatch):
    """The connector's frozen build is windowed, so an icacls child would flash.

    A GUI process has no console to lend, so Windows gives each console child a
    brand new one. _restrict runs on every config_dir() call, so the rider sees
    a burst of console windows open and shut on each launch - which reads as a
    crash, and is how a silently-exiting connector was misdiagnosed for a
    session. CREATE_NO_WINDOW is the whole fix.
    """
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    calls = _capture_run(monkeypatch)

    f = tmp_path / "connector.json"
    f.write_text("{}")
    config._restrict(str(f), 0o600, is_dir=False)

    assert calls[0][2].get("creationflags") == 0x08000000


def test_windows_icacls_creationflags_survive_a_missing_flag(tmp_path, monkeypatch):
    """The flag is Windows-only, and these tests fake Windows on POSIX.

    Reading it off the module with getattr rather than naming it directly is
    what keeps this file runnable on the machines most of the suite runs on.
    """
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.delattr(subprocess, "CREATE_NO_WINDOW", raising=False)
    calls = _capture_run(monkeypatch)

    f = tmp_path / "connector.json"
    f.write_text("{}")
    config._restrict(str(f), 0o600, is_dir=False)

    assert calls[0][2].get("creationflags") == 0


def test_windows_dir_gets_inheritable_owner_only_grant(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "name", "nt")
    calls = _capture_run(monkeypatch)

    d = tmp_path / "datadir"
    d.mkdir()
    config._restrict(str(d), 0o700, is_dir=True)

    argv = calls[0][0]
    assert argv == [
        "icacls",
        str(d),
        "/inheritance:r",
        "/grant:r",
        "alice:(OI)(CI)F",
    ]


def test_windows_infers_dir_vs_file_from_path(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "name", "nt")
    calls = _capture_run(monkeypatch)

    d = tmp_path / "inferdir"
    d.mkdir()
    f = tmp_path / "infer.db"
    f.write_text("x")

    config._restrict(str(d), 0o700)  # is_dir omitted -> inferred True
    config._restrict(str(f), 0o600)  # is_dir omitted -> inferred False

    assert calls[0][0][-1] == "alice:(OI)(CI)F"
    assert calls[1][0][-1] == "alice:F"


def test_windows_path_with_spaces_stays_one_argv_element(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "name", "nt")
    calls = _capture_run(monkeypatch)

    f = tmp_path / "my data dir" / "config.json"
    f.parent.mkdir()
    f.write_text("{}")
    config._restrict(str(f), 0o600, is_dir=False)

    argv = calls[0][0]
    # The path must be a single argument, not shell-split on the spaces.
    assert argv[1] == str(f)
    assert " " in argv[1]
    assert len(argv) == 5


# -------------------------------------------------------- failure is swallowed
def test_windows_icacls_nonzero_exit_swallowed(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr(getpass, "getuser", lambda: "alice")

    def boom(argv, *a, **k):
        raise subprocess.CalledProcessError(1, argv)

    monkeypatch.setattr(subprocess, "run", boom)

    f = tmp_path / "wattracker.db"
    f.write_text("x")
    config._restrict(str(f), 0o600, is_dir=False)  # must not raise


def test_windows_icacls_missing_swallowed(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr(getpass, "getuser", lambda: "alice")

    def missing(argv, *a, **k):
        raise FileNotFoundError("icacls not found")

    monkeypatch.setattr(subprocess, "run", missing)

    f = tmp_path / "wattracker.db"
    f.write_text("x")
    config._restrict(str(f), 0o600, is_dir=False)  # must not raise


def test_windows_no_username_skips_icacls(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr(getpass, "getuser", lambda: "")
    monkeypatch.delenv("USERNAME", raising=False)

    calls = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append(a))

    f = tmp_path / "wattracker.db"
    f.write_text("x")
    config._restrict(str(f), 0o600, is_dir=False)

    assert calls == []  # no user resolvable -> nothing spawned, no crash


def test_windows_db_sidecars_all_locked_via_restrict_db_files(tmp_path, monkeypatch):
    """db._restrict_db_files delegates to config._restrict for db + wal + shm."""
    from wattracker import db

    monkeypatch.setattr(os, "name", "nt")
    calls = _capture_run(monkeypatch)

    base = tmp_path / "wattracker.db"
    for suffix in ("", "-wal", "-shm"):
        p = tmp_path / ("wattracker.db" + suffix)
        p.write_text("x")

    db._restrict_db_files(str(base))

    locked = {c[0][1] for c in calls}
    assert locked == {
        str(base),
        str(base) + "-wal",
        str(base) + "-shm",
    }
    # sidecars are files -> plain full-control grant, not the dir (OI)(CI) form
    assert all(c[0][-1] == "alice:F" for c in calls)
