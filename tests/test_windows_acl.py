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

import pytest

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


#: What icacls prints for a /findsid that matched nothing. CAPTURED from
#: Windows 11 build 26100, not reasoned: the miss carries its own line ABOVE
#: the summary, so a miss is TWO lines and not one. Reading it as one is what
#: left the gate in ``config._acl_needs_reset`` resetting every path.
FINDSID_NO_MATCH = (
    "No files with a matching SID was found\n"
    "Successfully processed 1 files; Failed processing 0 files\n"
)


def _findsid_match(path):
    """What icacls prints for a /findsid that DID match. Captured, as above.

    The path is echoed behind a "SID Found: " label and carries a trailing
    period, so a substring test reads it and an equality test would not.
    """
    return (
        f"SID Found: {path}.\n"
        "Successfully processed 1 files; Failed processing 0 files\n"
    )


def _capture_run(monkeypatch, stdout=None, stderr=None):
    """Patch getpass + subprocess.run + %SystemRoot%; return the recorded argv.

    ``stdout`` is what the fake icacls writes; pass a callable to vary it per
    argv. It defaults to None - i.e. output that the /findsid probe cannot read
    - which is deliberately the INCONCLUSIVE case, so every test that does not
    care about the probe still exercises the reset-then-grant path it was
    written for. Tests that care pass FINDSID_NO_MATCH or _findsid_match().
    """
    calls = []

    def fake_run(argv, *args, **kwargs):
        calls.append((argv, args, kwargs))
        out = stdout(argv) if callable(stdout) else stdout
        return subprocess.CompletedProcess(argv, 0, stdout=out, stderr=stderr)

    monkeypatch.setattr(getpass, "getuser", lambda: "alice")
    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setenv("SystemRoot", FAKE_SYSTEM_ROOT)
    return calls


def _grants(calls):
    """Just the owner-only grant calls.

    Each _restrict spawns a /findsid probe (possibly several), then - only if
    the probe says the path already carries a foreign ace, or could not say -
    a /reset, then the grant. Tests about WHAT is granted want the grant;
    picking it by flag rather than by index keeps them readable and
    order-independent.
    """
    return [c for c in calls if "/grant:r" in c[0]]


def _resets(calls):
    return [c for c in calls if c[0][-1] == "/reset"]


def _probes(calls):
    return [c for c in calls if "/findsid" in c[0]]


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

    # probe (inconclusive under the default fake -> reset), /reset, grant
    assert len(calls) == 3
    assert calls[0][0] == [
        _expected_icacls(), str(f), "/findsid", "*S-1-5-32-545"
    ]
    assert calls[1][0] == [_expected_icacls(), str(f), "/reset"]
    assert calls[2][0] == [
        _expected_icacls(), str(f), "/inheritance:r", "/grant:r", "alice:F"
    ]
    # never shell=True, on any call
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

    # EVERY spawn, or whichever one was missed is the one that flashes.
    assert calls  # not vacuous
    assert [c[2].get("creationflags") for c in calls] == [0x08000000] * len(calls)


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

    assert calls  # not vacuous
    assert [c[2].get("creationflags") for c in calls] == [0] * len(calls)


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
    # every spawn, since any one taking it as two would touch the wrong file
    # (or fail and leave the real one untouched). For the probe that would be
    # a silent false negative: no match found on a path that was never looked
    # at, and the reset skipped on a path that needed it.
    for argv, _a, _k in calls:
        assert argv[1] == str(f)
        assert " " in argv[1]
    assert len(_grants(calls)[0][0]) == 5
    assert len(_resets(calls)[0][0]) == 3
    assert len(_probes(calls)[0][0]) == 4


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

    # a probe, a /reset and a grant for each of the two paths
    assert len(calls) == 6
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
    assert calls  # not vacuous
    assert [c[0][0] for c in calls] == [expected] * len(calls)


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
    d = tmp_path / "datadir"
    d.mkdir()
    # A path that already carries BUILTIN\Users, so the probe says "reset".
    calls = _capture_run(monkeypatch, stdout=_findsid_match(str(d)))

    config._restrict(str(d), 0o700, is_dir=True)

    assert [c[0][2:] for c in calls] == [
        ["/findsid", "*S-1-5-32-545"],
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

    f = tmp_path / "wattracker.db"
    f.write_text("x")

    def fake_run(argv, *a, **k):
        seen.append(argv)
        if argv[-1] == "/reset":
            raise subprocess.CalledProcessError(1, argv)
        return subprocess.CompletedProcess(argv, 0, stdout=_findsid_match(str(f)))

    monkeypatch.setattr(subprocess, "run", fake_run)

    config._restrict(str(f), 0o600, is_dir=False)  # must not raise

    assert [argv[2:] for argv in seen] == [
        ["/findsid", "*S-1-5-32-545"],
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

    d = tmp_path / "datadir"
    d.mkdir()

    def fake_run(argv, *a, **k):
        if "/findsid" in argv:
            return subprocess.CompletedProcess(
                argv, 0, stdout=_findsid_match(str(d))
            )
        if argv[-1] == "/reset":
            return subprocess.CompletedProcess(argv, 0)
        raise subprocess.CalledProcessError(1, argv)

    monkeypatch.setattr(subprocess, "run", fake_run)

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


# ---------------------------------------------- the /reset is gated on a probe
#
# WHY the gate is itself a security control, and not a spawn-count optimisation:
# /reset restores the PARENT's inheritable ACL, and Windows evaluates a DACL at
# CreateFile time, so a handle another local account opens between the reset and
# the grant keeps its access indefinitely. That window is harmless under an
# already-locked parent, but db_path() hands back a WATTRACKER_DB override
# verbatim and nothing restricts ITS parent - so an unconditional reset would
# widen D:\shared\wt.db (+ -wal, -shm) to that volume's default at every app
# start, on files no earlier version ever widened. Probing first means the reset
# only ever runs on a path that is ALREADY exposed, where the window grants
# nothing that was not already granted.
#
# The probe reads STDOUT, not the return code: /findsid is a reporting verb and
# exits 0 whether or not it matched, so a returncode-only check would make the
# reset either never or always run. The shapes asserted here were captured on
# Windows 11 build 26100; the earlier reasoned ones were wrong.


def test_windows_probe_finding_a_foreign_ace_runs_the_reset(tmp_path, monkeypatch):
    """A path that already names BUILTIN\\Users must still get its /reset."""
    monkeypatch.setattr(os, "name", "nt")
    f = tmp_path / "wattracker.db"
    f.write_text("x")
    calls = _capture_run(monkeypatch, stdout=_findsid_match(str(f)))

    config._restrict(str(f), 0o600, is_dir=False)

    assert len(_resets(calls)) == 1
    assert len(_grants(calls)) == 1


def test_windows_probe_finding_nothing_skips_the_reset_and_still_grants(
    tmp_path, monkeypatch
):
    """The regression this gate exists to prevent, stated directly.

    An already-clean path must NOT be reset: the reset would hand it its
    parent's ACL for the length of one spawn, and on a relocated WATTRACKER_DB
    nothing restricts that parent. The grant is not optional though - severing
    inheritance and narrowing to the owner is the whole job.
    """
    monkeypatch.setattr(os, "name", "nt")
    calls = _capture_run(monkeypatch, stdout=FINDSID_NO_MATCH)

    f = tmp_path / "wattracker.db"
    f.write_text("x")
    config._restrict(str(f), 0o600, is_dir=False)

    assert _resets(calls) == []  # nothing foreign found -> no window opened
    assert [c[0][2:] for c in _grants(calls)] == [
        ["/inheritance:r", "/grant:r", "alice:F"]
    ]


def test_windows_probe_covers_users_everyone_and_authenticated_users(
    tmp_path, monkeypatch
):
    r"""All three principals that make a path readable by another account.

    Numerical ``*``-prefixed SIDs, never friendly names: ``BUILTIN\Users`` is
    ``VORDEFINIERT\Benutzer`` on a German Windows, and a name that does not
    resolve makes the probe silently find nothing - which skips the reset on a
    path that needed it, i.e. reinstates the exposure this PR closes.
    """
    monkeypatch.setattr(os, "name", "nt")
    calls = _capture_run(monkeypatch, stdout=FINDSID_NO_MATCH)

    f = tmp_path / "wattracker.db"
    f.write_text("x")
    config._restrict(str(f), 0o600, is_dir=False)

    assert [c[0][3] for c in _probes(calls)] == [
        "*S-1-5-32-545",  # BUILTIN\Users
        "*S-1-1-0",  # Everyone
        "*S-1-5-11",  # NT AUTHORITY\Authenticated Users
    ]


def test_windows_probe_stops_at_the_first_sid_it_finds(tmp_path, monkeypatch):
    """One hit is enough to decide; the remaining SIDs are not worth a spawn."""
    monkeypatch.setattr(os, "name", "nt")
    f = tmp_path / "wattracker.db"
    f.write_text("x")

    def out(argv):
        if argv[-1] == "*S-1-1-0":  # Everyone: the second SID probed
            return _findsid_match(str(f))
        return FINDSID_NO_MATCH

    calls = _capture_run(monkeypatch, stdout=out)
    config._restrict(str(f), 0o600, is_dir=False)

    assert [c[0][3] for c in _probes(calls)] == ["*S-1-5-32-545", "*S-1-1-0"]
    assert len(_resets(calls)) == 1


def test_windows_probe_never_recurses_into_the_tree(tmp_path, monkeypatch):
    """/T would walk a whole data dir on every config save. The probe is per-path."""
    monkeypatch.setattr(os, "name", "nt")
    calls = _capture_run(monkeypatch, stdout=FINDSID_NO_MATCH)

    d = tmp_path / "datadir"
    d.mkdir()
    config._restrict(str(d), 0o700, is_dir=True)

    for argv, _a, _k in _probes(calls):
        assert "/T" not in argv and "/t" not in argv
        assert argv[2:] == ["/findsid", argv[3]]


# ------------------------------------------- a probe that cannot answer resets
#
# FAIL TOWARD RESETTING. Skipping the reset because the probe broke would leave
# the explicit foreign ace in place - the exact bug this branch exists to fix -
# and would do it silently. Resetting is what the code did before the probe, so
# it can never be worse than the status quo.


def _resets_despite(monkeypatch, tmp_path, **capture_kwargs):
    """Run _restrict with a misbehaving probe; return the recorded calls."""
    monkeypatch.setattr(os, "name", "nt")
    calls = _capture_run(monkeypatch, **capture_kwargs)
    f = tmp_path / "wattracker.db"
    f.write_text("x")
    config._restrict(str(f), 0o600, is_dir=False)  # must not raise
    return calls


def test_windows_probe_that_writes_to_stderr_resets_anyway(tmp_path, monkeypatch):
    calls = _resets_despite(
        monkeypatch, tmp_path, stdout=FINDSID_NO_MATCH, stderr="Access is denied.\n"
    )
    assert len(_resets(calls)) == 1
    assert len(_probes(calls)) == 1  # inconclusive short-circuits


@pytest.mark.parametrize(
    "stdout",
    [
        pytest.param(None, id="not-captured"),
        pytest.param(b"bytes", id="not-text"),
        pytest.param("", id="empty"),
        pytest.param("   \n\n", id="blank-only"),
        pytest.param("a\nb\nc\n", id="more-than-one-line"),
    ],
)
def test_windows_probe_output_of_an_unexpected_shape_resets_anyway(
    tmp_path, monkeypatch, stdout
):
    """Only "exactly one non-blank line that does not name the path" is a miss.

    Anything else is output this code does not understand - a different icacls,
    a localisation that wraps, a decode that mangled the path - and guessing
    "clean" from output we cannot read is how the reset silently stops running.
    """
    calls = _resets_despite(monkeypatch, tmp_path, stdout=stdout)
    assert len(_resets(calls)) == 1
    assert len(_probes(calls)) == 1


@pytest.mark.parametrize(
    "exc",
    [
        pytest.param(subprocess.CalledProcessError(1, ["icacls"]), id="nonzero-exit"),
        pytest.param(subprocess.TimeoutExpired(["icacls"], 15), id="timeout"),
        pytest.param(FileNotFoundError("icacls not found"), id="missing-icacls"),
        pytest.param(PermissionError("access denied"), id="access-denied"),
    ],
)
def test_windows_probe_that_raises_resets_anyway(tmp_path, monkeypatch, exc):
    """Every way the probe can blow up, including the OSError family.

    TimeoutExpired is included on purpose: it is a SubprocessError, so it lands
    in the same handler a non-zero exit does, and a probe hung on a stalled
    network mount must not be the reason a foreign ace survives.
    """
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr(getpass, "getuser", lambda: "alice")
    monkeypatch.setenv("SystemRoot", FAKE_SYSTEM_ROOT)

    seen = []

    def fake_run(argv, *a, **k):
        seen.append(argv)
        if "/findsid" in argv:
            raise exc
        return subprocess.CompletedProcess(argv, 0, stdout="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    f = tmp_path / "wattracker.db"
    f.write_text("x")
    config._restrict(str(f), 0o600, is_dir=False)  # must not raise

    assert [argv[2:] for argv in seen] == [
        ["/findsid", "*S-1-5-32-545"],
        ["/reset"],
        ["/inheritance:r", "/grant:r", "alice:F"],
    ]


# --------------------------------- the kwargs every spawn is required to carry


def test_windows_every_icacls_spawn_checks_its_exit_code(tmp_path, monkeypatch):
    """check=True is load-bearing, not a default worth keeping tidy.

    Without it a non-zero grant raises nothing, so a path whose /reset landed
    and whose grant did not is left carrying its parent's ACL and NO warning
    fires at all - the warning lives in an except branch that a returncode
    alone never reaches. On the probe, check=False turns a failed /findsid into
    an empty-stdout "clean" answer and skips the reset.
    """
    monkeypatch.setattr(os, "name", "nt")
    calls = _capture_run(monkeypatch, stdout=FINDSID_NO_MATCH)

    d = tmp_path / "datadir"
    d.mkdir()
    config._restrict(str(d), 0o700, is_dir=True)

    assert calls  # not vacuous
    assert [c[2].get("check") for c in calls] == [True] * len(calls)


def test_windows_every_icacls_spawn_carries_a_timeout(tmp_path, monkeypatch):
    """db.py supports a WATTRACKER_DB on a network mount; icacls there can hang.

    A hang in the GRANT is the dangerous one: the path stays at whatever the
    reset left it - the parent's inherited ACL - permanently, and the warning
    that would have said so is in an except branch a hang never reaches.
    TimeoutExpired is a SubprocessError, so timeout= alone routes it there.
    """
    monkeypatch.setattr(os, "name", "nt")
    calls = _capture_run(monkeypatch, stdout=FINDSID_NO_MATCH)

    d = tmp_path / "datadir"
    d.mkdir()
    config._restrict(str(d), 0o700, is_dir=True)

    assert calls  # not vacuous
    for _argv, _a, kwargs in calls:
        timeout = kwargs.get("timeout")
        assert isinstance(timeout, (int, float)) and timeout > 0
        assert timeout == config._ACL_SPAWN_TIMEOUT
    assert issubclass(subprocess.TimeoutExpired, subprocess.SubprocessError)


def test_windows_every_icacls_spawn_captures_its_output(tmp_path, monkeypatch):
    """Two reasons, and the second one is new.

    icacls writes to the console, and the connector's frozen build has none -
    uncaptured output is what CREATE_NO_WINDOW is hiding. And the /findsid
    probe READS stdout: without capture_output there is nothing to read, and
    the probe answers "inconclusive" forever, which quietly re-widens every
    relocated WATTRACKER_DB the gate exists to protect.
    """
    monkeypatch.setattr(os, "name", "nt")
    calls = _capture_run(monkeypatch, stdout=FINDSID_NO_MATCH)

    d = tmp_path / "datadir"
    d.mkdir()
    config._restrict(str(d), 0o700, is_dir=True)

    assert calls  # not vacuous
    assert [c[2].get("capture_output") for c in calls] == [True] * len(calls)
    # and decoded, since the probe compares the path against stdout as text.
    # errors="replace" because a non-ASCII path under a console codepage that
    # does not match the ANSI one must degrade to replacement characters - a
    # UnicodeDecodeError here would surface as "probe failed" for a reason that
    # has nothing to do with the ACL.
    assert all(c[2].get("text") is True for c in calls)
    assert all(c[2].get("errors") == "replace" for c in calls)


def test_windows_grant_still_runs_when_the_reset_raises_oserror(
    tmp_path, monkeypatch
):
    """The reset's handler must catch OSError, not only SubprocessError.

    The likeliest real failures - icacls missing, the path gone, access denied -
    are OSErrors. If the reset's except were narrowed to SubprocessError the
    OSError would propagate to _restrict's own `except OSError`, which swallows
    it and skips the GRANT entirely: no inheritance severed, no owner-only ace,
    silently. Asserting "must not raise" would not notice, because _restrict
    swallows it either way - so this asserts the grant argv was still spawned.
    """
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr(getpass, "getuser", lambda: "alice")
    monkeypatch.setenv("SystemRoot", FAKE_SYSTEM_ROOT)

    f = tmp_path / "wattracker.db"
    f.write_text("x")
    seen = []

    def fake_run(argv, *a, **k):
        seen.append(argv)
        if argv[-1] == "/reset":
            raise PermissionError("access is denied")
        return subprocess.CompletedProcess(argv, 0, stdout=_findsid_match(str(f)))

    monkeypatch.setattr(subprocess, "run", fake_run)

    config._restrict(str(f), 0o600, is_dir=False)  # must not raise

    assert [argv[2:] for argv in seen] == [
        ["/findsid", "*S-1-5-32-545"],
        ["/reset"],
        ["/inheritance:r", "/grant:r", "alice:F"],
    ]


def test_windows_probe_reads_a_path_echoed_without_a_summary(tmp_path, monkeypatch):
    """The path echo and the line count are two guards, not one.

    Every other match case here prints path + summary, which the "exactly one
    non-blank line" shape check already catches - so the path comparison sits
    behind it and a mutation deleting the comparison survives unnoticed. This
    is the case only the comparison can answer: one line, and that line is the
    matched path (a suppressed or absent summary). Reading it as a clean miss
    would skip the reset on a path that demonstrably carries a foreign ace.
    """
    monkeypatch.setattr(os, "name", "nt")
    f = tmp_path / "wattracker.db"
    f.write_text("x")
    calls = _capture_run(monkeypatch, stdout=f"{f}\n")

    config._restrict(str(f), 0o600, is_dir=False)

    assert len(_probes(calls)) == 1  # decided on the first sid
    assert len(_resets(calls)) == 1


def test_windows_probe_path_echo_is_matched_case_insensitively(
    tmp_path, monkeypatch
):
    """NTFS is case-insensitive and icacls may echo a differently-cased path.

    A case-sensitive compare would read a real match as a miss and skip the
    reset - a silent false negative, the one direction this must never fail in.
    """
    monkeypatch.setattr(os, "name", "nt")
    f = tmp_path / "wattracker.db"
    f.write_text("x")
    calls = _capture_run(monkeypatch, stdout=f"{str(f).upper()}\n")

    config._restrict(str(f), 0o600, is_dir=False)

    assert len(_resets(calls)) == 1


def test_windows_warns_when_an_oserror_kills_the_grant_after_a_reset(
    tmp_path, monkeypatch, caplog
):
    """The reset-outlives-the-grant warning must survive an OSError too.

    ``test_windows_warns_when_the_reset_outlives_the_grant`` only ever raises
    CalledProcessError, so narrowing the grant's except to SubprocessError
    leaves it green: the OSError would instead propagate into _restrict's own
    ``except OSError``, which swallows it at DEBUG. The path is then sitting on
    its parent's inherited ACL - the widest state this function can produce -
    and nobody is told. Missing icacls, a vanished path and access-denied are
    all OSErrors, so this is the likelier half of the failure, not the exotic
    one.
    """
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr(getpass, "getuser", lambda: "alice")
    monkeypatch.setenv("SystemRoot", FAKE_SYSTEM_ROOT)

    d = tmp_path / "datadir"
    d.mkdir()

    def fake_run(argv, *a, **k):
        if "/findsid" in argv:
            return subprocess.CompletedProcess(
                argv, 0, stdout=_findsid_match(str(d))
            )
        if argv[-1] == "/reset":
            return subprocess.CompletedProcess(argv, 0, stdout="")
        raise PermissionError("access is denied")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with caplog.at_level(logging.WARNING, logger=config._log.name):
        config._restrict(str(d), 0o700, is_dir=True)  # must not raise

    assert [r.levelname for r in caplog.records] == ["WARNING"]
    assert str(d) in caplog.records[0].getMessage()


def test_windows_probe_reads_the_real_two_line_miss_as_clean(
    tmp_path, monkeypatch
):
    """The defect the captured fixtures caught, pinned as its own test.

    Both answers are two lines, so a reader that requires exactly one calls
    every path inconclusive and resets unconditionally - reinstating the
    window this gate exists to close, on every app start.
    """
    monkeypatch.setattr(os, "name", "nt")
    calls = _capture_run(monkeypatch, stdout=FINDSID_NO_MATCH)
    f = tmp_path / "wattracker.db"
    f.write_text("x")

    assert config._acl_needs_reset(_expected_icacls(), str(f), {}) is False
    assert len(_probes(calls)) == 3  # all three sids walked, none matched


def test_windows_probe_resets_a_non_ascii_path_without_probing(
    tmp_path, monkeypatch
):
    """Outside ASCII the echo cannot be trusted, so the path resets.

    icacls writes the OEM codepage and text mode decodes the ANSI one. A
    mangled echo would read as a clean miss, which is the one direction this
    must not fail in.
    """
    monkeypatch.setattr(os, "name", "nt")
    calls = _capture_run(monkeypatch, stdout=FINDSID_NO_MATCH)
    f = tmp_path / "ライド.db"
    f.write_text("x")

    config._restrict(str(f), 0o600, is_dir=False)

    assert _probes(calls) == []  # not probed: the answer could not be read
    assert len(_resets(calls)) == 1
