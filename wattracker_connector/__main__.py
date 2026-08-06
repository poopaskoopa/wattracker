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
import sys
from typing import List, Optional

from .client import Connector, ConnectorStatus
from .config import config_path, load, save
from .handlers import ConnectorConfig

log = logging.getLogger(__name__)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wattracker-connector",
        description=(
            "Give a wattracker server access to this machine's Zwift folders "
            "and Bluetooth trainer."
        ),
    )
    parser.add_argument("--server", help="Server base URL, e.g. http://192.168.1.10:8000")
    parser.add_argument("--token", help="Device token from the server's Settings page")
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
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

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
