"""Confirmation-first OOTO adjustment persistence and routes."""
import datetime as dt

import pytest

pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from wattracker import db  # noqa: E402
from wattracker.server import create_app  # noqa: E402


@pytest.fixture()
def client():
    app = create_app()
    with TestClient(app) as c:
        yield c


def _register(client):
    client.post("/register", data={"username": "rider", "password": "password123"})


def _plan(uid):
    plan_id = db.create_plan(uid, "Plan", "2026-08-03", 4)
    rows = []
    for date, kind in (
        ("2026-08-10", "threshold"),
        ("2026-08-12", "vo2max"),
        ("2026-08-20", "endurance"),
    ):
        rows.append(db.add_plan_workout(
            plan_id, uid, date, kind.title(), kind, 3600, 60.0, "<x/>",
            origin="generated",
        ))
    return plan_id, rows


def test_ooto_add_creates_pending_proposal_without_plan_mutation(client, monkeypatch):
    import wattracker.server as servermod

    _register(client)
    uid = db.get_user_by_username("rider")["id"]
    plan_id, row_ids = _plan(uid)
    monkeypatch.setattr(servermod, "utc_today", lambda: dt.date(2026, 8, 1))

    response = client.post(
        "/ooto/add",
        data={"start_date": "2026-08-10", "end_date": "2026-08-12"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "adjustment_id=" in response.headers["location"]

    pending = db.list_pending_ooto_adjustments(uid)
    assert len(pending) == 1
    assert pending[0]["plan_id"] == plan_id
    assert pending[0]["proposal"]["recommended_option"] == "reschedule"
    assert [r["adjustment_state"] for r in db.plan_workouts_for_plan(uid, plan_id)] == [
        None, None, None,
    ]
    assert row_ids == [r["id"] for r in db.plan_workouts_for_plan(uid, plan_id)]

    calendar = client.get(response.headers["location"])
    assert "Review your OOTO adjustment" in calendar.text
    assert "2026-08-10" in calendar.text and "2026-08-20" in calendar.text


def test_confirm_reschedule_records_source_and_replacement_provenance(
    client, monkeypatch,
):
    import wattracker.server as servermod

    _register(client)
    uid = db.get_user_by_username("rider")["id"]
    plan_id, row_ids = _plan(uid)
    monkeypatch.setattr(servermod, "utc_today", lambda: dt.date(2026, 8, 1))
    client.post(
        "/ooto/add",
        data={"start_date": "2026-08-10", "end_date": "2026-08-12"},
    )
    adjustment = db.list_pending_ooto_adjustments(uid)[0]

    response = client.post(
        f"/ooto-adjustment/{adjustment['id']}/confirm",
        data={"option": "reschedule"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert db.get_ooto_adjustment(uid, adjustment["id"])["status"] == "applied"

    rows = db.plan_workouts_for_plan(uid, plan_id)
    source = next(r for r in rows if r["id"] == row_ids[0])
    displaced = next(r for r in rows if r["id"] == row_ids[2])
    replacement = next(r for r in rows if r["adjustment_state"] == "rescheduled")
    assert source["adjustment_state"] == "ooto_canceled"
    assert displaced["adjustment_state"] == "displaced"
    assert replacement["date"] == "2026-08-20"
    assert replacement["adjustment_source_id"] == source["id"]
    assert replacement["origin"] == "adjusted"


def test_confirm_rebalance_modifies_target_in_place(client, monkeypatch):
    import wattracker.server as servermod

    _register(client)
    uid = db.get_user_by_username("rider")["id"]
    plan_id, row_ids = _plan(uid)
    monkeypatch.setattr(servermod, "utc_today", lambda: dt.date(2026, 8, 1))
    client.post(
        "/ooto/add",
        data={"start_date": "2026-08-10", "end_date": "2026-08-12"},
    )
    adjustment = db.list_pending_ooto_adjustments(uid)[0]

    response = client.post(
        f"/ooto-adjustment/{adjustment['id']}/confirm",
        data={"option": "rebalance"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert db.get_ooto_adjustment(uid, adjustment["id"])["status"] == "applied"
    rows = db.plan_workouts_for_plan(uid, plan_id)
    source = next(r for r in rows if r["id"] == row_ids[0])
    target = next(r for r in rows if r["id"] == row_ids[2])
    assert source["adjustment_state"] == "ooto_canceled"
    assert target["adjustment_state"] == "modified"
    assert target["adjustment_source_id"] == source["id"]
    assert target["type"] == source["type"]


def test_confirm_rejects_stale_proposal_without_mutation(client, monkeypatch):
    import wattracker.server as servermod

    _register(client)
    uid = db.get_user_by_username("rider")["id"]
    plan_id, row_ids = _plan(uid)
    monkeypatch.setattr(servermod, "utc_today", lambda: dt.date(2026, 8, 1))
    client.post(
        "/ooto/add",
        data={"start_date": "2026-08-10", "end_date": "2026-08-12"},
    )
    adjustment = db.list_pending_ooto_adjustments(uid)[0]

    conn = db.connect()
    try:
        conn.execute("UPDATE plan_workouts SET name = ? WHERE id = ?", ("Changed", row_ids[0]))
        conn.commit()
    finally:
        conn.close()

    response = client.post(
        f"/ooto-adjustment/{adjustment['id']}/confirm",
        data={"option": "reschedule"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert db.get_ooto_adjustment(uid, adjustment["id"])["status"] == "stale"
    assert len(db.plan_workouts_for_plan(uid, plan_id)) == 3
    assert all(r["adjustment_state"] is None for r in db.plan_workouts_for_plan(uid, plan_id))


def test_dismiss_keeps_ooto_skip_without_schedule_mutation(client, monkeypatch):
    import wattracker.server as servermod

    _register(client)
    uid = db.get_user_by_username("rider")["id"]
    plan_id, _ = _plan(uid)
    monkeypatch.setattr(servermod, "utc_today", lambda: dt.date(2026, 8, 1))
    client.post(
        "/ooto/add",
        data={"start_date": "2026-08-10", "end_date": "2026-08-12"},
    )
    adjustment = db.list_pending_ooto_adjustments(uid)[0]
    response = client.post(
        f"/ooto-adjustment/{adjustment['id']}/dismiss", follow_redirects=False,
    )
    assert response.status_code == 303
    assert db.get_ooto_adjustment(uid, adjustment["id"])["status"] == "dismissed"
    assert len(db.plan_workouts_for_plan(uid, plan_id)) == 3
