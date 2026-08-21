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
    _ConnectorHandler,
    _ConnectorStreamHandler,
    _SecretRedactingFilter,
    _configure_logging,
    forget_secrets,
    main,
    redact_secret,
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
    forget_secrets()
    yield directory
    for handler in [h for h in root.handlers if h not in before]:
        root.removeHandler(handler)
        handler.close()
    root.handlers[:] = before
    root.setLevel(level)
    # A secret registered by one test must not make the next one pass for free.
    forget_secrets()


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


# ------------------------------------------------------------- -v
# The PR #93 review's third finding. --show-config and --save were pinned above
# and -v was not, so this file read as covering the credential when the one
# switch that actually writes it went untested.
#
# The exact line, from a real run with -v:
#   DEBUG websockets.client: > Authorization: Bearer FnI9DQ...
# websockets/client.py logs `"> %s: %s"` once per request header during the
# handshake, and this PR gave that output a persistent home (512 KiB x 3).
# "Send me your connector.log" then becomes "send me your device token", and
# with the escalation this branch adds, that is account takeover.
_HANDSHAKE_HEADER = "> %s: %s"


def _verbose_websockets_handshake(token=TOKEN):
    """Replay the handshake logging websockets does, at its own logger."""
    log = logging.getLogger("websockets.client")
    log.debug("> GET %s HTTP/1.1", "/ws/connector")
    log.debug(_HANDSHAKE_HEADER, "Host", "192.168.1.10:8000")
    log.debug(_HANDSHAKE_HEADER, "Authorization", f"Bearer {token}")


def test_verbose_does_not_write_the_bearer_token_to_the_log(connector_dir):
    _configure_logging(True)
    _verbose_websockets_handshake()

    text = _log_text(connector_dir)
    assert TOKEN not in text
    assert "Bearer [REDACTED]" in text


def test_verbose_does_not_write_the_bearer_token_to_stderr_either(
    connector_dir, capsys
):
    """The console script logs to both, and both outlive the moment."""
    _configure_logging(True)
    _verbose_websockets_handshake()

    err = capsys.readouterr().err
    assert TOKEN not in err
    assert "Bearer [REDACTED]" in err


def test_verbose_still_records_the_handshake_it_is_for(connector_dir):
    """Redaction must not be a level cut in disguise.

    -v exists so a rider with a flaky link can send a log that shows the
    handshake failing. Everything except the credential still has to be in it,
    including the other headers - a wrong Host is a real diagnosis.
    """
    _configure_logging(True)
    _verbose_websockets_handshake()

    text = _log_text(connector_dir)
    assert "> GET /ws/connector HTTP/1.1" in text
    assert "> Host: 192.168.1.10:8000" in text
    assert "> Authorization: " in text, "the header's presence is diagnostic"


def test_a_run_without_verbose_is_unchanged(connector_dir):
    """DEBUG stays off without -v; this fix must not turn it on."""
    _configure_logging(False)
    _verbose_websockets_handshake()

    assert "/ws/connector" not in _log_text(connector_dir)


def test_a_registered_token_is_redacted_in_any_shape(connector_dir):
    """Belt and braces: the scheme prefix is not the only way one gets out.

    The Bearer pattern only catches a credential that arrives spelled the way
    the header spells it. Anything that logs the configured settings, or a
    repr of the headers dict, prints the bare value - so the connector also
    registers its own token as a literal to scrub.
    """
    redact_secret(TOKEN)
    _configure_logging(True)

    logging.getLogger("wattracker_connector.test").debug(
        "connecting with %s", {"token": TOKEN}
    )
    text = _log_text(connector_dir)
    assert TOKEN not in text
    assert "[REDACTED]" in text


def test_registering_a_short_value_is_refused(connector_dir):
    """A one-character "secret" would redact the whole log into uselessness."""
    redact_secret("abc")
    _configure_logging(True)

    logging.getLogger("wattracker_connector.test").info("abcdefg is fine")
    assert "abcdefg is fine" in _log_text(connector_dir)


def test_the_filter_never_breaks_a_log_call(connector_dir):
    """Logging that raises is worse than logging that leaks."""
    class Explodes:
        def __str__(self):
            raise RuntimeError("no")

    redact_secret(TOKEN)
    record = logging.LogRecord(
        "x", logging.DEBUG, __file__, 0, "%s", (Explodes(),), None
    )
    assert _SecretRedactingFilter().filter(record) is True


def test_a_verbose_run_installs_the_redaction_on_every_handler(connector_dir):
    """Handler-level, not logger-level, and that distinction is the whole fix.

    A filter on the connector's root *logger* would never see this record:
    logger filters run only for records logged through that logger, and
    ``websockets.client`` is a different logger that merely propagates to the
    same handlers. Attaching to the handlers is what puts the filter in the
    path of every library the connector pulls in.
    """
    _configure_logging(True)

    ours = [
        h for h in logging.getLogger().handlers
        if isinstance(h, _ConnectorHandler)
    ]
    assert ours, "this run should have installed at least the file handler"
    for handler in ours:
        assert any(
            isinstance(f, _SecretRedactingFilter) for f in handler.filters
        ), handler
