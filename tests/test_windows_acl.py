r"""Windows owner-only ACL enforcement in ``config._restrict``.

POSIX ``chmod`` is inert on Windows (it only flips the read-only bit and sets no
ACL), so a data dir relocated onto a volume whose inherited ACL grants other
local accounts read access would leak the session secret and password hashes.
``config._restrict`` therefore applies an owner-only ACL via ``icacls`` on
Windows. These tests are platform-neutral: the Windows branch is exercised on
macOS/Linux by monkeypatching ``os.name`` and the ``icacls`` subprocess call.

The argv[0] assertions below are a security control, not a formatting
preference. ``subprocess.run`` with a list and no ``shell=True`` reaches
``CreateProcessW`` with ``lpApplicationName=NULL``, and Windows then resolves a
bare program name starting from the calling executable's own directory and the
current working directory - BOTH ahead of System32. The connector is a portable
.exe, and ``_restrict`` runs on its very first code path, so a bare "icacls"
would execute an ``icacls.exe`` planted beside that download, silently
(CREATE_NO_WINDOW + capture_output), on every launch and every config save. The
absolute ``%SystemRoot%\System32\icacls.exe`` skips the search entirely. If a
future edit reverts to the bare name these tests must fail, which is what
``test_windows_icacls_is_never_a_bare_or_relative_name`` exists to guarantee
even if the exact-argv assertions are ever loosened.
"""
import getpass
import logging
import ntpath
import os
import subprocess

from wattracker import config

#: Stand-in for a real Windows %SystemRoot%. Set explicitly in the tests rather
#: than relied on from the environment, because the machines this suite runs on
#: do not have one.
FAKE_SYSTEM_ROOT = r"C:\Windows"


def _expected_icacls() -> str:
    """The absolute path _restrict_windows_acl must invoke.

    Built with the same os.path.join the implementation uses, so this stays
    honest on POSIX (where the separator differs) without hardcoding a
    platform-specific literal.
    """
    return os.path.join(FAKE_SYSTEM_ROOT, "System32", "icacls.exe")


def _capture_run(monkeypatch):
    """Patch getpass + subprocess.run + %SystemRoot%; return the recorded argv."""
    calls = []

    def fake_run(argv, *args, **kwargs):
        calls.append((argv, args, kwargs))
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(getpass, "getuser", lambda: "alice")
    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setenv("SystemRoot", FAKE_SYSTEM_ROOT)
    return calls


def _grants(calls):
    """Just the owner-only grant calls.

    Each _restrict spawns two icacls: a /reset that drops explicit aces, then
    the grant. Tests about WHAT is granted want the second one; picking it by
    flag rather than by index keeps them readable and order-independent.
    """
    return [c for c in calls if "/grant:r" in c[0]]


def _resets(calls):
    return [c for c in calls if c[0][-1] == "/reset"]


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

    assert len(calls) == 2  # the /reset, then the grant
    assert calls[0][0] == [_expected_icacls(), str(f), "/reset"]
    assert calls[1][0] == [
        _expected_icacls(), str(f), "/inheritance:r", "/grant:r", "alice:F"
    ]
    # never shell=True, on either call
    assert all(c[2].get("shell", False) is False for c in calls)


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

    # Both spawns, or the reset is the one that flashes a console.
    assert [c[2].get("creationflags") for c in calls] == [0x08000000] * 2


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

    assert [c[2].get("creationflags") for c in calls] == [0] * 2


def test_windows_dir_gets_inheritable_owner_only_grant(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "name", "nt")
    calls = _capture_run(monkeypatch)

    d = tmp_path / "datadir"
    d.mkdir()
    config._restrict(str(d), 0o700, is_dir=True)

    argv = _grants(calls)[0][0]
    assert argv == [
        _expected_icacls(),
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

    grants = _grants(calls)
    assert grants[0][0][-1] == "alice:(OI)(CI)F"
    assert grants[1][0][-1] == "alice:F"


def test_windows_path_with_spaces_stays_one_argv_element(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "name", "nt")
    calls = _capture_run(monkeypatch)

    f = tmp_path / "my data dir" / "config.json"
    f.parent.mkdir()
    f.write_text("{}")
    config._restrict(str(f), 0o600, is_dir=False)

    # The path must be a single argument, not shell-split on the spaces - on
    # both spawns, since either one taking it as two would touch the wrong
    # file (or fail and leave the real one untouched).
    for argv, _a, _k in calls:
        assert argv[1] == str(f)
        assert " " in argv[1]
    assert len(_grants(calls)[0][0]) == 5
    assert len(_resets(calls)[0][0]) == 3


# ------------------------------------------ argv[0] must not be searched for
def test_windows_icacls_is_never_a_bare_or_relative_name(tmp_path, monkeypatch):
    """The binary-planting regression guard, stated as a property.

    With ``lpApplicationName=NULL`` - which is what passing a list to
    subprocess.run gives you - CreateProcessW searches the calling
    executable's directory and the current working directory BEFORE System32.
    The connector is a portable .exe and _restrict is the first thing its
    __main__ reaches, so a bare "icacls" means an attacker-supplied
    ``icacls.exe`` dropped next to the download runs as the rider on every
    launch, every config save and every log rotation - invisibly, because the
    call is CREATE_NO_WINDOW with capture_output.

    Asserted as a property rather than an exact string so that ANY relative
    form (``icacls``, ``icacls.exe``, ``.\\icacls.exe``, ``System32\\icacls.exe``)
    fails, not only the one that was there before.
    """
    monkeypatch.setattr(os, "name", "nt")
    calls = _capture_run(monkeypatch)

    d = tmp_path / "datadir"
    d.mkdir()
    f = tmp_path / "wattracker.db"
    f.write_text("x")
    config._restrict(str(d), 0o700, is_dir=True)
    config._restrict(str(f), 0o600, is_dir=False)

    assert len(calls) == 4  # a /reset and a grant for each of the two paths
    for argv, _args, _kwargs in calls:
        program = argv[0]
        # Not the bare name, under any spelling.
        assert os.path.basename(program) == "icacls.exe"
        assert program != "icacls"
        assert program != "icacls.exe"
        # Absolute, so no search happens at all. ntpath is used explicitly:
        # this asserts what WINDOWS would consider absolute, and these tests
        # run on POSIX where "C:\\Windows\\..." is just a relative filename.
        assert ntpath.isabs(program.replace("/", "\\")), program
        # And rooted in the system directory, not in whatever the process's
        # own directory or CWD happens to be.
        assert program.startswith(FAKE_SYSTEM_ROOT)


def test_windows_icacls_path_follows_systemroot(tmp_path, monkeypatch):
    """A relocated Windows install (%SystemRoot% is not always C:\\Windows).

    Reading the environment rather than hardcoding C:\\Windows keeps the fix
    from becoming a broken no-op on such a machine, which would silently
    reinstate the unrestricted-ACL finding the whole function exists to close.
    """
    monkeypatch.setattr(os, "name", "nt")
    calls = _capture_run(monkeypatch)
    monkeypatch.setenv("SystemRoot", r"D:\WinNT")

    f = tmp_path / "wattracker.db"
    f.write_text("x")
    config._restrict(str(f), 0o600, is_dir=False)

    expected = os.path.join(r"D:\WinNT", "System32", "icacls.exe")
    assert [c[0][0] for c in calls] == [expected] * 2


def test_windows_icacls_path_falls_back_when_systemroot_is_missing(monkeypatch):
    """Windows always sets %SystemRoot%; be defined anyway if it is absent.

    An empty or missing value must still produce an ABSOLUTE path. Guessing
    C:\\Windows wrongly degrades to the same best-effort no-op a missing
    icacls already gives (the caller swallows it); falling back to the bare
    name would degrade to running the planted binary.
    """
    monkeypatch.delenv("SystemRoot", raising=False)
    assert config._icacls_path() == os.path.join(
        r"C:\Windows", "System32", "icacls.exe"
    )

    monkeypatch.setenv("SystemRoot", "")
    assert config._icacls_path() == os.path.join(
        r"C:\Windows", "System32", "icacls.exe"
    )


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


def test_windows_clears_explicit_aces_before_granting(tmp_path, monkeypatch):
    r"""/inheritance:r and /grant:r together cannot produce an owner-only acl.

    ``/inheritance:r`` removes only INHERITED aces, and ``/grant:r`` replaces
    aces only for the user it names, so an explicit ace belonging to anyone
    else survives both. Confirmed on Windows 11: a directory carrying an
    explicit ``BUILTIN\Users:(OI)(CI)(R)`` still carried it afterwards, and
    the ``wattracker.db`` created inside then inherited it as
    ``Users:(I)(R)`` - every local standard account able to read the session
    secret, LLM api key and password hashes, which is the exact exposure this
    function exists to close.

    ``/reset`` drops the explicit aces, and it has to be its own spawn:
    icacls rejects it alongside ``/inheritance:r`` ("Invalid parameter").
    Order is the property under test - a reset AFTER the grant undoes it.
    """
    monkeypatch.setattr(os, "name", "nt")
    calls = _capture_run(monkeypatch)

    d = tmp_path / "datadir"
    d.mkdir()
    config._restrict(str(d), 0o700, is_dir=True)

    assert [c[0][2:] for c in calls] == [
        ["/reset"],
        ["/inheritance:r", "/grant:r", "alice:(OI)(CI)F"],
    ]


def test_windows_grant_still_runs_when_the_reset_fails(tmp_path, monkeypatch):
    """A failed reset must not cost us the grant.

    Without the reset this function is exactly what it was before, and that
    was not nothing: inherited aces still get severed and the owner still
    gets full control. Skipping the grant because the reset failed would
    trade a partial fix for no fix.
    """
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr(getpass, "getuser", lambda: "alice")
    monkeypatch.setenv("SystemRoot", FAKE_SYSTEM_ROOT)

    seen = []

    def fake_run(argv, *a, **k):
        seen.append(argv)
        if argv[-1] == "/reset":
            raise subprocess.CalledProcessError(1, argv)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)

    f = tmp_path / "wattracker.db"
    f.write_text("x")
    config._restrict(str(f), 0o600, is_dir=False)  # must not raise

    assert [argv[2:] for argv in seen] == [
        ["/reset"],
        ["/inheritance:r", "/grant:r", "alice:F"],
    ]


def test_windows_warns_when_the_reset_outlives_the_grant(
    tmp_path, monkeypatch, caplog
):
    """The one ordering that ends WIDER than it started.

    /reset restores the inherited default and the grant is what narrows it
    again, so a reset that lands followed by a grant that does not leaves the
    path carrying whatever its parent grants - worse than never having been
    touched. Every other failure here is best-effort and debug-level. This one
    is not: a silent no-op is precisely how the original unrestricted-acl
    finding survived long enough to need a security review.
    """
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr(getpass, "getuser", lambda: "alice")
    monkeypatch.setenv("SystemRoot", FAKE_SYSTEM_ROOT)

    def fake_run(argv, *a, **k):
        if argv[-1] == "/reset":
            return subprocess.CompletedProcess(argv, 0)
        raise subprocess.CalledProcessError(1, argv)

    monkeypatch.setattr(subprocess, "run", fake_run)

    d = tmp_path / "datadir"
    d.mkdir()
    with caplog.at_level(logging.WARNING, logger=config._log.name):
        config._restrict(str(d), 0o700, is_dir=True)  # must not raise

    assert [r.levelname for r in caplog.records] == ["WARNING"]
    assert str(d) in caplog.records[0].getMessage()


def test_windows_a_failure_that_changed_nothing_stays_quiet(
    tmp_path, monkeypatch, caplog
):
    """Both spawns failing is the ordinary no-op, not the loud one.

    A missing icacls or a path already gone fails both calls and leaves the
    acl exactly as it was found. Warning about that would teach the reader to
    ignore the warning that does mean something.
    """
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr(getpass, "getuser", lambda: "alice")
    monkeypatch.setenv("SystemRoot", FAKE_SYSTEM_ROOT)

    def missing(argv, *a, **k):
        raise FileNotFoundError("icacls not found")

    monkeypatch.setattr(subprocess, "run", missing)

    d = tmp_path / "datadir"
    d.mkdir()
    with caplog.at_level(logging.WARNING, logger=config._log.name):
        config._restrict(str(d), 0o700, is_dir=True)  # must not raise

    assert caplog.records == []


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
    assert all(c[0][-1] == "alice:F" for c in _grants(calls))
    # and each of the three got its explicit aces cleared first
    assert {c[0][1] for c in _resets(calls)} == locked
