"""First-class backup of the live SQLite database.

Two real incidents wiped the live DB (stale long-lived server processes re-running
``init_db`` against a future-schema database). Recovery was only possible because
ad-hoc manual copies happened to exist. This module makes copies happen
automatically (before every migration, and once a day) and keeps a bounded
history so restores are always available.

Backups live in ``<data_dir>/backups/`` and are named
``wattracker-YYYYMMDD-HHMMSS-<reason>.db``. Copies are taken with
``sqlite3.Connection.backup()`` -- an online, WAL-safe snapshot that captures
committed data still sitting in the -wal file (a plain file copy would miss it).

Restore is deliberately NOT here (see ``wattracker.restore_backup``): restoring
under a running server corrupts the DB, so it is an offline CLI only.
"""
from __future__ import annotations

import datetime as _dt
import os
import re
import sqlite3
from typing import Dict, List, Optional

from .config import app_data_dir, db_path

# Recognised backup reasons and how many of each to keep. "daily" and
# "pre-migration" are the safety-critical automatic classes, so they retain a
# deeper history; the user-triggered "manual" and the "pre-restore" snapshots
# taken by the restore CLI keep fewer.
RETENTION: Dict[str, int] = {
    "daily": 10,
    "manual": 5,
    "pre-migration": 10,
    "pre-restore": 5,
}

_FILENAME_RE = re.compile(
    r"^wattracker-(\d{8})-(\d{6})-(daily|manual|pre-migration|pre-restore)\.db$"
)
_TS_FMT = "%Y%m%d-%H%M%S"


def backups_dir() -> str:
    """The directory holding backup files, created on demand."""
    d = os.path.join(app_data_dir(), "backups")
    os.makedirs(d, exist_ok=True)
    return d


def _parse(name: str) -> "Optional[tuple[_dt.datetime, str]]":
    m = _FILENAME_RE.match(name)
    if not m:
        return None
    date_s, time_s, reason = m.groups()
    try:
        ts = _dt.datetime.strptime(f"{date_s}-{time_s}", _TS_FMT)
    except ValueError:
        return None
    return ts, reason


def create_backup(reason: str, src_path: Optional[str] = None) -> str:
    """Take an online snapshot of the live DB, returning the backup file path.

    ``reason`` must be one of ``RETENTION``'s keys. The copy is made with
    SQLite's backup API so WAL-resident committed data is included. ``prune`` is
    run afterward to enforce the per-reason retention caps.
    """
    if reason not in RETENTION:
        raise ValueError(
            f"unknown backup reason {reason!r}; expected one of "
            f"{sorted(RETENTION)}"
        )
    src_path = src_path or db_path()
    when = _dt.datetime.now()
    # A same-second collision (e.g. two backups within one second) would
    # otherwise overwrite; bump the timestamp forward a second until the name is
    # free so every backup stays a distinct, pattern-parseable file.
    while True:
        dest = os.path.join(
            backups_dir(), f"wattracker-{when.strftime(_TS_FMT)}-{reason}.db"
        )
        if not os.path.exists(dest):
            break
        when += _dt.timedelta(seconds=1)
    src = sqlite3.connect(src_path)
    try:
        dst = sqlite3.connect(dest)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    prune()
    return dest


def list_backups() -> List[dict]:
    """All recognised backups, newest first.

    Each entry: ``{"path", "name", "timestamp" (datetime), "reason", "size"}``.
    Files that do not match the naming pattern are ignored.
    """
    d = backups_dir()
    out: List[dict] = []
    for name in os.listdir(d):
        parsed = _parse(name)
        if parsed is None:
            continue
        ts, reason = parsed
        path = os.path.join(d, name)
        try:
            size = os.path.getsize(path)
        except OSError:
            continue
        out.append(
            {
                "path": path,
                "name": name,
                "timestamp": ts,
                "reason": reason,
                "size": size,
            }
        )
    out.sort(key=lambda b: (b["timestamp"], b["name"]), reverse=True)
    return out


def prune() -> List[str]:
    """Enforce per-reason retention caps, deleting the oldest excess backups.

    Returns the list of deleted paths.
    """
    by_reason: Dict[str, List[dict]] = {}
    for b in list_backups():  # already newest-first
        by_reason.setdefault(b["reason"], []).append(b)
    deleted: List[str] = []
    for reason, items in by_reason.items():
        keep = RETENTION.get(reason, 0)
        for b in items[keep:]:
            try:
                os.remove(b["path"])
                deleted.append(b["path"])
            except OSError:
                pass
    return deleted


def newest_backup(reason: str) -> Optional[dict]:
    """The most recent backup of a given reason, or None."""
    for b in list_backups():
        if b["reason"] == reason:
            return b
    return None


def create_daily_if_due(
    now: Optional[_dt.datetime] = None,
    max_age_hours: float = 23.0,
) -> Optional[str]:
    """Create a "daily" backup unless a recent one already exists.

    Returns the new backup path, or None if a daily backup younger than
    ``max_age_hours`` is already present. Called from the daily maintenance
    sweep, which may fire more than once a day.
    """
    now = now or _dt.datetime.now()
    latest = newest_backup("daily")
    if latest is not None:
        age = now - latest["timestamp"]
        if age < _dt.timedelta(hours=max_age_hours):
            return None
    return create_backup("daily")
