"""Route tests for Plan mode, calendar view, and plan export."""
import datetime as dt

import pytest

pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from wattracker import db  # noqa: E402
from wattracker.ingest import importer  # noqa: E402
from wattracker.prescribe import zwo  # noqa: E402
from wattracker.prescribe.planner import build_workout  # noqa: E402
from wattracker.server import create_app  # noqa: E402


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


def test_plan_creation_matches_already_imported_activity(client, monkeypatch):
    """Regression: creating a plan must match its workouts against activities
    that were already imported before the plan existed (the gated fast rescan
    only matches on new imports)."""
    from wattracker import server as servermod

    _register(client)
    uid = db.get_user_by_username("rider")["id"]
    # An activity imported BEFORE the plan is created.
    db.insert_activity(
        uid,
        {
            "dedup_hash": "pre-import-1", "filename": "ride.fit",
            "start_time": "2026-06-10T08:00:00", "duration_s": 3600,
            "distance_m": 0.0, "avg_power": 200.0, "avg_hr": 0.0,
            "np": 200.0, "if_": 1.0, "tss": 100.0, "streams": {"power": [200.0]},
        },
    )

    # Deterministic plan with one workout on that same date.
    def fake_generate_plan(*a, **k):
        return {
            "start_date": "2026-06-10", "weeks": 1, "model": "polarized",
            "polarized_hard_fraction": 0.0, "weekly": [],
            "workouts": [{
                "date": "2026-06-10", "name": "W", "type": "endurance",
                "duration_s": 3600, "tss": 100.0, "session": {},
            }],
        }

    monkeypatch.setattr(servermod.planmod, "generate_plan", fake_generate_plan)
    monkeypatch.setattr(servermod.zwo, "zwo_string", lambda s: "<x/>")

    r = client.post("/generate/plan", data=PLAN_FORM)
    assert r.status_code == 200

    plans = db.list_plans(uid)
    workouts = db.plan_workouts_for_plan(uid, plans[0]["id"])
    assert workouts[0]["completed_activity_id"] is not None


def test_plan_page_uses_graph_button_not_download_links(client):
    _register(client)
    r = client.post("/generate/plan", data=PLAN_FORM)
    assert r.status_code == 200
    uid = db.get_user_by_username("rider")["id"]
    plan_id = db.list_plans(uid)[0]["id"]
    workouts = db.plan_workouts_for_plan(uid, plan_id)
    # No per-workout .zwo download links remain.
    for w in workouts:
        assert f"/plan/workout/{w['id']}/download" not in r.text
    # The power-curve graph button and shared script are present instead.
    assert "wk-graph-btn" in r.text
    assert f'data-workout-id="{workouts[0]["id"]}"' in r.text
    assert "/static/workout_graph.js" in r.text
    # Plan-LEVEL export/zip controls stay untouched.
    assert f"/plan/{plan_id}/download.zip" in r.text
    assert f"/plan/{plan_id}/export" in r.text


def test_calendar_includes_shared_graph_script(client):
    _register(client)
    r = client.get("/calendar")
    assert r.status_code == 200
    assert "/static/workout_graph.js" in r.text


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
    client.post("/logout")
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


def _today_profile_workout(uid, date="2026-08-05", power_scale=210.0):
    session = build_workout("threshold", 60)
    xml = zwo.zwo_string(session)
    plan_id = db.create_plan(uid, "Today", date, 1)
    workout_id = db.add_plan_workout(
        plan_id,
        uid,
        date,
        session.name,
        "threshold",
        session.total_duration(),
        session.estimated_tss,
        xml,
    )
    profile = importer._zwo_fraction_profile(xml)
    activity_id = db.insert_activity(
        uid,
        {
            "dedup_hash": f"today-{uid}-{date}-{power_scale}",
            "filename": "today.fit",
            "start_time": f"{date}T10:00:00",
            "duration_s": len(profile),
            "distance_m": 0,
            "avg_power": power_scale,
            "avg_hr": None,
            "np": power_scale,
            "if_": 1.0,
            "tss": session.estimated_tss,
            "streams": {"power": [p * power_scale for p in profile]},
        },
    )
    return workout_id, activity_id


def test_calendar_click_reconciles_today_and_exposes_rpe_prompt(client, monkeypatch):
    import wattracker.server as servermod

    _register(client)
    uid = db.get_user_by_username("rider")["id"]
    today = dt.date(2026, 8, 5)
    monkeypatch.setattr(servermod, "utc_today", lambda: today)
    workout_id, activity_id = _today_profile_workout(uid, today.isoformat())

    reconciled = client.post(f"/api/plan/workout/{workout_id}/reconcile")
    assert reconciled.status_code == 200
    assert reconciled.json() == {
        "id": workout_id,
        "status": "matched",
        "matched": True,
    }
    stored = db.get_plan_workout(uid, workout_id)
    assert stored["completed_activity_id"] == activity_id
    assert stored["compliance"] >= importer.PROFILE_MIN_COMPLIANCE

    detail = client.get(f"/api/plan/workout/{workout_id}").json()
    assert detail["completed"] is True
    assert detail["rpe"] is None
    assert detail["too_hard"] is False
    assert "10 = hardest and means the workout was too hard" in client.get(
        "/calendar?year=2026&month=8"
    ).text

    # A repeated click is a no-op and preserves the original match.
    again = client.post(f"/api/plan/workout/{workout_id}/reconcile").json()
    assert again == {"id": workout_id, "status": "completed", "matched": False}
    assert db.get_plan_workout(uid, workout_id)["completed_activity_id"] == activity_id


def test_calendar_click_reconciles_past_scheduled_date(client, monkeypatch):
    import wattracker.server as servermod

    _register(client)
    uid = db.get_user_by_username("rider")["id"]
    today = dt.date(2026, 8, 8)
    scheduled = dt.date(2026, 8, 5)
    monkeypatch.setattr(servermod, "utc_today", lambda: today)
    workout_id, activity_id = _today_profile_workout(uid, scheduled.isoformat())

    reconciled = client.post(f"/api/plan/workout/{workout_id}/reconcile")

    assert reconciled.status_code == 200
    assert reconciled.json() == {
        "id": workout_id,
        "status": "matched",
        "matched": True,
    }
    assert (
        db.get_plan_workout(uid, workout_id)["completed_activity_id"]
        == activity_id
    )


def _manual_activity(uid, date, duration_s, suffix):
    return db.insert_activity(
        uid,
        {
            "dedup_hash": f"manual-{uid}-{date}-{suffix}",
            "filename": f"manual-{suffix}.fit",
            "start_time": f"{date}T10:00:00",
            "duration_s": duration_s,
            "distance_m": 0,
            "avg_power": 100,
            "avg_hr": None,
            "np": 100,
            "if_": 0.5,
            "tss": 10,
            "streams": {"power": [0.0] * min(duration_s, 600)},
        },
    )


def test_calendar_manual_completion_uses_closest_unused_same_day_activity(client):
    _register(client)
    uid = db.get_user_by_username("rider")["id"]
    date = "2026-06-05"
    plan_id = db.create_plan(uid, "Manual", date, 1)
    workout_id = db.add_plan_workout(
        plan_id, uid, date, "W", "threshold", 3600, 60, "<x/>"
    )
    farther = _manual_activity(uid, date, 1800, "farther")
    closest = _manual_activity(uid, date, 3500, "closest")

    response = client.post(f"/api/plan/workout/{workout_id}/complete")

    assert response.status_code == 200
    assert response.json()["activity_id"] == closest
    linked = db.get_plan_workout(uid, workout_id)
    assert linked["completed_activity_id"] == closest
    assert linked["compliance"] is None
    assert farther not in db.completed_activity_ids(uid)


def test_calendar_manual_completion_reports_no_activity_and_used_activity(client):
    _register(client)
    uid = db.get_user_by_username("rider")["id"]
    date = "2026-06-06"
    plan_id = db.create_plan(uid, "Manual errors", date, 1)
    first = db.add_plan_workout(
        plan_id, uid, date, "First", "endurance", 3600, 50, "<x/>"
    )
    second = db.add_plan_workout(
        plan_id, uid, date, "Second", "endurance", 3600, 50, "<x/>"
    )
    no_activity = client.post(f"/api/plan/workout/{first}/complete")
    assert no_activity.status_code == 400
    assert "no activity" in no_activity.json()["error"]

    activity_id = _manual_activity(uid, date, 3600, "only")
    assert client.post(f"/api/plan/workout/{first}/complete").status_code == 200
    used = client.post(f"/api/plan/workout/{second}/complete")
    assert used.status_code == 409
    assert "already linked" in used.json()["error"]
    assert db.get_plan_workout(uid, second)["completed_activity_id"] is None
    assert db.completed_activity_ids(uid) == {activity_id}


def test_calendar_manual_completion_is_user_scoped(client):
    _register(client, "alice")
    alice = db.get_user_by_username("alice")["id"]
    date = "2026-06-07"
    plan_id = db.create_plan(alice, "Alice", date, 1)
    workout_id = db.add_plan_workout(
        plan_id, alice, date, "Alice W", "endurance", 3600, 50, "<x/>"
    )
    client.post("/logout")
    _register(client, "bob")
    bob = db.get_user_by_username("bob")["id"]
    _manual_activity(bob, date, 3600, "bob")

    response = client.post(f"/api/plan/workout/{workout_id}/complete")

    assert response.status_code == 404
    assert db.get_plan_workout(alice, workout_id)["completed_activity_id"] is None


def test_calendar_future_workout_cannot_reconcile_or_be_manually_completed(client):
    _register(client)
    uid = db.get_user_by_username("rider")["id"]
    date = "2099-06-07"
    plan_id = db.create_plan(uid, "Future", date, 1)
    workout_id = db.add_plan_workout(
        plan_id, uid, date, "Future W", "endurance", 3600, 50, "<x/>"
    )
    _manual_activity(uid, date, 3600, "future")

    reconcile = client.post(f"/api/plan/workout/{workout_id}/reconcile")
    manual = client.post(f"/api/plan/workout/{workout_id}/complete")

    assert reconcile.status_code == 200
    assert reconcile.json()["status"] == "future"
    assert reconcile.json()["matched"] is False
    assert manual.status_code == 400
    assert "future workouts" in manual.json()["error"]
    assert db.get_plan_workout(uid, workout_id)["completed_activity_id"] is None


def test_calendar_manual_completion_ui_refreshes_completion_state(client):
    _register(client)
    text = client.get("/calendar").text

    assert 'id="wmCompleteButton"' in text
    assert "window.location.reload()" in text
    assert "today's imported Zwift activity" not in text


def test_calendar_click_rejects_profile_mismatch_and_foreign_workout(client, monkeypatch):
    import wattracker.server as servermod

    _register(client, "alice")
    alice = db.get_user_by_username("alice")["id"]
    today = dt.date(2026, 8, 5)
    monkeypatch.setattr(servermod, "utc_today", lambda: today)
    workout_id, _ = _today_profile_workout(alice, today.isoformat(), power_scale=0.0)

    no_match = client.post(f"/api/plan/workout/{workout_id}/reconcile")
    assert no_match.status_code == 200
    assert no_match.json()["status"] == "no_match"
    assert db.get_plan_workout(alice, workout_id)["completed_activity_id"] is None

    client.post("/logout")
    _register(client, "bob")
    assert client.post(
        f"/api/plan/workout/{workout_id}/reconcile"
    ).status_code == 404


def test_calendar_click_does_not_trust_mismatched_precompleted_profile(
    client, monkeypatch
):
    import wattracker.server as servermod

    _register(client)
    uid = db.get_user_by_username("rider")["id"]
    today = dt.date(2026, 8, 5)
    monkeypatch.setattr(servermod, "utc_today", lambda: today)
    workout_id, activity_id = _today_profile_workout(
        uid, today.isoformat(), power_scale=0.0
    )
    assert db.mark_plan_workout_completed(
        uid, workout_id, activity_id, today.isoformat()
    )

    reconciled = client.post(f"/api/plan/workout/{workout_id}/reconcile")
    assert reconciled.json() == {
        "id": workout_id,
        "status": "unverified_completion",
        "matched": False,
    }
    detail = client.get(f"/api/plan/workout/{workout_id}").json()
    assert detail["completed"] is True
    assert detail["completion_verified"] is False
    assert detail["rpe_eligible"] is False
    assert client.post(
        f"/api/plan/workout/{workout_id}/rpe", json={"rpe": 10}
    ).status_code == 400
    assert "Rate completed workouts" not in client.get(
        "/calendar?year=2026&month=8"
    ).text


def test_stored_compliance_cannot_verify_oversized_linked_activity(
    client, monkeypatch
):
    import wattracker.server as servermod

    _register(client)
    uid = db.get_user_by_username("rider")["id"]
    today = dt.date(2026, 8, 5)
    monkeypatch.setattr(servermod, "utc_today", lambda: today)
    workout_id, _ = _today_profile_workout(uid, today.isoformat())
    workout = db.get_plan_workout(uid, workout_id)
    profile = importer._zwo_fraction_profile(workout["zwo_or_segments"])
    oversized_power = [p * 210 for p in profile for _ in range(2)]
    oversized_id = db.insert_activity(
        uid,
        {
            "dedup_hash": "oversized-linked-activity",
            "filename": "oversized.fit",
            "start_time": f"{today.isoformat()}T12:00:00",
            "duration_s": len(oversized_power),
            "distance_m": 0,
            "avg_power": 210,
            "avg_hr": None,
            "np": 210,
            "if_": 1.0,
            "tss": workout["tss"],
            "streams": {"power": oversized_power},
        },
    )
    assert db.mark_plan_workout_completed(
        uid, workout_id, oversized_id, today.isoformat(), .95, 210
    )
    linked = db.get_plan_workout(uid, workout_id)

    assert not importer.plan_workout_completion_verified(uid, linked)
    assert not importer.save_workout_rpe(uid, "plan", workout_id, 10)
    detail = client.get(f"/api/plan/workout/{workout_id}").json()
    assert detail["completion_verified"] is False
    assert detail["rpe_eligible"] is False
    assert client.post(
        f"/api/plan/workout/{workout_id}/rpe", json={"rpe": 10}
    ).status_code == 400
    assert db.get_plan_workout(uid, workout_id)["rpe"] is None


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
    full = db.get_plan_workout(uid, w["id"])
    profile = importer._zwo_fraction_profile(full["zwo_or_segments"])
    activity_id = db.insert_activity(
        uid,
        {
            "dedup_hash": f"completed-{uid}-{w['id']}",
            "filename": "completed.fit",
            "start_time": f"{w['date']}T10:00:00",
            "duration_s": len(profile),
            "distance_m": 0,
            "avg_power": 210,
            "avg_hr": None,
            "np": 210,
            "if_": 1.0,
            "tss": w["tss"],
            "streams": {"power": [p * 210 for p in profile]},
        },
    )
    db.mark_plan_workout_completed(
        uid, w["id"], activity_id, w["date"], .95, 210
    )
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
    client.post("/logout")
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


def test_rpe_ten_is_reported_as_too_hard_for_ftp_feedback(client):
    _register(client)
    client.post("/generate/plan", data=PLAN_FORM)
    _uid, wid = _completed_workout_id(client)
    r = client.post(f"/api/plan/workout/{wid}/rpe", json={"rpe": 10})
    assert r.status_code == 200
    assert r.json()["too_hard"] is True
    detail = client.get(f"/api/plan/workout/{wid}").json()
    assert detail["rpe"] == 10
    assert detail["too_hard"] is True


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
    client.post("/logout")
    _register(client, "bob")
    # Bob's August calendar has no workouts from Alice's plan.
    r = client.get("/calendar?year=2026&month=8")
    assert r.status_code == 200
    assert "Intervals" not in r.text
    assert "cal-workout" not in r.text


# ---------------------------------------------------------- plan management
def test_generate_redirects_to_plan(client):
    _register(client)
    # No auto-follow so we can see the redirect itself.
    r = client.get("/generate", follow_redirects=False)
    assert r.status_code == 307
    assert r.headers["location"] == "/plan"


def test_plan_page_no_plans_message(client):
    _register(client)
    text = client.get("/plan").text
    assert "No training plans yet" in text


def test_plan_page_current_plan_covers_today(client, monkeypatch):
    """A plan whose date range covers today is shown as the current plan and
    marked in effect (no 'not currently in effect' label)."""
    import wattracker.server as servermod

    _register(client)
    uid = db.get_user_by_username("rider")["id"]
    today = dt.date(2026, 8, 20)
    monkeypatch.setattr(servermod, "utc_today", lambda: today)
    # Plan starts before today, ends after (4 weeks from Aug 3).
    form = dict(PLAN_FORM, name="Live Plan")
    client.post("/generate/plan", data=form)
    text = client.get("/plan").text
    assert "Live Plan" in text
    assert "Current plan" in text
    assert "not currently in effect" not in text


def test_plan_page_not_in_effect_when_no_plan_covers_today(client, monkeypatch):
    import wattracker.server as servermod

    _register(client)
    today = dt.date(2027, 1, 1)  # long after the Aug 2026 plan window
    monkeypatch.setattr(servermod, "utc_today", lambda: today)
    client.post("/generate/plan", data=dict(PLAN_FORM, name="Old Plan"))
    text = client.get("/plan").text
    assert "Old Plan" in text
    assert "not currently in effect" in text


def test_delete_plan_removes_rows_and_files(client, tmp_path):
    import os
    from wattracker import exporter
    from wattracker.prescribe import zwo

    out = tmp_path / "zwo"
    _register(client)
    uid = db.get_user_by_username("rider")["id"]
    db.save_user_settings(uid, {"workouts_dir": str(out), "zwift_id": "me"})
    client.post("/generate/plan", data=PLAN_FORM)  # 16 workouts, auto-exports
    plan_id = db.list_plans(uid)[0]["id"]
    workouts = db.plan_workouts_for_plan(uid, plan_id)
    assert len(os.listdir(out)) == 16

    # remove_plan_exports must run before the rows are gone -> route order.
    r = client.post(f"/plan/{plan_id}/delete", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/plan?flash=")

    # DB rows gone (user-scoped) and all .zwo files pruned from the folder.
    assert db.get_plan(uid, plan_id) is None
    assert db.plan_workouts_for_plan(uid, plan_id) == []
    for w in workouts:
        assert not os.path.exists(out / zwo.plan_filename(w["date"], w["name"]))
    assert os.listdir(out) == []


def test_delete_plan_direct_helper(client, tmp_path):
    _register(client)
    uid = db.get_user_by_username("rider")["id"]
    client.post("/generate/plan", data=PLAN_FORM)
    plan_id = db.list_plans(uid)[0]["id"]
    counts = db.delete_plan(uid, plan_id)
    assert counts == {"workouts": 16, "plans": 1}
    assert db.get_plan(uid, plan_id) is None
    # Deleting again is a clean no-op (None -> caller 404s).
    assert db.delete_plan(uid, plan_id) is None


def test_delete_plan_other_user_404(client):
    _register(client, "alice")
    client.post("/generate/plan", data=PLAN_FORM)
    alice_uid = db.get_user_by_username("alice")["id"]
    plan_id = db.list_plans(alice_uid)[0]["id"]
    client.post("/logout")

    _register(client, "bob")
    r = client.post(f"/plan/{plan_id}/delete", follow_redirects=False)
    assert r.status_code == 404
    # Alice's plan is untouched.
    assert db.get_plan(alice_uid, plan_id) is not None


# --------------------------------------------------- reopen plan via GET /plan
def test_get_plan_with_plan_id_shows_summary(client):
    _register(client)
    client.post("/generate/plan", data=PLAN_FORM)
    uid = db.get_user_by_username("rider")["id"]
    plan_id = db.list_plans(uid)[0]["id"]

    text = client.get(f"/plan?plan_id={plan_id}").text
    assert "Base Plan" in text
    assert "Export plan to Zwift" in text
    assert "Download .zip" in text
    assert f'action="/plan/{plan_id}/export"' in text
    assert f'/plan/{plan_id}/download.zip' in text


def test_get_plan_with_foreign_plan_id_does_not_leak(client):
    _register(client, "alice")
    client.post("/generate/plan", data=dict(PLAN_FORM, name="Alice Plan"))
    alice_uid = db.get_user_by_username("alice")["id"]
    plan_id = db.list_plans(alice_uid)[0]["id"]
    client.post("/logout")

    _register(client, "bob")
    r = client.get(f"/plan?plan_id={plan_id}")
    assert r.status_code == 200
    assert "Alice Plan" not in r.text
    assert "Export plan to Zwift" not in r.text


def test_get_plan_with_unknown_plan_id_renders_page(client):
    _register(client)
    r = client.get("/plan?plan_id=999999")
    assert r.status_code == 200
    assert "No training plans yet" in r.text


def test_plan_management_links_to_summary(client):
    _register(client)
    client.post("/generate/plan", data=PLAN_FORM)
    uid = db.get_user_by_username("rider")["id"]
    plan_id = db.list_plans(uid)[0]["id"]
    text = client.get("/plan").text
    assert f'/plan?plan_id={plan_id}' in text


def test_plan_management_other_plans_links(client):
    _register(client)
    client.post("/generate/plan", data=dict(PLAN_FORM, name="Plan A"))
    client.post("/generate/plan", data=dict(PLAN_FORM, name="Plan B", start_date="2026-09-02"))
    uid = db.get_user_by_username("rider")["id"]
    plans = db.list_plans(uid)  # created DESC -> [B, A]
    other_id = plans[1]["id"]
    text = client.get("/plan").text
    assert f'/plan?plan_id={other_id}' in text


def test_generate_plan_post_flow_unchanged(client):
    """Post-generation summary (with weekly table and hard-fraction) still
    renders exactly as before — the new GET path must not regress it."""
    _register(client)
    r = client.post("/generate/plan", data=PLAN_FORM)
    assert r.status_code == 200
    assert "weekly-table" in r.text
    assert "hard time" in r.text


