"""Backend regressions for activity lifecycle and calendar completion state."""

import pytest

pytest.importorskip("httpx")
from fastapi.testclient import TestClient

from wattracker import db
from wattracker.server import create_app


@pytest.fixture()
def client():
    with TestClient(create_app()) as c:
        yield c


def _register(client, username="rider"):
    client.post("/register", data={"username": username, "password": "password123"})
    return db.get_user_by_username(username)["id"]


def _activity(uid, key, date="2026-08-10"):
    return db.insert_activity(uid, {
        "dedup_hash": key, "filename": f"{key}.fit",
        "start_time": f"{date}T10:00:00", "duration_s": 3600,
        "distance_m": 1, "avg_power": 180, "avg_hr": 120,
        "np": 180, "if_": .8, "tss": 40, "streams": {"power": [180]},
    })


def test_drop_is_scoped_and_releases_duplicate_children(client):
    uid = _register(client)
    primary = _activity(uid, "primary")
    child = _activity(uid, "child")
    assert db.set_duplicate_of(uid, child, primary)
    assert client.delete(f"/api/activity/{primary}").json()["status"] == "deleted"
    assert db.get_activity(uid, child)["duplicate_of"] is None
    assert db.get_user_by_username("rider")

    client.post("/logout")
    _register(client, "other")
    assert client.delete(f"/api/activity/{child}").status_code == 404


def test_linked_drop_is_conflict_and_activity_context_exposes_link(client):
    uid = _register(client)
    activity = _activity(uid, "linked")
    plan = db.create_plan(uid, "P", "2026-08-01", 1)
    workout = db.add_plan_workout(plan, uid, "2026-08-10", "W", "endurance", 3600, 40, "<x/>")
    assert db.mark_plan_workout_completed(uid, workout, activity, "2026-08-10")
    assert client.delete(f"/api/activity/{activity}").status_code == 409
    row = client.get("/activities").text
    assert "linked" in row


def test_plan_completion_can_be_reversed_and_future_stays_rejected(client):
    uid = _register(client)
    activity = _activity(uid, "complete", "2026-08-10")
    plan = db.create_plan(uid, "P", "2026-08-01", 1)
    workout = db.add_plan_workout(plan, uid, "2026-08-10", "W", "endurance", 3600, 40, "<x/>")
    assert db.mark_plan_workout_completed(uid, workout, activity, "2026-08-10", .9, 210)
    assert db.set_plan_workout_rpe(uid, workout, 7)
    response = client.post(
        f"/api/plan/workout/{workout}/completion", json={"completed": False}
    )
    assert response.status_code == 200
    assert response.json()["completed"] is False
    stored = db.get_plan_workout(uid, workout)
    assert all(stored[field] is None for field in (
        "completed_activity_id", "completed_date", "compliance", "effective_ftp", "rpe"
    ))

    future = db.add_plan_workout(plan, uid, "2099-08-10", "Future", "endurance", 3600, 40, "<x/>")
    assert client.post(f"/api/plan/workout/{future}/complete", json={"completed": True}).status_code == 400


def test_calendar_has_unlinked_month_activity_but_not_linked_one(client):
    uid = _register(client)
    free = _activity(uid, "free", "2026-08-12")
    linked = _activity(uid, "used", "2026-08-13")
    plan = db.create_plan(uid, "P", "2026-08-01", 1)
    workout = db.add_plan_workout(plan, uid, "2026-08-13", "W", "endurance", 3600, 40, "<x/>")
    assert db.mark_plan_workout_completed(uid, workout, linked, "2026-08-13")
    calendar = client.get("/calendar?year=2026&month=8")
    assert calendar.status_code == 200
    assert "Actual activity" in calendar.text
    assert "W" in calendar.text
    assert f"/activity/{free}" in calendar.text
    assert f"/activity/{linked}" not in calendar.text
    assert db.activities_for_month_unlinked(uid, 2026, 8)[0]["id"] == free
    assert linked not in {a["id"] for a in db.activities_for_month_unlinked(uid, 2026, 8)}
