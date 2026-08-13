"""Command-line entry point for the connector.

Headless on purpose at this stage: the tray app (WP-8) wraps this same
``Connector`` rather than reimplementing it, so whatever is proven here is
what ships behind the icon.

    wattracker-connector --server http://192.168.1.10:8000 --token ... --save
    wattracker-connector                # reuses the saved settings
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import logging.handlers
import sys
from typing import List, Optional

from wattracker.config import _restrict

from .client import Connector, ConnectorStatus
from .config import config_path, load, log_path, save
from .handlers import ConnectorConfig

log = logging.getLogger(__name__)

# Small enough that a rider can send one, large enough to hold the reconnect
# history that makes a flaky link diagnosable.
_LOG_MAX_BYTES = 512 * 1024
_LOG_BACKUPS = 2


class _ConnectorHandler:
    """Marks the handlers this module installed, so a second call replaces them."""


class _OwnerOnlyRotatingFileHandler(
    _ConnectorHandler, logging.handlers.RotatingFileHandler
):
    """A rotating handler whose files are owner-only, rotations included.

    Restricting once after construction would protect today's file and none of
    the ones rotation creates later, so the lockdown belongs on the open path
    rather than beside the call that first opens it.
    """

    def _open(self):
        stream = super()._open()
        _restrict(self.baseFilename, 0o600, is_dir=False)
        return stream


class _ConnectorStreamHandler(_ConnectorHandler, logging.StreamHandler):
    """A plain stderr handler, tagged so it can be replaced rather than stacked."""


def _configure_logging(verbose: bool) -> None:
    """Log to a file always, and to stderr only when there is one.

    The frozen tray build is windowed: it has no stderr at all, and on Windows
    writing to that closed handle raises rather than being discarded. So the
    file is the primary destination and the stream handler is the conditional
    one - which also leaves the pip-installed console script behaving exactly
    as it did.

    Calling this twice replaces its own handlers rather than stacking a second
    copy of each, so a second call cannot start double-logging every line.
    """
    root = logging.getLogger()
    for existing in [h for h in root.handlers if isinstance(h, _ConnectorHandler)]:
        root.removeHandler(existing)
        existing.close()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    try:
        handler = _OwnerOnlyRotatingFileHandler(
            log_path(), maxBytes=_LOG_MAX_BYTES, backupCount=_LOG_BACKUPS,
            encoding="utf-8",
        )
    except OSError:
        # An unwritable config dir must not stop the connector running; it
        # only costs the log. Reported through whatever stderr exists.
        handler = None
        print("could not open the connector log file", file=sys.stderr)
    if handler is not None:
        handler.setFormatter(formatter)
        root.addHandler(handler)
    if sys.stderr is not None:
        stream = _ConnectorStreamHandler()
        stream.setFormatter(formatter)
        root.addHandler(stream)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wattracker-connector",
        description=(
            "Give a wattracker server access to this machine's Zwift folders "
            "and Bluetooth trainer."
        ),
    )
    parser.add_argument("--server", help="Server base URL, e.g. http://192.168.1.10:8000")
    parser.add_argument(
        "--token",
        help=(
            "Device token from the server's Settings page. Pass it once with "
            "--save and omit it afterwards: an argument is visible to every "
            "process on this machine (ps / Task Manager) and lands in shell "
            "history, whereas the saved config file is written 0600"
        ),
    )
    parser.add_argument("--activities-dir", help="Override the Zwift Activities folder")
    parser.add_argument("--workouts-dir", help="Override the Zwift Workouts folder")
    parser.add_argument(
        "--save", action="store_true",
        help="Write these settings to the config file and use them from now on",
    )
    parser.add_argument(
        "--show-config", action="store_true",
        help="Print the saved settings (token redacted) and exit",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = _parser().parse_args(argv)
    _configure_logging(args.verbose)

    stored = load()
    if args.show_config:
        from .config import describe

        print(f"config file: {config_path()}")
        for key, value in sorted(describe(stored).items()):
            print(f"  {key}: {value}")
        return 0

    settings = {
        "server": args.server or stored.get("server"),
        "token": args.token or stored.get("token"),
        "activities_dir": args.activities_dir or stored.get("activities_dir"),
        "workouts_dir": args.workouts_dir or stored.get("workouts_dir"),
    }
    missing = [k for k in ("server", "token") if not settings[k]]
    if missing:
        # Named explicitly rather than "invalid configuration": the first-run
        # experience is someone pasting a token, and they should be told which
        # half they left out.
        print(
            f"Missing: {', '.join(missing)}. Pair a device on the server's "
            "Settings page, then run:\n"
            "  wattracker-connector --server http://SERVER:8000 --token TOKEN --save",
            file=sys.stderr,
        )
        return 2

    if args.save:
        save(settings)
        print(f"Saved to {config_path()}")

    connector = Connector(
        server_url=settings["server"],
        token=settings["token"],
        config=ConnectorConfig(
            activities_dir=settings["activities_dir"],
            workouts_dir=settings["workouts_dir"],
        ),
        status=ConnectorStatus(),
    )
    try:
        asyncio.run(connector.run_forever())
    except KeyboardInterrupt:
        log.info("stopping")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
