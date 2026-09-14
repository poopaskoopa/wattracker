"""Tests: the v36 -> v37 backfill that restates completed_date locally.

The old matcher stored the scheduled day it matched on, which came from a
naive UTC-prefix comparison; the current matcher stores the linked activity's
rider-local date, and that is what plan_workout_completion_verified checks
against. For a non-UTC rider the two disagree, so legacy completions silently
read as unverified. The migration corrects the stored value.
"""
import datetime as dt
import sqlite3

import wattracker.ingest.importer as importer
from wattracker import auth, config, db
from wattracker.prescribe import zwo
from wattracker.prescribe.planner import build_workout


def _rider(username, timezone):
    uid = db.create_user(username, auth.hash_password("password123"))
    db.save_user_settings(uid, {"timezone": timezone})
    return uid


def _activity(user_id, start_time, power, suffix):
    """A ride whose power stream traces *power* exactly (so compliance is 1.0)."""
    return db.insert_activity(user_id, {
        "dedup_hash": f"{user_id}-{suffix}",
        "filename": f"{suffix}.fit",
        "start_time": start_time,
        "duration_s": len(power),
        "distance_m": 0.0,
        "avg_power": sum(power) / len(power),
        "avg_hr": None,
        "np": None,
        "if_": None,
        "tss": 60.0,
        "streams": {"power": power},
    })


def _legacy_plan_completion(user_id, scheduled, start_time, stored_date, suffix):
    """A profile-backed plan completion written the way the old matcher wrote it."""
    session = build_workout("threshold", 60)
    xml = zwo.zwo_string(session)
    plan_id = db.create_plan(user_id, suffix, scheduled, 1)
    workout_id = db.add_plan_workout(
        plan_id, user_id, scheduled, session.name, "threshold",
        session.total_duration(), session.estimated_tss, xml,
    )
    profile = importer._zwo_fraction_profile(xml)
    activity_id = _activity(
        user_id, start_time, [p * 220.0 for p in profile], suffix
    )
    assert db.mark_plan_workout_completed(
        user_id, workout_id, activity_id, stored_date, .95, 220.0
    )
    return workout_id, activity_id


def _legacy_standalone_completion(user_id, scheduled, start_time, stored_date,
                                  suffix):
    session = build_workout("threshold", 60)
    xml = zwo.zwo_string(session)
    workout_id = db.add_standalone_workout(
        user_id, suffix, scheduled, session.name, "threshold",
        session.total_duration(), session.estimated_tss, xml, 220.0,
    )
    profile = importer._zwo_fraction_profile(xml)
    activity_id = _activity(
        user_id, start_time, [p * 220.0 for p in profile], suffix
    )
    assert db.mark_standalone_completed(
        user_id, workout_id, activity_id, stored_date, .95, 220.0
    )
    return workout_id, activity_id


def _rewind_and_migrate():
    """Put the live test DB back at v36 and run the upgrade over it."""
    conn = sqlite3.connect(config.db_path())
    conn.execute("PRAGMA user_version = 36")
    conn.commit()
    conn.close()
    db.init_db()
    conn = sqlite3.connect(config.db_path())
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    finally:
        conn.close()


def _completed_dates(table):
    conn = sqlite3.connect(config.db_path())
    try:
        return {
            row[0]: row[1]
            for row in conn.execute(f"SELECT id, completed_date FROM {table}")
        }
    finally:
        conn.close()


def test_evening_ride_of_negative_offset_rider_is_restated_and_verifies(user_id):
    # UTC-4 (EDT): a 21:30 local ride on the 15th is stored as 01:30 UTC on the
    # 16th, and the old matcher filed it under the UTC day.
    db.save_user_settings(user_id, {"timezone": "America/New_York"})
    workout_id, _ = _legacy_plan_completion(
        user_id, "2026-07-15", "2026-07-16T01:30:00", "2026-07-16", "evening"
    )
    assert not importer.plan_workout_completion_verified(
        user_id, db.get_plan_workout(user_id, workout_id)
    )

    _rewind_and_migrate()

    workout = db.get_plan_workout(user_id, workout_id)
    assert workout["completed_date"] == "2026-07-15"
    assert importer.plan_workout_completion_verified(user_id, workout)


def test_positive_offset_rider_is_restated_too(user_id):
    # Asia/Tokyo is UTC+9, so the correction runs the other way: a 22:00 UTC
    # ride is the rider's next morning. One day late is inside the grace
    # window, so this row verifies once corrected.
    db.save_user_settings(user_id, {"timezone": "Asia/Tokyo"})
    workout_id, _ = _legacy_plan_completion(
        user_id, "2026-07-15", "2026-07-15T22:00:00", "2026-07-15", "morning"
    )
    assert not importer.plan_workout_completion_verified(
        user_id, db.get_plan_workout(user_id, workout_id)
    )

    _rewind_and_migrate()

    workout = db.get_plan_workout(user_id, workout_id)
    assert workout["completed_date"] == "2026-07-16"
    assert importer.plan_workout_completion_verified(user_id, workout)


def test_timezone_is_read_per_user(user_id):
    db.save_user_settings(user_id, {"timezone": "America/New_York"})
    other = _rider("tokyo", "Asia/Tokyo")
    mine, _ = _legacy_plan_completion(
        user_id, "2026-07-15", "2026-07-16T01:30:00", "2026-07-16", "mine"
    )
    theirs, _ = _legacy_plan_completion(
        other, "2026-07-15", "2026-07-15T22:00:00", "2026-07-15", "theirs"
    )

    _rewind_and_migrate()

    # Same database, opposite corrections - a single global timezone could not
    # produce both.
    assert db.get_plan_workout(user_id, mine)["completed_date"] == "2026-07-15"
    assert db.get_plan_workout(other, theirs)["completed_date"] == "2026-07-16"


def test_utc_rider_is_untouched(user_id):
    db.save_user_settings(user_id, {"timezone": "UTC"})
    workout_id, _ = _legacy_plan_completion(
        user_id, "2026-07-15", "2026-07-15T18:00:00", "2026-07-15", "utc"
    )

    _rewind_and_migrate()

    workout = db.get_plan_workout(user_id, workout_id)
    assert workout["completed_date"] == "2026-07-15"
    assert importer.plan_workout_completion_verified(user_id, workout)


def test_correction_outside_grace_window_becomes_unverified(user_id):
    # The mis-attribution the old rule created: a 22:00 local ride on the 15th
    # carried UTC date the 16th and was filed against the workout scheduled for
    # the 16th - a session the rider had not ridden yet. Correcting the date
    # puts the completion a day BEFORE its workout, which is not a completion.
    # That is the honest answer, not a regression, so it is pinned here.
    db.save_user_settings(user_id, {"timezone": "America/New_York"})
    workout_id, _ = _legacy_plan_completion(
        user_id, "2026-07-16", "2026-07-16T02:00:00", "2026-07-16", "ahead"
    )
    assert not importer.plan_workout_completion_verified(
        user_id, db.get_plan_workout(user_id, workout_id)
    )

    _rewind_and_migrate()

    workout = db.get_plan_workout(user_id, workout_id)
    # The stored date now agrees with the activity's local date, so the half of
    # the rule this migration exists to fix passes. It still does not verify,
    # because the completion is a day EARLIER than the workout it is attached
    # to - and nothing here tries to preserve its old status.
    assert workout["completed_date"] == "2026-07-15"
    assert not importer.plan_workout_completion_verified(user_id, workout)
    assert (dt.date.fromisoformat(workout["completed_date"])
            - dt.date.fromisoformat(workout["date"])).days == -1


def test_standalone_completions_are_restated(user_id):
    db.save_user_settings(user_id, {"timezone": "America/New_York"})
    evening, _ = _legacy_standalone_completion(
        user_id, "2026-07-15", "2026-07-16T01:30:00", "2026-07-16", "s-evening"
    )
    already_right, _ = _legacy_standalone_completion(
        user_id, "2026-07-17", "2026-07-17T14:00:00", "2026-07-17", "s-noon"
    )

    _rewind_and_migrate()

    assert db.get_standalone_workout(user_id, evening)["completed_date"] == "2026-07-15"
    assert (db.get_standalone_workout(user_id, already_right)["completed_date"]
            == "2026-07-17")


def test_migration_is_idempotent(user_id):
    db.save_user_settings(user_id, {"timezone": "America/New_York"})
    _legacy_plan_completion(
        user_id, "2026-07-15", "2026-07-16T01:30:00", "2026-07-16", "plan"
    )
    _legacy_standalone_completion(
        user_id, "2026-07-15", "2026-07-16T02:30:00", "2026-07-16", "solo"
    )

    _rewind_and_migrate()
    first = (_completed_dates("plan_workouts"),
             _completed_dates("standalone_workouts"))
    assert first[0] and first[1]

    _rewind_and_migrate()

    assert (_completed_dates("plan_workouts"),
            _completed_dates("standalone_workouts")) == first


def test_missing_activity_row_is_left_alone(user_id):
    db.save_user_settings(user_id, {"timezone": "America/New_York"})
    workout_id, activity_id = _legacy_plan_completion(
        user_id, "2026-07-15", "2026-07-16T01:30:00", "2026-07-16", "gone"
    )
    conn = sqlite3.connect(config.db_path())
    conn.execute("DELETE FROM activities WHERE id = ?", (activity_id,))
    conn.commit()
    conn.close()

    _rewind_and_migrate()  # must not raise

    workout = db.get_plan_workout(user_id, workout_id)
    assert workout["completed_date"] == "2026-07-16"
    # The verifier already returns False for a vanished activity, correctly.
    assert not importer.plan_workout_completion_verified(user_id, workout)


def test_unparseable_start_time_row_is_left_alone(user_id):
    db.save_user_settings(user_id, {"timezone": "America/New_York"})
    workout_id, activity_id = _legacy_plan_completion(
        user_id, "2026-07-15", "2026-07-16T01:30:00", "2026-07-16", "junk"
    )
    conn = sqlite3.connect(config.db_path())
    conn.execute(
        "UPDATE activities SET start_time = 'not-a-timestamp' WHERE id = ?",
        (activity_id,),
    )
    conn.commit()
    conn.close()

    _rewind_and_migrate()  # must not raise

    assert db.get_plan_workout(user_id, workout_id)["completed_date"] == "2026-07-16"


def test_migration_runs_on_a_database_with_no_completions(user_id):
    db.save_user_settings(user_id, {"timezone": "America/New_York"})

    _rewind_and_migrate()

    conn = sqlite3.connect(config.db_path())
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM plan_workouts WHERE completed_date IS NOT NULL"
        ).fetchone()[0] == 0
    finally:
        conn.close()
