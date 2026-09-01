"""Windows behaviour asserted against the real APIs, not against a mock.

Every other Windows test in this suite describes what the code DECIDES - the
argv it builds, the branch it takes - and those run happily on macOS by
monkeypatching ``os.name``. They are worth keeping. What they cannot do is say
what Windows DOES in response, and the difference is not academic:

* ``test_windows_acl.py`` is entirely mocked, and for weeks it encoded the
  wrong ``/findsid`` output shape. Every test passed while the reset gate it
  covered had never once returned False.
* ``windows_secrets`` is only ever exercised through an injected fake backend,
  so the ctypes DPAPI path that protects the rider's Zwift password has never
  executed under test on any platform.
* ``paths._windows_documents_known_folder`` is monkeypatched everywhere, and
  the one test that reaches the real shell32 call returned early off Windows -
  reporting PASSED on macOS having asserted nothing.

So this file is deliberately small and deliberately real: it runs only on
Windows, it calls no mock, and it exists to catch the class of error a mock
cannot, because a mock is the belief being tested.
"""

import os
import subprocess
import sys

import pytest

from wattracker import config, paths, windows_secrets

pytestmark = pytest.mark.skipif(
    os.name != "nt", reason="these assert what Windows does, not what we decide"
)

#: BUILTIN\Users, in the numerical form icacls takes. Numerical because the
#: friendly name is localised.
USERS_SID = "*S-1-5-32-545"


def _icacls(*args):
    """Run the real icacls and return (returncode, stdout)."""
    return subprocess.run(
        [config._icacls_path(), *args],
        capture_output=True, text=True, errors="replace", timeout=30,
    )


def _names_the_path(path):
    """Does a /findsid for BUILTIN\\Users report this path?

    Written out here rather than calling ``_acl_needs_reset``: a test that
    checks the code with the code cannot catch a wrong belief about icacls,
    which is the whole reason this file exists.
    """
    proc = _icacls(path, "/findsid", USERS_SID)
    return path.casefold() in proc.stdout.casefold()


# ------------------------------------------------------------------ the ACL


def test_an_explicit_foreign_ace_is_actually_cleared(tmp_path):
    """The exposure #134 closed, verified by asking Windows rather than a mock.

    ``/inheritance:r`` removes only INHERITED aces and ``/grant:r`` replaces
    aces only for the principal it names, so an explicit ace for anybody else
    survives both. The preceding ``/reset`` is what removes it.
    """
    target = str(tmp_path / "target")
    os.makedirs(target)
    _icacls(target, "/grant", f"{USERS_SID}:(OI)(CI)(R)")
    assert _names_the_path(target), "setup failed: no foreign ace to clear"

    config._restrict(target, 0o700)

    assert not _names_the_path(target)


def test_a_child_created_afterwards_is_owner_only(tmp_path):
    """(OI)(CI) on the directory has to reach the db, the -wal and the -shm."""
    target = str(tmp_path / "target")
    os.makedirs(target)
    _icacls(target, "/grant", f"{USERS_SID}:(OI)(CI)(R)")
    config._restrict(target, 0o700)

    child = os.path.join(target, "wattracker.db")
    open(child, "a").close()

    assert not _names_the_path(child)


def test_a_clean_path_is_not_reset(tmp_path):
    """The regression the probe gate exists to prevent.

    A reset hands the path its parent's ACL for the length of one spawn, and
    Windows evaluates a DACL at CreateFile time - so a handle opened in that
    window outlives the re-narrowing. On a relocated WATTRACKER_DB nothing
    restricts that parent. This is the assertion no mock could make honestly:
    it depends on what icacls prints for a miss.
    """
    target = str(tmp_path / "clean")
    os.makedirs(target)
    spawn = {
        "check": True, "capture_output": True, "text": True,
        "errors": "replace", "timeout": config._ACL_SPAWN_TIMEOUT,
        "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0),
    }

    assert config._acl_needs_reset(config._icacls_path(), target, spawn) is False


def test_a_path_carrying_a_foreign_ace_is_reset(tmp_path):
    """The other direction, so a gate stuck at False would fail too."""
    target = str(tmp_path / "exposed")
    os.makedirs(target)
    _icacls(target, "/grant", f"{USERS_SID}:(OI)(CI)(R)")
    spawn = {
        "check": True, "capture_output": True, "text": True,
        "errors": "replace", "timeout": config._ACL_SPAWN_TIMEOUT,
        "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0),
    }

    assert config._acl_needs_reset(config._icacls_path(), target, spawn) is True


# ----------------------------------------------------------------- the DPAPI


def test_dpapi_round_trip_through_the_real_ctypes_backend():
    """CryptProtectData/CryptUnprotectData, with no backend injected."""
    marker = windows_secrets.protect_password("correct horse", "wattracker-test", 42)

    assert marker.startswith("dpapi1$")
    assert windows_secrets.unprotect_password(marker, "wattracker-test", 42) == (
        "correct horse"
    )


def test_real_dpapi_refuses_another_user_s_entropy():
    """The entropy is what stops one rider's blob decoding for another."""
    marker = windows_secrets.protect_password("correct horse", "wattracker-test", 42)

    with pytest.raises(windows_secrets.DPAPIError):
        windows_secrets.unprotect_password(marker, "wattracker-test", 43)
    with pytest.raises(windows_secrets.DPAPIError):
        windows_secrets.unprotect_password(marker, "other-service", 42)


# ---------------------------------------------------------- the known folder

#: The real profile, captured at IMPORT time - before any fixture runs. See
#: the test below for why a value captured later would be useless.
_REAL_USERPROFILE = os.environ.get("USERPROFILE")

_ASK_SHELL32 = (
    "from wattracker import paths;"
    "print(paths._windows_documents_known_folder() or '')"
)


def test_the_real_known_folder_call_resolves_to_a_real_directory():
    """shell32.SHGetKnownFolderPath, unmocked - and it needs its own process.

    A rider whose Documents folder OneDrive has redirected is what the call
    exists for, and a monkeypatched stub cannot fail on that case. Reaching the
    real call takes more than unpatching, though:

    conftest's autouse ``isolated_env`` moves ``USERPROFILE`` into a sandbox -
    rightly, since without it the suite reads the rider's real folders. The
    known-folder answer is then decided by the FIRST call in the process:
    asked once under the sandbox profile it returns ERROR_FILE_NOT_FOUND
    (0x80070002) and keeps returning None afterwards, even once the real
    profile is restored. So the call is not merely patched here, it is
    poisoned for the life of the process, and no in-process unpatching helps.

    A child process with the real profile is therefore the only honest way to
    ask. That is also what makes this test worth having: the failure it would
    catch - shell32 refusing, or naming somewhere that does not exist - is
    invisible to every mocked test in the suite.
    """
    proc = subprocess.run(
        [sys.executable, "-c", _ASK_SHELL32],
        capture_output=True, text=True, timeout=60,
        env={**os.environ, "USERPROFILE": _REAL_USERPROFILE},
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    )
    known = proc.stdout.strip()

    assert proc.returncode == 0, proc.stderr
    assert known, "shell32 gave no Documents known folder"
    assert os.path.isabs(known)
    assert os.path.isdir(known)
