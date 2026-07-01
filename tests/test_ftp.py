"""Tests for per-user FTP history storage and the monthly auto-update logic."""
import datetime as dt

import pytest

from tranalyzer import db
from tranalyzer.ingest import importer


def _insert_activity(user_id, start_time, power_watts=300.0, seconds=1200):
    db.init_db()
    db.insert_activity(
        user_id,
        {
            "dedup_hash": f"h-{start_time}-{power_watts}",
            "filename": "a.fit",
            "start_time": start_time,
            "duration_s": seconds,
            "distance_m": 0.0,
            "avg_power": power_watts,
            "avg_hr": 0.0,
            "np": power_watts,
            "if_": 1.0,
            "tss": 100.0,
            "streams": {"power": [power_watts] * seconds},
        },
    )


def test_add_and_latest_ftp(user_id):
    db.add_ftp_entry(user_id, "2026-01-01", 240.0, "estimated")
    db.add_ftp_entry(user_id, "2026-02-01", 250.0, "manual")
    latest = db.latest_ftp(user_id)
    assert latest["date"] == "2026-02-01"
    assert latest["ftp_watts"] == pytest.approx(250.0)
    assert latest["source"] == "manual"


def test_estimated_does_not_overwrite_existing_date(user_id):
    db.add_ftp_entry(user_id, "2026-03-01", 260.0, "manual")
    db.add_ftp_entry(user_id, "2026-03-01", 999.0, "estimated")
    same_day = [r for r in db.ftp_history_list(user_id) if r["date"] == "2026-03-01"]
    assert len(same_day) == 1
    assert same_day[0]["source"] == "manual"
    assert same_day[0]["ftp_watts"] == pytest.approx(260.0)


def test_manual_overwrites_same_date(user_id):
    db.add_ftp_entry(user_id, "2026-04-01", 200.0, "estimated")
    db.add_ftp_entry(user_id, "2026-04-01", 275.0, "manual")
    rows = [r for r in db.ftp_history_list(user_id) if r["date"] == "2026-04-01"]
    assert len(rows) == 1
    assert rows[0]["source"] == "manual"
    assert rows[0]["ftp_watts"] == pytest.approx(275.0)


def test_update_not_due_when_recent(user_id):
    now = dt.datetime(2026, 7, 1, 12, 0)
    db.add_ftp_entry(user_id, now.date().isoformat(), 250.0, "manual")
    _insert_activity(user_id, now.isoformat())
    assert importer.ftp_update_due(user_id, now) is False
    assert importer.maybe_update_ftp(user_id, now) is False
    assert len(db.ftp_history_list(user_id)) == 1


def test_update_due_after_30_days_appends_row(user_id):
    now = dt.datetime(2026, 7, 1, 12, 0)
    last = (now - dt.timedelta(days=31)).date().isoformat()
    db.add_ftp_entry(user_id, last, 200.0, "estimated")
    _insert_activity(user_id, (now - dt.timedelta(days=5)).isoformat(), power_watts=300.0)

    assert importer.ftp_update_due(user_id, now) is True
    assert importer.maybe_update_ftp(user_id, now) is True

    latest = db.latest_ftp(user_id)
    assert latest["date"] == now.date().isoformat()
    assert latest["source"] == "estimated"
    assert latest["ftp_watts"] == pytest.approx(285.0, abs=0.5)


def test_update_seeds_first_entry_when_empty(user_id):
    now = dt.datetime(2026, 7, 1, 12, 0)
    _insert_activity(user_id, (now - dt.timedelta(days=2)).isoformat(), power_watts=300.0)
    assert db.latest_ftp(user_id) is None
    assert importer.maybe_update_ftp(user_id, now) is True
    assert db.latest_ftp(user_id)["ftp_watts"] == pytest.approx(285.0, abs=0.5)


def test_current_ftp_precedence(user_id):
    now = dt.datetime(2026, 7, 1, 12, 0)
    _insert_activity(user_id, (now - dt.timedelta(days=3)).isoformat(), power_watts=300.0)
    # No history, no override -> estimate.
    assert importer.current_ftp(user_id, now=now) == pytest.approx(285.0, abs=0.5)
    # A manual history row now wins.
    db.add_ftp_entry(user_id, now.date().isoformat(), 260.0, "manual")
    assert importer.current_ftp(user_id, now=now) == pytest.approx(260.0)


def test_settings_ftp_override_used_before_estimate(user_id):
    now = dt.datetime(2026, 7, 1, 12, 0)
    _insert_activity(user_id, (now - dt.timedelta(days=3)).isoformat(), power_watts=300.0)
    db.save_user_settings(user_id, {"ftp": 270.0})
    assert importer.current_ftp(user_id, now=now) == pytest.approx(270.0)


def test_ftp_history_isolated_per_user():
    from tranalyzer import auth

    db.init_db()
    a = db.create_user("alice", auth.hash_password("password123"))
    b = db.create_user("bob", auth.hash_password("password123"))
    db.add_ftp_entry(a, "2026-05-01", 300.0, "manual")
    assert db.latest_ftp(a)["ftp_watts"] == pytest.approx(300.0)
    assert db.latest_ftp(b) is None
    assert db.ftp_history_list(b) == []


def test_estimate_ftp_tz_aware_start_time_does_not_crash():
    # Regression: FIT timestamps are tz-aware (UTC) while the window cutoff is
    # naive; the trailing-window filter must not raise TypeError.
    from tranalyzer.metrics.power import estimate_ftp

    acts = [{"start_time": "2026-06-20T10:00:00+00:00",
             "streams": {"power": [200] * 1300}}]
    assert round(estimate_ftp(acts, window_days=42), 1) == 190.0
