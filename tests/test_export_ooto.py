"""Tests: export-all naming (dated file + internal name), OOTO suppression,
export exclusion/removal, schema v9 migration, calendar rendering."""
import os
import sqlite3

import pytest

from tranalyzer import db, exporter
from tranalyzer.prescribe import zwo
from tranalyzer.prescribe.planner import build_workout

pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from tranalyzer.server import create_app  # noqa: E402


@pytest.fixture()
def client():
    app = create_app()
    with TestClient(app) as c:
        yield c


def _register(client, username="rider"):
    client.post("/register", data={"username": username, "password": "password123"})


def _real_zwo(name="Threshold Intervals"):
    s = build_workout("threshold", 60)
    s.name = name
    return zwo.zwo_string(s)


def _plan_with_workouts(user_id, dates, name="Threshold Intervals", type_="threshold"):
    plan_id = db.create_plan(user_id, "Plan", dates[0], 4)
    ids = []
    for d in dates:
        ids.append(db.add_plan_workout(
            plan_id, user_id, d, name, type_, 3600, 60.0, _real_zwo(name)))
    return plan_id, ids


# ---------------------------------------------------- dated internal name
def test_dated_name_zwo_prefixes_and_is_idempotent():
    xml = "<workout_file><name>Threshold Intervals</name></workout_file>"
    once = zwo.dated_name_zwo(xml, "2026-07-12")
    assert "<name>2026-07-12 Threshold Intervals</name>" in once
    assert zwo.dated_name_zwo(once, "2026-07-12") == once  # idempotent


# ---------------------------------------------------- export-all naming
def test_export_all_writes_every_workout_with_dates(user_id, tmp_path):
    out = tmp_path / "zwo"
    out.mkdir()
    db.save_user_settings(user_id, {"workouts_dir": str(out), "zwift_id": "123"})
    dates = ["2026-07-07", "2026-07-08", "2026-07-12"]
    _plan_with_workouts(user_id, dates)
    res = exporter.sync_plan_exports(user_id)
    assert res["status"] == "ok"
    assert res["exported"] == 3
    files = sorted(os.listdir(out))
    assert files == [
        "2026-07-07 Threshold Intervals.zwo",
        "2026-07-08 Threshold Intervals.zwo",
        "2026-07-12 Threshold Intervals.zwo",
    ]
    # Internal <name> is date-prefixed too (Zwift lists by internal name).
    content = (out / "2026-07-12 Threshold Intervals.zwo").read_text()
    assert "<name>2026-07-12 Threshold Intervals</name>" in content


def test_export_all_skips_completed(user_id, tmp_path):
    out = tmp_path / "zwo"
    out.mkdir()
    db.save_user_settings(user_id, {"workouts_dir": str(out), "zwift_id": "123"})
    _plan_id, ids = _plan_with_workouts(user_id, ["2026-07-07", "2026-07-08"])
    db.mark_plan_workout_completed(user_id, ids[0], 999, "2026-07-07")
    res = exporter.sync_plan_exports(user_id)
    assert res["exported"] == 1
    files = os.listdir(out)
    assert files == ["2026-07-08 Threshold Intervals.zwo"]


# ------------------------------------------------------------- OOTO
def test_ooto_range_crud_and_coverage(user_id):
    oid = db.add_ooto_range(user_id, "2026-07-10", "2026-07-14", "holiday")
    ranges = db.list_ooto_ranges(user_id)
    assert len(ranges) == 1 and ranges[0]["note"] == "holiday"
    assert db.ooto_covers(user_id, "2026-07-12") is True
    assert db.ooto_covers(user_id, "2026-07-09") is False
    assert db.ooto_covers(user_id, "2026-07-14") is True  # inclusive end
    assert db.delete_ooto_range(user_id, oid) is True
    assert db.list_ooto_ranges(user_id) == []


def test_ooto_reversed_dates_normalized(user_id):
    db.add_ooto_range(user_id, "2026-07-14", "2026-07-10")
    r = db.list_ooto_ranges(user_id)[0]
    assert r["start_date"] == "2026-07-10" and r["end_date"] == "2026-07-14"


def test_export_excludes_and_removes_ooto_workouts(user_id, tmp_path):
    out = tmp_path / "zwo"
    out.mkdir()
    db.save_user_settings(user_id, {"workouts_dir": str(out), "zwift_id": "123"})
    dates = ["2026-07-07", "2026-07-12", "2026-07-13"]
    _plan_with_workouts(user_id, dates)
    exporter.sync_plan_exports(user_id)  # all 3 written
    assert len(os.listdir(out)) == 3
    # OOTO covers the 12th & 13th -> those .zwo removed on next sync.
    db.add_ooto_range(user_id, "2026-07-12", "2026-07-13")
    res = exporter.sync_plan_exports(user_id)
    assert res["exported"] == 1 and res["removed"] == 2
    assert os.listdir(out) == ["2026-07-07 Threshold Intervals.zwo"]


def test_export_all_route_and_ooto_routes(client, tmp_path):
    _register(client)
    uid = db.get_user_by_username("rider")["id"]
    out = tmp_path / "zwo"
    out.mkdir()
    db.save_user_settings(uid, {"workouts_dir": str(out), "zwift_id": "123"})
    _plan_with_workouts(uid, ["2026-07-07", "2026-07-08"])
    r = client.post("/plan/export-all", follow_redirects=False)
    assert r.status_code == 303 and "exported=ok" in r.headers["location"]
    assert len(os.listdir(out)) == 2
    # Add an OOTO range via the route -> re-sync drops that day.
    r = client.post("/ooto/add", data={"start_date": "2026-07-08", "end_date": ""},
                    follow_redirects=False)
    assert r.status_code == 303
    assert db.list_ooto_ranges(uid)[0]["start_date"] == "2026-07-08"
    assert os.listdir(out) == ["2026-07-07 Threshold Intervals.zwo"]


def test_calendar_shows_ooto_and_skipped_and_completed(client, tmp_path):
    _register(client)
    uid = db.get_user_by_username("rider")["id"]
    _plan_id, ids = _plan_with_workouts(uid, ["2026-07-07", "2026-07-08"])
    db.mark_plan_workout_completed(uid, ids[0], 5, "2026-07-07")
    db.add_ooto_range(uid, "2026-07-08", "2026-07-08", "trip")
    text = client.get("/calendar?year=2026&month=7").text
    assert "cal-completed" in text          # 7th completed
    assert "cal-check" in text
    assert "cal-ooto-day" in text           # 8th shaded OOTO
    assert "cal-skipped" in text            # 8th workout skipped
    assert "cal-ooto-tag" in text           # visible "OOTO" badge
    assert "Export all to Zwift" in text
    assert "Out of office" in text


def test_calendar_two_column_layout_markup(client):
    _register(client)
    text = client.get("/calendar?year=2026&month=7").text
    # Calendar (cal-main) and OOTO sidebar (aside.ooto-panel) live inside the
    # same two-column grid container; the panel comes AFTER the calendar.
    assert "cal-layout" in text
    assert "cal-main" in text and "ooto-panel" in text
    assert text.index("cal-main") < text.index("ooto-panel")


def test_ooto_marks_every_day_in_range_incl_boundaries(client):
    _register(client)
    uid = db.get_user_by_username("rider")["id"]
    # A multi-day range: every day from start..end inclusive must be marked.
    db.add_ooto_range(uid, "2026-07-10", "2026-07-13", "holiday")
    text = client.get("/calendar?year=2026&month=7").text

    # Locate each day cell and check its class for the OOTO marker. Cells are
    # rendered as <td class="cal-cell ..."><div class="cal-day">N ...
    def cell_for(day):
        # Find the <td ...> that contains the day-number div for this day.
        marker = '<div class="cal-day">%d' % day
        idx = text.index(marker)
        td_start = text.rfind("<td", 0, idx)
        return text[td_start:idx]

    for day in (10, 11, 12, 13):            # start, middle, end (all inclusive)
        assert "cal-ooto-day" in cell_for(day), f"day {day} not marked OOTO"
    for day in (9, 14):                     # just outside the range -> unmarked
        assert "cal-ooto-day" not in cell_for(day), f"day {day} wrongly marked"


# ----------------------------------------------------- schema migration
def test_v8_migrates_to_v9_in_place(tmp_path):
    path = str(tmp_path / "v8.db")
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL, password_hash TEXT, created TEXT);
        CREATE TABLE plans (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER,
            name TEXT, start_date TEXT, weeks INTEGER, created TEXT);
        INSERT INTO users (username, password_hash, created) VALUES ('keep','x','t');
        INSERT INTO plans (user_id,name,start_date,weeks,created)
            VALUES (1,'P','2026-07-06',4,'t');
        PRAGMA user_version = 8;
        """
    )
    conn.commit()
    conn.close()
    db.init_db(path=path)
    conn = sqlite3.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    assert conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE name='ooto_ranges'").fetchone()[0] == 1
    assert conn.execute("SELECT username FROM users").fetchone()[0] == "keep"  # kept
    conn.close()
