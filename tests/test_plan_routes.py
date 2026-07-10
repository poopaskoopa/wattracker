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


def test_plan_submit_honors_hard_days(client):
    _register(client)
    form = dict(PLAN_FORM)
    form["hard_days"] = ["2", "5"]  # Wed + Sat pinned hard
    r = client.post("/generate/plan", data=form)
    assert r.status_code == 200
    uid = db.get_user_by_username("rider")["id"]
    plan_id = db.list_plans(uid)[0]["id"]
    for w in db.plan_workouts_for_plan(uid, plan_id):
        weekday = dt.date.fromisoformat(w["date"]).weekday()
        if w["type"] in ("vo2max", "threshold"):
            assert weekday in (2, 5)
        else:
            assert weekday not in (2, 5)


def test_plan_submit_too_many_hard_days_rejected(client):
    _register(client)
    form = dict(PLAN_FORM)
    form["hard_days"] = ["0", "2", "4"]  # 3 marked > hit_days_per_week=2
    r = client.post("/generate/plan", data=form)
    assert r.status_code == 200
    assert "Error" in r.text
    uid = db.get_user_by_username("rider")["id"]
    assert db.list_plans(uid) == []


def test_generate_form_has_hard_day_toggles(client):
    _register(client)
    text = client.get("/generate").text
    assert 'name="hard_days"' in text


# ----------------------------------------------- workout power-detail API
def _first_workout_id(username="rider"):
    uid = db.get_user_by_username(username)["id"]
    plan_id = db.list_plans(uid)[0]["id"]
    return uid, db.plan_workouts_for_plan(uid, plan_id)[0]["id"]


def test_workout_detail_api_segments_and_profile(client):
    _register(client)
    client.post("/generate/plan", data=PLAN_FORM)
    _uid, wid = _first_workout_id()
    r = client.get(f"/api/plan/workout/{wid}")
    assert r.status_code == 200
    d = r.json()
    assert d["id"] == wid
    assert d["ftp"] > 0
    assert d["segments"] and d["profile"]
    # Profile blocks tile the whole workout.
    assert d["profile"][0]["start"] == 0
    assert d["profile"][-1]["end"] == d["total_duration"]
    for a, b in zip(d["profile"], d["profile"][1:]):
        assert a["end"] == b["start"]
    # Watts are %FTP x FTP for every segment field present.
    for s in d["segments"]:
        for frac_key, watts_key in (
            ("power", "watts"), ("power_low", "watts_low"),
            ("power_high", "watts_high"), ("on_power", "watts_on"),
            ("off_power", "watts_off"),
        ):
            if s[frac_key] is not None:
                assert s[watts_key] == round(s[frac_key] * d["ftp"])
    # Segment durations account for the stored duration.
    assert sum(s["duration"] for s in d["segments"]) == d["duration_s"]


def test_workout_detail_api_user_scoped(client):
    _register(client, "alice")
    client.post("/generate/plan", data=PLAN_FORM)
    _uid, wid = _first_workout_id("alice")
    client.get("/logout")
    _register(client, "bob")
    r = client.get(f"/api/plan/workout/{wid}")
    assert r.status_code == 404


def test_workout_detail_api_missing_404(client):
    _register(client)
    assert client.get("/api/plan/workout/999999").status_code == 404


def test_calendar_workouts_are_clickable(client):
    _register(client)
    client.post("/generate/plan", data=PLAN_FORM)
    r = client.get("/calendar?year=2026&month=8")
    assert 'data-workout-id="' in r.text
    assert "workoutModal" in r.text


def test_calendar_shows_completion_checkmark(client):
    _register(client)
    client.post("/generate/plan", data=PLAN_FORM)
    uid = db.get_user_by_username("rider")["id"]
    plan_id = db.list_plans(uid)[0]["id"]
    w = db.plan_workouts_for_plan(uid, plan_id)[0]
    assert db.mark_plan_workout_completed(uid, w["id"], 1234, w["date"]) is True
    y, m = w["date"][:4], int(w["date"][5:7])
    r = client.get(f"/calendar?year={y}&month={m}")
    assert "cal-completed" in r.text
    assert "cal-check" in r.text


def test_calendar_marks_missed_past_workouts(client):
    # A past-dated plan (well before any plausible "today") whose workouts were
    # never completed should be flagged missed; a completed one is not.
    _register(client)
    form = dict(PLAN_FORM, start_date="2020-01-06")  # Mon, long past
    client.post("/generate/plan", data=form)
    uid = db.get_user_by_username("rider")["id"]
    workouts = db.plan_workouts_for_plan(uid, db.list_plans(uid)[0]["id"])
    first = min(workouts, key=lambda w: w["date"])
    db.mark_plan_workout_completed(uid, first["id"], 42, first["date"])
    y, m = first["date"][:4], int(first["date"][5:7])
    text = client.get(f"/calendar?year={y}&month={m}").text
    assert "cal-missed" in text
    assert "cal-miss-mark" in text
    # completed + missed counts add up to that month's workouts (no overlap)
    month_workouts = [w for w in workouts if w["date"][:7] == f"{y}-{m:02d}"]
    assert text.count("cal-missed") == len(month_workouts) - 1


def test_calendar_future_workouts_not_missed(client):
    # A plan far in the future: none of its days have passed, so none is missed.
    future_year = dt.date.today().year + 5
    _register(client)
    form = dict(PLAN_FORM, start_date=f"{future_year}-08-05")
    client.post("/generate/plan", data=form)
    assert "cal-missed" not in client.get(
        f"/calendar?year={future_year}&month=8").text


# ----------------------------------------------------- RPE (exertion) grading
def _completed_workout_id(client, username="rider"):
    uid = db.get_user_by_username(username)["id"]
    plan_id = db.list_plans(uid)[0]["id"]
    w = db.plan_workouts_for_plan(uid, plan_id)[0]
    db.mark_plan_workout_completed(uid, w["id"], 1234, w["date"])
    return uid, w["id"]


def test_rpe_out_of_range_rejected(client):
    _register(client)
    client.post("/generate/plan", data=PLAN_FORM)
    _uid, wid = _completed_workout_id(client)
    assert client.post(f"/api/plan/workout/{wid}/rpe", json={"rpe": 0}).status_code == 400
    assert client.post(f"/api/plan/workout/{wid}/rpe", json={"rpe": 11}).status_code == 400


def test_rpe_not_completed_rejected(client):
    _register(client)
    client.post("/generate/plan", data=PLAN_FORM)
    _uid, wid = _first_workout_id()  # never completed
    r = client.post(f"/api/plan/workout/{wid}/rpe", json={"rpe": 5})
    assert r.status_code == 400


def test_rpe_other_users_workout_404(client):
    _register(client, "alice")
    client.post("/generate/plan", data=PLAN_FORM)
    _uid, wid = _completed_workout_id(client, "alice")
    client.get("/logout")
    _register(client, "bob")
    r = client.post(f"/api/plan/workout/{wid}/rpe", json={"rpe": 5})
    assert r.status_code == 404


def test_rpe_success_and_detail_returns_it(client):
    _register(client)
    client.post("/generate/plan", data=PLAN_FORM)
    _uid, wid = _completed_workout_id(client)
    r = client.post(f"/api/plan/workout/{wid}/rpe", json={"rpe": 7})
    assert r.status_code == 200 and r.json()["rpe"] == 7
    detail = client.get(f"/api/plan/workout/{wid}").json()
    assert detail["rpe"] == 7
    assert detail["completed"] is True


def test_rpe_overwrite_allowed(client):
    _register(client)
    client.post("/generate/plan", data=PLAN_FORM)
    _uid, wid = _completed_workout_id(client)
    client.post(f"/api/plan/workout/{wid}/rpe", json={"rpe": 3})
    r = client.post(f"/api/plan/workout/{wid}/rpe", json={"rpe": 9})
    assert r.status_code == 200 and r.json()["rpe"] == 9
    assert client.get(f"/api/plan/workout/{wid}").json()["rpe"] == 9


def test_plan_submit_persists_model(client):
    _register(client)
    form = dict(PLAN_FORM, model="sweet_spot")
    r = client.post("/generate/plan", data=form)
    assert r.status_code == 200
    uid = db.get_user_by_username("rider")["id"]
    plan_id = db.list_plans(uid)[0]["id"]
    assert db.get_plan(uid, plan_id)["model"] == "sweet_spot"


def test_generate_form_has_model_radios(client):
    _register(client)
    text = client.get("/generate").text
    assert 'name="model"' in text
    assert "sweet_spot" in text and "pyramidal" in text


def test_calendar_shows_rpe_badge(client):
    _register(client)
    client.post("/generate/plan", data=PLAN_FORM)
    _uid, wid = _completed_workout_id(client)
    db.set_plan_workout_rpe(_uid, wid, 6)
    w = db.get_plan_workout(_uid, wid)
    y, m = w["date"][:4], int(w["date"][5:7])
    text = client.get(f"/calendar?year={y}&month={m}").text
    assert "cal-rpe" in text
    assert "RPE 6" in text


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
