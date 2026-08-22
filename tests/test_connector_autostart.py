"""Start with Windows: the one registry value, and the ways it goes wrong.

The registry is real here, but the key is not: ``RUN_KEY`` is redirected to a
private key under HKCU that each test creates and removes. Writing the actual
Run value from a test would put a startup entry on whoever's machine ran the
suite - and pointing it at a pytest process is exactly the failure mode this
module is about.

Off Windows there is no registry at all, which is the other half of what is
pinned: this module must still import, still answer ``enabled()``, and still
refuse rather than raise something the caller has to guess at.
"""
import ast
import os
import pathlib
import sys

import pytest

from wattracker_connector import autostart

WINDOWS = os.name == "nt"
windows_only = pytest.mark.skipif(not WINDOWS, reason="the registry is Windows'")

# Under HKCU, alongside nothing anybody else uses, and deleted after each test.
_TEST_KEY = r"Software\wattracker-connector-tests\Run"


@pytest.fixture()
def registry(monkeypatch):
    """Point the module at a scratch key, and take it away afterwards."""
    import winreg

    monkeypatch.setattr(autostart, "RUN_KEY", _TEST_KEY)
    yield
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, _TEST_KEY, 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.DeleteValue(key, autostart.VALUE_NAME)
    except OSError:
        pass
    for path in (_TEST_KEY, r"Software\wattracker-connector-tests"):
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, path)
        except OSError:
            pass


@pytest.fixture()
def frozen(monkeypatch, tmp_path):
    """Pretend to be the packaged executable, from a path we control."""
    executable = tmp_path / "WattrackerConnector.exe"
    executable.write_bytes(b"")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(executable))
    return executable


# --------------------------------------------------------------- the target
def test_the_key_is_the_per_user_one():
    """HKLM, a service and a scheduled task all ask for elevation."""
    assert autostart.RUN_KEY == (
        r"Software\Microsoft\Windows\CurrentVersion\Run"
    )


def test_the_value_name_carries_no_version():
    """The name is the entry's identity: enable() rewrites, never duplicates."""
    assert autostart.VALUE_NAME == "wattracker-connector"
    assert not any(character.isdigit() for character in autostart.VALUE_NAME)


def test_the_path_is_quoted_so_program_files_survives(frozen, monkeypatch, tmp_path):
    """An unquoted Run value splits on its first space and launches C:\\Program."""
    spaced = tmp_path / "Program Files" / "WattrackerConnector.exe"
    spaced.parent.mkdir()
    spaced.write_bytes(b"")
    monkeypatch.setattr(sys, "executable", str(spaced))

    assert autostart.command() == f'"{spaced}"'


def test_importing_it_reaches_no_registry_and_asks_no_questions():
    """A module that acts at import time cannot be reasoned about.

    "Is winreg in sys.modules" would be the obvious check and is worthless on
    Windows, where the interpreter has already imported it before any of this
    runs. What actually settles it is that no statement at module scope calls
    anything, which is a question about the syntax tree.
    """
    source = pathlib.Path(autostart.__file__).read_text(encoding="utf-8")
    for statement in ast.parse(source).body:
        if isinstance(statement, (ast.FunctionDef, ast.ClassDef, ast.Import,
                                  ast.ImportFrom)):
            continue
        if isinstance(statement, ast.Expr) and isinstance(
            statement.value, ast.Constant
        ):
            continue
        assert isinstance(statement, (ast.Assign, ast.AnnAssign)), statement
        for node in ast.walk(statement):
            if not isinstance(node, ast.Call):
                continue
            # Getting a logger is the one exception, and the only one.
            assert ast.unparse(node.func) == "logging.getLogger", (
                f"autostart.py calls {ast.unparse(node.func)}() at import "
                f"time, line {statement.lineno}"
            )


# ------------------------------------------------------------- the refusals
def test_a_python_process_refuses_to_register(monkeypatch):
    """A Run entry naming a venv breaks silently the moment the venv moves."""
    monkeypatch.delattr(sys, "frozen", raising=False)

    with pytest.raises(autostart.AutostartUnavailable) as excinfo:
        autostart.enable()
    assert "packaged" in str(excinfo.value).lower() or "windows" in str(
        excinfo.value
    ).lower()
    assert not autostart.supported()


@pytest.mark.skipif(WINDOWS, reason="the point is the other platforms")
def test_off_windows_it_answers_rather_than_exploding():
    """The caller is a menu asking whether to draw a tick."""
    assert autostart.enabled() is False
    assert autostart.registered_command() is None
    assert autostart.supported() is False
    assert autostart.refresh() is False
    autostart.disable()  # must not raise: absent is the desired state
    with pytest.raises(autostart.AutostartUnavailable):
        autostart.enable()


# --------------------------------------------------------- the real registry
@windows_only
def test_enable_writes_exactly_one_value_and_disable_removes_it(registry, frozen):
    import winreg

    assert autostart.enabled() is False
    autostart.enable()

    assert autostart.enabled() is True
    assert autostart.registered_command() == f'"{frozen}"'
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _TEST_KEY) as key:
        assert winreg.QueryInfoKey(key)[1] == 1  # one value, not a pile of them

    autostart.disable()
    assert autostart.enabled() is False
    assert autostart.registered_command() is None


@windows_only
def test_enabling_twice_leaves_one_entry(registry, frozen):
    import winreg

    autostart.enable()
    autostart.enable()

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _TEST_KEY) as key:
        assert winreg.QueryInfoKey(key)[1] == 1


@windows_only
def test_disabling_something_that_was_never_enabled_is_quiet(registry, frozen):
    autostart.disable()
    autostart.disable()
    assert autostart.enabled() is False


@windows_only
def test_a_delete_that_really_failed_is_not_reported_as_success(
    registry, frozen, monkeypatch
):
    """The tray shows its warning by catching what disable() raises.

    So a swallowed OSError is not a quiet success, it is a lie: the rider is
    told "It will no longer start with Windows." while the entry survives and
    runs again at the next logon. Only FileNotFoundError means "already gone";
    access denied and a locked hive are the opposite and must reach the tray.
    """
    import winreg

    autostart.enable()
    real_delete = winreg.DeleteValue

    def _denied(key, name):
        raise PermissionError(5, "Access is denied")

    monkeypatch.setattr(winreg, "DeleteValue", _denied)
    with pytest.raises(OSError):
        autostart.disable()

    monkeypatch.setattr(winreg, "DeleteValue", real_delete)
    # And the entry really is still there, which is what made it a lie.
    assert autostart.enabled() is True


@windows_only
def test_a_moved_executable_is_repointed_at_where_it_now_is(
    registry, frozen, monkeypatch, tmp_path
):
    """The rider ticked the box, then moved the exe out of Downloads.

    Nothing tells Windows that. Without this the entry names a file that is no
    longer there and autostart stops working with nothing to say so.
    """
    autostart.enable()
    moved = tmp_path / "Tools" / "WattrackerConnector.exe"
    moved.parent.mkdir()
    moved.write_bytes(b"")
    monkeypatch.setattr(sys, "executable", str(moved))

    assert autostart.refresh() is True
    assert autostart.registered_command() == f'"{moved}"'
    assert autostart.enabled() is True


@windows_only
def test_refresh_never_creates_an_entry_nobody_asked_for(registry, frozen):
    """Startup calls this on every launch; it must not opt anybody in."""
    assert autostart.refresh() is False
    assert autostart.enabled() is False


@windows_only
def test_refresh_leaves_an_entry_that_is_already_right_alone(registry, frozen):
    """Case and quoting are free variations, not a reason to rewrite HKCU."""
    import winreg

    with winreg.CreateKeyEx(
        winreg.HKEY_CURRENT_USER, _TEST_KEY, 0, winreg.KEY_SET_VALUE
    ) as key:
        winreg.SetValueEx(
            key, autostart.VALUE_NAME, 0, winreg.REG_SZ, str(frozen).upper()
        )

    assert autostart.refresh() is False
    assert autostart.registered_command() == str(frozen).upper()


@windows_only
def test_a_non_frozen_process_never_repoints_an_entry(
    registry, frozen, monkeypatch, caplog
):
    """A rider with autostart on, running the pip script once, keeps their exe.

    And hears nothing about it. The refusal is checked before it is attempted
    rather than caught afterwards, because the caught version would log a
    warning about failing to do something nobody asked for - on every single
    launch of the console script, forever.
    """
    autostart.enable()
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.setattr(sys, "executable", r"C:\venv\Scripts\python.exe")

    with caplog.at_level("WARNING", logger="wattracker_connector.autostart"):
        assert autostart.refresh() is False

    assert autostart.registered_command() == f'"{frozen}"'
    assert caplog.records == []
