"""Tests for weekly_volume aggregation + the /volume page and /api/volume."""
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


def _seed(uid, start_time, watts=200.0, seconds=3600, distance_m=30000.0,
          tss=60.0):
    db.insert_activity(
        uid,
        {
            "dedup_hash": f"h-{uid}-{start_time}",
            "filename": "a.fit",
            "start_time": start_time,
            "duration_s": seconds,
            "distance_m": distance_m,
            "avg_power": watts,
            "avg_hr": 0.0,
            "np": watts,
            "if_": 1.0,
            "tss": tss,
            "streams": {},
        },
    )


# ------------------------------------------------------------- db aggregation
def test_two_weeks_aggregate_separately(user_id):
    # Week of 2026-07-06 (Mon) and week of 2026-07-13 (Mon).
    _seed(user_id, "2026-07-08T10:00:00")  # Wed of first week
    _seed(user_id, "2026-07-15T10:00:00")  # Wed of second week
    rows = db.weekly_volume(user_id)
    assert [r["week_start"] for r in rows] == ["2026-07-06", "2026-07-13"]


def test_monday_bucketing_groups_whole_week(user_id):
    # Monday, mid-week, and Sunday of the same week all bucket to that Monday.
    _seed(user_id, "2026-07-13T06:00:00")  # Monday
    _seed(user_id, "2026-07-16T06:00:00")  # Thursday
    _seed(user_id, "2026-07-19T23:00:00")  # Sunday
    rows = db.weekly_volume(user_id)
    assert len(rows) == 1
    assert rows[0]["week_start"] == "2026-07-13"
    assert rows[0]["hours"] == pytest.approx(3.0)  # 3 x 1h


def test_calories_math(user_id):
    # 200 W * 3600 s / 1000 = 720 kJ ~= 720 kcal.
    _seed(user_id, "2026-07-13T10:00:00", watts=200.0, seconds=3600)
    rows = db.weekly_volume(user_id)
    assert rows[0]["calories"] == pytest.approx(720)


def test_null_avg_power_contributes_zero_calories(user_id):
    _seed(user_id, "2026-07-13T10:00:00", watts=200.0, seconds=3600)
    # Second activity same week with NULL power.
    db.insert_activity(
        user_id,
        {
            "dedup_hash": "h-nullpower",
            "filename": "b.fit",
            "start_time": "2026-07-14T10:00:00",
            "duration_s": 3600,
            "distance_m": 20000.0,
            "avg_power": None,
            "tss": None,
            "streams": {},
        },
    )
    rows = db.weekly_volume(user_id)
    assert len(rows) == 1
    r = rows[0]
    assert r["calories"] == pytest.approx(720)      # only the powered ride
    assert r["hours"] == pytest.approx(2.0)         # both durations counted
    assert r["distance_km"] == pytest.approx(50.0)  # 30 + 20
    assert r["tss"] == pytest.approx(60.0)          # NULL tss counted as 0


def test_null_start_time_skipped(user_id):
    _seed(user_id, "2026-07-13T10:00:00")
    db.insert_activity(
        user_id,
        {
            "dedup_hash": "h-nostart",
            "filename": "c.fit",
            "start_time": None,
            "duration_s": 3600,
            "distance_m": 10000.0,
            "avg_power": 100.0,
            "tss": 10.0,
            "streams": {},
        },
    )
    rows = db.weekly_volume(user_id)
    assert len(rows) == 1
    assert rows[0]["week_start"] == "2026-07-13"


def test_empty_user_returns_empty(user_id):
    assert db.weekly_volume(user_id) == []


def test_weekly_volume_user_scoped(user_id):
    from wattracker import auth
    other = db.create_user("other", auth.hash_password("password123"))
    _seed(user_id, "2026-07-13T10:00:00")
    assert db.weekly_volume(other) == []


# ------------------------------------------------------------------- routes
def _register(client, username="rider", password="password123"):
    client.post("/register", data={"username": username, "password": password})
    return db.get_user_by_username(username)["id"]


def test_volume_page_auth_gated(client):
    r = client.get("/volume", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


def test_api_volume_auth_gated(client):
    r = client.get("/api/volume", follow_redirects=False)
    assert r.status_code == 303


def test_volume_page_renders(client):
    _register(client)
    r = client.get("/volume")
    assert r.status_code == 200
    assert "Training Volume" in r.text
    assert "Latest 4 weeks" in r.text
    assert "preceding four" in r.text
    assert "volume.js?v=" in r.text


def test_nav_has_volume_link(client):
    _register(client)
    assert '/volume"' in client.get("/").text


def test_api_volume_shape(client):
    uid = _register(client)
    _seed(uid, "2026-07-13T10:00:00", watts=200.0, seconds=3600)
    data = client.get("/api/volume").json()
    assert set(data.keys()) == {"weeks"}
    assert len(data["weeks"]) == 1
    w = data["weeks"][0]
    assert set(w.keys()) == {"week_start", "hours", "tss", "distance_km", "calories"}
    assert w["week_start"] == "2026-07-13"
    assert w["calories"] == pytest.approx(720)


def test_api_volume_empty_user(client):
    _register(client)
    assert client.get("/api/volume").json() == {"weeks": []}
