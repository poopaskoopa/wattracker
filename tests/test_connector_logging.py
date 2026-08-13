"""Where the connector's diagnostics go once there is no console to print to.

The frozen tray build is windowed. It has no stderr, and on Windows a write to
that closed handle raises rather than being quietly dropped, so "log to stderr"
stops being a harmless default and becomes a crash. The log file replaces it -
which makes the file the one place a rider's credential could end up sitting in
plaintext, and that is what most of this module is about.
"""
import logging
import os
import stat
import sys

import pytest

from wattracker_connector import config as connector_config
from wattracker_connector.__main__ import (
    _ConnectorStreamHandler,
    _configure_logging,
    main,
)

TOKEN = "s3cret-device-token-value-that-is-long-enough-abc"


@pytest.fixture(autouse=True)
def connector_dir(tmp_path, monkeypatch):
    """A private config dir, and a root logger returned to how it was found."""
    directory = tmp_path / "connector-config"
    monkeypatch.setenv("WATTRACKER_CONNECTOR_DIR", str(directory))
    root = logging.getLogger()
    before = list(root.handlers)
    level = root.level
    yield directory
    for handler in [h for h in root.handlers if h not in before]:
        root.removeHandler(handler)
        handler.close()
    root.handlers[:] = before
    root.setLevel(level)


def _log_text(directory) -> str:
    path = directory / "connector.log"
    return path.read_text(encoding="utf-8") if path.exists() else ""


# ------------------------------------------------------------- the file
def test_logging_writes_to_a_file_in_the_config_dir(connector_dir):
    _configure_logging(False)
    logging.getLogger("wattracker_connector.test").info("hello from the tray")

    assert "hello from the tray" in _log_text(connector_dir)
    assert connector_config.log_path() == str(connector_dir / "connector.log")


@pytest.mark.skipif(os.name == "nt", reason="POSIX modes are inert on Windows")
def test_the_log_is_owner_only(connector_dir):
    """It records what the connector does with the rider's files."""
    _configure_logging(False)
    logging.getLogger("wattracker_connector.test").info("something")

    mode = stat.S_IMODE(os.stat(connector_config.log_path()).st_mode)
    assert mode == 0o600, oct(mode)


@pytest.mark.skipif(os.name == "nt", reason="POSIX modes are inert on Windows")
def test_a_rotated_log_is_owner_only_too(connector_dir, monkeypatch):
    """Restricting only the first file would protect the wrong one by morning."""
    monkeypatch.setattr("wattracker_connector.__main__._LOG_MAX_BYTES", 256)
    _configure_logging(False)
    log = logging.getLogger("wattracker_connector.test")
    for index in range(40):
        log.info("a line long enough to push this over the rotation size %d", index)

    rotated = connector_dir / "connector.log.1"
    assert rotated.exists(), sorted(p.name for p in connector_dir.iterdir())
    mode = stat.S_IMODE(os.stat(rotated).st_mode)
    assert mode == 0o600, oct(mode)


# ------------------------------------------------------ the missing stderr
def test_a_windowed_process_with_no_stderr_still_logs(connector_dir, monkeypatch):
    """The whole reason this module exists.

    A frozen ``console=False`` build has ``sys.stderr is None``. Adding a
    StreamHandler for it would blow up on the first log line, on the machine
    furthest from a developer.
    """
    monkeypatch.setattr(sys, "stderr", None)
    _configure_logging(False)

    # Only our own handlers are inspected: pytest installs several of its own
    # on the root logger, and they are not what this is about.
    assert not any(
        isinstance(h, _ConnectorStreamHandler)
        for h in logging.getLogger().handlers
    )
    logging.getLogger("wattracker_connector.test").info("still recorded")
    assert "still recorded" in _log_text(connector_dir)


def test_a_console_run_still_gets_its_stream_handler(connector_dir):
    """The pip-installed console script must behave exactly as it did."""
    _configure_logging(False)

    assert any(
        isinstance(h, _ConnectorStreamHandler)
        for h in logging.getLogger().handlers
    )


def test_configuring_twice_does_not_double_every_line(connector_dir):
    _configure_logging(False)
    _configure_logging(True)
    logging.getLogger("wattracker_connector.test").info("once please")

    assert _log_text(connector_dir).count("once please") == 1


def test_an_unwritable_config_dir_costs_the_log_and_nothing_else(
    connector_dir, monkeypatch, capsys
):
    """Losing the log must never be what stops a rider connecting."""
    monkeypatch.setattr(
        "wattracker_connector.__main__.log_path",
        lambda: str(connector_dir / "no-such-subdir" / "connector.log"),
    )
    _configure_logging(False)
    logging.getLogger("wattracker_connector.test").info("carry on")

    assert "could not open the connector log file" in capsys.readouterr().err


# ---------------------------------------------------------- the credential
def test_the_token_never_reaches_the_log(connector_dir, capsys):
    """The property the PR #41 review confirmed, as an assertion.

    ``--show-config`` is the one command whose entire job is to show the saved
    settings, so it is where a token is most likely to be printed or logged by
    someone being helpful.
    """
    connector_config.save(
        {"server": "http://192.168.1.10:8000", "token": TOKEN,
         "activities_dir": None, "workouts_dir": None}
    )

    assert main(["--show-config"]) == 0

    captured = capsys.readouterr()
    assert TOKEN not in captured.out
    assert TOKEN not in captured.err
    assert "(set)" in captured.out
    assert TOKEN not in _log_text(connector_dir)


def test_a_token_passed_on_the_command_line_is_not_logged(connector_dir, capsys):
    """--save is the one path that is handed the plaintext directly."""
    assert main(
        ["--server", "http://192.168.1.10:8000", "--token", TOKEN, "--save",
         "--show-config"]
    ) == 0

    assert TOKEN not in _log_text(connector_dir)
    assert TOKEN not in capsys.readouterr().out
