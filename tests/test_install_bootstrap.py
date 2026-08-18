"""Static contracts for the source-install bootstrap path."""
import contextlib
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import uuid

import pytest


ROOT = Path(__file__).resolve().parents[1]
INSTALL = (ROOT / "scripts" / "install.sh").read_text()
START = (ROOT / "start.sh").read_text()
QUICKSTART = (ROOT / "docs" / "quickstart.md").read_text()
PYPROJECT = (ROOT / "pyproject.toml").read_text()
CLOUD_WORKFLOW = (ROOT / ".github" / "workflows" / "cloud.yml").read_text()
START_SCRIPT = ROOT / "start.sh"
VENV_PYTHON = ROOT / ".venv" / "bin" / "python"
INSTALL_MARKER = ROOT / ".venv" / ".wattracker-installed"


def _requires_python_floor():
    """The (major, minor) floor declared by pyproject's requires-python.

    Read by regex rather than tomllib, matching tests/test_windows_installer.py.
    """
    match = re.search(
        r'^requires-python\s*=\s*"\s*>=\s*(\d+)\.(\d+)', PYPROJECT, re.MULTILINE
    )
    assert match, "pyproject.toml has no parsable requires-python floor"
    return match.group(1), match.group(2)


def _version_gates(text):
    """Every `sys.version_info >= (x, y)` floor asserted in a file."""
    return set(re.findall(r"sys\.version_info\s*>=\s*\((\d+),\s*(\d+)\)", text))


def _prose_floors(text):
    """Every "Python x.y or newer" floor stated in prose."""
    return set(re.findall(r"Python (\d+)\.(\d+) or newer", text))


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


# What `ps -o command=` reports for a venv interpreter on a framework Python
# build: the process re-execs through the app bundle and rewrites argv[0], so
# the venv's own path is gone from the command line entirely.
FRAMEWORK_PYTHON = (
    "/usr/local/Cellar/python@3.12/3.12.13_4/Frameworks/Python.framework"
    "/Versions/3.12/Resources/Python.app/Contents/MacOS/Python"
)


def _ps_reporting_framework_python(path, shim_dir):
    # Prepend a `ps` shim that rewrites the interpreter path of a
    # "... -m wattracker" command line into the framework form, leaving every
    # other field (notably `lstart`, which start.sh compares against the
    # pidfile) untouched. This reproduces on any machine what start.sh sees on
    # a Homebrew/python.org interpreter.
    real_ps = shutil.which("ps", path=path)
    assert real_ps is not None
    shim_dir.mkdir(parents=True, exist_ok=True)
    shim = shim_dir / "ps"
    shim.write_text(
        "#!/bin/sh\n"
        f'{real_ps} "$@" | '
        f"sed 's|[^ ]*/[Pp]ython[0-9.]* -m wattracker|{FRAMEWORK_PYTHON} -m wattracker|'\n"
    )
    shim.chmod(0o755)
    return str(shim_dir) + os.pathsep + path


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


def test_installer_gate_and_messages_track_requires_python():
    # Derived from pyproject, not hardcoded: this gate has already drifted once
    # (#104 raised requires-python to >=3.12 and left the installer at 3.10),
    # which waved a too-old interpreter past the friendly check and into a raw
    # pip requires-python error. A hardcoded literal here would have stayed
    # green through exactly that drift. Set equality, not membership, so a
    # half-finished bump that updates one of the two gates or two of the three
    # messages fails too.
    floor = _requires_python_floor()
    assert _version_gates(INSTALL) == {floor}, INSTALL
    assert _prose_floors(INSTALL) == {floor}, INSTALL


def test_ci_and_quickstart_version_floors_track_requires_python():
    floor = _requires_python_floor()
    # A CI gate below requires-python is worse than none: it passes, then the
    # install resolves nothing and the failure surfaces as a dependency error.
    assert _version_gates(CLOUD_WORKFLOW) == {floor}, CLOUD_WORKFLOW
    assert f"requires-python of {floor[0]}.{floor[1]}" in CLOUD_WORKFLOW
    assert _prose_floors(QUICKSTART) == {floor}, QUICKSTART


def test_installer_marks_only_success():
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
    # The interpreter-path match must stay tolerant of the framework Python
    # argv[0] rewrite, and the reason must stay written down next to it.
    assert "[Pp]ython|[Pp]ython[0-9]*)" in START
    assert "Python.app/Contents/MacOS/Python" in START


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
def test_start_recognises_its_own_server_when_ps_reports_framework_python(
    tmp_path, without_lsof
):
    # On a framework Python the interpreter re-execs through
    # Python.app/Contents/MacOS/Python, so `ps` never shows the venv's
    # "$PYTHON". Matching that literal path made a second ./start.sh report
    # "Port N is already in use by something else" and exit 1 instead of
    # "Already running (pid N)", breaking the "Safe to run twice" contract.
    with _ready_repo_environment():
        env = _launcher_env(tmp_path, without_lsof)
        env["PATH"] = _ps_reporting_framework_python(
            env["PATH"], tmp_path / "ps-shim"
        )
        pid = None
        try:
            first = _run_start(env)
            _assert_started(first)
            pid = _state_pid(env)
            assert pid is not None

            shimmed = subprocess.run(
                ["ps", "-p", str(pid), "-o", "command="],
                env=env,
                capture_output=True,
                text=True,
                check=True,
            ).stdout
            assert FRAMEWORK_PYTHON in shimmed, shimmed
            assert str(VENV_PYTHON) not in shimmed, shimmed

            second = _run_start(env)
            output = second.stdout + second.stderr
            assert second.returncode == 0, output
            assert "Already running (pid " in output, output
        finally:
            if pid is not None:
                _stop_pid(pid)


@pytest.mark.parametrize(
    "damage", ["mismatched_start_time", "missing_start_time"]
)
def test_start_refuses_a_pid_whose_recorded_start_time_does_not_hold(
    tmp_path, damage
):
    # The recorded `lstart` is the compensating control for matching the
    # command line loosely: after PID reuse the live PID can run something
    # that looks exactly like our server, and only the start time separates
    # them. Rewriting the pidfile's second line reproduces that without
    # waiting for the PID space to wrap — same live PID, same command line,
    # start time the pidfile cannot vouch for.
    #
    # `missing_start_time` is the same guard from the other side: a one-line
    # pidfile (a legacy file from a pre-lstart start.sh, or one written when
    # `ps -o lstart=` returned nothing) must fail closed rather than skip the
    # comparison. It self-heals — the next successful start writes both lines.
    #
    # test_start_does_not_trust_a_reused_pid_without_lsof does not cover this:
    # its stale process runs a different command line, so it is rejected by
    # the command match before the start time is ever consulted.
    with _ready_repo_environment():
        env = _launcher_env(tmp_path, without_lsof=False)
        pid = None
        try:
            _assert_started(_run_start(env))
            pid = _state_pid(env)
            assert pid is not None
            pid_file = Path(env["WATTRACKER_HOME"]) / "server.pid"
            lines = pid_file.read_text().splitlines()
            assert len(lines) >= 2 and lines[1].strip(), lines
            if damage == "mismatched_start_time":
                pid_file.write_text(f"{pid}\nThu Jan  1 00:00:00 2015\n")
            else:
                pid_file.write_text(f"{pid}\n")

            second = _run_start(env)
            output = second.stdout + second.stderr
            # Unproven, so start.sh must not claim the process as its own —
            # and must still refuse to start a duplicate on the held port.
            assert "Already running (pid " not in output, output
            assert second.returncode == 1, output
            assert "already in use by something else" in output, output
            assert _state_pid(env) == pid, "the pidfile must not be overwritten"
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


# --- restart.sh process matching -----------------------------------------
#
# restart.sh signals every PID it selects, so these tests must never let it
# see a process they do not own: this machine can be running the real server
# and a self-hosted CI runner whose working directory is
# .../_work/wattracker/wattracker/. The sandbox below gives restart.sh its own
# ROOT (so its PID file is test-owned), its own free port (so lsof finds
# nothing), and a `pgrep` shim that runs the *real* pgrep — the pattern under
# test has to behave exactly as it does in production — then keeps only the
# PIDs this test spawned.

RESTART_SCRIPT = ROOT / "scripts" / "restart.sh"
RESTART = RESTART_SCRIPT.read_text()

IDLE_PROGRAM = "import time; time.sleep(600)\n"


class _RestartSandbox:
    def __init__(self, root, script, env, token, allow_file):
        self.root = root
        self.script = script
        self.env = env
        self.token = token
        self.allow_file = allow_file

    def allow(self, pid):
        """Let the pgrep shim report this PID even though it lacks the token."""
        with self.allow_file.open("a") as handle:
            handle.write(f"{pid}\n")

    def run(self, *args):
        return subprocess.run(
            [str(self.script), *args],
            cwd=str(self.root),
            env=self.env,
            capture_output=True,
            text=True,
            timeout=120,
        )


def _restart_sandbox(tmp_path, path=None):
    token = "tok" + uuid.uuid4().hex
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    script = root / "scripts" / "restart.sh"
    shutil.copy2(RESTART_SCRIPT, script)
    script.chmod(0o755)

    path = path or os.environ.get("PATH", os.defpath)
    real_pgrep = shutil.which("pgrep", path=path)
    real_ps = shutil.which("ps", path=path)
    assert real_pgrep and real_ps

    allow_file = tmp_path / "allowed-pids"
    allow_file.write_text("")
    shim_dir = tmp_path / "pgrep-shim"
    shim_dir.mkdir()
    shim = shim_dir / "pgrep"
    shim.write_text(
        "#!/bin/sh\n"
        f'{real_pgrep} "$@" | while IFS= read -r pid; do\n'
        f'  if grep -Fxq "$pid" "{allow_file}"; then printf "%s\\n" "$pid"; continue; fi\n'
        f'  case "$({real_ps} -p "$pid" -o command= 2>/dev/null)" in\n'
        f'    *{token}*) printf "%s\\n" "$pid" ;;\n'
        "  esac\n"
        "done\n"
    )
    shim.chmod(0o755)

    env = {
        **os.environ,
        "PATH": str(shim_dir) + os.pathsep + path,
        "WATTRACKER_PORT": str(_free_port()),
        "WATTRACKER_TERM_TIMEOUT": "2",
    }
    return _RestartSandbox(root, script, env, token, allow_file)


def _spawn(args, cwd=None, env=None):
    return subprocess.Popen(
        [str(arg) for arg in args],
        cwd=str(cwd) if cwd else None,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _reap(proc):
    if proc is None:
        return
    if proc.poll() is None:
        proc.kill()
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=10)


def _idle_in(directory, name="idle.py"):
    directory.mkdir(parents=True, exist_ok=True)
    script = directory / name
    script.write_text(IDLE_PROGRAM)
    return script


def test_restart_matches_the_module_invocation_not_the_bare_name():
    assert "pgrep -f 'wattracker'" not in RESTART
    assert "pgrep -f '[-]m wattracker'" in RESTART
    assert "process_is_wattracker_server()" in RESTART
    assert '*" -m wattracker")' in RESTART
    assert '*" -m wattracker "*)' in RESTART
    # The interpreter is constrained by basename, never by "$PYTHON": a
    # framework Python rewrites argv[0], and the reason must stay written down.
    assert '*"$PYTHON -m wattracker"*)' not in RESTART
    assert "[Pp]ython|[Pp]ython[0-9]*)" in RESTART
    assert "Python.app/Contents/MacOS/Python" in RESTART
    # The safety ordering is the other half of the contract.
    assert RESTART.index("kill -TERM") < RESTART.index("kill -KILL")
    assert "still alive after SIGKILL" in RESTART


def test_restart_stop_leaves_a_process_whose_path_merely_contains_the_name(
    tmp_path,
):
    # The self-hosted CI runner works out of
    # /Users/<user>/actions-runner/_work/wattracker/wattracker/, so every
    # process it spawns carries "wattracker" in its command line as a path
    # component. `pgrep -f 'wattracker'` matched those, and stop() SIGTERMed
    # and then SIGKILLed in-flight CI.
    box = _restart_sandbox(tmp_path)
    bystander_dir = tmp_path / box.token / "_work" / "wattracker" / "wattracker"
    proc = _spawn([sys.executable, _idle_in(bystander_dir)])
    try:
        result = box.run("stop")
        output = result.stdout + result.stderr
        assert result.returncode == 0, output
        assert "not running (nothing to stop)" in output, output
        assert str(proc.pid) not in output, output
        time.sleep(0.5)
        assert proc.poll() is None, "restart.sh signalled an unrelated process"
    finally:
        _reap(proc)


def test_restart_stop_leaves_the_connector_module_alone(tmp_path):
    # `-m wattracker_connector` is a different process with its own lifecycle;
    # the app's restart script must not take it down.
    box = _restart_sandbox(tmp_path)
    stub = tmp_path / box.token / "stub"
    _idle_in(stub / "wattracker_connector", name="__main__.py")
    (stub / "wattracker_connector" / "__init__.py").write_text("")
    # The trailing argument only carries this sandbox's token, so the pgrep
    # shim can see the process at all; it is not part of what is matched.
    proc = _spawn(
        [sys.executable, "-m", "wattracker_connector", box.token],
        cwd=stub,
        env={**os.environ, "PYTHONPATH": str(stub)},
    )
    try:
        result = box.run("stop")
        output = result.stdout + result.stderr
        assert result.returncode == 0, output
        assert "not running (nothing to stop)" in output, output
        time.sleep(0.5)
        assert proc.poll() is None, "restart.sh killed the connector"
    finally:
        _reap(proc)


@pytest.mark.parametrize("framework_python", [False, True])
def test_restart_stop_still_stops_a_real_module_server(tmp_path, framework_python):
    # The fallback's legitimate purpose: a real server that is neither in the
    # PID file nor holding the port restart.sh was asked about. `framework_python`
    # replays the argv[0] rewrite PR #107 fixed in start.sh — `ps` reports
    # Python.app/Contents/MacOS/Python, not the venv interpreter.
    if not VENV_PYTHON.is_file():
        pytest.skip("behavioral launcher tests need the repository virtualenv")
    path = os.environ.get("PATH", os.defpath)
    if framework_python:
        path = _ps_reporting_framework_python(path, tmp_path / "ps-shim")
    box = _restart_sandbox(tmp_path, path=path)
    server = _spawn(
        [VENV_PYTHON, "-m", "wattracker"],
        cwd=ROOT,
        env={
            **os.environ,
            "WATTRACKER_HOME": str(tmp_path / "state"),
            "WATTRACKER_DATA_DIR": str(tmp_path / "data"),
            # a port of its own: the fallback, not lsof, has to find it
            "WATTRACKER_PORT": str(_free_port()),
            "WATTRACKER_OPEN_BROWSER": "0",
            "WATTRACKER_AUTO_SCAN": "0",
        },
    )
    reported = ""
    try:
        box.allow(server.pid)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and server.poll() is None:
            reported = subprocess.run(
                ["ps", "-p", str(server.pid), "-o", "command="],
                env=box.env,
                capture_output=True,
                text=True,
            ).stdout
            if " -m wattracker" in reported:
                break
            time.sleep(0.2)
        assert server.poll() is None, "the test server exited before stop"
        assert " -m wattracker" in reported, reported
        if framework_python:
            assert FRAMEWORK_PYTHON in reported, reported
            assert str(VENV_PYTHON) not in reported, reported

        result = box.run("stop")
        output = result.stdout + result.stderr
        assert result.returncode == 0, output
        assert str(server.pid) in output, output
        server.wait(timeout=20)
    finally:
        _reap(server)


def test_restart_stop_does_not_report_a_phantom_from_transient_matches(tmp_path):
    # Observed live: the PIDs changed between the "stopping PID(s)" line and
    # the SIGKILL line, because the bare-name pattern kept matching whatever
    # short-lived process happened to mention the repo — including this
    # script's own pipeline. stop() then failed with a false
    # "still alive after SIGKILL" even though the real server was already down.
    box = _restart_sandbox(tmp_path)
    churn_dir = tmp_path / box.token / "_work" / "wattracker" / "wattracker"
    churn_dir.mkdir(parents=True)
    # Short-lived shells that mention the repo path and ignore SIGTERM: a
    # stand-in for the invoking pipeline and for whatever CI is running out of
    # that directory at the moment restart.sh looks.
    churner_src = tmp_path / "churn.py"
    churner_src.write_text(
        "import os, subprocess, time\n"
        "arg0 = os.environ['CHURN_ARG0']\n"
        "while True:\n"
        "    subprocess.Popen(['/bin/sh', '-c', 'trap \"\" TERM; sleep 3', arg0])\n"
        "    time.sleep(0.3)\n"
    )
    # The churner's own command line carries neither the token nor the name —
    # the child's $0 comes in through the environment — so only its
    # short-lived children are ever visible to the shim, and the churn keeps
    # producing fresh matches for as long as restart.sh keeps looking.
    churner = _spawn(
        [sys.executable, churner_src],
        env={**os.environ, "CHURN_ARG0": str(churn_dir / "run.sh")},
    )
    try:
        time.sleep(1)
        result = box.run("stop")
        output = result.stdout + result.stderr
        assert "still alive after SIGKILL" not in output, output
        assert result.returncode == 0, output
        assert "not running (nothing to stop)" in output, output
    finally:
        _reap(churner)
        # the children ignore SIGTERM by design; they exit on their own
        time.sleep(3.2)
