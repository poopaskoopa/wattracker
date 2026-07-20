"""Tests for backup creation / listing / pruning and the pre-migration hook."""
import datetime as dt
import os
import sqlite3

import pytest

from wattracker import backup, db
from wattracker.config import db_path


def _make_backup(reason, ts):
    """Write a valid-looking backup file directly, with a chosen timestamp."""
    name = f"wattracker-{ts.strftime('%Y%m%d-%H%M%S')}-{reason}.db"
    path = os.path.join(backup.backups_dir(), name)
    with open(path, "wb") as f:
        f.write(b"x" * 16)
    return path


def test_create_backup_copies_live_data():
    db.init_db()
    uid = db.create_user("alice", "hash")
    path = backup.create_backup("manual")
    assert os.path.exists(path)
    assert os.path.basename(path).startswith("wattracker-")
    assert path.endswith("-manual.db")
    # The snapshot is a real, queryable copy holding the live row.
    conn = sqlite3.connect(path)
    try:
        row = conn.execute(
            "SELECT username FROM users WHERE id=?", (uid,)
        ).fetchone()
    finally:
        conn.close()
    assert row[0] == "alice"


def test_create_backup_rejects_unknown_reason():
    db.init_db()
    with pytest.raises(ValueError):
        backup.create_backup("whatever")


def test_list_backups_newest_first_and_ignores_junk():
    db.init_db()
    base = dt.datetime(2026, 1, 1, 12, 0, 0)
    _make_backup("manual", base)
    _make_backup("daily", base + dt.timedelta(hours=1))
    # Junk files that must not appear in the listing.
    d = backup.backups_dir()
    open(os.path.join(d, "notes.txt"), "w").close()
    open(os.path.join(d, "wattracker-bad.db"), "w").close()
    listed = backup.list_backups()
    assert len(listed) == 2
    assert listed[0]["reason"] == "daily"  # newest first
    assert listed[1]["reason"] == "manual"
    assert all("size" in b and b["size"] >= 0 for b in listed)


def test_prune_keeps_cap_per_reason():
    db.init_db()
    base = dt.datetime(2026, 1, 1, 0, 0, 0)
    # daily cap is 10, manual cap is 5.
    for i in range(13):
        _make_backup("daily", base + dt.timedelta(minutes=i))
    for i in range(8):
        _make_backup("manual", base + dt.timedelta(hours=i))
    backup.prune()
    listed = backup.list_backups()
    dailies = [b for b in listed if b["reason"] == "daily"]
    manuals = [b for b in listed if b["reason"] == "manual"]
    assert len(dailies) == backup.RETENTION["daily"] == 10
    assert len(manuals) == backup.RETENTION["manual"] == 5
    # The survivors are the newest ones.
    assert dailies[0]["timestamp"] == base + dt.timedelta(minutes=12)


def test_create_backup_runs_prune():
    db.init_db()
    base = dt.datetime(2025, 1, 1, 0, 0, 0)
    for i in range(5):  # manual cap is 5, already full with old files
        _make_backup("manual", base + dt.timedelta(minutes=i))
    # A real create pushes to 6, then prune drops back to 5.
    backup.create_backup("manual")
    manuals = [b for b in backup.list_backups() if b["reason"] == "manual"]
    assert len(manuals) == 5
    # The freshly-created (real, now) one survived; an old placeholder was culled.
    assert manuals[0]["timestamp"].year == dt.datetime.now().year


def test_create_daily_if_due_at_most_once_per_day():
    db.init_db()
    first = backup.create_daily_if_due()
    assert first is not None
    # A second call moments later must not create another daily backup.
    assert backup.create_daily_if_due() is None
    dailies = [b for b in backup.list_backups() if b["reason"] == "daily"]
    assert len(dailies) == 1
    # But once the existing one is > 23h old, a new one is due.
    now_later = dt.datetime.now() + dt.timedelta(hours=24)
    assert backup.create_daily_if_due(now=now_later) is not None
    dailies = [b for b in backup.list_backups() if b["reason"] == "daily"]
    assert len(dailies) == 2


# ------------------------------------------------------- pre-migration hook
def _make_old_db(path, version):
    """A minimal DB stamped at an older, migratable schema version."""
    conn = sqlite3.connect(path)
    try:
        conn.executescript(db._SCHEMA)
        conn.execute(f"PRAGMA user_version = {version}")
        conn.commit()
    finally:
        conn.close()


def test_pre_migration_backup_fires_on_upgrade(monkeypatch):
    path = db_path()
    _make_old_db(path, db.SCHEMA_VERSION - 1)
    db.init_db()
    reasons = [b["reason"] for b in backup.list_backups()]
    assert "pre-migration" in reasons
    # And the DB was actually migrated up to current.
    conn = sqlite3.connect(path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    finally:
        conn.close()


def test_pre_migration_backup_failure_aborts_migration(monkeypatch):
    path = db_path()
    _make_old_db(path, db.SCHEMA_VERSION - 1)

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(backup, "create_backup", boom)
    with pytest.raises(OSError):
        db.init_db()
    # Migration must NOT have proceeded: version is unchanged.
    conn = sqlite3.connect(path)
    try:
        assert (
            conn.execute("PRAGMA user_version").fetchone()[0]
            == db.SCHEMA_VERSION - 1
        )
    finally:
        conn.close()


def test_fresh_db_skips_pre_migration_backup():
    # No existing DB file -> version 0 -> fresh create, no backup taken.
    db.init_db()
    assert [b for b in backup.list_backups() if b["reason"] == "pre-migration"] == []
