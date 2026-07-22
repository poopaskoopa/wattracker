"""Type-aware perceived-exertion (RPE) rating for imported activities.

Covers the schema column, the activity-scoped subjective-rating endpoint, and
the type-aware linkage exposed by the activity-detail API (a ride matched to a
verified plan/standalone workout drives that workout's rating; an unmatched
ride carries its own subjective rating).
"""
import sqlite3

import pytest

from wattracker import auth, db

pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from wattracker.server import create_app  # noqa: E402


@pytest.fixture()
def client():
    app = create_app()
    with TestClient(app) as c:
        yield c


def _register(client, username="rider"):
    client.post("/register", data={"username": username, "password": "password123"})
    return db.get_user_by_username(username)["id"]


def _insert(user_id, seconds=1800, start="2026-06-01T10:00:00", **over):
    rec = {
        "dedup_hash": over.get("dedup_hash", f"h-{user_id}-{seconds}-{start}"),
        "filename": "ride.fit", "start_time": start,
        "duration_s": seconds, "distance_m": 1000.0, "avg_power": 200.0,
        "avg_hr": 140.0, "np": 205.0, "if_": 0.8, "tss": 50.0,
        "streams": {"time": [None] * seconds, "power": [200.0] * seconds},
    }
    return db.insert_activity(user_id, rec)


# ------------------------------------------------------------------ schema
def test_fresh_schema_has_activities_rpe():
    db.init_db()
    conn = db.connect()
    try:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(activities)")}
    finally:
        conn.close()
    assert "rpe" in cols


def test_migration_adds_activities_rpe(tmp_path):
    """An older DB (no activities.rpe) gains the column on init_db()."""
    path = str(tmp_path / "old.db")
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE activities (id INTEGER PRIMARY KEY, user_id INTEGER, "
        "dedup_hash TEXT, start_time TEXT, tss REAL);"
        "CREATE TABLE plan_workouts (id INTEGER PRIMARY KEY, user_id INTEGER, "
        "date TEXT, completed_activity_id INTEGER);"
        "CREATE TABLE standalone_workouts (id INTEGER PRIMARY KEY, "
        "user_id INTEGER, scheduled_date TEXT, completed_activity_id INTEGER);"
    )
    conn.execute("PRAGMA user_version = 17")
    conn.commit()
    conn.close()

    db.init_db(path)

    conn = sqlite3.connect(path)
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(activities)")}
        ver = conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()
    assert "rpe" in cols
    assert ver == db.SCHEMA_VERSION


# ------------------------------------------------ activity rpe endpoint
def test_activity_rpe_stores_and_reflects(client):
    uid = _register(client)
    aid = _insert(uid)
    r = client.post(f"/api/activity/{aid}/rpe", json={"rpe": 7})
    assert r.status_code == 200
    assert r.json() == {"id": aid, "rpe": 7}
    assert db.get_activity(uid, aid)["rpe"] == 7
    # Detail API surfaces the subjective rating for an unlinked ride.
    d = client.get(f"/api/activity/{aid}").json()
    assert d["rpe"] == 7
    assert "linked_workout" not in d


@pytest.mark.parametrize("bad", [0, 11, -1, 100])
def test_activity_rpe_rejects_out_of_range(client, bad):
    uid = _register(client)
    aid = _insert(uid)
    r = client.post(f"/api/activity/{aid}/rpe", json={"rpe": bad})
    assert r.status_code == 400
    assert db.get_activity(uid, aid)["rpe"] is None


def test_activity_rpe_404_for_missing(client):
    _register(client)
    r = client.post("/api/activity/999999/rpe", json={"rpe": 5})
    assert r.status_code == 404


def test_activity_rpe_user_scoped(client):
    alice = _register(client, "alice")
    aid = _insert(alice)
    client.post("/logout")
    _register(client, "bob")
    r = client.post(f"/api/activity/{aid}/rpe", json={"rpe": 5})
    assert r.status_code == 404
    assert db.get_activity(alice, aid)["rpe"] is None


# ----------------------------------------------- type-aware linkage
def test_detail_exposes_verified_plan_link(client):
    uid = _register(client)
    aid = _insert(uid, seconds=3600, start="2026-06-02T09:00:00")
    plan_id = db.create_plan(uid, "Base", "2026-06-01", 4)
    # Trivial prescription (no objective power profile) => completion verified
    # by legacy duration/TSS semantics once linked.
    wid = db.add_plan_workout(
        plan_id, uid, "2026-06-02", "Endurance", "endurance", 3600, 50.0, "<>"
    )
    assert db.mark_plan_workout_completed(uid, wid, aid, "2026-06-02")

    d = client.get(f"/api/activity/{aid}").json()
    link = d["linked_workout"]
    assert link["kind"] == "plan"
    assert link["id"] == wid
    assert link["name"] == "Endurance"
    assert link["rpe_eligible"] is True

    # Rating a matched ride goes through the workout endpoint and feeds it.
    r = client.post(f"/api/plan/workout/{wid}/rpe", json={"rpe": 6})
    assert r.status_code == 200
    assert db.get_plan_workout(uid, wid)["rpe"] == 6
    # The activity's own subjective column stays untouched.
    assert db.get_activity(uid, aid)["rpe"] is None


def test_detail_exposes_verified_standalone_link(client):
    uid = _register(client)
    aid = _insert(uid, seconds=3600, start="2026-06-03T09:00:00")
    wid = db.add_standalone_workout(
        uid, "key-1", "2026-06-03", "Sweet Spot", "sweet_spot",
        3600, 60.0, "<workout/>", 250.0
    )
    assert db.mark_standalone_completed(uid, wid, aid, "2026-06-03", None, None)

    d = client.get(f"/api/activity/{aid}").json()
    link = d["linked_workout"]
    assert link["kind"] == "standalone"
    assert link["id"] == wid
    assert link["rpe_eligible"] is True


def test_unlinked_activity_has_no_link(client):
    uid = _register(client)
    aid = _insert(uid)
    d = client.get(f"/api/activity/{aid}").json()
    assert "linked_workout" not in d
    assert d["rpe"] is None
