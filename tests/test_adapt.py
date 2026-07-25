"""Tests for detection-driven plan adaptation and the dashboard status banner."""
import datetime as dt
import os
import xml.etree.ElementTree as ET

import pytest

from wattracker import db
from wattracker.analysis.state import TrainingState
from wattracker.prescribe import adapt
from wattracker.timeutil import utc_today

NOW = dt.datetime(2026, 7, 10, 9, 0)


def _state(overreach=False, plateau=False, alerts=None):
    return TrainingState(
        ftp=250.0, tsb=-10.0, overreach=overreach, plateau=plateau,
        alerts=list(alerts or []),
    )


def _workout(user_id, days_from_now, type_="vo2max", duration_s=3600,
             completed=False):
    date = (NOW.date() + dt.timedelta(days=days_from_now)).isoformat()
    plan_id = db.create_plan(user_id, "P", date, 1)
    wid = db.add_plan_workout(
        plan_id, user_id, date, type_.title(), type_, duration_s, 60.0, "<x/>"
    )
    if completed:
        db.mark_plan_workout_completed(user_id, wid, 999, date)
    return wid


# ------------------------------------------------------------- overreach
def test_overreach_eases_upcoming_workouts(user_id):
    wid = _workout(user_id, 2, type_="vo2max", duration_s=3600)
    summary = adapt.apply_adaptations(user_id, _state(overreach=True), NOW)
    assert summary["status"] == "overreach"
    assert summary["adjusted"] == 1
    w = db.get_plan_workout(user_id, wid)
    assert w["adapted"] == "recovery"
    assert w["type"] == "recovery"
    assert w["duration_s"] == 45 * 60  # 75% of 60 min
    # The stored .zwo was rebuilt to match (valid XML, easy targets only).
    root = ET.fromstring(w["zwo_or_segments"])
    assert root.find("name").text == w["name"]


def test_overreach_never_touches_past_completed_or_far_future(user_id):
    past = _workout(user_id, -1)
    today = _workout(user_id, 0)
    done = _workout(user_id, 3, completed=True)
    far = _workout(user_id, adapt.ADAPT_WINDOW_DAYS + 2)
    adapt.apply_adaptations(user_id, _state(overreach=True), NOW)
    for wid in (past, today, done, far):
        w = db.get_plan_workout(user_id, wid)
        assert w["adapted"] is None
        assert w["type"] == "vo2max"


# --------------------------------------------------------------- plateau
def test_plateau_swaps_hard_day_stimulus(user_id):
    v = _workout(user_id, 2, type_="vo2max")
    t = _workout(user_id, 3, type_="threshold")
    e = _workout(user_id, 4, type_="endurance")
    summary = adapt.apply_adaptations(user_id, _state(plateau=True), NOW)
    assert summary["status"] == "plateau"
    assert summary["adjusted"] == 2
    assert db.get_plan_workout(user_id, v)["type"] == "threshold"
    assert db.get_plan_workout(user_id, t)["type"] == "vo2max"
    easy = db.get_plan_workout(user_id, e)
    assert easy["type"] == "endurance" and easy["adapted"] is None


def test_overreach_wins_over_plateau(user_id):
    wid = _workout(user_id, 2, type_="vo2max")
    adapt.apply_adaptations(user_id, _state(overreach=True, plateau=True), NOW)
    assert db.get_plan_workout(user_id, wid)["type"] == "recovery"


# -------------------------------------------------------------- progress
def test_progress_leaves_volume_unchanged(user_id):
    # Volume must never increase week to week, so a healthy "progress" signal
    # is a no-op: no duration bump, no adaptation recorded.
    wid = _workout(user_id, 2, type_="endurance", duration_s=3600)
    summary = adapt.apply_adaptations(user_id, _state(), NOW)
    assert summary["status"] == "progress"
    assert summary["adjusted"] == 0
    w = db.get_plan_workout(user_id, wid)
    assert w["adapted"] is None
    assert w["duration_s"] == 3600


def test_adaptation_is_idempotent_no_stacking(user_id):
    # A later hard adaptation (overreach) still applies exactly once; the prior
    # progress runs never touched the workout.
    wid = _workout(user_id, 2, type_="endurance", duration_s=3600)
    for _ in range(5):  # repeated progress detection runs never adapt
        summary = adapt.apply_adaptations(user_id, _state(), NOW)
        assert summary["adjusted"] == 0
    assert db.get_plan_workout(user_id, wid)["duration_s"] == 3600
    # An overreach signal eases it once, then stays fixed (one adaptation ever).
    adapt.apply_adaptations(user_id, _state(overreach=True), NOW)
    eased = db.get_plan_workout(user_id, wid)
    assert eased["adapted"] == "recovery"
    adapt.apply_adaptations(user_id, _state(overreach=True), NOW)
    assert db.get_plan_workout(user_id, wid)["duration_s"] == eased["duration_s"]


def test_adapted_content_flows_to_detail_and_zwo(user_id):
    from wattracker.prescribe.planner import build_workout

    wid = _workout(user_id, 2, type_="vo2max", duration_s=3600)
    adapt.apply_adaptations(user_id, _state(overreach=True), NOW)
    w = db.get_plan_workout(user_id, wid)
    # The detail/ERG paths rebuild from (type, duration): identical session.
    session = build_workout(w["type"], w["duration_s"] / 60)
    assert session.total_duration() == w["duration_s"]
    assert session.name == w["name"]
    # All targets easy (recovery): every segment fraction <= 0.75.
    assert all(s.avg_fraction() <= 0.75 for s in session.segments)


def test_adaptation_reexports_zwo_when_configured(user_id, tmp_path):
    out = tmp_path / "zwo"
    out.mkdir()
    db.save_user_settings(user_id, {"workouts_dir": str(out)})
    wid = _workout(user_id, 2, type_="vo2max", duration_s=3600)
    w0 = db.get_plan_workout(user_id, wid)
    # Simulate a prior export of the original workout.
    from wattracker.prescribe import zwo as zwomod

    stale = out / zwomod.plan_filename(w0["date"], w0["name"])
    stale.write_text("<old/>")
    adapt.apply_adaptations(user_id, _state(overreach=True), NOW)
    w1 = db.get_plan_workout(user_id, wid)
    files = os.listdir(out)
    assert zwomod.plan_filename(w1["date"], w1["name"]) in files
    assert zwomod.plan_filename(w0["date"], w0["name"]) not in files  # replaced


# ---------------------------------------------------------------- banner
def test_banner_levels_and_adaptation_text(user_id):
    _workout(user_id, 2, type_="vo2max")
    summary = adapt.apply_adaptations(user_id, _state(overreach=True), NOW)
    banner = adapt.banner_for(_state(overreach=True, alerts=["Overreach: X"]), summary)
    assert banner["level"] == "danger"
    assert banner["headline"] == "Overreach detected"
    assert banner["detail"] == "Overreach: X"
    assert "eased for recovery" in banner["adaptation"]

    assert adapt.banner_for(_state(plateau=True), {"status": "plateau"})["level"] == "warn"
    ok = adapt.banner_for(_state(), {"status": "progress"})
    assert ok["level"] == "ok" and ok["adaptation"] is None


def test_dashboard_renders_status_banner(monkeypatch):
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    from wattracker import server as servermod

    app = servermod.create_app()
    with TestClient(app) as client:
        client.post("/register", data={"username": "rider", "password": "password123"})
        # All-clear banner by default.
        text = client.get("/").text
        assert "status-banner" in text and "status-ok" in text
        assert "Progressing well" in text

        # Overreach state -> red banner; upcoming plan workout gets adapted
        # and the calendar marks it.
        monkeypatch.setattr(
            servermod.pipeline, "build_state",
            lambda uid: _state(overreach=True, alerts=["Overreach: test reason"]),
        )
        uid = db.get_user_by_username("rider")["id"]
        date = (utc_today() + dt.timedelta(days=2)).isoformat()
        plan_id = db.create_plan(uid, "P", date, 1)
        db.add_plan_workout(plan_id, uid, date, "VO2max Intervals", "vo2max",
                            3600, 60.0, "<x/>")
        text = client.get("/").text
        assert "status-danger" in text and "Overreach detected" in text
        assert "Overreach: test reason" in text
        assert "eased for recovery" in text
        cal = client.get(f"/calendar?year={date[:4]}&month={int(date[5:7])}").text
        assert "cal-adapted" in cal
