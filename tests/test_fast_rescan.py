"""Fast incremental rescan: skip-without-parse, post-scan gating, v12 migration."""
import sqlite3
import time

import pytest

import tranalyzer.ingest.importer as importer
from tranalyzer import db


def _fake_parsed(start_time="2026-06-01T10:00:00", seconds=1800, watts=200.0):
    return {
        "start_time": start_time,
        "duration_s": seconds,
        "streams": {
            "time": [None] * seconds,
            "power": [watts] * seconds,
            "heartrate": [140.0] * seconds,
            "cadence": [90.0] * seconds,
            "distance": list(range(seconds)),
            "altitude": [0.0] * seconds,
        },
    }


@pytest.fixture()
def uid():
    from tranalyzer import auth
    db.init_db()
    return db.create_user("rider", auth.hash_password("password123"))


def test_recorded_files_are_skipped_without_parsing(uid, tmp_path, monkeypatch):
    act_dir = tmp_path / "Activities"
    act_dir.mkdir()
    (act_dir / "ride.fit").write_bytes(b"dummy")

    calls = {"n": 0}

    def counting(path):
        calls["n"] += 1
        return _fake_parsed()

    monkeypatch.setattr(importer, "parse_fit", counting)

    r1 = importer.scan_activities(uid, directory=str(act_dir))
    assert r1["imported"] == 1
    assert calls["n"] == 1  # parsed once on first scan

    # Second scan: file already recorded, unchanged mtime/size -> no parse.
    r2 = importer.scan_activities(uid, directory=str(act_dir))
    assert r2["imported"] == 0
    assert r2["skipped"] == 1
    assert calls["n"] == 1  # NOT reparsed


def test_duplicate_import_is_recorded_and_not_reparsed(uid, tmp_path, monkeypatch):
    # A file whose activity already exists (dup) must still be recorded so it is
    # never parsed again.
    act_dir = tmp_path / "Activities"
    act_dir.mkdir()
    (act_dir / "a.fit").write_bytes(b"dummy")
    (act_dir / "b.fit").write_bytes(b"dummy")
    calls = {"n": 0}

    def counting(path):
        calls["n"] += 1
        return _fake_parsed()  # identical -> same dedup hash -> b.fit is a dup

    monkeypatch.setattr(importer, "parse_fit", counting)

    r1 = importer.scan_activities(uid, directory=str(act_dir))
    assert r1["imported"] == 1 and r1["skipped"] == 1
    assert calls["n"] == 2  # both parsed once

    r2 = importer.scan_activities(uid, directory=str(act_dir))
    assert r2["skipped"] == 2 and r2["imported"] == 0
    assert calls["n"] == 2  # neither reparsed (dup was recorded too)


def test_changed_file_is_reprocessed(uid, tmp_path, monkeypatch):
    act_dir = tmp_path / "Activities"
    act_dir.mkdir()
    f = act_dir / "ride.fit"
    f.write_bytes(b"dummy")
    calls = {"n": 0}
    monkeypatch.setattr(
        importer, "parse_fit",
        lambda path: (calls.__setitem__("n", calls["n"] + 1) or _fake_parsed()),
    )

    importer.scan_activities(uid, directory=str(act_dir))
    assert calls["n"] == 1

    # Change size + mtime -> must reparse.
    time.sleep(0.01)
    f.write_bytes(b"dummy-changed")
    importer.scan_activities(uid, directory=str(act_dir))
    assert calls["n"] == 2


def test_post_scan_work_gated_on_new_imports(uid, tmp_path, monkeypatch):
    ev = {"n": 0}
    mp = {"n": 0}
    monkeypatch.setattr(
        importer, "evaluate_ftp",
        lambda *a, **k: (ev.__setitem__("n", ev["n"] + 1) or False),
    )
    monkeypatch.setattr(
        importer, "match_plan_completions",
        lambda *a, **k: (mp.__setitem__("n", mp["n"] + 1) or 0),
    )
    monkeypatch.setattr(importer, "parse_fit", lambda path: _fake_parsed())

    # Empty dir -> nothing imported -> FTP eval + matching NOT run.
    empty = tmp_path / "Empty"
    empty.mkdir()
    importer.scan_activities(uid, directory=str(empty))
    assert ev["n"] == 0 and mp["n"] == 0

    # A new file imported -> both run exactly once.
    act_dir = tmp_path / "Activities"
    act_dir.mkdir()
    (act_dir / "ride.fit").write_bytes(b"dummy")
    importer.scan_activities(uid, directory=str(act_dir))
    assert ev["n"] == 1 and mp["n"] == 1

    # Rescan with no new imports (already recorded) -> not run again.
    importer.scan_activities(uid, directory=str(act_dir))
    assert ev["n"] == 1 and mp["n"] == 1


def test_seen_files_helpers_roundtrip(uid):
    assert db.seen_files(uid) == {}
    db.record_scanned_file(uid, "/x/ride.fit", 123.5, 999)
    assert db.seen_files(uid) == {"/x/ride.fit": (123.5, 999)}
    # Upsert refreshes mtime/size in place.
    db.record_scanned_file(uid, "/x/ride.fit", 200.0, 1000)
    assert db.seen_files(uid) == {"/x/ride.fit": (200.0, 1000)}


def test_v12_migration_adds_scanned_files_and_preserves_data(tmp_path):
    p = str(tmp_path / "legacy.db")
    # Build a minimal pre-v12 (v11) database with real data.
    conn = sqlite3.connect(p)
    conn.execute(
        "CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "username TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL, created TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO users (username, password_hash, created) VALUES (?, ?, ?)",
        ("legacy", "hash", "2026-01-01T00:00:00"),
    )
    conn.execute("PRAGMA user_version = 11")
    conn.commit()
    conn.close()

    db.init_db(p)

    conn = sqlite3.connect(p)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 12
        # New table exists.
        tbl = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='scanned_files'"
        ).fetchone()
        assert tbl is not None
        # Existing data preserved (no drop/recreate).
        row = conn.execute("SELECT username FROM users").fetchone()
        assert row[0] == "legacy"
    finally:
        conn.close()
