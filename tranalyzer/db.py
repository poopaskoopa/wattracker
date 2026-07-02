"""SQLite storage (stdlib only) with per-user isolation.

Everything a user owns - activities, streams, ftp_history, settings - is scoped
by ``user_id``. Schema changes bump ``SCHEMA_VERSION``; a version mismatch
triggers a clean drop/recreate (safe for dev - there is no production data).
"""
from __future__ import annotations

import datetime as _dt
import json
import sqlite3
import zlib
from typing import Dict, List, Optional

from .config import db_path

SCHEMA_VERSION = 3

_DROP = """
DROP TABLE IF EXISTS plan_workouts;
DROP TABLE IF EXISTS plans;
DROP TABLE IF EXISTS activities;
DROP TABLE IF EXISTS ftp_history;
DROP TABLE IF EXISTS user_settings;
DROP TABLE IF EXISTS users;
"""

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    created       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS user_settings (
    user_id        INTEGER PRIMARY KEY,
    ftp            REAL,
    zwift_id       TEXT,
    activities_dir TEXT,
    workouts_dir   TEXT,
    FOREIGN KEY(user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS activities (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    dedup_hash  TEXT NOT NULL,
    filename    TEXT,
    start_time  TEXT,
    duration_s  INTEGER,
    distance_m  REAL,
    avg_power   REAL,
    avg_hr      REAL,
    np          REAL,
    if_         REAL,
    tss         REAL,
    streams     BLOB,
    UNIQUE(user_id, dedup_hash),
    FOREIGN KEY(user_id) REFERENCES users(id)
);
CREATE INDEX IF NOT EXISTS idx_activities_user_start
    ON activities(user_id, start_time);

CREATE TABLE IF NOT EXISTS ftp_history (
    user_id    INTEGER NOT NULL,
    date       TEXT NOT NULL,
    ftp_watts  REAL NOT NULL,
    source     TEXT NOT NULL DEFAULT 'estimated',
    PRIMARY KEY(user_id, date),
    FOREIGN KEY(user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS plans (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    name       TEXT NOT NULL,
    start_date TEXT NOT NULL,
    weeks      INTEGER NOT NULL,
    created    TEXT NOT NULL,
    FOREIGN KEY(user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS plan_workouts (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id           INTEGER NOT NULL,
    user_id           INTEGER NOT NULL,
    date              TEXT NOT NULL,
    name              TEXT NOT NULL,
    type              TEXT NOT NULL,
    duration_s        INTEGER NOT NULL,
    tss               REAL NOT NULL,
    zwo_or_segments   TEXT NOT NULL,
    FOREIGN KEY(plan_id) REFERENCES plans(id),
    FOREIGN KEY(user_id) REFERENCES users(id)
);
CREATE INDEX IF NOT EXISTS idx_plan_workouts_user_date
    ON plan_workouts(user_id, date);
"""


def connect(path: Optional[str] = None) -> sqlite3.Connection:
    conn = sqlite3.connect(path or db_path())
    conn.row_factory = sqlite3.Row
    return conn


def init_db(path: Optional[str] = None) -> None:
    """Create the schema, recreating cleanly if the schema version changed."""
    conn = connect(path)
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version != SCHEMA_VERSION:
            conn.executescript(_DROP)
            conn.executescript(_SCHEMA)
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        else:
            conn.executescript(_SCHEMA)  # idempotent CREATE IF NOT EXISTS
        conn.commit()
    finally:
        conn.close()


# ----------------------------------------------------------------- users
def create_user(username: str, password_hash: str, path: Optional[str] = None) -> Optional[int]:
    """Create a user. Returns the new id, or None if the username is taken."""
    conn = connect(path)
    try:
        cur = conn.execute(
            "INSERT INTO users (username, password_hash, created) VALUES (?, ?, ?)",
            (username, password_hash, _dt.datetime.now().isoformat()),
        )
        conn.commit()
        return cur.lastrowid
    except sqlite3.IntegrityError:
        return None
    finally:
        conn.close()


def get_user_by_username(username: str, path: Optional[str] = None) -> Optional[dict]:
    conn = connect(path)
    try:
        row = conn.execute(
            "SELECT id, username, password_hash, created FROM users WHERE username = ?",
            (username,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_user_by_id(user_id: int, path: Optional[str] = None) -> Optional[dict]:
    conn = connect(path)
    try:
        row = conn.execute(
            "SELECT id, username, created FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


# -------------------------------------------------------------- settings
_SETTING_KEYS = ("ftp", "zwift_id", "activities_dir", "workouts_dir")


def get_user_settings(user_id: int, path: Optional[str] = None) -> dict:
    conn = connect(path)
    try:
        row = conn.execute(
            "SELECT ftp, zwift_id, activities_dir, workouts_dir "
            "FROM user_settings WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        if not row:
            return {k: None for k in _SETTING_KEYS}
        return {k: row[k] for k in _SETTING_KEYS}
    finally:
        conn.close()


def save_user_settings(user_id: int, updates: dict, path: Optional[str] = None) -> dict:
    """Merge non-empty updates into a user's settings row (upsert)."""
    current = get_user_settings(user_id, path=path)
    for key in _SETTING_KEYS:
        if key in updates and updates[key] not in (None, ""):
            current[key] = updates[key]
    conn = connect(path)
    try:
        conn.execute(
            """
            INSERT INTO user_settings (user_id, ftp, zwift_id, activities_dir, workouts_dir)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                ftp=excluded.ftp,
                zwift_id=excluded.zwift_id,
                activities_dir=excluded.activities_dir,
                workouts_dir=excluded.workouts_dir
            """,
            (
                user_id,
                current["ftp"],
                current["zwift_id"],
                current["activities_dir"],
                current["workouts_dir"],
            ),
        )
        conn.commit()
        return current
    finally:
        conn.close()


# ------------------------------------------------------------ activities
def _pack_streams(streams: Dict[str, list]) -> bytes:
    return zlib.compress(json.dumps(streams).encode("utf-8"))


def _unpack_streams(blob: Optional[bytes]) -> Dict[str, list]:
    if not blob:
        return {}
    try:
        return json.loads(zlib.decompress(blob).decode("utf-8"))
    except Exception:
        return {}


def activity_exists(user_id: int, dedup_hash: str, path: Optional[str] = None) -> bool:
    conn = connect(path)
    try:
        cur = conn.execute(
            "SELECT 1 FROM activities WHERE user_id = ? AND dedup_hash = ?",
            (user_id, dedup_hash),
        )
        return cur.fetchone() is not None
    finally:
        conn.close()


def insert_activity(user_id: int, record: dict, path: Optional[str] = None) -> Optional[int]:
    """Insert an activity for a user. Returns row id, or None if it existed."""
    conn = connect(path)
    try:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO activities
              (user_id, dedup_hash, filename, start_time, duration_s, distance_m,
               avg_power, avg_hr, np, if_, tss, streams)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                record["dedup_hash"],
                record.get("filename"),
                record.get("start_time"),
                record.get("duration_s"),
                record.get("distance_m"),
                record.get("avg_power"),
                record.get("avg_hr"),
                record.get("np"),
                record.get("if_"),
                record.get("tss"),
                _pack_streams(record.get("streams", {})),
            ),
        )
        conn.commit()
        return cur.lastrowid if cur.rowcount else None
    finally:
        conn.close()


def _row_summary(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "filename": row["filename"],
        "start_time": row["start_time"],
        "duration_s": row["duration_s"],
        "distance_m": row["distance_m"],
        "avg_power": row["avg_power"],
        "avg_hr": row["avg_hr"],
        "np": row["np"],
        "if_": row["if_"],
        "tss": row["tss"],
    }


def list_activities(user_id: int, path: Optional[str] = None) -> List[dict]:
    conn = connect(path)
    try:
        rows = conn.execute(
            "SELECT * FROM activities WHERE user_id = ? ORDER BY start_time DESC",
            (user_id,),
        ).fetchall()
        return [_row_summary(r) for r in rows]
    finally:
        conn.close()


def get_activity(user_id: int, activity_id: int, path: Optional[str] = None) -> Optional[dict]:
    conn = connect(path)
    try:
        row = conn.execute(
            "SELECT * FROM activities WHERE user_id = ? AND id = ?",
            (user_id, activity_id),
        ).fetchone()
        if not row:
            return None
        d = _row_summary(row)
        d["streams"] = _unpack_streams(row["streams"])
        return d
    finally:
        conn.close()


def recent_power_streams(user_id: int, days: int = 90, path: Optional[str] = None) -> List[List[float]]:
    cutoff = (_dt.datetime.now() - _dt.timedelta(days=days)).isoformat()
    conn = connect(path)
    try:
        rows = conn.execute(
            "SELECT streams FROM activities "
            "WHERE user_id = ? AND start_time >= ? ORDER BY start_time",
            (user_id, cutoff),
        ).fetchall()
        out: List[List[float]] = []
        for r in rows:
            power = _unpack_streams(r["streams"]).get("power") or []
            if power:
                out.append(power)
        return out
    finally:
        conn.close()


def daily_tss(user_id: int, path: Optional[str] = None) -> Dict[_dt.date, float]:
    conn = connect(path)
    try:
        rows = conn.execute(
            "SELECT start_time, tss FROM activities "
            "WHERE user_id = ? AND start_time IS NOT NULL",
            (user_id,),
        ).fetchall()
        out: Dict[_dt.date, float] = {}
        for r in rows:
            try:
                day = _dt.datetime.fromisoformat(r["start_time"]).date()
            except (ValueError, TypeError):
                continue
            out[day] = out.get(day, 0.0) + float(r["tss"] or 0.0)
        return out
    finally:
        conn.close()


def full_activities(user_id: int, path: Optional[str] = None) -> List[dict]:
    conn = connect(path)
    try:
        rows = conn.execute(
            "SELECT * FROM activities WHERE user_id = ? ORDER BY start_time",
            (user_id,),
        ).fetchall()
        out = []
        for r in rows:
            d = _row_summary(r)
            d["streams"] = _unpack_streams(r["streams"])
            out.append(d)
        return out
    finally:
        conn.close()


# ----------------------------------------------------------- ftp history
def add_ftp_entry(
    user_id: int,
    date: str,
    ftp_watts: float,
    source: str = "estimated",
    path: Optional[str] = None,
) -> None:
    """Append/update an FTP history row for (user, date).

    - source='manual' replaces any existing row for that date.
    - source='estimated' inserts only if no row exists (never overwrites manual).
    """
    conn = connect(path)
    try:
        if source == "manual":
            conn.execute(
                "INSERT OR REPLACE INTO ftp_history (user_id, date, ftp_watts, source) "
                "VALUES (?, ?, ?, ?)",
                (user_id, date, float(ftp_watts), "manual"),
            )
        else:
            conn.execute(
                "INSERT OR IGNORE INTO ftp_history (user_id, date, ftp_watts, source) "
                "VALUES (?, ?, ?, ?)",
                (user_id, date, float(ftp_watts), source),
            )
        conn.commit()
    finally:
        conn.close()


def latest_ftp(user_id: int, path: Optional[str] = None) -> Optional[dict]:
    conn = connect(path)
    try:
        row = conn.execute(
            "SELECT date, ftp_watts, source FROM ftp_history "
            "WHERE user_id = ? ORDER BY date DESC LIMIT 1",
            (user_id,),
        ).fetchone()
        if not row:
            return None
        return {"date": row["date"], "ftp_watts": row["ftp_watts"], "source": row["source"]}
    finally:
        conn.close()


def ftp_history_list(user_id: int, path: Optional[str] = None) -> List[dict]:
    conn = connect(path)
    try:
        rows = conn.execute(
            "SELECT date, ftp_watts, source FROM ftp_history "
            "WHERE user_id = ? ORDER BY date ASC",
            (user_id,),
        ).fetchall()
        return [
            {"date": r["date"], "ftp_watts": r["ftp_watts"], "source": r["source"]}
            for r in rows
        ]
    finally:
        conn.close()


# ---------------------------------------------------------------- plans
def create_plan(
    user_id: int, name: str, start_date: str, weeks: int, path: Optional[str] = None
) -> int:
    conn = connect(path)
    try:
        cur = conn.execute(
            "INSERT INTO plans (user_id, name, start_date, weeks, created) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, name, start_date, int(weeks), _dt.datetime.now().isoformat()),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def add_plan_workout(
    plan_id: int,
    user_id: int,
    date: str,
    name: str,
    type: str,
    duration_s: int,
    tss: float,
    zwo_or_segments: str,
    path: Optional[str] = None,
) -> int:
    conn = connect(path)
    try:
        cur = conn.execute(
            """
            INSERT INTO plan_workouts
              (plan_id, user_id, date, name, type, duration_s, tss, zwo_or_segments)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (plan_id, user_id, date, name, type, int(duration_s), float(tss),
             zwo_or_segments),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def get_plan(user_id: int, plan_id: int, path: Optional[str] = None) -> Optional[dict]:
    conn = connect(path)
    try:
        row = conn.execute(
            "SELECT id, name, start_date, weeks, created FROM plans "
            "WHERE user_id = ? AND id = ?",
            (user_id, plan_id),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_plans(user_id: int, path: Optional[str] = None) -> List[dict]:
    conn = connect(path)
    try:
        rows = conn.execute(
            "SELECT id, name, start_date, weeks, created FROM plans "
            "WHERE user_id = ? ORDER BY created DESC",
            (user_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _plan_workout_row(r: sqlite3.Row, include_zwo: bool = False) -> dict:
    d = {
        "id": r["id"],
        "plan_id": r["plan_id"],
        "date": r["date"],
        "name": r["name"],
        "type": r["type"],
        "duration_s": r["duration_s"],
        "tss": r["tss"],
    }
    if include_zwo:
        d["zwo_or_segments"] = r["zwo_or_segments"]
    return d


def plan_workouts_for_plan(
    user_id: int, plan_id: int, include_zwo: bool = False, path: Optional[str] = None
) -> List[dict]:
    conn = connect(path)
    try:
        rows = conn.execute(
            "SELECT * FROM plan_workouts WHERE user_id = ? AND plan_id = ? "
            "ORDER BY date ASC",
            (user_id, plan_id),
        ).fetchall()
        return [_plan_workout_row(r, include_zwo) for r in rows]
    finally:
        conn.close()


def plan_workouts_for_month(
    user_id: int, year: int, month: int, path: Optional[str] = None
) -> List[dict]:
    prefix = f"{int(year):04d}-{int(month):02d}-"
    conn = connect(path)
    try:
        rows = conn.execute(
            "SELECT * FROM plan_workouts WHERE user_id = ? AND date LIKE ? "
            "ORDER BY date ASC",
            (user_id, prefix + "%"),
        ).fetchall()
        return [_plan_workout_row(r) for r in rows]
    finally:
        conn.close()


def get_plan_workout(
    user_id: int, workout_id: int, path: Optional[str] = None
) -> Optional[dict]:
    conn = connect(path)
    try:
        row = conn.execute(
            "SELECT * FROM plan_workouts WHERE user_id = ? AND id = ?",
            (user_id, workout_id),
        ).fetchone()
        return _plan_workout_row(row, include_zwo=True) if row else None
    finally:
        conn.close()
