"""Rides recorded twice (in-app + Zwift .fit) are linked and counted once."""
import contextlib
import datetime as dt
import os
import sqlite3
import time

import pytest

from wattracker import db
from wattracker.ble.runner import RideController
from wattracker.ingest import importer
from wattracker.prescribe.planner import Segment, Session


@contextlib.contextmanager
def _timezone(name):
    """Run a block under a fixed system timezone (restored afterwards)."""
    previous = os.environ.get("TZ")
    os.environ["TZ"] = name
    time.tzset()
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = previous
        time.tzset()


def _session(name="Endurance Negative Split", duration=20):
    return Session(
        name=name,
        description="",
        workout_type="endurance",
        segments=[Segment(kind="steadystate", duration=duration, power=0.6)],
    )


def _insert(user_id, filename, start_time, duration_s, avg_power=135.0, tss=60.0,
            rpe=None):
    """Insert an activity row directly, bypassing duplicate detection."""
    activity_id = db.insert_activity(
        user_id,
        {
            "dedup_hash": f"h-{filename}-{start_time}",
            "filename": filename,
            "start_time": start_time,
            "duration_s": duration_s,
            "distance_m": 0.0,
            "avg_power": avg_power,
            "avg_hr": 0.0,
            "np": avg_power,
            "if_": 0.7,
            "tss": tss,
            "streams": {"power": [avg_power] * 10},
        },
    )
    if rpe is not None:
        db.set_activity_rpe(user_id, activity_id, rpe)
    return activity_id


# The real pair from the user's database: one ride, two records.
IN_APP = ("Ride 2026-07-24 Endurance Negative Split",
          "2026-07-24T23:45:43.351308", 5216, 135.73, 63.3)
FIT = ("2026-07-24-19-45-13.fit", "2026-07-24T23:45:42", 5401, 134.25, 64.7)


def _seed_real_pair(user_id):
    in_app = _insert(user_id, *IN_APP)
    fit = _insert(user_id, *FIT)
    return in_app, fit


# --------------------------------------------------------------- write path
def test_in_app_ride_is_stored_as_utc_with_a_local_filename_date(user_id):
    with _timezone("America/New_York"):
        controller = RideController(
            _session(), 200, user_id=user_id, start_grace_s=0, autosave=True
        )
        for _ in range(20):
            controller.tick(power=150, dt=1)

        assert controller.status == "finished"
        activity = db.list_activities(user_id)[0]
        started = dt.datetime.fromisoformat(activity["start_time"])
        utc_now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
        assert abs((started - utc_now).total_seconds()) < 120
        # ... and it is NOT the local wall clock (the old, skewed behavior).
        assert abs((started - dt.datetime.now()).total_seconds()) > 3600
        # The rider names the ride by the day they rode it, so the filename
        # keeps the local date.
        local_date = dt.datetime.now().date().isoformat()
        assert activity["filename"] == f"Ride {local_date} Endurance Negative Split"


def test_in_app_ride_links_to_an_already_imported_fit(monkeypatch, user_id):
    started = dt.datetime(2026, 7, 24, 23, 45, 42)
    _insert(user_id, "2026-07-24-19-45-13.fit", started.isoformat(), 1400, 150.0)

    controller = RideController(
        _session(duration=1300), 200, user_id=user_id,
        started_at=started + dt.timedelta(seconds=30),
        start_grace_s=0, autosave=True,
    )
    for _ in range(1300):
        controller.tick(power=150, dt=1)

    assert controller.status == "finished"
    assert len(db.list_activities(user_id)) == 1  # the .fit, counted once


# ---------------------------------------------------------------- migration
def _v18_database(path, rows):
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE activities (id INTEGER PRIMARY KEY, user_id INTEGER, "
        "dedup_hash TEXT, filename TEXT, start_time TEXT, duration_s INTEGER, "
        "tss REAL, rpe INTEGER);"
        "CREATE TABLE plan_workouts (id INTEGER PRIMARY KEY, user_id INTEGER, "
        "date TEXT, completed_activity_id INTEGER);"
        "CREATE TABLE standalone_workouts (id INTEGER PRIMARY KEY, "
        "user_id INTEGER, scheduled_date TEXT, completed_activity_id INTEGER);"
    )
    conn.executemany(
        "INSERT INTO activities (id, user_id, dedup_hash, filename, start_time, "
        "duration_s, tss) VALUES (?, 1, 'h' || ?, ?, ?, 3600, 50)",
        [(i, i, name, start) for i, (name, start) in enumerate(rows, start=1)],
    )
    conn.execute("PRAGMA user_version = 18")
    conn.commit()
    conn.close()


def test_v19_backfills_in_app_rides_to_utc_across_dst(tmp_path):
    path = str(tmp_path / "old.db")
    _v18_database(path, [
        ("Ride 2025-01-15 Winter", "2025-01-15T19:00:00"),        # EST, -5
        ("Ride 2025-07-15 Summer", "2025-07-15T19:00:00.500000"), # EDT, -4
        ("2025-07-15-19-00-00.fit", "2025-07-15T23:00:00"),       # already UTC
    ])
    with _timezone("America/New_York"):
        db.init_db(path)

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    starts = [r["start_time"] for r in
              conn.execute("SELECT start_time FROM activities ORDER BY id")]
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    conn.close()

    assert version == 19
    assert starts[0] == "2025-01-16T00:00:00"          # +5h in winter
    assert starts[1] == "2025-07-15T23:00:00.500000"   # +4h in summer, precision kept
    assert starts[2] == "2025-07-15T23:00:00"          # imported row untouched


def test_v19_migration_is_not_applied_twice(tmp_path):
    path = str(tmp_path / "old.db")
    _v18_database(path, [("Ride 2025-07-15 Summer", "2025-07-15T19:00:00")])
    with _timezone("America/New_York"):
        db.init_db(path)
        db.init_db(path)

    conn = sqlite3.connect(path)
    start = conn.execute("SELECT start_time FROM activities").fetchone()[0]
    conn.close()
    assert start == "2025-07-15T23:00:00"


# ------------------------------------------------------------ match rules
def test_cross_source_pair_is_linked_fit_first(user_id):
    in_app, fit = _seed_real_pair(user_id)
    assert importer.link_duplicate_activity(user_id, fit) == in_app
    assert db.get_activity(user_id, in_app)["duplicate_of"] == fit
    assert db.get_activity(user_id, fit)["duplicate_of"] is None


def test_two_fit_files_minutes_apart_are_never_linked(user_id):
    # Two genuinely separate rides recorded to two .fit files on the same
    # afternoon; only the cross-source rule keeps these apart.
    first = _insert(user_id, "2025-05-30-14-00-00.fit", "2025-05-30T18:00:00", 243)
    second = _insert(user_id, "2025-05-30-14-04-00.fit", "2025-05-30T18:04:00", 220)
    assert importer.link_duplicate_activity(user_id, second) is None
    assert db.get_activity(user_id, first)["duplicate_of"] is None
    assert db.get_activity(user_id, second)["duplicate_of"] is None

    # Same source, seconds apart, near-identical duration: still not a pair.
    third = _insert(user_id, "2025-05-30-14-00-30.fit", "2025-05-30T18:00:30", 240)
    assert importer.link_duplicate_activity(user_id, third) is None
    assert db.get_activity(user_id, third)["duplicate_of"] is None


def test_two_in_app_rides_are_never_linked(user_id):
    _insert(user_id, "Ride 2026-07-24 One", "2026-07-24T23:45:43", 5216)
    second = _insert(user_id, "Ride 2026-07-24 Two", "2026-07-24T23:46:10", 5300)
    assert importer.link_duplicate_activity(user_id, second) is None


@pytest.mark.parametrize("start,duration,power", [
    ("2026-07-25T00:05:00", 5401, 134.25),  # 19 minutes apart
    ("2026-07-24T23:45:42", 3000, 134.25),  # duration 42% short
    ("2026-07-24T23:45:42", 5401, 180.0),   # avg power 25% higher
])
def test_near_misses_are_not_linked(user_id, start, duration, power):
    in_app = _insert(user_id, *IN_APP)
    fit = _insert(user_id, "2026-07-24-19-45-13.fit", start, duration, power)
    assert importer.link_duplicate_activity(user_id, fit) is None
    assert db.get_activity(user_id, in_app)["duplicate_of"] is None


def test_missing_power_falls_back_to_time_and_duration(user_id):
    in_app = _insert(user_id, *IN_APP)
    fit = _insert(user_id, "2026-07-24-19-45-13.fit", "2026-07-24T23:45:42",
                  5401, avg_power=0.0)
    assert importer.link_duplicate_activity(user_id, fit) == in_app


def test_fit_import_links_the_in_app_ride(monkeypatch, user_id):
    in_app = _insert(user_id, *IN_APP)
    monkeypatch.setattr(importer, "parse_fit", lambda path: {
        "start_time": "2026-07-24T23:45:42",
        "duration_s": 5401,
        "streams": {"power": [134.25] * 5401, "heartrate": [], "cadence": [],
                    "distance": list(range(5401)), "altitude": []},
    })
    fit = importer.ingest_file(user_id, "/tmp/2026-07-24-19-45-13.fit")
    assert db.get_activity(user_id, in_app)["duplicate_of"] == fit


# ------------------------------------------------------- carry-over to primary
def test_rpe_and_plan_link_move_to_the_primary(user_id):
    in_app = _insert(user_id, *IN_APP[:4], rpe=4)
    fit = _insert(user_id, *FIT)
    plan_id = db.create_plan(user_id, "Plan", "2026-07-24", 1)
    workout_id = db.add_plan_workout(
        plan_id, user_id, "2026-07-24", "Endurance", "endurance", 5400, 60.0,
        "<workout_file/>",
    )
    assert db.mark_plan_workout_completed(
        user_id, workout_id, in_app, "2026-07-24"
    )

    assert importer.link_duplicate_activity(user_id, fit) == in_app
    assert db.get_activity(user_id, fit)["rpe"] == 4
    assert db.get_plan_workout(user_id, workout_id)["completed_activity_id"] == fit


def test_primary_keeps_its_own_rpe_and_existing_workout_link(user_id):
    in_app = _insert(user_id, *IN_APP[:4], rpe=4)
    fit = _insert(user_id, *FIT[:4], rpe=7)
    plan_id = db.create_plan(user_id, "Plan", "2026-07-24", 1)
    other = db.add_plan_workout(
        plan_id, user_id, "2026-07-24", "Other", "endurance", 5400, 60.0,
        "<workout_file/>",
    )
    mine = db.add_plan_workout(
        plan_id, user_id, "2026-07-24", "Mine", "endurance", 5400, 60.0,
        "<workout_file/>",
    )
    db.mark_plan_workout_completed(user_id, other, fit, "2026-07-24")
    db.mark_plan_workout_completed(user_id, mine, in_app, "2026-07-24")

    assert importer.link_duplicate_activity(user_id, fit) == in_app
    assert db.get_activity(user_id, fit)["rpe"] == 7
    # The primary is already consumed by another workout, so the secondary's
    # link stays put rather than breaking the one-workout-per-activity rule.
    assert db.get_plan_workout(user_id, mine)["completed_activity_id"] == in_app


# ------------------------------------------------------------- aggregations
def test_linked_duplicate_is_counted_once_everywhere(user_id):
    in_app, fit = _seed_real_pair(user_id)
    importer.link_duplicate_activity(user_id, fit)

    day = dt.date(2026, 7, 24)
    assert db.daily_tss(user_id) == {day: pytest.approx(64.7)}
    weeks = db.weekly_volume(user_id)
    assert len(weeks) == 1
    assert weeks[0]["tss"] == pytest.approx(64.7)
    assert weeks[0]["hours"] == pytest.approx(5401 / 3600.0, abs=0.01)
    assert [a["id"] for a in db.full_activities(user_id)] == [fit]
    assert [a["id"] for a in db.list_activities(user_id)] == [fit]
    assert [a["id"] for a in db.activities_on_date(user_id, "2026-07-24")] == [fit]
    assert db.recent_power_streams(user_id, days=36500) != []
    assert len(db.recent_power_streams(user_id, days=36500)) == 1
    # The secondary is still readable on its own detail page.
    assert db.get_activity(user_id, in_app) is not None


def test_recent_full_activities_excludes_the_duplicate(user_id):
    started = dt.datetime.now() - dt.timedelta(hours=2)
    in_app = _insert(user_id, "Ride 2026-07-24 Endurance",
                     started.isoformat(), 5216, 135.73)
    fit = _insert(user_id, "recent.fit",
                  (started - dt.timedelta(seconds=1)).isoformat(), 5401, 134.25)
    importer.link_duplicate_activity(user_id, fit)
    assert [a["id"] for a in db.recent_full_activities(user_id, 7)] == [fit]
    assert in_app not in {a["id"] for a in db.recent_full_activities(user_id, 7)}


# ----------------------------------------------------------------- backfill
def test_backfill_links_historical_pairs_and_is_idempotent(user_id):
    in_app, fit = _seed_real_pair(user_id)
    solo = _insert(user_id, "2026-07-20-08-00-00.fit", "2026-07-20T12:00:00", 3600)

    assert importer.backfill_duplicate_links(user_id) == 1
    assert db.get_activity(user_id, in_app)["duplicate_of"] == fit
    assert db.get_activity(user_id, solo)["duplicate_of"] is None
    assert importer.backfill_duplicate_links(user_id) == 0


def test_backfill_pairs_each_ride_at_most_once(user_id):
    first = _insert(user_id, "Ride 2026-07-24 One", "2026-07-24T23:45:43", 5216)
    second = _insert(user_id, "Ride 2026-07-24 Two", "2026-07-24T23:45:50", 5216)
    fit = _insert(user_id, *FIT)

    assert importer.backfill_duplicate_links(user_id) == 1
    linked = [a for a in (first, second)
              if db.get_activity(user_id, a)["duplicate_of"] == fit]
    assert len(linked) == 1


def test_activities_page_merges_and_flags_the_pair():
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    from wattracker.server import create_app

    with TestClient(create_app()) as client:
        client.post("/register", data={"username": "tester",
                                       "password": "password123"})
        uid = db.get_user_by_username("tester")["id"]
        in_app, fit = _seed_real_pair(uid)

        page = client.post("/activities/link-duplicates").text
        assert "1 ride(s) recorded both in-app and by Zwift" in page
        assert "also recorded in-app" in page
        assert f'/activity/{in_app}"' not in page
        assert f'/activity/{fit}"' in page
        assert db.get_activity(uid, in_app)["duplicate_of"] == fit


def test_is_in_app_activity_recognizes_both_sources():
    assert importer.is_in_app_activity("Ride 2026-07-24 Endurance")
    assert not importer.is_in_app_activity("2026-07-24-19-45-13.fit")
    assert not importer.is_in_app_activity("Ride the Rockies.fit")
    assert not importer.is_in_app_activity("")
    assert not importer.is_in_app_activity(None)
