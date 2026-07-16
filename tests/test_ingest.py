"""Tests for the importer using a mocked FIT parser (no real .fit needed)."""
import tranalyzer.ingest.importer as importer
from tranalyzer import auth, db


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


def test_ingest_and_dedup(monkeypatch, user_id):
    monkeypatch.setattr(importer, "parse_fit", lambda path: _fake_parsed())

    first = importer.ingest_file(user_id, "dummy.fit")
    assert first is not None

    acts = db.list_activities(user_id)
    assert len(acts) == 1
    assert acts[0]["np"] == 200.0
    assert acts[0]["tss"] > 0

    # Re-ingesting the same (start_time, duration) is idempotent.
    second = importer.ingest_file(user_id, "dummy.fit")
    assert second is None
    assert len(db.list_activities(user_id)) == 1


def test_dedup_same_start_time_different_duration(monkeypatch, user_id):
    # A ride captured in two files (Zwift temp vs. final .fit) shares the exact
    # start second but differs in duration, so the dedup_hash differs. The
    # start_time dedup must still reject the second file.
    monkeypatch.setattr(
        importer, "parse_fit",
        lambda path: _fake_parsed(start_time="2026-07-15T22:43:06", seconds=1847),
    )
    first = importer.ingest_file(user_id, "2026-07-15-18-39-02.fit")
    assert first is not None

    monkeypatch.setattr(
        importer, "parse_fit",
        lambda path: _fake_parsed(start_time="2026-07-15T22:43:06", seconds=1809),
    )
    second = importer.ingest_file(user_id, "inProgressActivity.fit")
    assert second is None
    assert len(db.list_activities(user_id)) == 1


def test_daily_tss_aggregation(monkeypatch, user_id):
    monkeypatch.setattr(
        importer,
        "parse_fit",
        lambda path: _fake_parsed(start_time="2026-06-02T09:00:00"),
    )
    importer.ingest_file(user_id, "d2.fit")
    tss = db.daily_tss(user_id)
    import datetime as dt

    assert dt.date(2026, 6, 2) in tss
    assert tss[dt.date(2026, 6, 2)] > 0


def test_activities_isolated_between_users(monkeypatch):
    db.init_db()
    a = db.create_user("alice", auth.hash_password("password123"))
    b = db.create_user("bob", auth.hash_password("password123"))
    monkeypatch.setattr(importer, "parse_fit", lambda path: _fake_parsed())

    importer.ingest_file(a, "dummy.fit")
    assert len(db.list_activities(a)) == 1
    # Bob sees nothing from Alice.
    assert db.list_activities(b) == []
    assert db.daily_tss(b) == {}
