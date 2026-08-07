"""Static contracts for the source-install bootstrap path."""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INSTALL = (ROOT / "scripts" / "install.sh").read_text()
START = (ROOT / "start.sh").read_text()
QUICKSTART = (ROOT / "docs" / "quickstart.md").read_text()


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
    assert 'port_is_listening()' in START
    assert 'if command -v lsof' in START
    assert 'lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -a -p "$1"' in START
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
