"""Persisted all-time mean-maximal-power curves.

The dashboard needs all-time peaks, but computing them must not unpack every
historical stream on every request. This module owns the small per-user cache;
the trailing-window curve remains in the analysis pipeline.
"""
from __future__ import annotations

import json
import math
import sqlite3
from typing import Dict, List, Optional

from .. import db
from .curve import MMP_DURATIONS, mean_maximal_power


def _power(activity: dict) -> List[float]:
    streams = activity.get("streams")
    power = streams.get("power") if isinstance(streams, dict) else None
    if not isinstance(power, list):
        return []
    # Historical imports can contain values too large for NumPy's float dtype.
    # Preserve sample positions, but make unusable samples inert like missing
    # power rather than letting a cache maintenance path make inserts fail.
    cleaned = []
    for value in power:
        try:
            number = float(value or 0.0)
        except (TypeError, ValueError, OverflowError):
            number = 0.0
        cleaned.append(number if math.isfinite(number) else 0.0)
    return cleaned


def _decode(value: str) -> Dict[int, float]:
    try:
        data = json.loads(value)
    except (TypeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    result = {}
    for duration, power in data.items():
        try:
            duration, power = int(duration), float(power)
        except (TypeError, ValueError):
            continue
        if duration in MMP_DURATIONS and power > 0:
            result[duration] = power
    return result


def _encode(mmp: Dict[int, float]) -> str:
    return json.dumps({str(t): p for t, p in sorted(mmp.items())}, separators=(",", ":"))


def _rebuild_locked(conn: sqlite3.Connection, user_id: int) -> Dict[int, float]:
    # Fold one effective ride at a time. The first legacy bootstrap may still
    # be CPU-heavy, but it never retains the history's inflated streams (or a
    # second copy made by mean_maximal_power) in memory.
    mmp: Dict[int, float] = {}
    rows = conn.execute(
        "SELECT id, streams FROM activities "
        "WHERE user_id = ? AND duplicate_of IS NULL",
        (user_id,),
    )
    for row in rows:
        effective = db._effective_streams(
            row["streams"],
            db._activity_correction_ranges(conn, user_id, int(row["id"])),
        )
        power = _power({"streams": effective})
        if not power:
            continue
        activity_mmp = mean_maximal_power([power], MMP_DURATIONS)
        for duration, watts in activity_mmp.items():
            mmp[duration] = max(mmp.get(duration, 0.0), watts)
    conn.execute(
        "INSERT INTO curve_cache (user_id, mmp_json, dirty) VALUES (?, ?, 0) "
        "ON CONFLICT(user_id) DO UPDATE SET mmp_json = excluded.mmp_json, dirty = 0",
        (user_id, _encode(mmp)),
    )
    return mmp


def all_time(user_id: int, path: Optional[str] = None) -> Dict[int, float]:
    """Return the persisted all-time curve, rebuilding legacy/dirty rows once."""
    conn = db.connect(path)
    try:
        row = conn.execute(
            "SELECT mmp_json, dirty FROM curve_cache WHERE user_id = ?", (user_id,)
        ).fetchone()
        if row is not None and not row["dirty"]:
            return _decode(row["mmp_json"])
        # A write transaction prevents a simultaneous insert/correction from
        # being missed between the scan and cache write.
        conn.execute("BEGIN IMMEDIATE")
        return _rebuild_locked(conn, user_id)
    except Exception:
        conn.rollback()
        raise
    finally:
        if conn.in_transaction:
            conn.commit()
        conn.close()


def ensure(user_id: int, path: Optional[str] = None) -> Dict[int, float]:
    """Eagerly establish a usable cache after a write path or batch."""
    return all_time(user_id, path=path)


def _activity_mmp(record: dict) -> Dict[int, float]:
    power = _power(record)
    if not power:
        return {}
    activity_mmp = mean_maximal_power([power], MMP_DURATIONS)
    if not activity_mmp:
        return {}
    return activity_mmp


def merge_activity_in_transaction(
    conn: sqlite3.Connection, user_id: int, record: dict
) -> None:
    """Merge one activity while its insertion transaction is still open."""
    activity_mmp = _activity_mmp(record)
    if not activity_mmp:
        return
    row = conn.execute(
        "SELECT mmp_json, dirty FROM curve_cache WHERE user_id = ?", (user_id,)
    ).fetchone()
    if row is None or row["dirty"]:
        return
    merged = _decode(row["mmp_json"])
    for duration, watts in activity_mmp.items():
        merged[duration] = max(merged.get(duration, 0.0), watts)
    conn.execute(
        "UPDATE curve_cache SET mmp_json = ? WHERE user_id = ?",
        (_encode(merged), user_id),
    )


def merge_activity(user_id: int, record: dict, path: Optional[str] = None) -> None:
    """Merge one just-inserted non-duplicate activity into an already-clean cache."""
    conn = db.connect(path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        merge_activity_in_transaction(conn, user_id, record)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def last_ride(user_id: int, path: Optional[str] = None) -> Dict[int, float]:
    """MMP for the newest effective ride that has positive power, newest first."""
    for activity in db.iter_full_activities_desc(user_id, path=path):
        power = _power(activity)
        if power:
            mmp = mean_maximal_power([power], MMP_DURATIONS)
            if mmp:
                return mmp
    return {}
