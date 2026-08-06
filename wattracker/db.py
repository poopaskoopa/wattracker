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
import math
import os
import sqlite3
import zlib
from typing import Callable, Dict, Iterator, List, Optional, Sequence, Union

from .config import db_path
from .config import _restrict
from .paths import safe_zwift_id
from .timeutil import utc_now, valid_timezone

_log = logging.getLogger(__name__)

SCHEMA_VERSION = 29


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
    22: [
        # The unattended nightly reflow now leaves a note behind when it changed
        # a rider's upcoming workouts. NULL means "nothing to tell them", which
        # is the correct state for every plan that existed before this column.
        "ALTER TABLE plans ADD COLUMN reflow_notice TEXT",
    ],
    23: [
        # New table power_sample_corrections is created by _SCHEMA after
        # migrating. Activity stream BLOBs remain immutable.
    ],
    24: [
        "ALTER TABLE user_settings ADD COLUMN timezone TEXT",
    ],
    25: [
        # Per-user bearer token for the read-only /calendar.ics feed. Only the
        # sha256 hex digest is stored - a phone calendar client sends the
        # plaintext in a URL, so the database must not be a second copy of it
        # (same reasoning as password_hash). NULL means "no feed link yet".
        # The UNIQUE constraint cannot ride on ALTER TABLE ADD COLUMN in
        # SQLite; _SCHEMA creates idx_users_calendar_token_hash right after
        # this runs, and a UNIQUE INDEX permits many NULLs, which is what we
        # want for the users who never generate a link.
        "ALTER TABLE users ADD COLUMN calendar_token_hash TEXT",
    ],
    26: [
        # The FTP a plan workout's fractions were generated against, so a
        # completion match can sanity-check the wattage it fitted - standalone
        # exports have carried this since they existed, plan rows never did,
        # which left plan completions (the primary evidence for RPE -> FTP
        # feedback) on the loose absolute 50-600W fallback.
        # Deliberately NOT backfilled: today's FTP is not the FTP a row from
        # six months ago was built at, and guessing one would retro-reject
        # completions that already matched. NULL keeps the old fallback.
        "ALTER TABLE plan_workouts ADD COLUMN export_ftp REAL",
    ],
    27: [
        # New table ftp_suggestions is created by _SCHEMA after migrating. It
        # holds what the RPE evidence implies for a rider who has set a manual
        # FTP - the one case where the feedback loop must never write the
        # training FTP itself. Nothing to backfill: the evidence that would
        # have produced past suggestions was never consumed, so the next
        # rating (or the next scan) computes one from it.
    ],
    28: [
        # The column default must match the fresh _SCHEMA (0 = onboarding not
        # done), because SQLite cannot ALTER a default later: whatever this
        # ALTER bakes into the users DDL is what a future column-omitting
        # INSERT would get on every upgraded install. Legacy semantics are
        # carried by the backfill below, not by the default - existing
        # accounts have already lived through the first-hour setup.
        "ALTER TABLE users ADD COLUMN onboarding_complete INTEGER NOT NULL DEFAULT 0",
        "UPDATE users SET onboarding_complete = 1",
    ],
}

_DROP = """
DROP TABLE IF EXISTS ftp_suggestions;
DROP TABLE IF EXISTS power_sample_corrections;
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
    created       TEXT NOT NULL,
    -- sha256 hex of the /calendar.ics feed token; never the token itself.
    calendar_token_hash TEXT,
    onboarding_complete INTEGER NOT NULL DEFAULT 0
);
-- UNIQUE, but as an index so the many NULLs (users without a feed link) are
-- allowed: SQLite only treats NULLs as distinct inside a unique index.
CREATE UNIQUE INDEX IF NOT EXISTS idx_users_calendar_token_hash
    ON users(calendar_token_hash);

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
    timezone           TEXT,
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

CREATE TABLE IF NOT EXISTS power_sample_corrections (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    activity_id INTEGER NOT NULL,
    start_index INTEGER NOT NULL,
    end_index   INTEGER NOT NULL,
    ftp_basis   REAL NOT NULL,
    original_avg_power REAL,
    original_np        REAL,
    original_if        REAL,
    original_tss       REAL,
    reason      TEXT,
    created     TEXT NOT NULL,
    undone_at   TEXT,
    CHECK(start_index >= 0),
    CHECK(end_index >= start_index),
    CHECK(end_index - start_index + 1 <= 3600),
    CHECK(ftp_basis > 0),
    FOREIGN KEY(user_id) REFERENCES users(id),
    FOREIGN KEY(activity_id) REFERENCES activities(id)
);
CREATE INDEX IF NOT EXISTS idx_power_corrections_active
    ON power_sample_corrections(user_id, activity_id, undone_at);
CREATE TRIGGER IF NOT EXISTS trg_power_correction_owner_insert
BEFORE INSERT ON power_sample_corrections
WHEN NOT EXISTS (
    SELECT 1 FROM activities
    WHERE id = NEW.activity_id AND user_id = NEW.user_id
)
BEGIN
    SELECT RAISE(ABORT, 'power correction activity ownership mismatch');
END;
CREATE TRIGGER IF NOT EXISTS trg_power_correction_owner_update
BEFORE UPDATE OF user_id, activity_id ON power_sample_corrections
WHEN NOT EXISTS (
    SELECT 1 FROM activities
    WHERE id = NEW.activity_id AND user_id = NEW.user_id
)
BEGIN
    SELECT RAISE(ABORT, 'power correction activity ownership mismatch');
END;

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
    -- Pending "the nightly sweep changed your plan" notice (JSON), cleared
    -- when the rider dismisses it. See set_plan_reflow_notice.
    reflow_notice TEXT,
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
    export_ftp        REAL,
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

-- What the RPE evidence implies for a rider whose training FTP is a manual
-- value. Nothing here changes any FTP on its own: the row is a proposal the
-- rider accepts (which writes their manual override) or dismisses. batch_id
-- ties it to the ftp_feedback_batch that consumed the evidence, so correcting
-- a rating retracts the suggestion along with its evidence.
CREATE TABLE IF NOT EXISTS ftp_suggestions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER NOT NULL,
    batch_id      INTEGER,
    current_ftp   REAL NOT NULL,
    suggested_ftp REAL NOT NULL,
    workouts      INTEGER NOT NULL,
    evidence      TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'pending',
    created       TEXT NOT NULL,
    resolved      TEXT,
    FOREIGN KEY(user_id) REFERENCES users(id)
);
CREATE INDEX IF NOT EXISTS idx_ftp_suggestions_user_status
    ON ftp_suggestions(user_id, status);

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
    """Create a user. Returns the new id, or None if the username is taken.

    onboarding_complete is always named explicitly. Databases upgraded before
    migration 28 was corrected still carry DEFAULT 1 in their users DDL (SQLite
    cannot ALTER a default), so an INSERT that omits the column would create a
    pre-onboarded account there. Every INSERT INTO users must supply it -
    test_onboarding.py enforces that against the source.
    """
    conn = connect(path)
    try:
        cur = conn.execute(
            "INSERT INTO users (username, password_hash, created, onboarding_complete) "
            "VALUES (?, ?, ?, 0)",
            (username, password_hash, utc_now().isoformat()),
        )
        conn.commit()
        return cur.lastrowid
    except sqlite3.IntegrityError:
        return None
    finally:
        conn.close()


def onboarding_complete(user_id: int, path: Optional[str] = None) -> bool:
    """Return whether this account has finished first-hour onboarding."""
    conn = connect(path)
    try:
        row = conn.execute(
            "SELECT onboarding_complete FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        return bool(row and row["onboarding_complete"])
    finally:
        conn.close()


def complete_onboarding(user_id: int, path: Optional[str] = None) -> bool:
    """Mark an existing account's onboarding as complete."""
    conn = connect(path)
    try:
        cur = conn.execute(
            "UPDATE users SET onboarding_complete = 1 WHERE id = ?", (user_id,)
        )
        conn.commit()
        return cur.rowcount > 0
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


# ------------------------------------------------- calendar feed token
# Only the sha256 hex digest of the token is ever stored or accepted here.
# Callers hash the plaintext (see calendarfeed.py); this layer never sees it.
def set_calendar_token_hash(
    user_id: int, token_hash: str, path: Optional[str] = None
) -> bool:
    """Store (or replace) a user's calendar-feed token hash.

    Replacing is the rotation primitive: one row per user, so writing a new
    hash makes the previous token unresolvable and therefore dead.
    Returns False if there is no such user.
    """
    if not token_hash:
        raise ValueError("calendar token hash must be non-empty")
    conn = connect(path)
    try:
        cur = conn.execute(
            "UPDATE users SET calendar_token_hash = ? WHERE id = ?",
            (token_hash, user_id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def get_calendar_token_hash(
    user_id: int, path: Optional[str] = None
) -> Optional[str]:
    conn = connect(path)
    try:
        row = conn.execute(
            "SELECT calendar_token_hash FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        return row["calendar_token_hash"] if row else None
    finally:
        conn.close()


def user_by_calendar_token_hash(
    token_hash: str, path: Optional[str] = None
) -> Optional[dict]:
    """Resolve a user from a calendar token hash, or None.

    An empty/None hash returns None without querying, so a user row whose
    calendar_token_hash is NULL can never be matched by a missing token.
    The caller must still re-verify the returned hash in constant time.
    """
    if not token_hash:
        return None
    conn = connect(path)
    try:
        row = conn.execute(
            "SELECT id, username, calendar_token_hash FROM users "
            "WHERE calendar_token_hash IS NOT NULL AND calendar_token_hash = ?",
            (token_hash,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


# -------------------------------------------------------------- settings
_SETTING_KEYS = ("ftp", "zwift_id", "activities_dir", "workouts_dir",
                 "zwift_email", "weight_kg", "hr_max", "timezone")


def get_user_settings(user_id: int, path: Optional[str] = None) -> dict:
    conn = connect(path)
    try:
        row = conn.execute(
            "SELECT ftp, zwift_id, activities_dir, workouts_dir, zwift_email, "
            "weight_kg, hr_max, timezone FROM user_settings WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        if not row:
            return {k: None for k in _SETTING_KEYS}
        return {k: row[k] for k in _SETTING_KEYS}
    finally:
        conn.close()


def save_user_settings(user_id: int, updates: dict, path: Optional[str] = None) -> dict:
    """Merge non-empty updates into a user's settings row (upsert).

    Two keys are filtered here rather than at each caller, because this is the
    single choke point every writer goes through: ``timezone`` (must be a real
    IANA zone) and ``zwift_id`` (becomes one folder name under the Zwift
    Workouts root, so it must not be able to traverse out of it). A rejected
    value leaves the stored one unchanged; routes that can talk to the user
    also check first, so they can say why.
    """
    current = get_user_settings(user_id, path=path)
    for key in _SETTING_KEYS:
        if key in updates and updates[key] not in (None, ""):
            value = updates[key]
            if key == "timezone":
                timezone = value.strip() if isinstance(value, str) else value
                if valid_timezone(timezone):
                    current[key] = timezone
            elif key == "zwift_id":
                safe = safe_zwift_id(str(value))
                if safe:
                    current[key] = safe
                else:
                    _log.warning("refusing to store unusable zwift_id: %r", value)
            else:
                current[key] = value
    conn = connect(path)
    try:
        conn.execute(
            """
            INSERT INTO user_settings
                (user_id, ftp, zwift_id, activities_dir, workouts_dir,
                 zwift_email, weight_kg, hr_max, timezone)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                ftp=excluded.ftp,
                zwift_id=excluded.zwift_id,
                activities_dir=excluded.activities_dir,
                workouts_dir=excluded.workouts_dir,
                zwift_email=excluded.zwift_email,
                weight_kg=excluded.weight_kg,
                hr_max=excluded.hr_max,
                timezone=excluded.timezone
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
                current["timezone"],
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


def _correction_ranges(
    conn: sqlite3.Connection, user_id: int, activity_ids: Sequence[int]
) -> Dict[int, List[tuple]]:
    """Active inclusive correction ranges, grouped by owned activity id."""
    if not activity_ids:
        return {}
    allowed = set(activity_ids)
    rows = conn.execute(
        "SELECT activity_id, start_index, end_index "
        "FROM power_sample_corrections "
        "WHERE user_id = ? AND undone_at IS NULL "
        "ORDER BY activity_id, start_index",
        (user_id,),
    ).fetchall()
    grouped: Dict[int, List[tuple]] = {}
    for row in rows:
        activity_id = int(row["activity_id"])
        if activity_id not in allowed:
            continue
        grouped.setdefault(activity_id, []).append(
            (int(row["start_index"]), int(row["end_index"]))
        )
    return grouped


def _effective_streams(blob: Optional[bytes], ranges: Sequence[tuple]) -> Dict[str, list]:
    """Inflate streams and mask corrected power samples without mutating storage."""
    streams = _unpack_streams(blob)
    if not isinstance(streams, dict):
        return streams
    power = streams.get("power")
    if not isinstance(power, list) or not ranges:
        return streams
    masked = list(power)
    for start, end in ranges:
        lo = max(0, int(start))
        hi = min(len(masked) - 1, int(end))
        if lo <= hi:
            masked[lo:hi + 1] = [None] * (hi - lo + 1)
    streams["power"] = masked
    return streams


def _activity_correction_ranges(
    conn: sqlite3.Connection, user_id: int, activity_id: int
) -> List[tuple]:
    rows = conn.execute(
        "SELECT start_index, end_index FROM power_sample_corrections "
        "WHERE user_id = ? AND activity_id = ? AND undone_at IS NULL "
        "ORDER BY start_index",
        (user_id, activity_id),
    ).fetchall()
    return [(int(row["start_index"]), int(row["end_index"])) for row in rows]


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


def _valid_activity_date(value: Optional[str]) -> bool:
    try:
        _dt.datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return False
    return True


def activities_for_ftp_rescore(
    user_id: int, activity_ids: Sequence[int], path: Optional[str] = None
) -> Iterator[List[dict]]:
    """Yield activity summaries in bounded batches with date-effective FTP."""
    ids = sorted({int(activity_id) for activity_id in activity_ids})
    if not ids:
        return
    conn = connect(path)
    try:
        for offset in range(0, len(ids), 500):
            chunk = ids[offset:offset + 500]
            placeholders = ",".join("?" for _ in chunk)
            rows = conn.execute(
                f"""
                SELECT
                  a.id,
                  a.start_time,
                  a.duration_s,
                  a.np,
                  COALESCE(
                    (
                      SELECT h.ftp_watts FROM ftp_history h
                      WHERE h.user_id = a.user_id
                        AND h.date <= substr(a.start_time, 1, 10)
                      ORDER BY h.date DESC LIMIT 1
                    ),
                    (
                      SELECT h.ftp_watts FROM ftp_history h
                      WHERE h.user_id = a.user_id
                      ORDER BY h.date ASC LIMIT 1
                    )
                  ) AS ftp_watts
                FROM activities a
                WHERE a.user_id = ? AND a.id IN ({placeholders})
                """,
                (user_id, *chunk),
            ).fetchall()
            yield [
                {
                    "id": row["id"],
                    "start_time": row["start_time"],
                    "duration_s": row["duration_s"],
                    "np": row["np"],
                    "ftp_watts": row["ftp_watts"],
                }
                for row in rows
                if _valid_activity_date(row["start_time"])
            ]
    finally:
        conn.close()


def update_activity_ftp_metrics(
    user_id: int, summaries: Sequence[dict], path: Optional[str] = None
) -> int:
    """Batch update FTP-dependent stored metrics for owned activities."""
    rows = [
        (summary.get("if_"), summary.get("tss"), user_id, int(summary["id"]))
        for summary in summaries
    ]
    if not rows:
        return 0
    conn = connect(path)
    try:
        cur = conn.executemany(
            "UPDATE activities SET if_ = ?, tss = ? WHERE user_id = ? AND id = ?",
            rows,
        )
        conn.commit()
        return cur.rowcount
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
POWER_CORRECTION_MAX_SAMPLES = 3600
POWER_CORRECTION_REASON_MAX_LENGTH = 500


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
        ranges = _correction_ranges(conn, user_id, [activity_id]).get(activity_id, [])
        d["streams"] = _effective_streams(row["streams"], ranges)
        return d
    finally:
        conn.close()


def recent_power_streams(user_id: int, days: int = 90, path: Optional[str] = None) -> List[List[float]]:
    cutoff = (utc_now() - _dt.timedelta(days=days)).isoformat()
    conn = connect(path)
    try:
        rows = conn.execute(
            "SELECT id, streams FROM activities "
            f"WHERE user_id = ? AND start_time >= ? AND {_NOT_DUPLICATE} "
            "ORDER BY start_time",
            (user_id, cutoff),
        ).fetchall()
        out: List[List[float]] = []
        ranges = _correction_ranges(conn, user_id, [int(r["id"]) for r in rows])
        for r in rows:
            streams = _effective_streams(
                r["streams"], ranges.get(int(r["id"]), [])
            )
            power = streams.get("power") if isinstance(streams, dict) else None
            power = power or []
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
        ranges = _correction_ranges(conn, user_id, [int(r["id"]) for r in rows])
        out = []
        for r in rows:
            d = _row_summary(r)
            d["streams"] = _effective_streams(
                r["streams"], ranges.get(int(r["id"]), [])
            )
            out.append(d)
        return out
    finally:
        conn.close()


def iter_full_activities_desc(
    user_id: int, path: Optional[str] = None
) -> Iterator[dict]:
    """Yield effective full-resolution activities newest first, one at a time."""
    conn = connect(path)
    try:
        rows = conn.execute(
            f"SELECT * FROM activities WHERE user_id = ? AND {_NOT_DUPLICATE} "
            "ORDER BY start_time DESC",
            (user_id,),
        )
        for row in rows:
            activity_id = int(row["id"])
            activity = _row_summary(row)
            activity["streams"] = _effective_streams(
                row["streams"],
                _activity_correction_ranges(conn, user_id, activity_id),
            )
            yield activity
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
        ranges = _correction_ranges(conn, user_id, [int(r["id"]) for r in rows])
        out = []
        for r in rows:
            d = _row_summary(r)
            d["streams"] = _effective_streams(
                r["streams"], ranges.get(int(r["id"]), [])
            )
            out.append(d)
        return out
    finally:
        conn.close()


# ---------------------------------------------- power sample corrections
def list_power_corrections(
    user_id: int, active_only: bool = False, path: Optional[str] = None
) -> List[dict]:
    """Audited corrections owned by ``user_id``, newest first."""
    conn = connect(path)
    try:
        active_sql = " AND c.undone_at IS NULL" if active_only else ""
        rows = conn.execute(
            "SELECT c.*, a.filename, a.start_time "
            "FROM power_sample_corrections c "
            "JOIN activities a ON a.id = c.activity_id AND a.user_id = c.user_id "
            f"WHERE c.user_id = ?{active_sql} ORDER BY c.created DESC, c.id DESC",
            (user_id,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def power_correction_activity(
    user_id: int, activity_id: int, path: Optional[str] = None
) -> Optional[dict]:
    """Owned activity with immutable raw streams and active correction ranges."""
    conn = connect(path)
    try:
        row = conn.execute(
            "SELECT * FROM activities WHERE user_id = ? AND id = ?",
            (user_id, activity_id),
        ).fetchone()
        if row is None:
            return None
        result = _row_summary(row)
        result["raw_streams"] = _unpack_streams(row["streams"])
        result["corrections"] = _correction_ranges(
            conn, user_id, [activity_id]
        ).get(activity_id, [])
        provenance = conn.execute(
            "SELECT ftp_basis, original_avg_power, original_np, original_if, "
            "original_tss FROM power_sample_corrections "
            "WHERE user_id = ? AND activity_id = ? AND undone_at IS NULL "
            "ORDER BY id LIMIT 1",
            (user_id, activity_id),
        ).fetchone()
        result["correction_ftp_basis"] = (
            float(provenance["ftp_basis"]) if provenance is not None else None
        )
        result["correction_original_summary"] = (
            {
                "avg_power": provenance["original_avg_power"],
                "np": provenance["original_np"],
                "if_": provenance["original_if"],
                "tss": provenance["original_tss"],
            }
            if provenance is not None else None
        )
        return result
    finally:
        conn.close()


def apply_power_correction(
    user_id: int,
    activity_id: int,
    start_index: int,
    end_index: int,
    ftp_basis: float,
    reason: Optional[str],
    summary: dict,
    expected_ranges: Optional[Sequence[tuple]] = None,
    path: Optional[str] = None,
) -> Optional[int]:
    """Atomically add an active correction and refresh power-derived summary.

    Returns the audit-row id. Invalid, overlapping, overlarge, or non-owned
    requests return ``None`` without changing any data.
    """
    if (
        isinstance(start_index, bool)
        or isinstance(end_index, bool)
        or not isinstance(start_index, int)
        or not isinstance(end_index, int)
        or start_index < 0
        or end_index < start_index
        or end_index - start_index + 1 > POWER_CORRECTION_MAX_SAMPLES
    ):
        return None
    try:
        ftp_basis = float(ftp_basis)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(ftp_basis) or ftp_basis <= 0:
        return None
    if reason is not None and not isinstance(reason, str):
        return None
    cleaned_reason = (reason or "").strip() or None
    if cleaned_reason and len(cleaned_reason) > POWER_CORRECTION_REASON_MAX_LENGTH:
        return None
    conn = connect(path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT streams, avg_power, np, if_, tss FROM activities "
            "WHERE user_id = ? AND id = ?",
            (user_id, activity_id),
        ).fetchone()
        streams = _unpack_streams(row["streams"]) if row is not None else None
        power = streams.get("power") if isinstance(streams, dict) else None
        if not isinstance(power, list) or end_index >= len(power):
            conn.rollback()
            return None
        current_ranges = _correction_ranges(conn, user_id, [activity_id]).get(
            activity_id, []
        )
        if expected_ranges is not None and list(expected_ranges) != current_ranges:
            conn.rollback()
            return None
        overlap = conn.execute(
            "SELECT 1 FROM power_sample_corrections "
            "WHERE user_id = ? AND activity_id = ? AND undone_at IS NULL "
            "AND NOT (end_index < ? OR start_index > ?)",
            (user_id, activity_id, start_index, end_index),
        ).fetchone()
        if overlap:
            conn.rollback()
            return None
        inconsistent_basis = conn.execute(
            "SELECT 1 FROM power_sample_corrections "
            "WHERE user_id = ? AND activity_id = ? AND undone_at IS NULL "
            "AND ftp_basis != ?",
            (user_id, activity_id, ftp_basis),
        ).fetchone()
        if inconsistent_basis:
            conn.rollback()
            return None
        existing_provenance = conn.execute(
            "SELECT original_avg_power, original_np, original_if, original_tss "
            "FROM power_sample_corrections "
            "WHERE user_id = ? AND activity_id = ? AND undone_at IS NULL "
            "ORDER BY id LIMIT 1",
            (user_id, activity_id),
        ).fetchone()
        if existing_provenance is None:
            original_summary = (
                row["avg_power"], row["np"], row["if_"], row["tss"]
            )
        else:
            original_summary = (
                existing_provenance["original_avg_power"],
                existing_provenance["original_np"],
                existing_provenance["original_if"],
                existing_provenance["original_tss"],
            )
        cur = conn.execute(
            "INSERT INTO power_sample_corrections "
            "(user_id, activity_id, start_index, end_index, ftp_basis, "
            "original_avg_power, original_np, original_if, original_tss, "
            "reason, created) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                user_id,
                activity_id,
                start_index,
                end_index,
                ftp_basis,
                *original_summary,
                cleaned_reason,
                utc_now().isoformat(timespec="seconds"),
            ),
        )
        conn.execute(
            "UPDATE activities SET avg_power = ?, np = ?, if_ = ?, tss = ? "
            "WHERE user_id = ? AND id = ?",
            (
                summary.get("avg_power"),
                summary.get("np"),
                summary.get("if_"),
                summary.get("tss"),
                user_id,
                activity_id,
            ),
        )
        conn.commit()
        return int(cur.lastrowid)
    except sqlite3.Error:
        conn.rollback()
        raise
    finally:
        conn.close()


def undo_power_correction(
    user_id: int,
    correction_id: int,
    summary: dict,
    expected_ranges: Optional[Sequence[tuple]] = None,
    path: Optional[str] = None,
) -> bool:
    """Atomically retire one owned correction and refresh its activity summary."""
    conn = connect(path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT activity_id, original_avg_power, original_np, original_if, "
            "original_tss FROM power_sample_corrections "
            "WHERE id = ? AND user_id = ? AND undone_at IS NULL",
            (correction_id, user_id),
        ).fetchone()
        if row is None:
            conn.rollback()
            return False
        activity_id = int(row["activity_id"])
        current_ranges = _correction_ranges(conn, user_id, [activity_id]).get(
            activity_id, []
        )
        if expected_ranges is not None and list(expected_ranges) != current_ranges:
            conn.rollback()
            return False
        if len(current_ranges) == 1:
            summary = {
                "avg_power": row["original_avg_power"],
                "np": row["original_np"],
                "if_": row["original_if"],
                "tss": row["original_tss"],
            }
        cur = conn.execute(
            "UPDATE power_sample_corrections SET undone_at = ? "
            "WHERE id = ? AND user_id = ? AND undone_at IS NULL",
            (utc_now().isoformat(timespec="seconds"), correction_id, user_id),
        )
        if cur.rowcount != 1:
            conn.rollback()
            return False
        conn.execute(
            "UPDATE activities SET avg_power = ?, np = ?, if_ = ?, tss = ? "
            "WHERE user_id = ? AND id = ?",
            (
                summary.get("avg_power"),
                summary.get("np"),
                summary.get("if_"),
                summary.get("tss"),
                user_id,
                activity_id,
            ),
        )
        conn.commit()
        return True
    except sqlite3.Error:
        conn.rollback()
        raise
    finally:
        conn.close()


def power_correction_fingerprint(
    user_id: int, path: Optional[str] = None
) -> tuple:
    """Persisted state token that changes on every apply and undo."""
    conn = connect(path)
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS c, COALESCE(MAX(id), 0) AS m, "
            "SUM(undone_at IS NOT NULL) AS u "
            "FROM power_sample_corrections WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        return int(row["c"]), int(row["m"]), int(row["u"] or 0)
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
    *,
    replace_existing: bool = False,
) -> None:
    """Append/update an FTP history row for (user, date).

    - source='manual' replaces any existing row for that date.
    - source='estimated' inserts only if no row exists (never overwrites manual),
      unless ``replace_existing`` is explicitly requested by an onboarding
      choice that replaces a previously entered value.
    """
    conn = connect(path)
    try:
        if source == "manual" or replace_existing:
            conn.execute(
                "INSERT OR REPLACE INTO ftp_history (user_id, date, ftp_watts, source) "
                "VALUES (?, ?, ?, ?)",
                (user_id, date, float(ftp_watts), source),
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
_PLAN_COLUMNS = ("id, name, start_date, weeks, created, model, recipe, active, "
                 "reflow_notice")


def _plan_row(r: sqlite3.Row) -> dict:
    """Plan row with the JSON columns parsed back and active as a bool.

    A recipe that fails to parse is surfaced as None - i.e. the plan degrades
    to "legacy, not reflowable" rather than blowing up a page render. The
    pending reflow notice degrades the same way: an unreadable notice is no
    notice, never an exception on the calendar.
    """
    d = dict(r)
    for key in ("recipe", "reflow_notice"):
        raw = d.get(key)
        if raw:
            try:
                parsed = json.loads(raw)
            except (ValueError, TypeError):
                parsed = None
            d[key] = parsed if isinstance(parsed, dict) else None
        else:
            d[key] = None
    d["active"] = bool(d.get("active"))
    return d


def set_plan_reflow_notice(
    user_id: int, plan_id: int, notice: Optional[dict],
    path: Optional[str] = None,
) -> bool:
    """Record (or clear, with None) the pending "your plan changed" notice.

    The unattended nightly sweep rewrites upcoming workouts; a rewrite the rider
    is never told about is indistinguishable from us quietly changing their
    training. The notice is stored on the plan rather than flashed through the
    URL because the sweep runs with nobody watching - the rider has to be able
    to find out hours later, on whichever page they open first.
    """
    conn = connect(path)
    try:
        cur = conn.execute(
            "UPDATE plans SET reflow_notice = ? WHERE user_id = ? AND id = ?",
            (json.dumps(notice) if notice is not None else None,
             user_id, plan_id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


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
    export_ftp: Optional[float] = None,
    path: Optional[str] = None,
) -> int:
    conn = connect(path)
    try:
        cur = conn.execute(
            """
            INSERT INTO plan_workouts
              (plan_id, user_id, date, name, type, duration_s, tss,
               zwo_or_segments, variant, origin, export_ftp)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (plan_id, user_id, date, name, type, int(duration_s), float(tss),
             zwo_or_segments, variant, origin,
             float(export_ftp) if export_ftp else None),
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
        "export_ftp": r["export_ftp"],
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


def plan_workouts_in_range(
    user_id: int, start_date: str, end_date: str, path: Optional[str] = None
) -> List[dict]:
    """Plan workouts with ``start_date <= date <= end_date``, user-scoped.

    Dates are stored as ISO 'YYYY-MM-DD' text, so a lexicographic BETWEEN is
    the same as a calendar comparison.
    """
    conn = connect(path)
    try:
        rows = conn.execute(
            "SELECT * FROM plan_workouts WHERE user_id = ? "
            "AND date >= ? AND date <= ? ORDER BY date ASC, id ASC",
            (user_id, start_date, end_date),
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


def standalone_workouts_in_range(
    user_id: int, start_date: str, end_date: str, path: Optional[str] = None
) -> List[dict]:
    """One-off workouts scheduled within [start_date, end_date], user-scoped."""
    conn = connect(path)
    try:
        rows = conn.execute(
            "SELECT * FROM standalone_workouts WHERE user_id=? "
            "AND scheduled_date >= ? AND scheduled_date <= ? "
            "ORDER BY scheduled_date ASC, id ASC",
            (user_id, start_date, end_date),
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
            # zwo travels with the evidence so the feedback loop can read how
            # much time in zone was actually prescribed (see
            # importer._neutral_rpe) without a second query per workout.
            "SELECT 'plan' AS kind,id,completed_date,type,rpe,compliance,"
            "effective_ftp,zwo_or_segments AS zwo "
            "FROM plan_workouts WHERE user_id=? AND completed_date>=? "
            "AND rpe IS NOT NULL AND feedback_batch_id IS NULL AND type IN "
            "('threshold','sweet_spot','vo2max') AND compliance IS NOT NULL "
            "AND effective_ftp IS NOT NULL UNION ALL "
            "SELECT 'standalone',id,completed_date,type,rpe,compliance,effective_ftp,zwo "
            "FROM standalone_workouts WHERE user_id=? AND completed_date>=? "
            "AND rpe IS NOT NULL AND feedback_batch_id IS NULL AND type IN "
            "('threshold','sweet_spot','vo2max') AND compliance IS NOT NULL "
            "AND effective_ftp IS NOT NULL ORDER BY completed_date,id",
            (user_id, since_date, user_id, since_date),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _consume_evidence(
    conn: sqlite3.Connection, user_id: int, batch_id: int, evidence: List[dict]
) -> None:
    """Attach every rated workout in `evidence` to `batch_id` (same tx)."""
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
        _consume_evidence(conn, user_id, batch_id, evidence)
        conn.commit()
        return batch_id
    finally:
        conn.close()


def record_ftp_suggestion(
    user_id: int, ftp_date: str, current_ftp: float,
    suggested_ftp: Optional[float], evidence: List[dict],
    summary: Optional[List[dict]] = None,
    path: Optional[str] = None,
) -> Optional[int]:
    """Consume evidence WITHOUT touching any FTP, recording what it implies.

    The manual-FTP counterpart of ``apply_feedback_batch``. It writes the same
    reversible batch (delta 0.0 - there is nothing to reverse, because no FTP
    row was changed) so a corrected rating releases this evidence through the
    existing rollback path, and files the implied number as a suggestion the
    rider can accept or dismiss. ``suggested_ftp`` of None (or a value equal to
    the current FTP) consumes the evidence with nothing to show for it, which
    is the honest outcome when the evidence implies no change.

    Returns the suggestion id, or None when there was no suggestion to file.
    """
    conn = connect(path)
    try:
        cur = conn.execute(
            "INSERT INTO ftp_feedback_batches(user_id,ftp_date,delta,created) "
            "VALUES (?,?,0.0,?)",
            (user_id, ftp_date, utc_now().isoformat()),
        )
        batch_id = int(cur.lastrowid)
        _consume_evidence(conn, user_id, batch_id, evidence)
        suggestion_id = None
        if suggested_ftp is not None and abs(
            float(suggested_ftp) - float(current_ftp)
        ) >= 0.1:
            # One live suggestion per rider: newer evidence supersedes the
            # older proposal rather than queueing a second banner.
            conn.execute(
                "UPDATE ftp_suggestions SET status='superseded',resolved=? "
                "WHERE user_id=? AND status='pending'",
                (utc_now().isoformat(), user_id),
            )
            cur = conn.execute(
                "INSERT INTO ftp_suggestions(user_id,batch_id,current_ftp,"
                "suggested_ftp,workouts,evidence,status,created) "
                "VALUES (?,?,?,?,?,?,'pending',?)",
                (
                    user_id, batch_id, float(current_ftp), float(suggested_ftp),
                    len(evidence), json.dumps(summary or []),
                    utc_now().isoformat(),
                ),
            )
            suggestion_id = int(cur.lastrowid)
        conn.commit()
        return suggestion_id
    finally:
        conn.close()


def _suggestion_row(row: sqlite3.Row) -> dict:
    out = dict(row)
    try:
        out["evidence"] = json.loads(out.get("evidence") or "[]")
    except (TypeError, ValueError):
        out["evidence"] = []
    return out


def pending_ftp_suggestion(
    user_id: int, path: Optional[str] = None
) -> Optional[dict]:
    """The rider's live FTP suggestion, if any."""
    conn = connect(path)
    try:
        row = conn.execute(
            "SELECT * FROM ftp_suggestions WHERE user_id=? AND status='pending' "
            "ORDER BY id DESC LIMIT 1",
            (user_id,),
        ).fetchone()
        return _suggestion_row(row) if row else None
    finally:
        conn.close()


def resolve_ftp_suggestion(
    user_id: int, suggestion_id: int, status: str, path: Optional[str] = None
) -> Optional[dict]:
    """Close a pending suggestion as 'accepted' or 'dismissed'.

    Returns the row that was closed (so the caller can act on the number it
    proposed), or None when it was not this user's, or already resolved. The
    evidence stays consumed either way - a dismissed suggestion must not come
    straight back from the same workouts.
    """
    if status not in ("accepted", "dismissed"):
        return None
    conn = connect(path)
    try:
        row = conn.execute(
            "SELECT * FROM ftp_suggestions WHERE id=? AND user_id=? "
            "AND status='pending'",
            (suggestion_id, user_id),
        ).fetchone()
        if not row:
            return None
        conn.execute(
            "UPDATE ftp_suggestions SET status=?,resolved=? WHERE id=? AND user_id=?",
            (status, utc_now().isoformat(), suggestion_id, user_id),
        )
        conn.commit()
        return _suggestion_row(row)
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
        # A suggestion is only as good as the ratings behind it; releasing its
        # evidence retracts it, and the recomputation that follows the
        # correction files an up-to-date one.
        conn.execute(
            "UPDATE ftp_suggestions SET status='retracted',resolved=? "
            "WHERE user_id=? AND batch_id=? AND status='pending'",
            (utc_now().isoformat(), user_id, batch_id),
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
    export_ftp: Optional[float] = None,
    path: Optional[str] = None,
) -> bool:
    """Rewrite an (unadapted) workout's prescription; records the adaptation.

    The ``adapted IS NULL`` guard is what makes adaptation once-only and stops
    plateau/overreach adjustments from stacking - do not relax it. Whole-plan
    recomputation uses ``replace_plan_workout_content`` instead, which has the
    opposite contract (it may claim an already-adapted row).

    ``export_ftp`` is restamped with the content: it describes the FTP the new
    fractions were written for, so leaving the old one behind would leave the
    completion matcher checking fitted wattage against an FTP that no longer
    belongs to the prescription stored beside it.
    """
    conn = connect(path)
    try:
        cur = conn.execute(
            "UPDATE plan_workouts SET name = ?, type = ?, duration_s = ?, "
            "tss = ?, zwo_or_segments = ?, adapted = ?, adapted_at = ?, "
            "variant = ?, export_ftp = ? "
            "WHERE user_id = ? AND id = ? AND adapted IS NULL",
            (name, type, int(duration_s), float(tss), zwo_or_segments,
             adapted, adapted_at, variant,
             float(export_ftp) if export_ftp else None, user_id, workout_id),
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
    export_ftp: Optional[float] = None,
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

    ``export_ftp`` is restamped with the content, for the same reason as in
    ``update_plan_workout_content``: it must keep describing the fractions it
    is stored next to.
    """
    conn = connect(path)
    try:
        cur = conn.execute(
            "UPDATE plan_workouts SET name = ?, type = ?, duration_s = ?, "
            "tss = ?, zwo_or_segments = ?, variant = ?, export_ftp = ?, "
            "adapted = NULL, adapted_at = NULL "
            "WHERE user_id = ? AND id = ? AND origin = 'generated' "
            "AND completed_activity_id IS NULL AND date > ?",
            (name, type, int(duration_s), float(tss), zwo_or_segments, variant,
             float(export_ftp) if export_ftp else None,
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


def _race_result_row(row) -> dict:
    """A race_results row as a dict, with power_json decoded into ``power``."""
    d = dict(row)
    try:
        d["power"] = json.loads(d.pop("power_json") or "{}")
    except ValueError:
        d["power"] = {}
    return d


def race_results_on_date(
    user_id: int, date_iso: str, path: Optional[str] = None
) -> List[dict]:
    """Cached race results (any source) for this user on one date.

    Used to resolve a planned ``race_dates`` row against the past results
    cached from ZwiftPower/local heuristics at read time - see
    ``races.match_result_for_race_date``. Resolving a whole calendar's worth
    of planned races goes through ``race_results_on_dates`` instead, so the
    render costs one query rather than one per race.
    """
    return race_results_on_dates(user_id, [date_iso], path).get(date_iso, [])


def race_results_on_dates(
    user_id: int, dates: Sequence[str], path: Optional[str] = None
) -> Dict[str, List[dict]]:
    """Cached race results for this user on any of ``dates``, grouped by date.

    Each date's list keeps ``id ASC`` order, which the read-time matcher in
    ``races`` relies on as its final, deterministic tie-break.
    """
    wanted = sorted({d for d in dates if d})
    if not wanted:
        return {}
    out: Dict[str, List[dict]] = {}
    conn = connect(path)
    try:
        # Chunked so a rider with an implausibly long race list can still
        # never exceed SQLite's bound-parameter ceiling.
        for start in range(0, len(wanted), 400):
            chunk = wanted[start:start + 400]
            placeholders = ",".join("?" * len(chunk))
            rows = conn.execute(
                "SELECT * FROM race_results WHERE user_id = ? "
                f"AND event_date IN ({placeholders}) ORDER BY id ASC",
                (user_id, *chunk),
            ).fetchall()
            for r in rows:
                d = _race_result_row(r)
                out.setdefault(d["event_date"], []).append(d)
    finally:
        conn.close()
    return out


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
