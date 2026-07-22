"""Tests: daily auto-scan, plan-workout completion matching, schema migration."""
import asyncio
import datetime as dt
import sqlite3

import pytest

import wattracker.ingest.importer as importer
from wattracker import auth, config, db
from wattracker.prescribe import zwo
from wattracker.prescribe.planner import build_workout

NOW = dt.datetime(2026, 7, 10, 18, 0)


def _activity(user_id, start_time, seconds=3600, tss=60.0, watts=200.0):
    db.insert_activity(
        user_id,
        {
            "dedup_hash": f"h-{user_id}-{start_time}-{seconds}",
            "filename": "ride.fit",
            "start_time": start_time,
            "duration_s": seconds,
            "distance_m": 0.0,
            "avg_power": watts,
            "avg_hr": 0.0,
            "np": watts,
            "if_": 1.0,
            "tss": tss,
            "streams": {"power": [watts] * min(seconds, 10)},
        },
    )
    return db.activities_on_date(user_id, start_time[:10])[-1]["id"]


def _plan_workout(user_id, date, duration_s=3600, tss=60.0, name="W", type_="endurance"):
    plan_id = db.create_plan(user_id, "P", date, 1)
    return db.add_plan_workout(plan_id, user_id, date, name, type_, duration_s, tss, "<x/>")


# ------------------------------------------------------------- matching
def test_matches_same_day_activity_within_duration_tolerance(user_id):
    wid = _plan_workout(user_id, "2026-07-10", duration_s=3600)
    _activity(user_id, "2026-07-10T08:00:00", seconds=3300)  # ~8% short
    assert importer.match_plan_completions(user_id, NOW) == 1
    w = db.get_plan_workout(user_id, wid)
    assert w["completed_activity_id"] is not None
    assert w["completed_date"] == "2026-07-10"


def test_no_match_outside_tolerance(user_id):
    _plan_workout(user_id, "2026-07-10", duration_s=3600, tss=60.0)
    _activity(user_id, "2026-07-10T08:00:00", seconds=1200, tss=15.0)  # way short
    assert importer.match_plan_completions(user_id, NOW) == 0


def test_no_match_on_other_days(user_id):
    _plan_workout(user_id, "2026-07-09", duration_s=3600)
    _activity(user_id, "2026-07-10T08:00:00", seconds=3600)  # a day late
    assert importer.match_plan_completions(user_id, NOW) == 0


def test_tss_fallback_matches_ride_cut_short_but_hard(user_id):
    # Duration off by 40%, but TSS within 30% -> still counts as completed.
    _plan_workout(user_id, "2026-07-10", duration_s=3600, tss=60.0)
    _activity(user_id, "2026-07-10T08:00:00", seconds=2100, tss=55.0)
    assert importer.match_plan_completions(user_id, NOW) == 1


def test_one_activity_completes_at_most_one_workout(user_id):
    plan_id = db.create_plan(user_id, "P", "2026-07-10", 1)
    db.add_plan_workout(plan_id, user_id, "2026-07-10", "A", "endurance", 3600, 60.0, "<x/>")
    db.add_plan_workout(plan_id, user_id, "2026-07-10", "B", "endurance", 3600, 60.0, "<y/>")
    _activity(user_id, "2026-07-10T08:00:00", seconds=3600)
    assert importer.match_plan_completions(user_id, NOW) == 1
    workouts = db.plan_workouts_for_plan(user_id, plan_id)
    done = [w for w in workouts if w["completed_activity_id"]]
    assert len(done) == 1


def test_closest_duration_wins(user_id):
    wid = _plan_workout(user_id, "2026-07-10", duration_s=3600)
    _activity(user_id, "2026-07-10T06:00:00", seconds=3000)  # 17% off
    close_id = _activity(user_id, "2026-07-10T18:00:00", seconds=3550)  # 1.4% off
    importer.match_plan_completions(user_id, NOW)
    assert db.get_plan_workout(user_id, wid)["completed_activity_id"] == close_id


def test_future_workouts_never_marked(user_id):
    _plan_workout(user_id, "2026-07-20", duration_s=3600)  # after NOW
    _activity(user_id, "2026-07-20T08:00:00", seconds=3600)
    assert importer.match_plan_completions(user_id, NOW) == 0


def test_matching_is_idempotent_and_user_scoped(user_id):
    other = db.create_user("other", auth.hash_password("password123"))
    _plan_workout(user_id, "2026-07-10", duration_s=3600)
    _plan_workout(other, "2026-07-10", duration_s=3600)
    _activity(user_id, "2026-07-10T08:00:00", seconds=3600)
    assert importer.match_plan_completions(user_id, NOW) == 1
    assert importer.match_plan_completions(user_id, NOW) == 0  # already done
    assert importer.match_plan_completions(other, NOW) == 0  # no ride of their own


def test_single_workout_match_is_today_only_and_does_not_sweep_plan(user_id):
    session = build_workout("threshold", 60)
    xml = zwo.zwo_string(session)
    profile = importer._zwo_fraction_profile(xml)
    plan_id = db.create_plan(user_id, "P", "2026-07-10", 1)
    first = db.add_plan_workout(
        plan_id, user_id, "2026-07-10", "A", "threshold",
        session.total_duration(), session.estimated_tss, xml,
    )
    second = db.add_plan_workout(
        plan_id, user_id, "2026-07-10", "B", "threshold",
        session.total_duration(), session.estimated_tss, xml,
    )
    activity_id = db.insert_activity(
        user_id,
        {
            "dedup_hash": "single-click-match",
            "filename": "ride.fit",
            "start_time": "2026-07-10T10:00:00",
            "duration_s": len(profile),
            "distance_m": 0,
            "avg_power": 210,
            "avg_hr": None,
            "np": 210,
            "if_": 1.0,
            "tss": session.estimated_tss,
            "streams": {"power": [p * 210 for p in profile]},
        },
    )

    assert not importer.match_plan_workout_completion(
        user_id, second, dt.date(2026, 7, 9)
    )
    assert importer.match_plan_workout_completion(
        user_id, second, dt.date(2026, 7, 10)
    )
    assert db.get_plan_workout(user_id, second)["completed_activity_id"] == activity_id
    assert db.get_plan_workout(user_id, first)["completed_activity_id"] is None


@pytest.mark.parametrize("power", [None, "mismatch"])
def test_profile_backed_plan_never_batch_matches_without_strong_profile(
    user_id, power
):
    session = build_workout("threshold", 60)
    xml = zwo.zwo_string(session)
    profile = importer._zwo_fraction_profile(xml)
    plan_id = db.create_plan(user_id, "Profile", "2026-07-10", 1)
    workout_id = db.add_plan_workout(
        plan_id, user_id, "2026-07-10", session.name, "threshold",
        session.total_duration(), session.estimated_tss, xml,
    )
    stream = [] if power is None else [0.0] * len(profile)
    db.insert_activity(
        user_id,
        {
            "dedup_hash": f"weak-profile-{power}",
            "filename": "weak.fit",
            "start_time": "2026-07-10T10:00:00",
            "duration_s": len(profile),
            "distance_m": 0,
            "avg_power": 0,
            "avg_hr": None,
            "np": 0,
            "if_": 0,
            "tss": session.estimated_tss,
            "streams": {"power": stream},
        },
    )

    assert importer.match_plan_completions(user_id, NOW) == 0
    assert db.get_plan_workout(user_id, workout_id)["completed_activity_id"] is None


# ------------------------------------------------------- schema migration
def test_v3_database_migrates_in_place_without_data_loss(tmp_path):
    """A live v3 database must be upgraded (new columns), keeping all rows."""
    path = str(tmp_path / "v3.db")
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL,
            created TEXT NOT NULL);
        CREATE TABLE plans (id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL, name TEXT NOT NULL,
            start_date TEXT NOT NULL, weeks INTEGER NOT NULL, created TEXT NOT NULL);
        CREATE TABLE plan_workouts (id INTEGER PRIMARY KEY AUTOINCREMENT,
            plan_id INTEGER NOT NULL, user_id INTEGER NOT NULL, date TEXT NOT NULL,
            name TEXT NOT NULL, type TEXT NOT NULL, duration_s INTEGER NOT NULL,
            tss REAL NOT NULL, zwo_or_segments TEXT NOT NULL);
        INSERT INTO users (username, password_hash, created)
            VALUES ('keeper', 'x', '2026-01-01');
        INSERT INTO plans (user_id, name, start_date, weeks, created)
            VALUES (1, 'Keep', '2026-07-06', 4, '2026-01-01');
        INSERT INTO plan_workouts
            (plan_id, user_id, date, name, type, duration_s, tss, zwo_or_segments)
            VALUES (1, 1, '2026-07-07', 'W', 'endurance', 3600, 60.0, '<x/>');
        PRAGMA user_version = 3;
        """
    )
    conn.commit()
    conn.close()

    db.init_db(path=path)

    conn = sqlite3.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    row = conn.execute(
        "SELECT name, completed_activity_id, completed_date FROM plan_workouts"
    ).fetchone()
    assert row == ("W", None, None)  # data kept, new columns present
    assert conn.execute("SELECT username FROM users").fetchone()[0] == "keeper"
    conn.close()


def test_newer_db_version_refused_data_intact(tmp_path):
    # Stale code (older SCHEMA_VERSION) against a newer live DB must crash,
    # not drop/recreate - this exact path has wiped live tables twice.
    path = str(tmp_path / "future.db")
    conn = sqlite3.connect(path)
    conn.executescript(
        f"""
        CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT);
        INSERT INTO users (username) VALUES ('keeper');
        PRAGMA user_version = {db.SCHEMA_VERSION + 1};
        """
    )
    conn.commit()
    conn.close()

    with pytest.raises(RuntimeError, match="refusing"):
        db.init_db(path=path)

    conn = sqlite3.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION + 1
    assert conn.execute("SELECT username FROM users").fetchone()[0] == "keeper"
    conn.close()


def test_unknown_old_version_still_recreates(tmp_path):
    path = str(tmp_path / "old.db")
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE junk (x)")
    conn.execute("PRAGMA user_version = 1")  # no migration chain from 1
    conn.commit()
    conn.close()
    db.init_db(path=path)
    conn = sqlite3.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    assert conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE name='plan_workouts'"
    ).fetchone()[0] == 1
    conn.close()


# ------------------------------------------------------------ auto scan
def _fake_parsed(start_time="2026-07-10T08:00:00", seconds=3600, watts=200.0):
    return {
        "start_time": start_time,
        "duration_s": seconds,
        "streams": {
            "time": [None] * 10,
            "power": [watts] * seconds,
            "heartrate": [140.0] * 10,
            "cadence": [90.0] * 10,
            "distance": [0.0] * 10,
            "altitude": [0.0] * 10,
        },
    }


def test_run_auto_scan_imports_and_marks_completions(user_id, tmp_path, monkeypatch):
    watch = tmp_path / "Watch"
    watch.mkdir()
    (watch / "ride.fit").write_bytes(b"dummy")
    db.save_user_settings(user_id, {"activities_dir": str(watch)})
    # A date safely in the past (the sweep matches with wall-clock "today").
    _plan_workout(user_id, "2026-06-10", duration_s=3600, tss=100.0)
    monkeypatch.setattr(
        importer, "parse_fit",
        lambda path: _fake_parsed(start_time="2026-06-10T08:00:00"),
    )

    totals = importer.run_auto_scan()
    assert totals["users"] >= 1
    assert totals["imported"] == 1
    assert totals["completed"] == 1
    assert len(db.list_activities(user_id)) == 1
    # Second sweep is a no-op (dedup + already completed).
    totals2 = importer.run_auto_scan()
    assert totals2["imported"] == 0
    assert totals2["completed"] == 0


def test_run_auto_scan_covers_settings_only_users(user_id, tmp_path, monkeypatch):
    # A user id present only via settings/activities (no users row) is still
    # swept - mirrors the live database where per-user rows outlived the row.
    watch = tmp_path / "Watch2"
    watch.mkdir()
    (watch / "ride.fit").write_bytes(b"dummy")
    # Keep the regular user's watch folder off this machine's real default.
    db.save_user_settings(user_id, {"activities_dir": str(tmp_path / "none")})
    orphan = user_id + 100
    db.save_user_settings(orphan, {"activities_dir": str(watch)})
    monkeypatch.setattr(importer, "parse_fit", lambda path: _fake_parsed())
    assert orphan in db.all_user_ids()
    totals = importer.run_auto_scan()
    assert totals["imported"] == 1
    assert len(db.list_activities(orphan)) == 1


def test_run_auto_scan_survives_missing_dirs(user_id):
    db.save_user_settings(user_id, {"activities_dir": "/nope/definitely/missing"})
    totals = importer.run_auto_scan()  # must not raise
    assert totals["imported"] == 0


def test_run_daily_maintenance_self_heals_late_created_plan(user_id, monkeypatch):
    """Regression: with rescan gated on imported>0, a plan created AFTER its
    rides were already imported would never get completions matched. The daily
    maintenance pass must re-run matching unconditionally to self-heal."""
    from wattracker import server as servermod

    # Ride imported first, then a same-day matching workout created afterwards -
    # no NEW import happens, so only the daily self-heal can match it.
    _activity(user_id, "2026-06-10T08:00:00", seconds=3600, tss=100.0)
    wid = _plan_workout(user_id, "2026-06-10", duration_s=3600, tss=100.0)
    assert db.get_plan_workout(user_id, wid)["completed_activity_id"] is None

    # Neutralize folder scanning + network race refresh; keep real matching.
    monkeypatch.setattr(
        servermod.importer, "run_auto_scan",
        lambda: {"users": 0, "imported": 0, "completed": 0},
    )
    monkeypatch.setattr(
        servermod.races, "refresh_race_results", lambda *a, **k: None
    )
    totals = servermod.run_daily_maintenance()
    assert totals["completed"] >= 1
    assert db.get_plan_workout(user_id, wid)["completed_activity_id"] is not None


def test_auto_scan_loop_runs_daily_without_real_sleep(monkeypatch):
    from wattracker import server as servermod

    calls = []
    monkeypatch.setattr(
        servermod.importer, "run_auto_scan", lambda: calls.append(1) or {}
    )
    monkeypatch.setattr(servermod, "SCAN_INTERVAL_S", 0.01)

    async def drive():
        stop = asyncio.Event()
        task = asyncio.create_task(servermod.auto_scan_loop(stop))
        while len(calls) < 3:  # startup run + at least two interval runs
            await asyncio.sleep(0.005)
        stop.set()
        await asyncio.wait_for(task, timeout=2)

    asyncio.run(drive())
    assert len(calls) >= 3


def test_auto_scan_loop_stops_promptly(monkeypatch):
    from wattracker import server as servermod

    monkeypatch.setattr(servermod.importer, "run_auto_scan", lambda: {})
    monkeypatch.setattr(servermod, "SCAN_INTERVAL_S", 3600.0)  # long interval

    async def drive():
        stop = asyncio.Event()
        task = asyncio.create_task(servermod.auto_scan_loop(stop))
        await asyncio.sleep(0.05)  # let the startup run happen
        stop.set()
        await asyncio.wait_for(task, timeout=2)  # returns despite the interval

    asyncio.run(drive())


def test_auto_scan_config_flag(monkeypatch):
    monkeypatch.setenv("WATTRACKER_AUTO_SCAN", "0")
    assert config.auto_scan_enabled() is False
    monkeypatch.setenv("WATTRACKER_AUTO_SCAN", "1")
    assert config.auto_scan_enabled() is True
    monkeypatch.delenv("WATTRACKER_AUTO_SCAN")
    assert config.auto_scan_enabled() is True


def test_lifespan_starts_scan_task_when_enabled(monkeypatch):
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    from wattracker import server as servermod

    calls = []
    monkeypatch.setattr(
        servermod.importer, "run_auto_scan", lambda: calls.append(1) or {}
    )
    monkeypatch.setenv("WATTRACKER_AUTO_SCAN", "1")
    app = servermod.create_app()
    with TestClient(app):
        for _ in range(100):
            if calls:
                break
            import time

            time.sleep(0.01)
    assert calls, "startup sweep should run once when auto-scan is enabled"
