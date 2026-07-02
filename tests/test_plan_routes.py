"""Route tests for Plan mode, calendar view, and plan export."""
import datetime as dt

import pytest

pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from tranalyzer import db  # noqa: E402
from tranalyzer.server import create_app  # noqa: E402


@pytest.fixture()
def client():
    app = create_app()
    with TestClient(app) as c:
        yield c


def _register(client, username="rider"):
    client.post("/register", data={"username": username, "password": "password123"})


PLAN_FORM = {
    "name": "Base Plan",
    "weeks": "4",
    "hours_per_week": "8",
    "hit_days_per_week": "2",
    "start_date": "2026-08-05",  # Wed -> week anchors to Mon 2026-08-03
    "days": ["0", "2", "4", "5"],
}


def test_generate_page_has_both_modes(client):
    _register(client)
    text = client.get("/generate").text
    assert "Single Workout" in text
    assert "Training Plan" in text


def test_plan_submit_creates_and_persists(client):
    _register(client)
    r = client.post("/generate/plan", data=PLAN_FORM)
    assert r.status_code == 200
    assert "Base Plan" in r.text
    uid = db.get_user_by_username("rider")["id"]
    plans = db.list_plans(uid)
    assert len(plans) == 1
    workouts = db.plan_workouts_for_plan(uid, plans[0]["id"])
    assert len(workouts) == 4 * 4  # 4 weeks x 4 days


def test_plan_submit_invalid_shows_error(client):
    _register(client)
    bad = dict(PLAN_FORM)
    bad["hit_days_per_week"] = "9"  # more than selected days
    r = client.post("/generate/plan", data=bad)
    assert r.status_code == 200
    assert "Error" in r.text
    uid = db.get_user_by_username("rider")["id"]
    assert db.list_plans(uid) == []


def test_calendar_page_ok_empty(client):
    _register(client)
    r = client.get("/calendar")
    assert r.status_code == 200
    assert "Calendar" in r.text


def test_calendar_shows_plan_workouts(client):
    _register(client)
    client.post("/generate/plan", data=PLAN_FORM)
    r = client.get("/calendar?year=2026&month=8")
    assert r.status_code == 200
    # August has plan workouts; a HIT or endurance session name should appear.
    assert ("Intervals" in r.text) or ("Endurance" in r.text)


def test_plan_zip_download(client):
    _register(client)
    client.post("/generate/plan", data=PLAN_FORM)
    uid = db.get_user_by_username("rider")["id"]
    plan_id = db.list_plans(uid)[0]["id"]
    r = client.get(f"/plan/{plan_id}/download.zip")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"
    assert r.content[:2] == b"PK"  # zip magic


def test_plan_export_to_temp_dir(client, tmp_path):
    import os
    out = tmp_path / "zwo"
    _register(client)
    uid = db.get_user_by_username("rider")["id"]
    db.save_user_settings(uid, {"workouts_dir": str(out), "zwift_id": "me"})
    client.post("/generate/plan", data=PLAN_FORM)
    plan_id = db.list_plans(uid)[0]["id"]
    r = client.post(f"/plan/{plan_id}/export")
    assert r.status_code == 200
    files = os.listdir(out)
    assert len(files) == 16
    assert all(f.endswith(".zwo") for f in files)


def test_calendar_isolated_between_users(client):
    _register(client, "alice")
    client.post("/generate/plan", data=PLAN_FORM)
    client.get("/logout")
    _register(client, "bob")
    # Bob's August calendar has no workouts from Alice's plan.
    r = client.get("/calendar?year=2026&month=8")
    assert r.status_code == 200
    assert "Intervals" not in r.text
    assert "cal-workout" not in r.text
