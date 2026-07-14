"""SQLite storage (stdlib only) with per-user isolation.

Everything a user owns - activities, streams, ftp_history, settings - is scoped
by ``user_id``. Schema changes bump ``SCHEMA_VERSION``. Versions with an entry
in ``_MIGRATIONS`` are upgraded in place (no data loss); anything without a
migration chain falls back to a clean drop/recreate.
"""
from __future__ import annotations

import datetime as _dt
import json
import sqlite3
import zlib
from typing import Dict, List, Optional

from .config import db_path

SCHEMA_VERSION = 12

# In-place migrations: version N -> N+1 statement lists. A database whose
# version has an unbroken chain here is upgraded without losing live data.
# (Brand-new tables need no ALTERs - init_db runs _SCHEMA after migrating - but
# each version still needs an entry, even an empty list, to keep the chain.)
_MIGRATIONS: Dict[int, List[str]] = {
    3: [
        "ALTER TABLE plan_workouts ADD COLUMN completed_activity_id INTEGER",
        "ALTER TABLE plan_workouts ADD COLUMN completed_date TEXT",
    ],
    4: [
        "ALTER TABLE plan_workouts ADD COLUMN adapted TEXT",
        "ALTER TABLE plan_workouts ADD COLUMN adapted_at TEXT",
    ],
    5: [
        "ALTER TABLE user_settings ADD COLUMN zwift_email TEXT",
        "ALTER TABLE user_settings ADD COLUMN zwift_password_enc TEXT",
        "ALTER TABLE race_sync ADD COLUMN auth_failed INTEGER NOT NULL DEFAULT 0",
    ],
    6: [
        "ALTER TABLE user_settings ADD COLUMN weight_kg REAL",
    ],
    7: [
        "ALTER TABLE race_results ADD COLUMN source_type TEXT",
    ],
    8: [
        # New table ooto_ranges is created by _SCHEMA after migrating.
    ],
    9: [
        "ALTER TABLE race_results ADD COLUMN distance_km REAL",
    ],
    10: [
        "ALTER TABLE plan_workouts ADD COLUMN rpe INTEGER",
        "ALTER TABLE plans ADD COLUMN model TEXT",
    ],
    11: [
        # New table scanned_files is created by _SCHEMA after migrating.
    ],
}

_DROP = """
DROP TABLE IF EXISTS scanned_files;
DROP TABLE IF EXISTS ooto_ranges;
DROP TABLE IF EXISTS race_sync;
DROP TABLE IF EXISTS race_results;
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
    user_id            INTEGER PRIMARY KEY,
    ftp                REAL,
    zwift_id           TEXT,
    activities_dir     TEXT,
    workouts_dir       TEXT,
    zwift_email        TEXT,
    zwift_password_enc TEXT,
    weight_kg          REAL,
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
    model      TEXT,
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
    completed_activity_id INTEGER,
    completed_date    TEXT,
    adapted           TEXT,
    adapted_at        TEXT,
    rpe               INTEGER,
    FOREIGN KEY(plan_id) REFERENCES plans(id),
    FOREIGN KEY(user_id) REFERENCES users(id)
);
CREATE INDEX IF NOT EXISTS idx_plan_workouts_user_date
    ON plan_workouts(user_id, date);

CREATE TABLE IF NOT EXISTS race_results (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL,
    source       TEXT NOT NULL,
    event_date   TEXT NOT NULL,
    event_title  TEXT NOT NULL,
    position     TEXT,
    category     TEXT,
    activity_id  INTEGER,
    duration_s   INTEGER,
    avg_power    REAL,
    np           REAL,
    if_          REAL,
    power_json   TEXT,
    source_type  TEXT,
    distance_km  REAL,
    fetched_at   TEXT NOT NULL,
    UNIQUE(user_id, source, event_date, event_title),
    FOREIGN KEY(user_id) REFERENCES users(id)
);
CREATE INDEX IF NOT EXISTS idx_race_results_user_date
    ON race_results(user_id, event_date);

CREATE TABLE IF NOT EXISTS race_sync (
    user_id      INTEGER PRIMARY KEY,
    rider_id     TEXT,
    last_refresh TEXT,
    source       TEXT,
    error        TEXT,
    bests_json   TEXT,
    auth_failed  INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY(user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS ooto_ranges (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    start_date TEXT NOT NULL,
    end_date   TEXT NOT NULL,
    note       TEXT,
    created    TEXT NOT NULL,
    FOREIGN KEY(user_id) REFERENCES users(id)
);
CREATE INDEX IF NOT EXISTS idx_ooto_user
    ON ooto_ranges(user_id, start_date);

CREATE TABLE IF NOT EXISTS scanned_files (
    user_id    INTEGER NOT NULL,
    path       TEXT NOT NULL,
    mtime      REAL,
    size       INTEGER,
    UNIQUE(user_id, path),
    FOREIGN KEY(user_id) REFERENCES users(id)
);
"""


def connect(path: Optional[str] = None) -> sqlite3.Connection:
    conn = sqlite3.connect(path or db_path(), timeout=10)
    conn.row_factory = sqlite3.Row
    # WAL lets the background scan thread's writes proceed without blocking
    # concurrent reads (dashboard/pipeline) on the same DB file. journal_mode is
    # persisted in the DB header, but set it on every connect so fresh temp/test
    # DBs get it too. busy_timeout backs the connect timeout for in-flight locks;
    # synchronous=NORMAL is the recommended durability pairing with WAL.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _can_migrate(version: int) -> bool:
    """True when an unbroken migration chain leads from `version` to current."""
    v = version
    while v < SCHEMA_VERSION:
        if v not in _MIGRATIONS:
            return False
        v += 1
    return v == SCHEMA_VERSION


def init_db(path: Optional[str] = None) -> None:
    """Create/upgrade the schema.

    - Current version: idempotent CREATE IF NOT EXISTS.
    - Older version with a migration chain: upgraded in place (data preserved).
    - Anything else (fresh db, unknown/newer version): clean drop/recreate.
    """
    conn = connect(path)
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version == SCHEMA_VERSION:
            conn.executescript(_SCHEMA)  # idempotent CREATE IF NOT EXISTS
        elif 0 < version < SCHEMA_VERSION and _can_migrate(version):
            for v in range(version, SCHEMA_VERSION):
                for stmt in _MIGRATIONS[v]:
                    try:
                        conn.execute(stmt)
                    except sqlite3.OperationalError as e:
                        # A later migration may target a table the older
                        # database never had (_SCHEMA below creates it in its
                        # final shape) or a column that already exists.
                        msg = str(e).lower()
                        if "no such table" in msg or "duplicate column" in msg:
                            continue
                        raise
            conn.executescript(_SCHEMA)  # new tables/indexes, if any
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        else:
            conn.executescript(_DROP)
            conn.executescript(_SCHEMA)
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
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


def set_password_hash(
    username: str, password_hash: str, path: Optional[str] = None
) -> bool:
    """Overwrite a user's stored password hash. Returns False if no such user."""
    conn = connect(path)
    try:
        cur = conn.execute(
            "UPDATE users SET password_hash = ? WHERE username = ?",
            (password_hash, username),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def list_usernames(path: Optional[str] = None) -> List[str]:
    """All usernames, alphabetically. No hashes or other columns exposed."""
    conn = connect(path)
    try:
        rows = conn.execute(
            "SELECT username FROM users ORDER BY username ASC"
        ).fetchall()
        return [r["username"] for r in rows]
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
_SETTING_KEYS = ("ftp", "zwift_id", "activities_dir", "workouts_dir",
                 "zwift_email", "weight_kg")


def get_user_settings(user_id: int, path: Optional[str] = None) -> dict:
    conn = connect(path)
    try:
        row = conn.execute(
            "SELECT ftp, zwift_id, activities_dir, workouts_dir, zwift_email, "
            "weight_kg FROM user_settings WHERE user_id = ?",
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
            INSERT INTO user_settings
                (user_id, ftp, zwift_id, activities_dir, workouts_dir,
                 zwift_email, weight_kg)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                ftp=excluded.ftp,
                zwift_id=excluded.zwift_id,
                activities_dir=excluded.activities_dir,
                workouts_dir=excluded.workouts_dir,
                zwift_email=excluded.zwift_email,
                weight_kg=excluded.weight_kg
            """,
            (
                user_id,
                current["ftp"],
                current["zwift_id"],
                current["activities_dir"],
                current["workouts_dir"],
                current["zwift_email"],
                current["weight_kg"],
            ),
        )
        conn.commit()
        return current
    finally:
        conn.close()


def set_zwift_credentials_row(
    user_id: int,
    email: Optional[str],
    password_enc: Optional[str],
    path: Optional[str] = None,
) -> None:
    """Set (or clear, with Nones) the stored Zwift email + encrypted password."""
    save_user_settings(user_id, {}, path=path)  # ensure the row exists
    conn = connect(path)
    try:
        conn.execute(
            "UPDATE user_settings SET zwift_email = ?, zwift_password_enc = ? "
            "WHERE user_id = ?",
            (email, password_enc, user_id),
        )
        conn.commit()
    finally:
        conn.close()


def get_zwift_credentials_row(
    user_id: int, path: Optional[str] = None
) -> "tuple[Optional[str], Optional[str]]":
    conn = connect(path)
    try:
        row = conn.execute(
            "SELECT zwift_email, zwift_password_enc FROM user_settings "
            "WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        if not row:
            return None, None
        return row["zwift_email"], row["zwift_password_enc"]
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


def update_estimated_ftp_entry(
    user_id: int, date: str, ftp_watts: float, path: Optional[str] = None
) -> bool:
    """Refresh the value of an existing *estimated* row (never touches manual)."""
    conn = connect(path)
    try:
        cur = conn.execute(
            "UPDATE ftp_history SET ftp_watts = ? "
            "WHERE user_id = ? AND date = ? AND source = 'estimated'",
            (float(ftp_watts), user_id, date),
        )
        conn.commit()
        return cur.rowcount > 0
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


def ftp_as_of(
    user_id: int, date_iso: str, path: Optional[str] = None
) -> Optional[float]:
    """The user's FTP effective on a date: the latest ftp_history entry on or
    before it, else the earliest entry after it (so older races still get a
    sensible FTP). Returns watts, or None when there is no history at all."""
    conn = connect(path)
    try:
        row = conn.execute(
            "SELECT ftp_watts FROM ftp_history WHERE user_id = ? AND date <= ? "
            "ORDER BY date DESC LIMIT 1",
            (user_id, date_iso),
        ).fetchone()
        if row is None:
            row = conn.execute(
                "SELECT ftp_watts FROM ftp_history WHERE user_id = ? "
                "ORDER BY date ASC LIMIT 1",
                (user_id,),
            ).fetchone()
        return float(row["ftp_watts"]) if row and row["ftp_watts"] else None
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
    user_id: int, name: str, start_date: str, weeks: int,
    model: Optional[str] = None, path: Optional[str] = None
) -> int:
    conn = connect(path)
    try:
        cur = conn.execute(
            "INSERT INTO plans (user_id, name, start_date, weeks, created, model) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, name, start_date, int(weeks),
             _dt.datetime.now().isoformat(), model),
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
            "SELECT id, name, start_date, weeks, created, model FROM plans "
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
            "SELECT id, name, start_date, weeks, created, model FROM plans "
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
        "completed_activity_id": r["completed_activity_id"],
        "completed_date": r["completed_date"],
        "adapted": r["adapted"],
        "adapted_at": r["adapted_at"],
        "rpe": r["rpe"],
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


def delete_plan(
    user_id: int, plan_id: int, path: Optional[str] = None
) -> Optional[Dict[str, int]]:
    """Delete a plan and its workouts, user-scoped.

    Returns {"workouts": n, "plans": 1} on success, or None if the plan does not
    exist for this user (so the caller can 404). Deletes plan_workouts first,
    then the plans row, in one transaction.
    """
    conn = connect(path)
    try:
        exists = conn.execute(
            "SELECT 1 FROM plans WHERE user_id = ? AND id = ?",
            (user_id, plan_id),
        ).fetchone()
        if exists is None:
            return None
        wcur = conn.execute(
            "DELETE FROM plan_workouts WHERE user_id = ? AND plan_id = ?",
            (user_id, plan_id),
        )
        pcur = conn.execute(
            "DELETE FROM plans WHERE user_id = ? AND id = ?",
            (user_id, plan_id),
        )
        conn.commit()
        return {"workouts": wcur.rowcount, "plans": pcur.rowcount}
    finally:
        conn.close()


# ------------------------------------------------- workout completion state
def incomplete_plan_workouts_up_to(
    user_id: int, date_iso: str, path: Optional[str] = None
) -> List[dict]:
    """Not-yet-completed plan workouts dated on or before `date_iso`."""
    conn = connect(path)
    try:
        rows = conn.execute(
            "SELECT * FROM plan_workouts WHERE user_id = ? AND date <= ? "
            "AND completed_activity_id IS NULL ORDER BY date ASC",
            (user_id, date_iso),
        ).fetchall()
        return [_plan_workout_row(r) for r in rows]
    finally:
        conn.close()


def completed_activity_ids(user_id: int, path: Optional[str] = None) -> set:
    """Activity ids already linked to a completed plan workout."""
    conn = connect(path)
    try:
        rows = conn.execute(
            "SELECT completed_activity_id FROM plan_workouts "
            "WHERE user_id = ? AND completed_activity_id IS NOT NULL",
            (user_id,),
        ).fetchall()
        return {r[0] for r in rows}
    finally:
        conn.close()


def mark_plan_workout_completed(
    user_id: int,
    workout_id: int,
    activity_id: int,
    completed_date: str,
    path: Optional[str] = None,
) -> bool:
    conn = connect(path)
    try:
        cur = conn.execute(
            "UPDATE plan_workouts SET completed_activity_id = ?, completed_date = ? "
            "WHERE user_id = ? AND id = ? AND completed_activity_id IS NULL",
            (activity_id, completed_date, user_id, workout_id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def set_plan_workout_rpe(
    user_id: int, workout_id: int, rpe: int, path: Optional[str] = None
) -> bool:
    """Set (or overwrite) the perceived-exertion grade of a completed workout.

    Scoped to the user and to workouts that have been completed; returns True
    when a row was updated.
    """
    conn = connect(path)
    try:
        cur = conn.execute(
            "UPDATE plan_workouts SET rpe = ? "
            "WHERE user_id = ? AND id = ? AND completed_activity_id IS NOT NULL",
            (int(rpe), user_id, workout_id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def activities_on_date(
    user_id: int, date_iso: str, path: Optional[str] = None
) -> List[dict]:
    """Activity summaries whose start_time falls on the given date."""
    conn = connect(path)
    try:
        rows = conn.execute(
            "SELECT * FROM activities WHERE user_id = ? AND start_time LIKE ? "
            "ORDER BY start_time ASC",
            (user_id, date_iso + "%"),
        ).fetchall()
        return [_row_summary(r) for r in rows]
    finally:
        conn.close()


# --------------------------------------------------------- plan adaptation
def adaptable_plan_workouts(
    user_id: int, after_date: str, up_to_date: str, path: Optional[str] = None
) -> List[dict]:
    """Future, not-completed, never-adapted plan workouts in (after, up_to]."""
    conn = connect(path)
    try:
        rows = conn.execute(
            "SELECT * FROM plan_workouts WHERE user_id = ? AND date > ? "
            "AND date <= ? AND completed_activity_id IS NULL "
            "AND adapted IS NULL ORDER BY date ASC",
            (user_id, after_date, up_to_date),
        ).fetchall()
        return [_plan_workout_row(r, include_zwo=True) for r in rows]
    finally:
        conn.close()


def update_plan_workout_content(
    user_id: int,
    workout_id: int,
    name: str,
    type: str,
    duration_s: int,
    tss: float,
    zwo_or_segments: str,
    adapted: str,
    adapted_at: str,
    path: Optional[str] = None,
) -> bool:
    """Rewrite an (unadapted) workout's prescription; records the adaptation."""
    conn = connect(path)
    try:
        cur = conn.execute(
            "UPDATE plan_workouts SET name = ?, type = ?, duration_s = ?, "
            "tss = ?, zwo_or_segments = ?, adapted = ?, adapted_at = ? "
            "WHERE user_id = ? AND id = ? AND adapted IS NULL",
            (name, type, int(duration_s), float(tss), zwo_or_segments,
             adapted, adapted_at, user_id, workout_id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def upcoming_adapted_counts(
    user_id: int, after_date: str, path: Optional[str] = None
) -> Dict[str, int]:
    """Count of future adapted workouts per adaptation kind."""
    conn = connect(path)
    try:
        rows = conn.execute(
            "SELECT adapted, COUNT(*) AS n FROM plan_workouts "
            "WHERE user_id = ? AND date > ? AND adapted IS NOT NULL "
            "GROUP BY adapted",
            (user_id, after_date),
        ).fetchall()
        return {r["adapted"]: r["n"] for r in rows}
    finally:
        conn.close()


# ------------------------------------------------------------ race results
def replace_race_results(
    user_id: int, source: str, results: List[dict], path: Optional[str] = None
) -> int:
    """Replace the user's cached race results for a source. Returns row count."""
    conn = connect(path)
    try:
        conn.execute(
            "DELETE FROM race_results WHERE user_id = ? AND source = ?",
            (user_id, source),
        )
        for r in results:
            conn.execute(
                """
                INSERT OR REPLACE INTO race_results
                  (user_id, source, event_date, event_title, position, category,
                   activity_id, duration_s, avg_power, np, if_, power_json,
                   source_type, distance_km, fetched_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id, source, r["event_date"], r["event_title"],
                    r.get("position"), r.get("category"), r.get("activity_id"),
                    r.get("duration_s"), r.get("avg_power"), r.get("np"),
                    r.get("if_"), json.dumps(r.get("power") or {}),
                    r.get("source_type"), r.get("distance_km"), r["fetched_at"],
                ),
            )
        conn.commit()
        return len(results)
    finally:
        conn.close()


def delete_race_results(
    user_id: int, source: str, path: Optional[str] = None
) -> int:
    """Delete a user's cached results of one source. Returns rows removed."""
    conn = connect(path)
    try:
        cur = conn.execute(
            "DELETE FROM race_results WHERE user_id = ? AND source = ?",
            (user_id, source),
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def count_race_results(
    user_id: int, source: str, path: Optional[str] = None
) -> int:
    conn = connect(path)
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM race_results WHERE user_id = ? AND source = ?",
            (user_id, source),
        ).fetchone()[0]
    finally:
        conn.close()


def list_race_results(user_id: int, path: Optional[str] = None) -> List[dict]:
    conn = connect(path)
    try:
        rows = conn.execute(
            "SELECT * FROM race_results WHERE user_id = ? ORDER BY event_date DESC",
            (user_id,),
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["power"] = json.loads(d.pop("power_json") or "{}")
            except ValueError:
                d["power"] = {}
            out.append(d)
        return out
    finally:
        conn.close()


def save_race_sync(
    user_id: int,
    rider_id: Optional[str],
    source: Optional[str],
    error: Optional[str],
    bests: Optional[dict] = None,
    last_refresh: Optional[str] = None,
    auth_failed: bool = False,
    path: Optional[str] = None,
) -> None:
    conn = connect(path)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO race_sync "
            "(user_id, rider_id, last_refresh, source, error, bests_json, "
            " auth_failed) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                user_id, rider_id,
                last_refresh or _dt.datetime.now().isoformat(timespec="seconds"),
                source, error, json.dumps(bests or {}), int(bool(auth_failed)),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def clear_race_auth_failure(user_id: int, path: Optional[str] = None) -> None:
    """Re-arm authenticated fetching (e.g. after new credentials are saved)."""
    conn = connect(path)
    try:
        conn.execute(
            "UPDATE race_sync SET auth_failed = 0 WHERE user_id = ?", (user_id,)
        )
        conn.commit()
    finally:
        conn.close()


def get_race_sync(user_id: int, path: Optional[str] = None) -> Optional[dict]:
    conn = connect(path)
    try:
        row = conn.execute(
            "SELECT * FROM race_sync WHERE user_id = ?", (user_id,)
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        try:
            d["bests"] = json.loads(d.pop("bests_json") or "{}")
        except ValueError:
            d["bests"] = {}
        return d
    finally:
        conn.close()


# -------------------------------------------------- out-of-office (OOTO)
def add_ooto_range(
    user_id: int, start_date: str, end_date: str, note: Optional[str] = None,
    path: Optional[str] = None,
) -> int:
    """Add an out-of-office date range (inclusive). start<=end is enforced."""
    if end_date < start_date:
        start_date, end_date = end_date, start_date
    conn = connect(path)
    try:
        cur = conn.execute(
            "INSERT INTO ooto_ranges (user_id, start_date, end_date, note, created) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, start_date, end_date, note or None,
             _dt.datetime.now().isoformat(timespec="seconds")),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def list_ooto_ranges(user_id: int, path: Optional[str] = None) -> List[dict]:
    conn = connect(path)
    try:
        rows = conn.execute(
            "SELECT id, start_date, end_date, note FROM ooto_ranges "
            "WHERE user_id = ? ORDER BY start_date ASC",
            (user_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def delete_ooto_range(user_id: int, ooto_id: int, path: Optional[str] = None) -> bool:
    conn = connect(path)
    try:
        cur = conn.execute(
            "DELETE FROM ooto_ranges WHERE user_id = ? AND id = ?",
            (user_id, ooto_id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def ooto_covers(user_id: int, date_iso: str, path: Optional[str] = None) -> bool:
    """True if the given date falls within any of the user's OOTO ranges."""
    conn = connect(path)
    try:
        row = conn.execute(
            "SELECT 1 FROM ooto_ranges WHERE user_id = ? "
            "AND start_date <= ? AND end_date >= ? LIMIT 1",
            (user_id, date_iso, date_iso),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


# ---------------------------------------------------------- scanned files
def seen_files(
    user_id: int, path: Optional[str] = None
) -> "Dict[str, tuple]":
    """Map of already-scanned file path -> (mtime, size) for a user.

    Lets a rescan skip files whose mtime+size are unchanged WITHOUT parsing.
    """
    conn = connect(path)
    try:
        rows = conn.execute(
            "SELECT path, mtime, size FROM scanned_files WHERE user_id = ?",
            (user_id,),
        ).fetchall()
        return {r["path"]: (r["mtime"], r["size"]) for r in rows}
    finally:
        conn.close()


def record_scanned_file(
    user_id: int,
    file_path: str,
    mtime: float,
    size: int,
    path: Optional[str] = None,
) -> None:
    """Remember (or refresh) a scanned file's mtime+size so it is not reparsed."""
    conn = connect(path)
    try:
        conn.execute(
            "INSERT INTO scanned_files (user_id, path, mtime, size) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(user_id, path) DO UPDATE SET "
            "mtime = excluded.mtime, size = excluded.size",
            (user_id, file_path, float(mtime), int(size)),
        )
        conn.commit()
    finally:
        conn.close()


def all_user_ids(path: Optional[str] = None) -> List[int]:
    """Every known user id (union of users, settings and activity owners).

    The union is defensive: per-user rows can outlive a users row, and the
    background scanner must still serve those accounts.
    """
    conn = connect(path)
    try:
        rows = conn.execute(
            "SELECT id FROM users "
            "UNION SELECT user_id FROM user_settings "
            "UNION SELECT DISTINCT user_id FROM activities"
        ).fetchall()
        return sorted(r[0] for r in rows)
    finally:
        conn.close()
