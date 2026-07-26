"""SQLite storage (stdlib only) with per-user isolation.

Everything a user owns - activities, streams, ftp_history, settings - is scoped
by ``user_id``. Schema changes bump ``SCHEMA_VERSION``. Versions with an entry
in ``_MIGRATIONS`` are upgraded in place (no data loss); anything without a
migration chain falls back to a clean drop/recreate.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import sqlite3
import zlib
from typing import Callable, Dict, List, Optional, Sequence, Union

from .config import db_path
from .config import _restrict
from .timeutil import utc_now

_log = logging.getLogger(__name__)

SCHEMA_VERSION = 22


def _restrict_db_files(path: str) -> None:
    """Best-effort owner-only lockdown of the DB file and its WAL/SHM sidecars.

    The DB holds password hashes and encrypted Zwift credentials, so it must not
    be readable by other accounts. Delegates to ``config._restrict``: 0600 on
    POSIX, an owner-only ACL on Windows. Best-effort - filesystems that don't
    support either (some network mounts) must not crash the app.
    """
    for p in (path, path + "-wal", path + "-shm"):
        _restrict(p, 0o600, is_dir=False)

# In-app rides are named "Ride <ISO date> <workout name>" (ble/runner.py);
# imported rows carry the .fit file's basename. The distinction is what tells
# the two sources apart in SQL (the '_' are LIKE single-char wildcards).
IN_APP_FILENAME_SQL = "filename LIKE 'Ride ____-__-__ %' AND filename NOT LIKE '%.fit'"


def _add_duplicate_of_and_backfill_utc(conn: sqlite3.Connection) -> None:
    """v18 -> v19: add ``duplicate_of``, then rewrite in-app start_time as UTC.

    The two steps are one unit because the column doubles as the "already
    migrated" marker. Shifting timestamps is the only migration here that is
    not naturally repeatable - running it twice moves a ride another four or
    five hours - and the version counter alone does not protect a database
    that was left at v18 with the column already added. If the column is
    present, this version has already run and there is nothing to do.
    """
    columns = {row[1] for row in conn.execute("PRAGMA table_info(activities)")}
    if "duplicate_of" in columns:
        return
    conn.execute("ALTER TABLE activities ADD COLUMN duplicate_of INTEGER")
    _backfill_inapp_utc(conn)


def _backfill_inapp_utc(conn: sqlite3.Connection) -> None:
    """Rewrite in-app rides' naive-local start_time as naive UTC.

    Until v19 an in-app ride stored ``datetime.now()`` (local) while an
    imported .fit stored UTC, so the same ride recorded by both landed hours
    apart. One UPDATE fixes every historical row: a CASE over the local
    timezone's offset ranges picks the offset that was actually in force at
    each timestamp (so historical DST is honored), and every row is shifted
    exactly once regardless of statement ordering.

    Not idempotent on its own - see ``_add_duplicate_of_and_backfill_utc``.
    """
    from .timeutil import local_offset_ranges

    now = _dt.datetime.now()  # local on purpose: bounds a local-offset scan
    ranges = local_offset_ranges(
        _dt.datetime(2000, 1, 1), now + _dt.timedelta(days=366)
    )
    prev = ranges[0][1]
    if len(ranges) == 1:
        # A timezone that has never observed DST (UTC, Tokyo, Phoenix) yields a
        # single range, and "CASE ELSE x END" with no WHEN is a syntax error.
        shift = str(-prev)
    else:
        case = "CASE "
        for boundary, offset in ranges[1:]:
            case += f"WHEN start_time < '{boundary.isoformat()}' THEN {-prev} "
            prev = offset
        shift = case + f"ELSE {-prev} END"
    conn.execute(
        # COALESCE keeps an unparseable timestamp as-is instead of nulling it;
        # substr(...,20) carries the original fractional seconds across, since
        # strftime would truncate them to milliseconds.
        "UPDATE activities SET start_time = COALESCE("
        f"strftime('%Y-%m-%dT%H:%M:%S', start_time, printf('%+d seconds', {shift}))"
        " || substr(start_time, 20), start_time) "
        f"WHERE start_time IS NOT NULL AND {IN_APP_FILENAME_SQL}"
    )


# In-place migrations: version N -> N+1 statement lists. A database whose
# version has an unbroken chain here is upgraded without losing live data.
# (Brand-new tables need no ALTERs - init_db runs _SCHEMA after migrating - but
# each version still needs an entry, even an empty list, to keep the chain.)
# An entry may also be a callable taking the open connection, for a migration
# whose SQL has to be computed (see _backfill_inapp_utc).
_MIGRATIONS: Dict[int, Sequence[Union[str, Callable[[sqlite3.Connection], None]]]] = {
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
    12: [
        "ALTER TABLE plan_workouts ADD COLUMN variant TEXT",
    ],
    13: [
        "ALTER TABLE race_results ADD COLUMN zp_event_id TEXT",
    ],
    14: [
        "ALTER TABLE plan_workouts ADD COLUMN compliance REAL",
        "ALTER TABLE plan_workouts ADD COLUMN effective_ftp REAL",
        "ALTER TABLE plan_workouts ADD COLUMN feedback_applied INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE plan_workouts ADD COLUMN feedback_batch_id INTEGER",
        # New standalone_workouts and ftp_feedback_batches tables are created
        # by _SCHEMA after migrating.
    ],
    15: [
        "ALTER TABLE race_results ADD COLUMN avg_hr REAL",
        "ALTER TABLE race_results ADD COLUMN max_hr REAL",
        "ALTER TABLE race_results ADD COLUMN weight_kg REAL",
    ],
    16: [
        "ALTER TABLE user_settings ADD COLUMN hr_max INTEGER",
    ],
    17: [
        "ALTER TABLE activities ADD COLUMN rpe INTEGER",
    ],
    18: [
        _add_duplicate_of_and_backfill_utc,
    ],
    19: [
        # The generator inputs a plan was built from ("the recipe"), so a plan
        # can be recomputed from scratch instead of incrementally patched.
        # Deliberately NOT backfilled: a guessed recipe would silently rewrite
        # a user's plan into something they never asked for. recipe IS NULL
        # marks a legacy plan, which reflow refuses to touch.
        "ALTER TABLE plans ADD COLUMN recipe TEXT",
        "ALTER TABLE plans ADD COLUMN active INTEGER NOT NULL DEFAULT 0",
        # Provenance: 'generated' rows are the ones the recipe owns and may
        # rewrite. Legacy rows stay NULL and are never reflowed.
        "ALTER TABLE plan_workouts ADD COLUMN origin TEXT",
    ],
    20: [
        # New table race_dates is created by _SCHEMA after migrating.
    ],
    21: [
        # New table rider_profile is created by _SCHEMA after migrating.
        # Deliberately not backfilled: an absent row means "not computed yet",
        # which prescribes the population constants - the same thing the app
        # did before profiles existed - and the next maintenance sweep fills
        # it in. Guessing a profile at migration time would be both slow (it
        # decompresses months of streams per user) and wrong for any user
        # whose data has since changed.
    ],
}

_DROP = """
DROP TABLE IF EXISTS ftp_feedback_batches;
DROP TABLE IF EXISTS standalone_workouts;
DROP TABLE IF EXISTS scanned_files;
DROP TABLE IF EXISTS ooto_ranges;
DROP TABLE IF EXISTS rider_profile;
DROP TABLE IF EXISTS race_dates;
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
    hr_max             INTEGER,
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
    rpe         INTEGER,
    duplicate_of INTEGER,
    streams     BLOB,
    UNIQUE(user_id, dedup_hash),
    FOREIGN KEY(user_id) REFERENCES users(id)
);
CREATE INDEX IF NOT EXISTS idx_activities_user_start
    ON activities(user_id, start_time);
CREATE INDEX IF NOT EXISTS idx_activities_duplicate_of
    ON activities(user_id, duplicate_of);

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
    recipe     TEXT,
    active     INTEGER NOT NULL DEFAULT 0,
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
    variant           TEXT,
    compliance        REAL,
    effective_ftp     REAL,
    feedback_applied  INTEGER NOT NULL DEFAULT 0,
    feedback_batch_id INTEGER,
    origin            TEXT,
    FOREIGN KEY(plan_id) REFERENCES plans(id),
    FOREIGN KEY(user_id) REFERENCES users(id)
);
CREATE INDEX IF NOT EXISTS idx_plan_workouts_user_date
    ON plan_workouts(user_id, date);

CREATE TABLE IF NOT EXISTS standalone_workouts (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id               INTEGER NOT NULL,
    export_key            TEXT NOT NULL,
    scheduled_date        TEXT NOT NULL,
    name                  TEXT NOT NULL,
    type                  TEXT NOT NULL,
    duration_s            INTEGER NOT NULL,
    tss                   REAL NOT NULL,
    zwo                   TEXT NOT NULL,
    export_ftp            REAL NOT NULL,
    completed_activity_id INTEGER,
    completed_date        TEXT,
    rpe                   INTEGER,
    compliance            REAL,
    effective_ftp         REAL,
    feedback_applied      INTEGER NOT NULL DEFAULT 0,
    feedback_batch_id     INTEGER,
    created               TEXT NOT NULL,
    UNIQUE(user_id, export_key),
    FOREIGN KEY(user_id) REFERENCES users(id)
);
CREATE INDEX IF NOT EXISTS idx_standalone_user_date
    ON standalone_workouts(user_id, scheduled_date);

CREATE TABLE IF NOT EXISTS ftp_feedback_batches (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    ftp_date   TEXT NOT NULL,
    delta      REAL NOT NULL,
    applied    INTEGER NOT NULL DEFAULT 1,
    created    TEXT NOT NULL,
    FOREIGN KEY(user_id) REFERENCES users(id)
);

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
    avg_hr       REAL,
    max_hr       REAL,
    weight_kg    REAL,
    np           REAL,
    if_          REAL,
    power_json   TEXT,
    source_type  TEXT,
    distance_km  REAL,
    zp_event_id  TEXT,
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

-- Races the rider INTENDS to do: future events the plan bends around (taper,
-- post-race recovery, no workout on the day itself). Deliberately NOT the same
-- table as race_results above, which caches PAST results fetched from
-- ZwiftPower. Future intent and historical fact have different lifecycles,
-- different owners and different columns - do not merge them.
CREATE TABLE IF NOT EXISTS race_dates (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL,
    date         TEXT NOT NULL,
    priority     TEXT NOT NULL,      -- 'A' important | 'B' casual
    name         TEXT,
    duration_min INTEGER,            -- expected race duration; drives post-race recovery
    created      TEXT NOT NULL,
    FOREIGN KEY(user_id) REFERENCES users(id)
);
CREATE INDEX IF NOT EXISTS idx_race_dates_user
    ON race_dates(user_id, date);

-- The rider's MEASURED capacities (metrics/rider.py), stored rather than
-- recomputed on read. Deriving them decompresses ~90 days of power streams and
-- a year of heart-rate streams, so they cannot be computed per request; but
-- they also cannot be memoized in process, because their inputs include
-- wall-clock time (FTP decays with detraining, and HRmax detection has a
-- rolling lookback), so any in-memory cache key has a staleness class nobody
-- thought of. A stored snapshot makes staleness bounded and INSPECTABLE:
-- computed_at says exactly how old the prescription's basis is, a missing row
-- says "not computed yet" instead of being silently wrong, and the write side
-- is the daily sweep and activity import - the two places that already know
-- the rider's data changed.
CREATE TABLE IF NOT EXISTS rider_profile (
    user_id          INTEGER PRIMARY KEY,
    computed_at      TEXT NOT NULL,
    ftp              REAL,
    weight_kg        REAL,
    hr_max           REAL,
    hr_max_source    TEXT,
    n_hr_activities  INTEGER NOT NULL DEFAULT 0,
    cp               REAL,
    wprime           REAL,
    wprime_j_per_kg  REAL,
    cp_w_per_kg      REAL,
    peak_5s          REAL,
    peak_60s         REAL,
    peak_300s        REAL,
    sprint_ratio     REAL,
    vo2_ratio        REAL,
    FOREIGN KEY(user_id) REFERENCES users(id)
);

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
    - NEWER version than this code: refuse loudly. A long-lived server process
      still holding old code in memory has twice re-run init_db against a
      freshly-migrated live DB and wiped tables via the drop/recreate branch
      (v10 incident, and the 2026-07-18 users/plans wipe) - stale code must
      crash, never "fix" a database from the future.
    - Anything else (fresh db, unmigratable older version): clean drop/recreate.
    """
    resolved = path or db_path()
    conn = connect(path)
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise RuntimeError(
                f"database schema is v{version} but this code only knows "
                f"v{SCHEMA_VERSION} - refusing to touch it. You are running "
                "outdated code (often a stale long-lived server process); "
                "stop it and start the current version."
            )
        if version == SCHEMA_VERSION:
            conn.executescript(_SCHEMA)  # idempotent CREATE IF NOT EXISTS
        elif 0 < version < SCHEMA_VERSION and _can_migrate(version):
            # Back up the live data BEFORE mutating it. A migration that goes
            # wrong (or stale code that mis-migrates) has twice wiped the live
            # DB; a pre-migration snapshot is the recovery anchor. If the backup
            # cannot be written, abort - never migrate an unbacked database.
            from . import backup as _backup

            _backup.create_backup("pre-migration", src_path=path or db_path())
            for v in range(version, SCHEMA_VERSION):
                for stmt in _MIGRATIONS[v]:
                    try:
                        if callable(stmt):
                            stmt(conn)
                        else:
                            conn.execute(stmt)
                    except sqlite3.OperationalError as e:
                        # A later migration may target a table or column the
                        # older database never had (_SCHEMA below creates it in
                        # its final shape) or a column that already exists.
                        msg = str(e).lower()
                        if ("no such table" in msg or "duplicate column" in msg
                                or "no such column" in msg):
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
    # Lock down the DB file (and any WAL/SHM sidecars) after it exists.
    _restrict_db_files(resolved)


# ----------------------------------------------------------------- users
def create_user(username: str, password_hash: str, path: Optional[str] = None) -> Optional[int]:
    """Create a user. Returns the new id, or None if the username is taken."""
    conn = connect(path)
    try:
        cur = conn.execute(
            "INSERT INTO users (username, password_hash, created) VALUES (?, ?, ?)",
            (username, password_hash, utc_now().isoformat()),
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
                 "zwift_email", "weight_kg", "hr_max")


def get_user_settings(user_id: int, path: Optional[str] = None) -> dict:
    conn = connect(path)
    try:
        row = conn.execute(
            "SELECT ftp, zwift_id, activities_dir, workouts_dir, zwift_email, "
            "weight_kg, hr_max FROM user_settings WHERE user_id = ?",
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
                 zwift_email, weight_kg, hr_max)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                ftp=excluded.ftp,
                zwift_id=excluded.zwift_id,
                activities_dir=excluded.activities_dir,
                workouts_dir=excluded.workouts_dir,
                zwift_email=excluded.zwift_email,
                weight_kg=excluded.weight_kg,
                hr_max=excluded.hr_max
            """,
            (
                user_id,
                current["ftp"],
                current["zwift_id"],
                current["activities_dir"],
                current["workouts_dir"],
                current["zwift_email"],
                current["weight_kg"],
                current["hr_max"],
            ),
        )
        conn.commit()
        return current
    finally:
        conn.close()


def set_user_hr_max(
    user_id: int, hr_max: Optional[int], path: Optional[str] = None
) -> None:
    """Set or explicitly clear a user's manual HRmax override."""
    save_user_settings(user_id, {}, path=path)
    conn = connect(path)
    try:
        conn.execute(
            "UPDATE user_settings SET hr_max = ? WHERE user_id = ?",
            (hr_max, user_id),
        )
        conn.commit()
    finally:
        conn.close()


def set_user_ftp_override(
    user_id: int, ftp: Optional[float], path: Optional[str] = None
) -> None:
    """Set or explicitly clear a user's manual Training FTP override."""
    save_user_settings(user_id, {}, path=path)
    conn = connect(path)
    try:
        conn.execute(
            "UPDATE user_settings SET ftp = ? WHERE user_id = ?",
            (float(ftp) if ftp is not None else None, user_id),
        )
        conn.commit()
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


def activity_exists_by_start(
    user_id: int, start_time: str, path: Optional[str] = None
) -> bool:
    """True if the user already has any activity with this exact start_time.

    Robust dedup for a ride captured in two files (e.g. Zwift's in-progress temp
    file and the final timestamped .fit share a start second but differ in
    duration, so the dedup_hash differs).
    """
    conn = connect(path)
    try:
        cur = conn.execute(
            "SELECT 1 FROM activities WHERE user_id = ? AND start_time = ?",
            (user_id, start_time),
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
        "rpe": row["rpe"],
        "duplicate_of": row["duplicate_of"],
    }


# A ride recorded twice (in-app and by Zwift) keeps both rows, but the
# secondary carries duplicate_of = <primary id> and must never be listed or
# summed a second time. Every aggregation over activities filters on this.
_NOT_DUPLICATE = "duplicate_of IS NULL"


def list_activities(user_id: int, path: Optional[str] = None) -> List[dict]:
    conn = connect(path)
    try:
        rows = conn.execute(
            f"SELECT * FROM activities WHERE user_id = ? AND {_NOT_DUPLICATE} "
            "ORDER BY start_time DESC",
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
    cutoff = (utc_now() - _dt.timedelta(days=days)).isoformat()
    conn = connect(path)
    try:
        rows = conn.execute(
            "SELECT streams FROM activities "
            f"WHERE user_id = ? AND start_time >= ? AND {_NOT_DUPLICATE} "
            "ORDER BY start_time",
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
            f"WHERE user_id = ? AND start_time IS NOT NULL AND {_NOT_DUPLICATE}",
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


def weekly_volume(user_id: int, path: Optional[str] = None) -> List[dict]:
    """Per-week training volume for a user, ordered by week ascending.

    Each dict is one Monday-started week (only weeks with at least one activity
    appear here; JS fills any gaps with zeros):
      - week_start: ISO date of that week's Monday
      - hours:       sum of duration_s / 3600
      - tss:         sum of tss (NULLs counted as 0)
      - distance_km: sum of distance_m / 1000 (NULLs counted as 0)
      - calories:    sum of avg_power * duration_s / 1000 over activities that
                     have avg_power (kJ ~= kcal for cycling); rows with a NULL
                     avg_power contribute nothing to calories.
    Rows with a NULL start_time are skipped (no week to bucket them into).
    """
    conn = connect(path)
    try:
        rows = conn.execute(
            """
            SELECT date(start_time, 'weekday 0', '-6 days') AS week_start,
                   SUM(COALESCE(duration_s, 0)) / 3600.0        AS hours,
                   SUM(COALESCE(tss, 0))                        AS tss,
                   SUM(COALESCE(distance_m, 0)) / 1000.0        AS distance_km,
                   SUM(CASE WHEN avg_power IS NOT NULL
                            THEN avg_power * COALESCE(duration_s, 0) / 1000.0
                            ELSE 0 END)                          AS calories
            FROM activities
            WHERE user_id = ? AND start_time IS NOT NULL
              AND duplicate_of IS NULL
            GROUP BY week_start
            ORDER BY week_start ASC
            """,
            (user_id,),
        ).fetchall()
        return [
            {
                "week_start": r["week_start"],
                "hours": round(float(r["hours"] or 0.0), 2),
                "tss": round(float(r["tss"] or 0.0), 1),
                "distance_km": round(float(r["distance_km"] or 0.0), 1),
                "calories": round(float(r["calories"] or 0.0)),
            }
            for r in rows
        ]
    finally:
        conn.close()


def full_activities(user_id: int, path: Optional[str] = None) -> List[dict]:
    conn = connect(path)
    try:
        rows = conn.execute(
            f"SELECT * FROM activities WHERE user_id = ? AND {_NOT_DUPLICATE} "
            "ORDER BY start_time",
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


def recent_full_activities(
    user_id: int, days: int, path: Optional[str] = None
) -> List[dict]:
    """``full_activities`` restricted to the trailing ``days`` (streams inflated).

    Only recent activities are decompressed, so callers that need short trailing
    windows (e.g. plateau detection over the last few weeks) avoid inflating the
    user's entire stream history. Ordered by start_time ascending, like
    ``full_activities``.
    """
    cutoff = (utc_now() - _dt.timedelta(days=days)).isoformat()
    conn = connect(path)
    try:
        rows = conn.execute(
            "SELECT * FROM activities "
            f"WHERE user_id = ? AND start_time >= ? AND {_NOT_DUPLICATE} "
            "ORDER BY start_time",
            (user_id, cutoff),
        ).fetchall()
        out = []
        for r in rows:
            d = _row_summary(r)
            d["streams"] = _unpack_streams(r["streams"])
            out.append(d)
        return out
    finally:
        conn.close()


# ------------------------------------------------- cross-source duplicates
def activities_for_matching(
    user_id: int,
    lo: Optional[str] = None,
    hi: Optional[str] = None,
    path: Optional[str] = None,
) -> List[dict]:
    """Summaries in a start_time window, INCLUDING rows already marked duplicate.

    Duplicate detection is the one reader that must see every row: filtering
    linked secondaries out here would make re-linking and repair impossible.
    """
    sql = "SELECT * FROM activities WHERE user_id = ? AND start_time IS NOT NULL"
    args: List = [user_id]
    if lo is not None:
        sql += " AND start_time >= ?"
        args.append(lo)
    if hi is not None:
        sql += " AND start_time <= ?"
        args.append(hi)
    conn = connect(path)
    try:
        rows = conn.execute(sql + " ORDER BY start_time ASC", args).fetchall()
        return [_row_summary(r) for r in rows]
    finally:
        conn.close()


def set_duplicate_of(
    user_id: int, activity_id: int, primary_id: int, path: Optional[str] = None
) -> bool:
    """Mark ``activity_id`` as a duplicate of ``primary_id`` (same user).

    Refuses to build chains: the row must not already be a duplicate or be some
    other row's primary, and the primary must not itself be a duplicate.
    """
    if activity_id == primary_id:
        return False
    conn = connect(path)
    try:
        cur = conn.execute(
            "UPDATE activities SET duplicate_of = ? "
            "WHERE user_id = ? AND id = ? AND duplicate_of IS NULL "
            "AND EXISTS (SELECT 1 FROM activities WHERE user_id = ? AND id = ? "
            "            AND duplicate_of IS NULL) "
            "AND NOT EXISTS (SELECT 1 FROM activities WHERE user_id = ? "
            "                AND duplicate_of = ?)",
            (primary_id, user_id, activity_id, user_id, primary_id,
             user_id, activity_id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def primaries_with_duplicates(user_id: int, path: Optional[str] = None) -> set:
    """Ids of activities that have at least one linked duplicate."""
    conn = connect(path)
    try:
        rows = conn.execute(
            "SELECT DISTINCT duplicate_of FROM activities "
            "WHERE user_id = ? AND duplicate_of IS NOT NULL",
            (user_id,),
        ).fetchall()
        return {r[0] for r in rows}
    finally:
        conn.close()


def repoint_completed_activity(
    user_id: int, old_id: int, new_id: int, path: Optional[str] = None
) -> bool:
    """Move a workout completion from one activity to another.

    Used when a duplicate is linked: whatever the secondary completed has to
    follow the primary, or the workout would point at a hidden ride. Honors the
    "an activity is consumed by only one workout" invariant - if the target is
    already consumed, the link stays where it is.
    """
    if old_id == new_id:
        return False
    conn = connect(path)
    try:
        guard = (
            " AND NOT EXISTS (SELECT 1 FROM plan_workouts WHERE user_id = ? "
            "                 AND completed_activity_id = ?)"
            " AND NOT EXISTS (SELECT 1 FROM standalone_workouts WHERE user_id = ? "
            "                 AND completed_activity_id = ?)"
        )
        moved = 0
        for table in ("plan_workouts", "standalone_workouts"):
            cur = conn.execute(
                f"UPDATE {table} SET completed_activity_id = ? "
                "WHERE user_id = ? AND completed_activity_id = ?" + guard,
                (new_id, user_id, old_id, user_id, new_id, user_id, new_id),
            )
            moved += cur.rowcount
        conn.commit()
        return moved > 0
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
_PLAN_COLUMNS = "id, name, start_date, weeks, created, model, recipe, active"


def _plan_row(r: sqlite3.Row) -> dict:
    """Plan row with the recipe parsed back to a dict and active as a bool.

    A recipe that fails to parse is surfaced as None - i.e. the plan degrades
    to "legacy, not reflowable" rather than blowing up a page render.
    """
    d = dict(r)
    raw = d.get("recipe")
    if raw:
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            parsed = None
        d["recipe"] = parsed if isinstance(parsed, dict) else None
    else:
        d["recipe"] = None
    d["active"] = bool(d.get("active"))
    return d


def create_plan(
    user_id: int, name: str, start_date: str, weeks: int,
    model: Optional[str] = None, recipe: Optional[dict] = None,
    path: Optional[str] = None
) -> int:
    """Insert a plan and make it the user's active plan.

    ``recipe`` is the generator input dict (see prescribe/reflow.py); it is
    serialized here so callers never deal with JSON. Leaving it None creates a
    plan that reflow will refuse to touch.
    """
    conn = connect(path)
    try:
        cur = conn.execute(
            "INSERT INTO plans "
            "(user_id, name, start_date, weeks, created, model, recipe, active) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 0)",
            (user_id, name, start_date, int(weeks), utc_now().isoformat(),
             model, json.dumps(recipe) if recipe is not None else None),
        )
        plan_id = cur.lastrowid
        # A new plan supersedes whatever was active - same transaction so the
        # "at most one active" invariant is never observable as broken.
        conn.execute("UPDATE plans SET active = 0 WHERE user_id = ?", (user_id,))
        conn.execute(
            "UPDATE plans SET active = 1 WHERE user_id = ? AND id = ?",
            (user_id, plan_id),
        )
        conn.commit()
        return plan_id
    finally:
        conn.close()


def set_active_plan(user_id: int, plan_id: int, path: Optional[str] = None) -> bool:
    """Make `plan_id` the user's only active plan. False if it isn't theirs."""
    conn = connect(path)
    try:
        owned = conn.execute(
            "SELECT 1 FROM plans WHERE user_id = ? AND id = ?",
            (user_id, plan_id),
        ).fetchone()
        if owned is None:
            return False
        conn.execute("UPDATE plans SET active = 0 WHERE user_id = ?", (user_id,))
        conn.execute(
            "UPDATE plans SET active = 1 WHERE user_id = ? AND id = ?",
            (user_id, plan_id),
        )
        conn.commit()
        return True
    finally:
        conn.close()


def get_active_plan(user_id: int, path: Optional[str] = None) -> Optional[dict]:
    """The user's active plan row, or None if none is flagged (legacy users)."""
    conn = connect(path)
    try:
        row = conn.execute(
            f"SELECT {_PLAN_COLUMNS} FROM plans WHERE user_id = ? AND active = 1 "
            "ORDER BY created DESC",
            (user_id,),
        ).fetchone()
        return _plan_row(row) if row else None
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
    variant: Optional[str] = None,
    origin: Optional[str] = None,
    path: Optional[str] = None,
) -> int:
    conn = connect(path)
    try:
        cur = conn.execute(
            """
            INSERT INTO plan_workouts
              (plan_id, user_id, date, name, type, duration_s, tss,
               zwo_or_segments, variant, origin)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (plan_id, user_id, date, name, type, int(duration_s), float(tss),
             zwo_or_segments, variant, origin),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def get_plan(user_id: int, plan_id: int, path: Optional[str] = None) -> Optional[dict]:
    conn = connect(path)
    try:
        row = conn.execute(
            f"SELECT {_PLAN_COLUMNS} FROM plans WHERE user_id = ? AND id = ?",
            (user_id, plan_id),
        ).fetchone()
        return _plan_row(row) if row else None
    finally:
        conn.close()


def list_plans(user_id: int, path: Optional[str] = None) -> List[dict]:
    conn = connect(path)
    try:
        rows = conn.execute(
            f"SELECT {_PLAN_COLUMNS} FROM plans WHERE user_id = ? "
            "ORDER BY created DESC",
            (user_id,),
        ).fetchall()
        return [_plan_row(r) for r in rows]
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
        "variant": r["variant"],
        "compliance": r["compliance"],
        "effective_ftp": r["effective_ftp"],
        "feedback_applied": bool(r["feedback_applied"]),
        "feedback_batch_id": r["feedback_batch_id"],
        "origin": r["origin"],
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


def plan_workout_dates(
    user_id: int, plan_id: int, start_date: str, end_date: str,
    path: Optional[str] = None,
) -> List[str]:
    """Distinct dates ONE plan has a workout on, within an inclusive range.

    Just the dates: this answers "which days does this plan ride?", which is
    what race handling needs to place post-race recovery days, without
    inflating a single stored .zwo. Scoped to a plan rather than a user
    because the generator's own view is - a second, overlapping plan's rows
    are days this plan never scheduled.
    """
    conn = connect(path)
    try:
        rows = conn.execute(
            "SELECT DISTINCT date FROM plan_workouts WHERE user_id = ? "
            "AND plan_id = ? AND date >= ? AND date <= ? ORDER BY date ASC",
            (user_id, plan_id, start_date, end_date),
        ).fetchall()
        return [r["date"] for r in rows]
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
    then the plans row, in one transaction. Deleting the ACTIVE plan promotes
    the most recently created survivor, so a user who had an active plan still
    has one afterwards (unless that was their last plan).
    """
    conn = connect(path)
    try:
        exists = conn.execute(
            "SELECT active FROM plans WHERE user_id = ? AND id = ?",
            (user_id, plan_id),
        ).fetchone()
        if exists is None:
            return None
        was_active = bool(exists["active"])
        wcur = conn.execute(
            "DELETE FROM plan_workouts WHERE user_id = ? AND plan_id = ?",
            (user_id, plan_id),
        )
        pcur = conn.execute(
            "DELETE FROM plans WHERE user_id = ? AND id = ?",
            (user_id, plan_id),
        )
        if was_active:
            successor = conn.execute(
                # id DESC breaks the tie when two plans share a `created`
                # timestamp, so promotion is deterministic.
                "SELECT id FROM plans WHERE user_id = ? "
                "ORDER BY created DESC, id DESC LIMIT 1",
                (user_id,),
            ).fetchone()
            if successor is not None:
                conn.execute(
                    "UPDATE plans SET active = 1 WHERE user_id = ? AND id = ?",
                    (user_id, successor["id"]),
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
        return [_plan_workout_row(r, include_zwo=True) for r in rows]
    finally:
        conn.close()


def completed_activity_ids(user_id: int, path: Optional[str] = None) -> set:
    """Activity ids already linked to any completed persisted workout."""
    conn = connect(path)
    try:
        rows = conn.execute(
            "SELECT completed_activity_id FROM plan_workouts "
            "WHERE user_id = ? AND completed_activity_id IS NOT NULL "
            "UNION SELECT completed_activity_id FROM standalone_workouts "
            "WHERE user_id = ? AND completed_activity_id IS NOT NULL",
            (user_id, user_id),
        ).fetchall()
        return {r[0] for r in rows}
    finally:
        conn.close()


def mark_plan_workout_completed(
    user_id: int,
    workout_id: int,
    activity_id: int,
    completed_date: str,
    compliance: Optional[float] = None,
    effective_ftp: Optional[float] = None,
    path: Optional[str] = None,
) -> bool:
    conn = connect(path)
    try:
        cur = conn.execute(
            "UPDATE plan_workouts SET completed_activity_id=?, completed_date=?, "
            "compliance=?, effective_ftp=? "
            "WHERE user_id = ? AND id = ? AND completed_activity_id IS NULL "
            "AND NOT EXISTS (SELECT 1 FROM plan_workouts "
            "WHERE user_id=? AND completed_activity_id=?) "
            "AND NOT EXISTS (SELECT 1 FROM standalone_workouts "
            "WHERE user_id=? AND completed_activity_id=?)",
            (
                activity_id, completed_date, compliance, effective_ftp,
                user_id, workout_id, user_id, activity_id, user_id, activity_id,
            ),
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


# ------------------------------------------------ standalone exported workouts
def _standalone_row(row: sqlite3.Row, include_zwo: bool = False) -> dict:
    out = {
        "id": row["id"],
        "scheduled_date": row["scheduled_date"],
        "name": row["name"],
        "type": row["type"],
        "duration_s": row["duration_s"],
        "tss": row["tss"],
        "export_ftp": row["export_ftp"],
        "completed_activity_id": row["completed_activity_id"],
        "completed_date": row["completed_date"],
        "rpe": row["rpe"],
        "compliance": row["compliance"],
        "effective_ftp": row["effective_ftp"],
        "feedback_applied": bool(row["feedback_applied"]),
        "feedback_batch_id": row["feedback_batch_id"],
    }
    if include_zwo:
        out["zwo"] = row["zwo"]
    return out


def add_standalone_workout(
    user_id: int, export_key: str, scheduled_date: str, name: str, type: str,
    duration_s: int, tss: float, zwo: str, export_ftp: float,
    path: Optional[str] = None,
) -> int:
    """Persist an exported one-off workout, idempotently by export_key."""
    conn = connect(path)
    try:
        conn.execute(
            "INSERT OR IGNORE INTO standalone_workouts "
            "(user_id,export_key,scheduled_date,name,type,duration_s,tss,zwo,"
            " export_ftp,created) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (user_id, export_key, scheduled_date, name, type, int(duration_s),
             float(tss), zwo, float(export_ftp), utc_now().isoformat()),
        )
        row = conn.execute(
            "SELECT id FROM standalone_workouts WHERE user_id=? AND export_key=?",
            (user_id, export_key),
        ).fetchone()
        conn.commit()
        return int(row["id"])
    finally:
        conn.close()


def standalone_workouts_on_date(
    user_id: int, date_iso: str, path: Optional[str] = None,
    include_zwo: bool = False,
) -> List[dict]:
    conn = connect(path)
    try:
        rows = conn.execute(
            "SELECT * FROM standalone_workouts WHERE user_id=? AND scheduled_date=? "
            "ORDER BY id", (user_id, date_iso),
        ).fetchall()
        return [_standalone_row(r, include_zwo) for r in rows]
    finally:
        conn.close()


def incomplete_standalone_workouts_up_to(
    user_id: int, date_iso: str, path: Optional[str] = None,
) -> List[dict]:
    conn = connect(path)
    try:
        rows = conn.execute(
            "SELECT * FROM standalone_workouts WHERE user_id=? "
            "AND scheduled_date<=? AND completed_activity_id IS NULL "
            "ORDER BY scheduled_date,id", (user_id, date_iso),
        ).fetchall()
        return [_standalone_row(r, True) for r in rows]
    finally:
        conn.close()


def mark_standalone_completed(
    user_id: int, workout_id: int, activity_id: int, completed_date: str,
    compliance: Optional[float], effective_ftp: Optional[float],
    path: Optional[str] = None,
) -> bool:
    conn = connect(path)
    try:
        cur = conn.execute(
            "UPDATE standalone_workouts SET completed_activity_id=?,completed_date=?,"
            "compliance=?,effective_ftp=? WHERE user_id=? AND id=? "
            "AND completed_activity_id IS NULL "
            "AND NOT EXISTS (SELECT 1 FROM plan_workouts "
            "WHERE user_id=? AND completed_activity_id=?) "
            "AND NOT EXISTS (SELECT 1 FROM standalone_workouts "
            "WHERE user_id=? AND completed_activity_id=?)",
            (
                activity_id, completed_date, compliance, effective_ftp,
                user_id, workout_id, user_id, activity_id, user_id, activity_id,
            ),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def standalone_workouts_for_month(
    user_id: int, year: int, month: int, path: Optional[str] = None,
) -> List[dict]:
    prefix = f"{int(year):04d}-{int(month):02d}"
    conn = connect(path)
    try:
        rows = conn.execute(
            "SELECT * FROM standalone_workouts WHERE user_id=? "
            "AND scheduled_date LIKE ? ORDER BY scheduled_date,id",
            (user_id, prefix + "%"),
        ).fetchall()
        return [_standalone_row(r) for r in rows]
    finally:
        conn.close()


def pending_ratings(user_id: int, path: Optional[str] = None) -> List[dict]:
    """Completed workouts that still require a perceived-effort rating."""
    conn = connect(path)
    try:
        rows = conn.execute(
            "SELECT 'plan' AS kind,id,date AS scheduled_date,name,type "
            "FROM plan_workouts WHERE user_id=? AND completed_activity_id IS NOT NULL "
            "AND rpe IS NULL UNION ALL "
            "SELECT 'standalone',id,scheduled_date,name,type FROM standalone_workouts "
            "WHERE user_id=? AND completed_activity_id IS NOT NULL AND rpe IS NULL "
            "ORDER BY scheduled_date,id", (user_id, user_id),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def set_standalone_rpe(
    user_id: int, workout_id: int, rpe: int, path: Optional[str] = None,
) -> bool:
    conn = connect(path)
    try:
        cur = conn.execute(
            "UPDATE standalone_workouts SET rpe=? WHERE user_id=? AND id=? "
            "AND completed_activity_id IS NOT NULL",
            (int(rpe), user_id, workout_id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def set_activity_rpe(
    user_id: int, activity_id: int, rpe: int, path: Optional[str] = None,
) -> bool:
    """Set a subjective perceived-exertion rating directly on an activity.

    Used only for rides not matched to a verified plan/standalone workout;
    matched rides route their rating through the workout so it feeds the FTP
    loop. Scoped to the user; returns True when a row was updated.
    """
    conn = connect(path)
    try:
        cur = conn.execute(
            "UPDATE activities SET rpe=? WHERE user_id=? AND id=?",
            (int(rpe), user_id, activity_id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def linked_workout_for_activity(
    user_id: int, activity_id: int, path: Optional[str] = None,
) -> Optional[dict]:
    """The plan/standalone workout this activity completes, if any.

    Returns {"kind","id","name","rpe"} for the workout whose
    completed_activity_id is this activity (plan checked first), else None.
    Verification/eligibility is decided by the caller.
    """
    conn = connect(path)
    try:
        row = conn.execute(
            "SELECT id, name, rpe FROM plan_workouts "
            "WHERE user_id=? AND completed_activity_id=? LIMIT 1",
            (user_id, activity_id),
        ).fetchone()
        if row:
            return {"kind": "plan", "id": row["id"], "name": row["name"],
                    "rpe": row["rpe"]}
        row = conn.execute(
            "SELECT id, name, rpe FROM standalone_workouts "
            "WHERE user_id=? AND completed_activity_id=? LIMIT 1",
            (user_id, activity_id),
        ).fetchone()
        if row:
            return {"kind": "standalone", "id": row["id"], "name": row["name"],
                    "rpe": row["rpe"]}
        return None
    finally:
        conn.close()


def get_standalone_workout(
    user_id: int, workout_id: int, path: Optional[str] = None,
) -> Optional[dict]:
    conn = connect(path)
    try:
        row = conn.execute(
            "SELECT * FROM standalone_workouts WHERE user_id=? AND id=?",
            (user_id, workout_id),
        ).fetchone()
        return _standalone_row(row, True) if row else None
    finally:
        conn.close()


def unused_feedback_evidence(
    user_id: int, since_date: str, path: Optional[str] = None,
) -> List[dict]:
    conn = connect(path)
    try:
        rows = conn.execute(
            "SELECT 'plan' AS kind,id,completed_date,type,rpe,compliance,effective_ftp "
            "FROM plan_workouts WHERE user_id=? AND completed_date>=? "
            "AND rpe IS NOT NULL AND feedback_batch_id IS NULL AND type IN "
            "('threshold','sweet_spot','vo2max') AND compliance IS NOT NULL "
            "AND effective_ftp IS NOT NULL UNION ALL "
            "SELECT 'standalone',id,completed_date,type,rpe,compliance,effective_ftp "
            "FROM standalone_workouts WHERE user_id=? AND completed_date>=? "
            "AND rpe IS NOT NULL AND feedback_batch_id IS NULL AND type IN "
            "('threshold','sweet_spot','vo2max') AND compliance IS NOT NULL "
            "AND effective_ftp IS NOT NULL ORDER BY completed_date,id",
            (user_id, since_date, user_id, since_date),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def apply_feedback_batch(
    user_id: int, ftp_date: str, updated_ftp: float, delta: float,
    evidence: List[dict],
    path: Optional[str] = None,
) -> Optional[int]:
    """Atomically adjust estimated FTP, record the batch, and consume evidence."""
    conn = connect(path)
    try:
        updated = conn.execute(
            "UPDATE ftp_history SET ftp_watts=? WHERE user_id=? AND date=? "
            "AND source='estimated'",
            (float(updated_ftp), user_id, ftp_date),
        )
        if updated.rowcount == 0:
            conn.rollback()
            return None
        cur = conn.execute(
            "INSERT INTO ftp_feedback_batches(user_id,ftp_date,delta,created) "
            "VALUES (?,?,?,?)",
            (user_id, ftp_date, float(delta), utc_now().isoformat()),
        )
        batch_id = int(cur.lastrowid)
        for kind in ("plan", "standalone"):
            ids = [int(e["id"]) for e in evidence if e["kind"] == kind]
            if not ids:
                continue
            table = "plan_workouts" if kind == "plan" else "standalone_workouts"
            marks = ",".join("?" for _ in ids)
            conn.execute(
                f"UPDATE {table} SET feedback_applied=1,feedback_batch_id=? "
                f"WHERE user_id=? AND id IN ({marks})",
                (batch_id, user_id, *ids),
            )
        conn.commit()
        return batch_id
    finally:
        conn.close()


def rollback_feedback_for_workout(
    user_id: int, kind: str, workout_id: int, path: Optional[str] = None,
) -> bool:
    """Undo the active batch containing a workout and release its evidence."""
    table = "plan_workouts" if kind == "plan" else "standalone_workouts"
    conn = connect(path)
    try:
        row = conn.execute(
            f"SELECT feedback_batch_id FROM {table} WHERE user_id=? AND id=?",
            (user_id, workout_id),
        ).fetchone()
        batch_id = row["feedback_batch_id"] if row else None
        if batch_id is None:
            return False
        batch = conn.execute(
            "SELECT * FROM ftp_feedback_batches WHERE id=? AND user_id=? AND applied=1",
            (batch_id, user_id),
        ).fetchone()
        if not batch:
            return False
        # The batch points at the exact estimated row it changed. Reversing
        # that row is safe even when a newer manual row/override now exists;
        # the source predicate guarantees manual FTP is never touched.
        conn.execute(
            "UPDATE ftp_history SET ftp_watts=ftp_watts-? "
            "WHERE user_id=? AND date=? AND source='estimated'",
            (batch["delta"], user_id, batch["ftp_date"]),
        )
        conn.execute(
            "UPDATE ftp_feedback_batches SET applied=0 WHERE id=? AND user_id=?",
            (batch_id, user_id),
        )
        for evidence_table in ("plan_workouts", "standalone_workouts"):
            conn.execute(
                f"UPDATE {evidence_table} SET feedback_applied=0,feedback_batch_id=NULL "
                "WHERE user_id=? AND feedback_batch_id=?", (user_id, batch_id),
            )
        conn.commit()
        return True
    finally:
        conn.close()


def activities_on_date(
    user_id: int, date_iso: str, path: Optional[str] = None
) -> List[dict]:
    """Activity summaries whose start_time falls on the given date."""
    conn = connect(path)
    try:
        rows = conn.execute(
            f"SELECT * FROM activities WHERE user_id = ? AND start_time LIKE ? "
            f"AND {_NOT_DUPLICATE} ORDER BY start_time ASC",
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
    variant: Optional[str] = None,
    path: Optional[str] = None,
) -> bool:
    """Rewrite an (unadapted) workout's prescription; records the adaptation.

    The ``adapted IS NULL`` guard is what makes adaptation once-only and stops
    plateau/overreach adjustments from stacking - do not relax it. Whole-plan
    recomputation uses ``replace_plan_workout_content`` instead, which has the
    opposite contract (it may claim an already-adapted row).
    """
    conn = connect(path)
    try:
        cur = conn.execute(
            "UPDATE plan_workouts SET name = ?, type = ?, duration_s = ?, "
            "tss = ?, zwo_or_segments = ?, adapted = ?, adapted_at = ?, "
            "variant = ? "
            "WHERE user_id = ? AND id = ? AND adapted IS NULL",
            (name, type, int(duration_s), float(tss), zwo_or_segments,
             adapted, adapted_at, variant, user_id, workout_id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def replace_plan_workout_content(
    user_id: int,
    workout_id: int,
    name: str,
    type: str,
    duration_s: int,
    tss: float,
    zwo_or_segments: str,
    after_date: str,
    variant: Optional[str] = None,
    path: Optional[str] = None,
) -> bool:
    """Overwrite a generated, future, not-completed workout from a recomputation.

    Counterpart to ``update_plan_workout_content``: that one is adapt.py's
    once-only patch, this one is whole-plan reflow, which is a pure function of
    the recipe and therefore safe to run any number of times. The guard here is
    about ownership (generated + future + not ridden), not about a budget.

    Claiming a row CLEARS ``adapted``/``adapted_at`` - the recomputed content
    replaces whatever adapt.py had put there, and the row's one-shot adaptation
    budget is handed back. See prescribe/reflow.py for why.
    """
    conn = connect(path)
    try:
        cur = conn.execute(
            "UPDATE plan_workouts SET name = ?, type = ?, duration_s = ?, "
            "tss = ?, zwo_or_segments = ?, variant = ?, "
            "adapted = NULL, adapted_at = NULL "
            "WHERE user_id = ? AND id = ? AND origin = 'generated' "
            "AND completed_activity_id IS NULL AND date > ?",
            (name, type, int(duration_s), float(tss), zwo_or_segments, variant,
             user_id, workout_id, after_date),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def delete_generated_plan_workout(
    user_id: int, workout_id: int, after_date: str, path: Optional[str] = None
) -> bool:
    """Drop a generated, future, not-completed workout (reflow's delete arm)."""
    conn = connect(path)
    try:
        cur = conn.execute(
            "DELETE FROM plan_workouts WHERE user_id = ? AND id = ? "
            "AND origin = 'generated' AND completed_activity_id IS NULL "
            "AND date > ?",
            (user_id, workout_id, after_date),
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
        # Preserve any ZwiftPower event id already stored for a race so a later
        # refresh whose incoming row omits it (auth payloads vary) never clobbers
        # it with NULL; incoming ids still win and backfill rows that lacked one.
        prior_zid = {
            (row["event_date"], row["event_title"]): row["zp_event_id"]
            for row in conn.execute(
                "SELECT event_date, event_title, zp_event_id FROM race_results "
                "WHERE user_id = ? AND source = ?",
                (user_id, source),
            )
        }
        conn.execute(
            "DELETE FROM race_results WHERE user_id = ? AND source = ?",
            (user_id, source),
        )
        for r in results:
            zp_event_id = r.get("zp_event_id") or prior_zid.get(
                (r["event_date"], r["event_title"])
            )
            conn.execute(
                """
                INSERT OR REPLACE INTO race_results
                  (user_id, source, event_date, event_title, position, category,
                   activity_id, duration_s, avg_power, avg_hr, max_hr, weight_kg,
                   np, if_, power_json, source_type, distance_km, zp_event_id,
                   fetched_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id, source, r["event_date"], r["event_title"],
                    r.get("position"), r.get("category"), r.get("activity_id"),
                    r.get("duration_s"), r.get("avg_power"), r.get("avg_hr"),
                    r.get("max_hr"), r.get("weight_kg"), r.get("np"), r.get("if_"),
                    json.dumps(r.get("power") or {}), r.get("source_type"),
                    r.get("distance_km"), zp_event_id, r["fetched_at"],
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
                last_refresh or utc_now().isoformat(timespec="seconds"),
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
             utc_now().isoformat(timespec="seconds")),
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


# ------------------------------------------------------- race dates (intent)
# NOTE: these operate on `race_dates` (FUTURE races the plan bends around), not
# on `race_results` (PAST results cached from ZwiftPower). See the DDL comment.

RACE_PRIORITIES = ("A", "B")


def _clean_priority(priority: str) -> str:
    """Normalise a priority to 'A' or 'B'; anything unknown becomes 'B'.

    'B' is the safe default: it only nudges the two adjacent days, whereas a
    wrongly-inferred 'A' would rewrite three weeks of a rider's plan.
    """
    p = (priority or "").strip().upper()
    return p if p in RACE_PRIORITIES else "B"


def add_race_date(
    user_id: int, date: str, priority: str = "B", name: Optional[str] = None,
    duration_min: Optional[int] = None, path: Optional[str] = None,
) -> int:
    """Add a planned race. Returns the new row id."""
    conn = connect(path)
    try:
        cur = conn.execute(
            "INSERT INTO race_dates "
            "(user_id, date, priority, name, duration_min, created) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, date, _clean_priority(priority), (name or None),
             int(duration_min) if duration_min else None,
             utc_now().isoformat(timespec="seconds")),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def list_race_dates(user_id: int, path: Optional[str] = None) -> List[dict]:
    conn = connect(path)
    try:
        rows = conn.execute(
            "SELECT id, date, priority, name, duration_min FROM race_dates "
            "WHERE user_id = ? ORDER BY date ASC, id ASC",
            (user_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def update_race_date(
    user_id: int, race_id: int, date: str, priority: str = "B",
    name: Optional[str] = None, duration_min: Optional[int] = None,
    path: Optional[str] = None,
) -> bool:
    """Rewrite a race. Returns False if it is not this user's race."""
    conn = connect(path)
    try:
        cur = conn.execute(
            "UPDATE race_dates SET date = ?, priority = ?, name = ?, "
            "duration_min = ? WHERE user_id = ? AND id = ?",
            (date, _clean_priority(priority), (name or None),
             int(duration_min) if duration_min else None, user_id, race_id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def delete_race_date(user_id: int, race_id: int, path: Optional[str] = None) -> bool:
    conn = connect(path)
    try:
        cur = conn.execute(
            "DELETE FROM race_dates WHERE user_id = ? AND id = ?",
            (user_id, race_id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def race_on(user_id: int, date_iso: str, path: Optional[str] = None) -> Optional[dict]:
    """The user's race on this date, or None. Earliest-added race wins a tie."""
    conn = connect(path)
    try:
        row = conn.execute(
            "SELECT id, date, priority, name, duration_min FROM race_dates "
            "WHERE user_id = ? AND date = ? ORDER BY id ASC LIMIT 1",
            (user_id, date_iso),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


# ------------------------------------------------------ rider profile
# Column order is the write order; ``computed_at`` is added by the writer.
RIDER_PROFILE_FIELDS = (
    "ftp", "weight_kg", "hr_max", "hr_max_source", "n_hr_activities",
    "cp", "wprime", "wprime_j_per_kg", "cp_w_per_kg",
    "peak_5s", "peak_60s", "peak_300s", "sprint_ratio", "vo2_ratio",
)


def save_rider_profile(
    user_id: int, values: dict, computed_at: Optional[str] = None,
    path: Optional[str] = None,
) -> None:
    """Store (or replace) a user's measured-capacity snapshot.

    ``values`` carries any subset of ``RIDER_PROFILE_FIELDS``; anything absent
    is stored as NULL, which reads back as "unmeasured".
    """
    cols = ", ".join(("user_id", "computed_at") + RIDER_PROFILE_FIELDS)
    marks = ", ".join(["?"] * (2 + len(RIDER_PROFILE_FIELDS)))
    row = [user_id, computed_at or utc_now().isoformat(timespec="seconds")]
    row.extend(values.get(f) for f in RIDER_PROFILE_FIELDS)
    conn = connect(path)
    try:
        conn.execute(
            f"INSERT OR REPLACE INTO rider_profile ({cols}) VALUES ({marks})",
            row,
        )
        conn.commit()
    finally:
        conn.close()


def get_rider_profile(user_id: int, path: Optional[str] = None) -> Optional[dict]:
    """A user's stored profile as a dict (with ``computed_at``), or None.

    None means "never computed" - not "no capacity". Callers prescribe the
    population constants in that case and the next sweep fills the row in.
    """
    conn = connect(path)
    try:
        row = conn.execute(
            "SELECT * FROM rider_profile WHERE user_id = ?", (user_id,)
        ).fetchone()
        if row is None:
            return None
        out = {f: row[f] for f in RIDER_PROFILE_FIELDS}
        out["computed_at"] = row["computed_at"]
        return out
    finally:
        conn.close()


def races_in_range(
    user_id: int, start_date: str, end_date: str, path: Optional[str] = None
) -> List[dict]:
    """The user's races within an inclusive date range."""
    conn = connect(path)
    try:
        rows = conn.execute(
            "SELECT id, date, priority, name, duration_min FROM race_dates "
            "WHERE user_id = ? AND date >= ? AND date <= ? ORDER BY date ASC",
            (user_id, start_date, end_date),
        ).fetchall()
        return [dict(r) for r in rows]
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
