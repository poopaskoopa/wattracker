"""Read-only local SQLite snapshots for optional cloud synchronization."""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import math
import os
import sqlite3
import zlib
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any, Iterator, Optional
from urllib.parse import quote

from .. import db
from ..analysis import zones
from ..metrics.curve import MMP_DURATIONS, fit_cp_wprime, mean_maximal_power
from ..metrics.decoupling import aerobic_decoupling
from ..metrics.load import compute_load, daily_tss_series
from ..prescribe import goals
from ..timeutil import parse_naive, to_user_timezone, utc_now
from .models import (
    MAX_BATCH_OBJECTS,
    MAX_PAYLOAD_ARRAY_ITEMS,
    MAX_PAYLOAD_BYTES,
    CloudObject,
    ModelError,
    SyncBatch,
)

DETAIL_MAX_POINTS = 1500
MAX_STREAM_DECODED = 512 * 1024

_DANGEROUS_KEYS = {
    "account_id", "azure_account", "blob_path", "command", "file", "filename",
    "installation_id", "installation", "local_user_id", "local_user_scope",
    "namespace", "partition_key", "path", "sas", "sas_token", "storage_account",
    "storage_url", "table_partition_key", "tenant", "url", "user_id", "user_name",
    "username",
}
_DANGEROUS_NORMALIZED = {
    "".join(ch for ch in key if ch.isalnum()) for key in _DANGEROUS_KEYS
}
_ACTIVITY_FIELDS = (
    "start_time", "duration_s", "distance_m", "avg_power", "avg_hr", "np",
    "if_", "tss", "rpe",
)


def _safe_data(value: Any) -> Any:
    """Copy accessor data while dropping local selectors and non-finite values."""
    if isinstance(value, Mapping):
        out = {}
        for key, child in value.items():
            if not isinstance(key, str):
                continue
            normalized = "".join(ch for ch in key.lower() if ch.isalnum())
            if key.lower() in _DANGEROUS_KEYS or normalized in _DANGEROUS_NORMALIZED:
                continue
            out[key] = _safe_data(child)
        return out
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_safe_data(child) for child in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {
        row["name"]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }


def _query(
    conn: sqlite3.Connection, sql: str, args: tuple = (),
) -> list[sqlite3.Row]:
    try:
        return conn.execute(sql, args).fetchall()
    except sqlite3.Error:
        return []


def _finite(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _settings(conn: sqlite3.Connection, user_id: int) -> dict:
    rows = _query(conn, "SELECT * FROM user_settings WHERE user_id = ?", (user_id,))
    return dict(rows[0]) if rows else {}


def _local_date(start_time: Any, settings: Mapping[str, Any]) -> Optional[str]:
    started = parse_naive(start_time)
    if started is None:
        return None
    return to_user_timezone(started, settings.get("timezone")).date().isoformat()


def _utc_date(start_time: Any) -> Optional[str]:
    started = parse_naive(start_time)
    return started.date().isoformat() if started is not None else None


def _visible_activity(row: sqlite3.Row, settings: Mapping[str, Any]) -> bool:
    cutoff = settings.get("history_start_date")
    if not cutoff:
        return True
    local_date = _local_date(row["start_time"], settings)
    return local_date is not None and local_date >= str(cutoff)


def _decode_streams(
    blob: Any, ranges: Sequence[tuple[int, int]],
    max_decoded: int = MAX_STREAM_DECODED,
) -> Any:
    """Inflate one stream blob under a hard bound and apply active corrections."""
    if blob is None:
        return {}
    try:
        decoder = zlib.decompressobj()
        decoded = decoder.decompress(blob, max_decoded + 1)
        if len(decoded) > max_decoded or decoder.unconsumed_tail:
            return {}
        tail = decoder.flush(max_decoded - len(decoded) + 1)
        decoded += tail
        if (
            len(decoded) > max_decoded
            or not decoder.eof
            or decoder.unused_data
            or decoder.unconsumed_tail
        ):
            return {}
        value = json.loads(
            decoded.decode("utf-8"), parse_constant=_reject_json_constant
        )
        if isinstance(value, dict):
            return db._effective_stream_mapping(value, ranges)
        return value
    except (
        TypeError, ValueError, UnicodeDecodeError, zlib.error, RecursionError,
    ):
        return {}


def _downsample(values: Any, target: int = DETAIL_MAX_POINTS) -> list:
    """Block-average a sequence to at most ``target`` points."""
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes, bytearray)):
        return []
    values = list(values)
    if not values:
        return []
    if len(values) <= target:
        return [
            round(float(value), 1)
            if isinstance(value, (int, float)) and math.isfinite(float(value))
            else None
            for value in values
        ]
    out = []
    size = len(values)
    for index in range(target):
        lo = (index * size) // target
        hi = ((index + 1) * size) // target
        if hi <= lo:
            hi = lo + 1
        bucket = [
            float(value)
            for value in values[lo:hi]
            if isinstance(value, (int, float)) and math.isfinite(float(value))
        ]
        out.append(round(sum(bucket) / len(bucket), 1) if bucket else None)
    return out


def _activity_rows(
    conn: sqlite3.Connection, user_id: int, settings: Mapping[str, Any],
    *, decode: bool, chronological: bool,
) -> list[dict]:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(activities)")}
    duplicate_filter = " AND duplicate_of IS NULL" if "duplicate_of" in columns else ""
    order_by = "start_time ASC, id ASC" if chronological else "id ASC"
    rows = conn.execute(
        "SELECT * FROM activities WHERE user_id = ?"
        f"{duplicate_filter} ORDER BY {order_by}",
        (user_id,),
    ).fetchall()
    ids = [int(row["id"]) for row in rows]
    ranges = {}
    if decode and ids and _correction_schema_available(conn):
        ranges = db._correction_ranges(conn, user_id, ids)
    records = []
    for row in rows:
        if not _visible_activity(row, settings):
            continue
        summary = {
            key: row[key]
            for key in _ACTIVITY_FIELDS
            if key in row.keys() and row[key] is not None
        }
        summary["id"] = int(row["id"])
        streams = (
            _decode_streams(row["streams"], ranges.get(int(row["id"]), []))
            if decode and "streams" in row.keys()
            else {}
        )
        records.append({"row": row, "summary": summary, "streams": streams})
    return records


def _ftp_as_of(
    conn: sqlite3.Connection, user_id: int, date_iso: Optional[str],
) -> Optional[float]:
    if "ftp_history" not in _tables(conn):
        return None
    if date_iso:
        row = conn.execute(
            "SELECT ftp_watts FROM ftp_history WHERE user_id = ? AND date <= ? "
            "ORDER BY date DESC LIMIT 1",
            (user_id, date_iso),
        ).fetchone()
    else:
        row = None
    if row is None:
        row = conn.execute(
            "SELECT ftp_watts FROM ftp_history WHERE user_id = ? ORDER BY date ASC LIMIT 1"
            if date_iso else
            "SELECT ftp_watts FROM ftp_history WHERE user_id = ? ORDER BY date DESC LIMIT 1",
            (user_id,),
        ).fetchone()
    return _finite(row["ftp_watts"]) if row else None


def _ftp_state(
    conn: sqlite3.Connection, user_id: int, settings: Mapping[str, Any],
    profile: Mapping[str, Any],
) -> dict:
    value = _finite(settings.get("ftp"))
    source = "Manual Training FTP setting" if value and value > 0 else None
    if not value or value <= 0:
        value = _ftp_as_of(conn, user_id, None)
        source = "Training FTP history" if value and value > 0 else None
    if not value or value <= 0:
        value = _finite(profile.get("ftp"))
        source = "Stored rider profile" if value and value > 0 else None
    if not value or value <= 0:
        value = None
        source = "No personalized FTP available"
    return {
        "available": value is not None,
        "value": value,
        "source": source,
    }


def _hr_state(
    settings: Mapping[str, Any], profile: Mapping[str, Any],
) -> dict:
    value = _finite(settings.get("hr_max"))
    source = "Manual HRmax" if value and 80 <= value <= 230 else None
    if not source:
        value = _finite(profile.get("hr_max"))
        source = "Stored rider profile" if value and 80 <= value <= 230 else None
    if not source:
        value = None
        source = "Insufficient FIT heart-rate data"
    return {
        "available": value is not None,
        "value": int(value) if value is not None else None,
        "source": source,
    }


def _weight_resolution(
    conn: sqlite3.Connection, user_id: int, date_iso: Optional[str],
) -> Optional[dict]:
    row = None
    if "weight_history" in _tables(conn):
        if date_iso:
            row = conn.execute(
                "SELECT date, weight_kg, source FROM weight_history "
                "WHERE user_id = ? AND date <= ? ORDER BY date DESC LIMIT 1",
                (user_id, date_iso),
            ).fetchone()
        if row is None:
            row = conn.execute(
                "SELECT date, weight_kg, source FROM weight_history "
                "WHERE user_id = ? ORDER BY date ASC LIMIT 1"
                if date_iso else
                "SELECT date, weight_kg, source FROM weight_history "
                "WHERE user_id = ? ORDER BY date DESC LIMIT 1",
                (user_id,),
            ).fetchone()
    value = _finite(row["weight_kg"]) if row else None
    if value is not None and value > 0:
        return {
            "date": row["date"], "weight_kg": value, "source": row["source"],
        }
    settings = _query(conn, "SELECT weight_kg FROM user_settings WHERE user_id = ?", (user_id,))
    value = _finite(settings[0]["weight_kg"]) if settings else None
    if value is not None and value > 0:
        return {"date": date_iso, "weight_kg": value, "source": "settings"}
    return None


def _profile_row(conn: sqlite3.Connection, user_id: int) -> dict:
    rows = _query(conn, "SELECT * FROM rider_profile WHERE user_id = ?", (user_id,))
    return dict(rows[0]) if rows else {}


def _profile_object(
    conn: sqlite3.Connection, user_id: int, settings: Mapping[str, Any],
    profile: Mapping[str, Any],
) -> CloudObject:
    users = _query(conn, "SELECT username FROM users WHERE id = ?", (user_id,))
    display_name = users[0]["username"] if users else None
    ftp = _ftp_state(conn, user_id, settings, profile)
    hr = _hr_state(settings, profile)
    if ftp["available"]:
        power_zones = zones.zone_ranges(ftp["value"], zones.POWER_ZONES)
    else:
        power_zones = []
    if hr["available"]:
        hr_zones = zones.zone_ranges(hr["value"], zones.HR_ZONES)
    else:
        hr_zones = []
    weight = _weight_resolution(conn, user_id, None)
    return CloudObject(
        object_id="profile",
        kind="profile",
        revision=1,
        data=_safe_data({
            "display_name": display_name,
            "ftp": ftp["value"],
            "power": {**ftp, "zones": power_zones},
            "heart_rate": {**hr, "zones": hr_zones},
            "zones": {"power": power_zones, "heart_rate": hr_zones},
            "weight_kg": weight["weight_kg"] if weight else None,
            "weight_date": weight["date"] if weight else None,
            "weight_source": weight["source"] if weight else None,
        }),
    )


def _ftp_history_objects(
    conn: sqlite3.Connection, user_id: int,
) -> list[CloudObject]:
    if "ftp_history" not in _tables(conn):
        return []
    objects = []
    for row in conn.execute(
        "SELECT date, ftp_watts, source FROM ftp_history "
        "WHERE user_id = ? ORDER BY date ASC",
        (user_id,),
    ):
        objects.append(
            CloudObject(
                object_id=f"ftp-history-{row['date']}",
                kind="ftp_history",
                revision=1,
                data=_safe_data({
                    "date": row["date"],
                    "ftp_watts": _finite(row["ftp_watts"]),
                    "source": row["source"],
                }),
            )
        )
    return objects


def _load_points(
    records: Sequence[Mapping[str, Any]], settings: Mapping[str, Any],
) -> list[dict]:
    daily: dict[_dt.date, float] = {}
    for record in records:
        day = _utc_date(record["row"]["start_time"])
        if day is None:
            continue
        parsed = _dt.date.fromisoformat(day)
        daily[parsed] = daily.get(parsed, 0.0) + float(
            record["row"]["tss"] or 0.0
        )
    cutoff = settings.get("history_start_date")
    if cutoff:
        try:
            cutoff_day = _dt.date.fromisoformat(str(cutoff))
        except (TypeError, ValueError):
            pass
        else:
            daily.setdefault(cutoff_day, 0.0)
    return compute_load(daily_tss_series(daily))


def _merge_mmp(target: dict[int, float], power: Any) -> None:
    if not isinstance(power, Sequence) or isinstance(power, (str, bytes, bytearray)):
        return
    clean = []
    for value in power:
        number = _finite(value)
        clean.append(number if number is not None else 0.0)
    if not clean:
        return
    for duration, watts in mean_maximal_power([clean], MMP_DURATIONS).items():
        target[duration] = max(target.get(duration, 0.0), float(watts))


def _points(mmp: Mapping[int, float]) -> list[dict]:
    return [
        {"t": int(duration), "power": round(float(power), 1)}
        for duration, power in sorted(mmp.items())
    ]


def _curve_data(
    records: Sequence[Mapping[str, Any]],
) -> tuple[dict, Optional[float], Optional[float]]:
    all_time: dict[int, float] = {}
    measured: dict[int, float] = {}
    last_ride: dict[int, float] = {}
    latest_when: Optional[_dt.datetime] = None
    latest_power: Any = None
    recent_since = utc_now() - _dt.timedelta(days=90)
    for record in records:
        streams = record.get("streams")
        power = streams.get("power") if isinstance(streams, Mapping) else None
        _merge_mmp(all_time, power)
        when = parse_naive(record["row"]["start_time"])
        if when is not None and when >= recent_since:
            _merge_mmp(measured, power)
        if (
            when is not None
            and isinstance(power, Sequence)
            and not isinstance(power, (str, bytes, bytearray))
            and power
            and (latest_when is None or when > latest_when)
        ):
            latest_when = when
            latest_power = power
    _merge_mmp(last_ride, latest_power)
    try:
        cp, wprime = fit_cp_wprime(measured)
    except ValueError:
        cp = wprime = None
    model = []
    if cp is not None and wprime is not None:
        durations = sorted(set(MMP_DURATIONS) | set(measured))
        model = [
            {"t": int(duration), "power": round(cp + wprime / duration, 1)}
            for duration in durations if duration > 0
        ]
    return (
        {
            "measured": _points(measured),
            "all_time": _points(all_time),
            "last_ride": _points(last_ride),
            "model": model,
            "cp": cp,
            "wprime": wprime,
        },
        cp,
        wprime,
    )


def _training_state(
    records: Sequence[Mapping[str, Any]], settings: Mapping[str, Any],
    profile: Mapping[str, Any], ftp: Mapping[str, Any],
    curve: Mapping[str, Any],
) -> CloudObject:
    load = _load_points(records, settings)
    latest = load[-1] if load else {}
    cp = _finite(curve.get("cp"))
    wprime = _finite(curve.get("wprime"))
    if cp is None:
        cp = _finite(profile.get("cp"))
    if wprime is None:
        wprime = _finite(profile.get("wprime"))
    decoupling = None
    for record in reversed(records):
        streams = record.get("streams")
        if not isinstance(streams, Mapping):
            continue
        try:
            decoupling = aerobic_decoupling(
                streams.get("power") or [], streams.get("heartrate") or []
            )
        except (TypeError, ValueError, OverflowError):
            decoupling = None
        if decoupling is not None:
            break
    return CloudObject(
        object_id="training-state",
        kind="training_state",
        revision=1,
        data=_safe_data({
            "ftp": ftp.get("value"),
            "cp": cp,
            "wprime": wprime,
            "ctl": latest.get("ctl", 0.0),
            "atl": latest.get("atl", 0.0),
            "tsb": latest.get("tsb", 0.0),
            "decoupling": decoupling,
        }),
    )


def _volume_objects(
    records: Sequence[Mapping[str, Any]], settings: Mapping[str, Any],
) -> list[CloudObject]:
    totals: dict[str, list[float]] = {}
    for record in records:
        row = record["row"]
        day = _utc_date(row["start_time"])
        if day is None:
            continue
        parsed = _dt.date.fromisoformat(day)
        monday = parsed - _dt.timedelta(days=parsed.weekday())
        values = totals.setdefault(monday.isoformat(), [0.0] * 4)
        values[0] += float(row["duration_s"] or 0.0) / 3600.0
        values[1] += float(row["tss"] or 0.0)
        values[2] += float(row["distance_m"] or 0.0) / 1000.0
        if row["avg_power"] is not None:
            values[3] += float(row["avg_power"]) * float(
                row["duration_s"] or 0.0
            ) / 1000.0
    return [
        CloudObject(
            object_id=f"volume-week-{week}",
            kind="volume_week",
            revision=1,
            data={
                "week_start": week,
                "hours": round(values[0], 2),
                "tss": round(values[1], 1),
                "distance_km": round(values[2], 1),
                "calories": round(values[3]),
            },
        )
        for week, values in sorted(totals.items())
    ]


def _row_payload(row: sqlite3.Row) -> dict:
    """Use the DB's normalized workout row when the full schema is present."""
    try:
        return db._plan_workout_row(row)
    except (AttributeError, KeyError, IndexError):
        return {
            key: row[key]
            for key in row.keys()
            if key not in {"user_id", "zwo_or_segments", "zwo"}
        }


def _standalone_payload(row: sqlite3.Row) -> dict:
    try:
        return db._standalone_row(row)
    except (AttributeError, KeyError, IndexError):
        return {
            key: row[key]
            for key in row.keys()
            if key not in {"user_id", "export_key", "zwo"}
        }


def _calendar_day_objects(
    day: str,
    workouts: Sequence[Mapping[str, Any]],
    activities: Sequence[Mapping[str, Any]],
    race: Optional[Mapping[str, Any]],
    in_ooto: bool,
    phase: Optional[str],
) -> list[CloudObject]:
    """Build one day, splitting unusually high-cardinality days safely."""
    base = {
        "date": day,
        "race": race,
        "ooto": in_ooto,
        "phase": phase,
    }
    chunks: list[tuple[list[dict], list[dict]]] = []
    current_workouts: list[dict] = []
    current_activities: list[dict] = []

    def _json_size(value: Any) -> int:
        return len(json.dumps(
            value, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode("utf-8"))

    # Size is additive because the two variable fields are JSON arrays. Keep a
    # small reserve for the part metadata added when a day is split.
    current_size = _json_size({
        **base, "workouts": [], "activities": [],
    })

    for key, item in (
        [("workouts", value) for value in workouts]
        + [("activities", value) for value in activities]
    ):
        item = _safe_data(dict(item))
        target = current_workouts if key == "workouts" else current_activities
        increment = _json_size(item) + (1 if target else 0)
        if (
            (current_workouts or current_activities)
            and (
                current_size + increment > MAX_PAYLOAD_BYTES - 128
                or len(target) >= MAX_PAYLOAD_ARRAY_ITEMS
            )
        ):
            chunks.append((current_workouts, current_activities))
            current_workouts = []
            current_activities = []
            current_size = _json_size({
                **base, "workouts": [], "activities": [],
            })
            target = current_workouts if key == "workouts" else current_activities
            increment = _json_size(item)
        target.append(item)
        current_size += increment

    if current_workouts or current_activities or not chunks:
        chunks.append((current_workouts, current_activities))

    total = len(chunks)
    objects = []
    for index, (day_workouts, day_activities) in enumerate(chunks, start=1):
        data = {
            **base,
            "workouts": day_workouts,
            "activities": day_activities,
        }
        if total > 1:
            data["part"] = index
            data["parts"] = total
        object_id = (
            f"calendar-day-{day}"
            if total == 1
            else f"calendar-day-{day}-part-{index}"
        )
        objects.append(
            CloudObject(
                object_id=object_id,
                kind="calendar_day",
                revision=1,
                data=_safe_data(data),
            )
        )
    return objects


def _calendar_objects(
    conn: sqlite3.Connection,
    user_id: int,
    records: Sequence[Mapping[str, Any]],
    settings: Mapping[str, Any],
) -> list[CloudObject]:
    workouts_by_date: dict[str, list[dict]] = {}
    activities_by_date: dict[str, list[dict]] = {}
    dates: set[str] = set()
    linked_activity_ids: set[int] = set()
    tables = _tables(conn)
    ooto: list[dict] = []
    if "ooto_ranges" in tables:
        ooto = [
            _safe_data(dict(row))
            for row in conn.execute(
                "SELECT id, start_date, end_date, note FROM ooto_ranges "
                "WHERE user_id = ? ORDER BY start_date, id",
                (user_id,),
            )
        ]
        for item in ooto:
            start, end = item.get("start_date"), item.get("end_date")
            try:
                current = _dt.date.fromisoformat(start)
                last = _dt.date.fromisoformat(end)
            except (TypeError, ValueError):
                continue
            if last < current:
                current, last = last, current
            span = min((last - current).days, 3660)
            for day_offset in range(span + 1):
                dates.add((current + _dt.timedelta(days=day_offset)).isoformat())

    def _in_ooto(day: str) -> bool:
        return any(
            item.get("start_date", "") <= day <= item.get("end_date", "")
            for item in ooto
        )

    today = utc_now().date().isoformat()
    if "plan_workouts" in tables:
        linked_activity_ids.update(
            int(row["completed_activity_id"])
            for row in conn.execute(
                "SELECT completed_activity_id FROM plan_workouts "
                "WHERE user_id = ? AND completed_activity_id IS NOT NULL",
                (user_id,),
            )
        )
    if "standalone_workouts" in tables:
        linked_activity_ids.update(
            int(row["completed_activity_id"])
            for row in conn.execute(
                "SELECT completed_activity_id FROM standalone_workouts "
                "WHERE user_id = ? AND completed_activity_id IS NOT NULL",
                (user_id,),
            )
        )
    for record in records:
        if int(record["summary"]["id"]) in linked_activity_ids:
            continue
        day = _utc_date(record["row"]["start_time"])
        if day is None:
            continue
        dates.add(day)
        activity = dict(record["summary"])
        activity["activity"] = True
        activities_by_date.setdefault(day, []).append(_safe_data(activity))

    if "plan_workouts" in tables:
        for row in conn.execute(
            "SELECT * FROM plan_workouts WHERE user_id = ? ORDER BY date, id",
            (user_id,),
        ):
            day = row["date"]
            if day:
                day = str(day)[:10]
                dates.add(day)
                item = _row_payload(row)
                completed = row["completed_activity_id"] is not None
                skipped = _in_ooto(day) and not completed
                item.update({
                    "adjustment_cancelled": row["adjustment_state"] in {
                        "ooto_canceled", "displaced",
                    } if "adjustment_state" in row.keys() else False,
                    "adjustment_replacement": row["adjustment_state"] in {
                        "rescheduled", "rebalanced",
                    } if "adjustment_state" in row.keys() else False,
                    "skipped": skipped,
                    "missed": day < today and not completed and not skipped,
                })
                workouts_by_date.setdefault(day, []).append(
                    _safe_data(item)
                )
    if "standalone_workouts" in tables:
        for row in conn.execute(
            "SELECT * FROM standalone_workouts WHERE user_id = ? "
            "ORDER BY scheduled_date, id",
            (user_id,),
        ):
            day = row["scheduled_date"]
            if day:
                day = str(day)[:10]
                dates.add(day)
                item = _standalone_payload(row)
                completed = row["completed_activity_id"] is not None
                item.update({
                    "date": day,
                    "standalone": True,
                    "adapted": None,
                    "skipped": False,
                    "missed": day < today and not completed,
                })
                workouts_by_date.setdefault(day, []).append(_safe_data(item))

    races: dict[str, dict] = {}
    if "race_dates" in tables:
        for row in conn.execute(
            "SELECT id, date, priority, name, duration_min FROM race_dates "
            "WHERE user_id = ? ORDER BY date, id",
            (user_id,),
        ):
            day = row["date"]
            if day:
                day = str(day)[:10]
                dates.add(day)
                races.setdefault(day, _safe_data(dict(row)))

    phase_by_date: dict[str, str] = {}
    if "plans" in tables:
        plan = conn.execute(
            "SELECT * FROM plans WHERE user_id = ? AND active = 1 "
            "ORDER BY created DESC LIMIT 1",
            (user_id,),
        ).fetchone()
        if plan is not None:
            plan_data = db._plan_row(plan)
            goal_key = (plan_data.get("recipe") or {}).get("goal")
            phase_by_date = goals.phase_by_date(
                plan_data.get("start_date"), plan_data.get("weeks"), goal_key
            )
            dates.update(phase_by_date)

    objects = []
    for day in sorted(dates):
        objects.extend(
            _calendar_day_objects(
                day,
                workouts_by_date.get(day, []),
                activities_by_date.get(day, []),
                races.get(day),
                _in_ooto(day),
                phase_by_date.get(day),
            )
        )
    return objects


def _activity_ftp(
    conn: sqlite3.Connection,
    user_id: int,
    summary: Mapping[str, Any],
    settings: Mapping[str, Any],
) -> dict:
    # FTP history follows the stored activity date, matching the activity
    # detail and zone accessors; weight history is the separate local-date
    # resolution below.
    date_iso = _utc_date(summary.get("start_time"))
    value = _ftp_as_of(conn, user_id, date_iso)
    source = f"Training FTP as of {date_iso}" if value else None
    if not value:
        np_value = _finite(summary.get("np"))
        intensity = _finite(summary.get("if_"))
        if np_value and intensity and np_value > 0 and intensity > 0:
            value = np_value / intensity
            source = "Recovered from activity NP / IF"
    if not value:
        profile = _profile_row(conn, user_id)
        value = _ftp_state(conn, user_id, settings, profile).get("value")
        source = "Current training FTP" if value else None
    return {"available": bool(value), "value": value, "source": source}


def _zone_summary(
    conn: sqlite3.Connection,
    user_id: int,
    summary: Mapping[str, Any],
    streams: Mapping[str, Any],
    settings: Mapping[str, Any],
    ftp: Mapping[str, Any],
    hr: Mapping[str, Any],
) -> dict:
    power = zones.time_in_zones(
        streams.get("power") or [], streams.get("time") or [],
        ftp.get("value"), zones.POWER_ZONES, "power",
    )
    heart_rate = zones.time_in_zones(
        streams.get("heartrate") or [], streams.get("time") or [],
        hr.get("value"), zones.HR_ZONES, "heart-rate",
    )
    power["anchor"] = ftp.get("value")
    power["source"] = ftp.get("source")
    heart_rate["anchor"] = hr.get("value")
    heart_rate["source"] = hr.get("source")
    return {"power": power, "heart_rate": heart_rate}


def _activity_detail(
    conn: sqlite3.Connection,
    user_id: int,
    record: Mapping[str, Any],
    settings: Mapping[str, Any],
    ftp: Mapping[str, Any],
    hr: Mapping[str, Any],
) -> tuple[CloudObject, Optional[CloudObject]]:
    summary = record["summary"]
    streams = record.get("streams")
    if not isinstance(streams, Mapping):
        streams = {}
    activity_ftp = _activity_ftp(conn, user_id, summary, settings)
    weight = _weight_resolution(
        conn, user_id, _local_date(summary.get("start_time"), settings)
    )
    detail = {
        "id": summary.get("id"),
        **{key: summary.get(key) for key in _ACTIVITY_FIELDS if key in summary},
        "weight_kg": weight["weight_kg"] if weight else None,
        "weight_source": weight["source"] if weight else None,
        "weight_date": weight["date"] if weight else None,
        "zones": _zone_summary(
            conn, user_id, summary, streams, settings, activity_ftp, hr
        ),
    }
    detail_object = CloudObject(
        object_id=f"activity-detail-{int(summary['id'])}",
        kind="activity_detail",
        revision=max(1, int(summary["id"])),
        data=_safe_data(detail),
    )
    stream_object = None
    if streams:
        stream_data = {
            key: _downsample(streams.get(key) or [])
            for key in ("time", "power", "heartrate", "cadence", "altitude")
            if isinstance(streams.get(key), Sequence)
            and not isinstance(streams.get(key), (str, bytes, bytearray))
        }
        if stream_data:
            stream_object = CloudObject(
                object_id=f"stream-{int(summary['id'])}",
                kind="stream",
                revision=max(1, int(summary["id"])),
                data=_safe_data({"streams": stream_data}),
            )
    return detail_object, stream_object


def _derived_objects(
    conn: sqlite3.Connection,
    user_id: int,
    records: Sequence[Mapping[str, Any]],
    settings: Mapping[str, Any],
) -> list[CloudObject]:
    profile = _profile_row(conn, user_id)
    ftp = _ftp_state(conn, user_id, settings, profile)
    hr = _hr_state(settings, profile)
    load = _load_points(records, settings)
    curve, _cp, _wprime = _curve_data(records)
    objects = [_profile_object(conn, user_id, settings, profile)]
    objects.append(_training_state(records, settings, profile, ftp, curve))
    objects.extend(_ftp_history_objects(conn, user_id))
    objects.extend(
        CloudObject(
            object_id=f"load-point-{point['date']}",
            kind="load_point",
            revision=1,
            data=_safe_data(point),
        )
        for point in load
    )
    objects.append(
        CloudObject(
            object_id="curve",
            kind="curve",
            revision=1,
            data=_safe_data(curve),
        )
    )
    objects.extend(_volume_objects(records, settings))
    objects.extend(_calendar_objects(conn, user_id, records, settings))
    return objects


class SnapshotError(RuntimeError):
    """The local database could not be opened as a read-only snapshot."""


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant: {value}")


def _correction_schema_available(conn: sqlite3.Connection) -> bool:
    required = {"activity_id", "user_id", "start_index", "end_index", "undone_at"}
    columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(power_sample_corrections)")
    }
    return required <= columns


@contextmanager
def readonly_connection(path: str | os.PathLike[str]) -> Iterator[sqlite3.Connection]:
    """Open the live DB read-only on a separate SQLite connection.

    The cloud path never shares the request connection or changes local
    schema/data.  SQLite's URI mode also prevents accidental database creation
    when a configured path is wrong.
    """
    resolved = Path(path).expanduser().resolve()
    if not resolved.is_file():
        raise SnapshotError("local database does not exist")
    uri = f"file:{quote(str(resolved), safe='/')}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
    except sqlite3.Error as exc:
        raise SnapshotError("could not open local database read-only") from exc
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def snapshot_counts(path: str | os.PathLike[str], user_id: int) -> dict[str, int]:
    """Return bounded integrity counts without exposing local identifiers."""
    if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id < 1:
        raise ValueError("user_id must be positive")
    with readonly_connection(path) as conn:
        result: dict[str, int] = {}
        for table in ("activities", "streams", "race_results", "plan_workouts"):
            try:
                row = conn.execute(
                    f"SELECT COUNT(*) AS n FROM {table} WHERE user_id = ?",  # table is constant
                    (user_id,),
                ).fetchone()
            except sqlite3.Error:
                result[table] = 0
            else:
                result[table] = int(row["n"] if row else 0)
        return result


def snapshot_objects(
    path: str | os.PathLike[str],
    user_id: int,
    *,
    limit: int = MAX_BATCH_OBJECTS,
    include_streams: bool = False,
    include_derived: bool = True,
    offset: int = 0,
) -> list[CloudObject]:
    """Read a bounded, user-scoped object snapshot on a separate connection.

    ``offset`` is an object offset into one deterministic flattened snapshot,
    not a SQL row offset. This lets a caller page through derived objects and
    activity/detail/stream families without ever returning more than ``limit``
    objects. ``include_derived=False`` retains the legacy activity-only view.
    """
    if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id < 1:
        raise ValueError("user_id must be positive")
    if limit < 1 or limit > MAX_BATCH_OBJECTS:
        raise ValueError("limit is out of bounds")
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ValueError("offset is out of bounds")
    with readonly_connection(path) as conn:
        tables = _tables(conn)
        derived_enabled = bool(
            include_derived and {"users", "user_settings", "rider_profile"} & tables
        )
        records = _activity_rows(
            conn, user_id, _settings(conn, user_id),
            decode=(include_streams or derived_enabled),
            chronological=derived_enabled,
        )
        derived_objects: list[CloudObject] = []
        if derived_enabled:
            settings = _settings(conn, user_id)
            derived_objects = _derived_objects(conn, user_id, records, settings)
        else:
            settings = {}
        profile = _profile_row(conn, user_id) if derived_enabled else {}
        ftp = (
            _ftp_state(conn, user_id, settings, profile)
            if derived_enabled else {}
        )
        hr = _hr_state(settings, profile) if derived_enabled else {}
        activity_objects: list[CloudObject] = []
        detail_objects: list[CloudObject] = []
        publish_records = reversed(records) if derived_enabled else records
        for record in publish_records:
            data = dict(record["summary"])
            streams = record.get("streams")
            if include_streams and not derived_enabled and streams:
                data["streams"] = streams
            safe_data = _safe_data(data)
            try:
                activity_object = CloudObject(
                    object_id=f"activity-{int(record['row']['id'])}",
                    kind="activity",
                    revision=max(1, int(record["row"]["id"])),
                    data=safe_data,
                )
            except ModelError:
                if "streams" not in safe_data:
                    raise
                safe_data.pop("streams")
                activity_object = CloudObject(
                    object_id=f"activity-{int(record['row']['id'])}",
                    kind="activity",
                    revision=max(1, int(record["row"]["id"])),
                    data=safe_data,
                )
            activity_objects.append(activity_object)
            if derived_enabled:
                detail, stream = _activity_detail(
                    conn, user_id, record, settings, ftp, hr
                )
                detail_objects.append(detail)
                if include_streams and stream is not None:
                    detail_objects.append(stream)
        objects = activity_objects + detail_objects + derived_objects
        return objects[offset : offset + limit]


def snapshot_digest(objects: list[CloudObject]) -> str:
    """Return a stable integrity hash for a read-only snapshot."""
    material = json.dumps(
        [obj.wire() for obj in sorted(objects, key=lambda item: item.object_id)],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def snapshot_batch(
    path: str | os.PathLike[str],
    user_id: int,
    *,
    batch_id: str,
    revision: int,
    limit: int = MAX_BATCH_OBJECTS,
    include_streams: bool = False,
    include_derived: bool = True,
    offset: int = 0,
) -> Optional[SyncBatch]:
    """Build one bounded sync batch without mutating the local database."""
    objects = snapshot_objects(
        path, user_id, limit=limit, include_streams=include_streams,
        include_derived=include_derived, offset=offset,
    )
    if not objects:
        return None
    if revision < 1 or revision > (1 << 63) - 1:
        raise ValueError("revision is out of bounds")
    versioned = tuple(replace(obj, revision=revision) for obj in objects)
    return SyncBatch(
        batch_id=batch_id,
        revision=revision,
        objects=versioned,
    )


# ---------------------------------------------------------------------------
# The ``profile`` object kind
# ---------------------------------------------------------------------------
# One object, one field: the rider's current FTP.  It exists so the walking
# skeleton (#171) has something real to carry end to end, and it is
# deliberately the smallest thing that can be.  Issue #154 owns the full
# object model, and a richer ``profile`` invented here would collide with it.
#
# Nothing else about the rider goes in.  Weight, heart-rate maxima, zones and
# provenance are all things #154 gets to shape; a number the phone renders is
# not.
PROFILE_OBJECT_ID = "profile"
PROFILE_KIND = "profile"


def _finite_positive(value: object) -> Optional[float]:
    """``value`` as a usable wattage, or ``None``.

    A NaN or an infinity would be refused by the wire model later, after the
    caller had already built a batch around it; a non-positive number is not
    an FTP.  Both are filtered here, at the read.
    """

    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if number != number or number in (float("inf"), float("-inf")) or number <= 0:
        return None
    return number


def _current_ftp_watts(conn: sqlite3.Connection, user_id: int) -> Optional[float]:
    """The FTP the desktop would show, read on a read-only connection.

    Precedence mirrors :func:`wattracker.ingest.importer.current_ftp` for its
    first two steps -- the rider's manual override, then the newest
    ``ftp_history`` row -- and deliberately stops there.  The third step is a
    live detraining-decayed *estimate*, which needs ``init_db()`` and a write
    connection, and an estimate is not a fact about the rider worth publishing
    to another device as though it were one.  A rider with neither a stated
    nor a recorded FTP publishes no profile at all, rather than a default that
    would arrive on the phone looking like a measurement.
    """

    try:
        row = conn.execute(
            "SELECT ftp FROM user_settings WHERE user_id = ?", (user_id,)
        ).fetchone()
    except sqlite3.Error:
        row = None
    if row is not None and row["ftp"] is not None:
        override = _finite_positive(row["ftp"])
        if override is not None:
            return override
    try:
        row = conn.execute(
            "SELECT ftp_watts FROM ftp_history WHERE user_id = ? "
            "ORDER BY date DESC LIMIT 1",
            (user_id,),
        ).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    return _finite_positive(row["ftp_watts"])


def profile_object(
    path: str | os.PathLike[str], user_id: int, *, revision: int = 1
) -> Optional[CloudObject]:
    """The rider's ``profile`` object, or ``None`` when there is no FTP.

    ``revision`` is the publisher's counter, not a database row id: the
    profile is a single mutable object, so there is no local row whose
    identity could supply a version.  Callers publishing it in a batch pass
    the batch revision, which is monotonic by construction.
    """

    if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id < 1:
        raise ValueError("user_id must be positive")
    with readonly_connection(path) as conn:
        ftp_watts = _current_ftp_watts(conn, user_id)
    if ftp_watts is None:
        return None
    return CloudObject(
        object_id=PROFILE_OBJECT_ID,
        kind=PROFILE_KIND,
        revision=revision,
        data={"ftp_watts": ftp_watts},
    )


def profile_batch(
    path: str | os.PathLike[str],
    user_id: int,
    *,
    batch_id: str,
    revision: int,
) -> Optional[SyncBatch]:
    """One batch carrying only the profile object.

    ``None`` when there is nothing to publish: a batch with no objects is
    invalid by model, so the absence is returned rather than an empty batch.
    """

    obj = profile_object(path, user_id, revision=revision)
    if obj is None:
        return None
    return SyncBatch(batch_id=batch_id, revision=revision, objects=(obj,))
