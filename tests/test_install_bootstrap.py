"""Static contracts for the source-install bootstrap path."""
import contextlib
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import time

import pytest


ROOT = Path(__file__).resolve().parents[1]
INSTALL = (ROOT / "scripts" / "install.sh").read_text()
START = (ROOT / "start.sh").read_text()
QUICKSTART = (ROOT / "docs" / "quickstart.md").read_text()
START_SCRIPT = ROOT / "start.sh"
VENV_PYTHON = ROOT / ".venv" / "bin" / "python"
INSTALL_MARKER = ROOT / ".venv" / ".wattracker-installed"


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _without_lsof(path, shim_root):
    # Hiding lsof by dropping its whole directory from PATH is wrong: on
    # usrmerge Linux lsof lives in /usr/bin, and dropping that directory
    # also removes bash/env/python3, breaking the `#!/usr/bin/env bash`
    # launcher itself. Instead, rebuild only the offending directory as a
    # shim that symlinks every entry except lsof.
    #
    # A simpler shim (prepend a directory with a fake/non-executable lsof)
    # also doesn't work: `command -v lsof` (what start.sh uses) skips
    # non-executable candidates and keeps searching PATH, and an
    # exit-127 executable shim would make start.sh take the "lsof found"
    # branch instead of the no-lsof branch under test.
    entries = path.split(os.pathsep)
    new_entries = []
    shim_root.mkdir(parents=True, exist_ok=True)
    for i, entry in enumerate(entries):
        entry_dir = Path(entry or os.curdir)
        candidate = entry_dir / "lsof"
        if not (candidate.is_file() and os.access(candidate, os.X_OK)):
            new_entries.append(entry)
            continue
        shim_dir = shim_root / f"shim{i}"
        shim_dir.mkdir(parents=True, exist_ok=True)
        for other in entry_dir.iterdir():
            if other.name == "lsof":
                continue
            link = shim_dir / other.name
            if not link.is_symlink():
                link.symlink_to(other)
        new_entries.append(str(shim_dir))
    stripped = os.pathsep.join(new_entries)

    assert shutil.which("lsof", path=stripped) is None
    assert shutil.which("bash", path=stripped) is not None
    assert shutil.which("env", path=stripped) is not None
    return stripped


def _make_exe(path):
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(0o755)


def test_without_lsof_hides_lsof_but_keeps_bash_on_usrmerge_layout(tmp_path):
    # Regression test for a usrmerge-style layout (e.g. lsof and bash both
    # live in /usr/bin): dropping the whole directory to hide lsof would
    # also take bash out, which is exactly the bug this guards against.
    usrbin = tmp_path / "usr" / "bin"
    usrbin.mkdir(parents=True)
    _make_exe(usrbin / "lsof")
    _make_exe(usrbin / "bash")
    _make_exe(usrbin / "env")

    stripped = _without_lsof(str(usrbin), tmp_path / "shims")

    assert shutil.which("lsof", path=stripped) is None
    assert shutil.which("bash", path=stripped) is not None
    assert shutil.which("env", path=stripped) is not None


@contextlib.contextmanager
def _ready_repo_environment():
    if not VENV_PYTHON.is_file():
        pytest.skip("behavioral launcher tests need the repository virtualenv")

    had_marker = INSTALL_MARKER.exists()
    old_marker = INSTALL_MARKER.read_bytes() if had_marker else None
    old_marker_stat = INSTALL_MARKER.stat() if had_marker else None
    INSTALL_MARKER.parent.mkdir(parents=True, exist_ok=True)
    INSTALL_MARKER.write_text("behavioral test\n")
    newer_than_project = max(
        (ROOT / "pyproject.toml").stat().st_mtime,
        (ROOT / "scripts" / "install.sh").stat().st_mtime,
    ) + 1
    os.utime(INSTALL_MARKER, (newer_than_project, newer_than_project))
    try:
        yield
    finally:
        if had_marker:
            INSTALL_MARKER.write_bytes(old_marker)
            os.utime(
                INSTALL_MARKER,
                ns=(old_marker_stat.st_atime_ns, old_marker_stat.st_mtime_ns),
            )
        else:
            INSTALL_MARKER.unlink(missing_ok=True)


def _launcher_env(tmp_path, without_lsof):
    path = os.environ.get("PATH", os.defpath)
    if without_lsof:
        path = _without_lsof(path, tmp_path / "lsof-shims")
    return {
        **os.environ,
        "PATH": path,
        "WATTRACKER_HOME": str(tmp_path / "state"),
        "WATTRACKER_DATA_DIR": str(tmp_path / "data"),
        "WATTRACKER_PORT": str(_free_port()),
        "WATTRACKER_OPEN_BROWSER": "0",
        "WATTRACKER_AUTO_SCAN": "0",
    }


def _run_start(env):
    return subprocess.run(
        [str(START_SCRIPT)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _stop_pid(pid):
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.1)
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _state_pid(env):
    pid_file = Path(env["WATTRACKER_HOME"]) / "server.pid"
    if not pid_file.exists():
        return None
    try:
        return int(pid_file.read_text().splitlines()[0])
    except (ValueError, IndexError):
        return None


def _assert_started(result):
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "Started (pid " in output, output


def test_installer_is_local_and_never_escalates_or_installs_globally():
    assert "\nsudo " not in INSTALL
    assert "\nsudo\t" not in INSTALL
    assert '"$VENV_PYTHON" -m pip install' in INSTALL
    assert "pip install -e ." not in INSTALL.replace(
        '"$VENV_PYTHON" -m pip install --disable-pip-version-check -e .', ""
    )
    assert 'VENV="$ROOT/.venv"' in INSTALL
    assert "global pip install" in INSTALL


def test_installer_checks_python_and_marks_only_success():
    assert "sys.version_info >= (3, 10)" in INSTALL
    assert 'if [ -x "$VENV_PYTHON" ]; then' in INSTALL
    pip = INSTALL.index('"$VENV_PYTHON" -m pip install')
    marker = INSTALL.index('> "$MARKER"')
    assert marker > pip
    assert "Ready. Run ./start.sh" in INSTALL


def test_start_bootstraps_missing_or_stale_environment_before_launching():
    assert 'INSTALLER="scripts/install.sh"' in START
    assert 'MARKER=".venv/.wattracker-installed"' in START
    assert '"$INSTALLER"' in START
    assert '"pyproject.toml" -nt "$MARKER"' in START
    assert 'recorded="$(sed -n \'1p\' "$PIDFILE"' in START
    assert 'recorded_start="$(sed -n \'2p\' "$PIDFILE"' in START
    assert 'port_is_listening()' in START
    assert 'process_command_is_wattracker()' in START
    assert 'recorded_process_is_wattracker()' in START
    assert 'if command -v lsof' in START
    assert 'lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -a -p "$1"' in START
    assert 'log_has_pid_bind()' in START
    assert 'log_has_fresh_bind()' in START
    assert 'grep -F "Uvicorn running on"' in START
    assert 'server_is_ready()' in START
    assert 'pgrep -f' not in START


def test_quickstart_describes_one_command_first_run_and_current_distribution_limit():
    assert "./start.sh" in QUICKSTART
    assert "git clone" in QUICKSTART
    assert "does not use `sudo`" in QUICKSTART
    assert "not yet a public notarized macOS DMG" in QUICKSTART


def test_readme_surfaces_the_one_command_path():
    readme = (ROOT / "README.md").read_text()
    assert "./start.sh" in readme
    assert "docs/quickstart.md" in readme


@pytest.mark.parametrize(
    "without_lsof",
    [
        pytest.param(
            False,
            marks=pytest.mark.skipif(
                shutil.which("lsof") is None,
                reason="lsof-present behavior requires lsof",
            ),
        ),
        True,
    ],
)
def test_start_twice_reports_its_own_server_without_starting_a_duplicate(
    tmp_path, without_lsof
):
    with _ready_repo_environment():
        env = _launcher_env(tmp_path, without_lsof)
        pid = None
        try:
            first = _run_start(env)
            _assert_started(first)
            pid = _state_pid(env)
            assert pid is not None
            second = _run_start(env)
            output = second.stdout + second.stderr
            assert second.returncode == 0, output
            assert "Already running (pid " in output, output
        finally:
            if pid is not None:
                _stop_pid(pid)


def test_start_does_not_trust_a_reused_pid_without_lsof(tmp_path):
    with _ready_repo_environment():
        env = _launcher_env(tmp_path, without_lsof=True)
        stale = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
        )
        state_dir = Path(env["WATTRACKER_HOME"])
        state_dir.mkdir(parents=True)
        (state_dir / "server.pid").write_text(f"{stale.pid}\n")
        started_pid = None
        try:
            result = _run_start(env)
            _assert_started(result)
            output = result.stdout + result.stderr
            assert "Already running (pid " not in output, output
            started_pid = _state_pid(env)
            assert started_pid is not None
            assert started_pid != stale.pid
        finally:
            if started_pid is not None:
                _stop_pid(started_pid)
            stale.terminate()
            stale.wait(timeout=10)
