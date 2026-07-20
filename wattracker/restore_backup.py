"""Offline restore CLI: replace the live DB with one of its backups.

Usage:
    python -m wattracker.restore_backup                # list backups + usage
    python -m wattracker.restore_backup --restore N    # restore backup number N
    python -m wattracker.restore_backup N              # same, positional

Why offline only
----------------
SQLite in WAL mode keeps committed data across ``wattracker.db`` and its
``-wal`` sidecar. Swapping the main file out from under a running server leaves
the old -wal pointing at the wrong database and corrupts it. So this tool
REFUSES to run when a server appears to be up: it checks for a
``python -m wattracker`` process and for something accepting TCP on port 8000.

Safety
------
Before overwriting, it takes a ``pre-restore`` backup of the *current* DB (so a
mistaken restore is itself reversible), copies the chosen backup over
``wattracker.db``, and DELETES any stale ``-wal`` / ``-shm`` sidecars (they
belong to the replaced database and would otherwise re-apply its data on top of
the restored file).

Schema note: restoring an OLDER-schema backup is fine -- ``init_db`` migrates it
forward on the next server start (and takes its own pre-migration backup first).
Restoring a NEWER-schema backup against older code makes ``init_db`` refuse to
touch it (the future-schema guard); update the code before starting the server.
"""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
from typing import Callable, List, Optional

from . import backup
from .config import db_path, server_host, server_port

# A callable returning (running: bool, why: str). Injectable so tests can force
# either verdict without a real server or process table.
ServerCheck = Callable[[], "tuple[bool, str]"]


def _server_running(port: Optional[int] = None) -> "tuple[bool, str]":
    """Best-effort detection of a live wattracker server.

    True if either a ``python -m wattracker`` process is found (pgrep) or
    something accepts a TCP connection on ``port``.
    """
    port = server_port() if port is None else port
    try:
        res = subprocess.run(
            ["pgrep", "-f", "--", "-m wattracker"],
            capture_output=True,
            text=True,
        )
        # pgrep exits 0 when at least one match is found. Exclude our own PID so
        # this very process (also "-m wattracker.restore_backup") never counts.
        pids = [
            p for p in res.stdout.split() if p.strip() and int(p) != os.getpid()
        ]
        if res.returncode == 0 and pids:
            return True, f"a wattracker process is running (pid {pids[0]})"
    except (OSError, ValueError):
        pass  # pgrep missing (non-unix) -> fall back to the port probe
    try:
        with socket.create_connection((server_host(), port), timeout=0.5):
            return True, f"something is listening on port {port}"
    except OSError:
        pass
    return False, ""


def _fmt_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    for unit, div in (("KB", 1024), ("MB", 1024**2), ("GB", 1024**3)):
        if n < div * 1024 or unit == "GB":
            return f"{n/div:.1f} {unit}"
    return f"{n} B"


def _print_list(backups: List[dict]) -> None:
    if not backups:
        print("No backups found.")
        return
    print("Available backups (newest first):")
    for i, b in enumerate(backups, start=1):
        ts = b["timestamp"].strftime("%Y-%m-%d %H:%M:%S")
        print(f"  [{i}] {ts}  {b['reason']:<13} {_fmt_size(b['size'])}")


def _usage() -> None:
    prog = "python -m wattracker.restore_backup"
    print(
        f"\nUsage:\n"
        f"  {prog}                list backups\n"
        f"  {prog} --restore N    restore backup number N\n"
        f"  {prog} N              restore backup number N (positional)\n",
        file=sys.stderr,
    )


def restore(index: int, server_check: Optional[ServerCheck] = None) -> int:
    """Restore the 1-based ``index`` backup over the live DB. Returns exit code."""
    check = server_check or _server_running
    running, why = check()
    if running:
        print(
            f"Refusing to restore: {why}.\n"
            "Stop the wattracker server first, then re-run this command.",
            file=sys.stderr,
        )
        return 1

    backups = backup.list_backups()
    if not backups:
        print("No backups found; nothing to restore.", file=sys.stderr)
        return 1
    if index < 1 or index > len(backups):
        print(
            f"Error: backup number {index} is out of range (1-{len(backups)}).",
            file=sys.stderr,
        )
        return 2

    chosen = backups[index - 1]
    live = db_path()

    # Snapshot the current DB before clobbering it, so this restore is itself
    # reversible. If the live DB does not exist yet there is nothing to snapshot.
    if os.path.exists(live):
        try:
            pre = backup.create_backup("pre-restore", src_path=live)
            print(f"Saved current database to {os.path.basename(pre)} first.")
        except Exception as e:
            print(f"Refusing to restore: pre-restore backup failed: {e}",
                  file=sys.stderr)
            return 1

    shutil.copyfile(chosen["path"], live)
    # The -wal/-shm sidecars belong to the DB we just replaced; leaving them
    # would let SQLite re-apply the old data over the restored file. Remove them.
    removed = []
    for suffix in ("-wal", "-shm"):
        side = live + suffix
        if os.path.exists(side):
            try:
                os.remove(side)
                removed.append(os.path.basename(side))
            except OSError:
                pass

    print(f"Restored {chosen['name']} -> {os.path.basename(live)}.")
    if removed:
        print(f"Removed stale sidecar file(s): {', '.join(removed)}.")
    print(
        "On next server start the schema will be migrated forward if the backup "
        "is older. If it is NEWER than this code, init_db will refuse to start "
        "until you update wattracker."
    )
    return 0


def main(argv: "Optional[List[str]]" = None,
         server_check: Optional[ServerCheck] = None) -> int:
    args = sys.argv[1:] if argv is None else argv

    if not args:
        _print_list(backup.list_backups())
        _usage()
        return 0
    if args[0] in ("-h", "--help"):
        _usage()
        return 0

    if args[0] == "--restore":
        if len(args) != 2:
            print("Error: --restore needs a backup number.", file=sys.stderr)
            _usage()
            return 2
        num_s = args[1]
    elif len(args) == 1:
        num_s = args[0]
    else:
        _usage()
        return 2

    try:
        index = int(num_s)
    except ValueError:
        print(f"Error: {num_s!r} is not a number.", file=sys.stderr)
        _usage()
        return 2

    return restore(index, server_check=server_check)


if __name__ == "__main__":
    sys.exit(main())
