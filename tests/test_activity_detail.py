"""Tests for the activity-detail streams endpoint + page (downsampling, gaps)."""
import pytest

from wattracker import auth, db
from wattracker.analysis import pipeline

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


def _insert(user_id, seconds=1800, streams=None, **over):
    rec = {
        "dedup_hash": over.get("dedup_hash", f"h-{user_id}-{seconds}"),
        "filename": over.get("filename", "ride.fit"),
        "start_time": over.get("start_time", "2026-06-01T10:00:00"),
        "duration_s": seconds, "distance_m": 1000.0, "avg_power": 200.0,
        "avg_hr": 140.0, "np": 205.0, "if_": 0.8, "tss": 50.0,
        "streams": streams if streams is not None else {
            "time": [None] * seconds,
            "power": [float(200 + i % 50) for i in range(seconds)],
            "heartrate": [float(140 + i % 20) for i in range(seconds)],
            "cadence": [float(90 + i % 10) for i in range(seconds)],
            "altitude": [float(100 + i % 30) for i in range(seconds)],
        },
    }
    return db.insert_activity(user_id, rec)


# --------------------------------------------------------- downsampling
def test_downsample_averages_to_target():
    out = pipeline._downsample(list(range(10000)), 1000)
    assert 900 <= len(out) <= 1100
    assert out[0] < out[-1]  # monotonic input stays monotonic


def test_downsample_none_safe():
    assert pipeline._downsample([], 100) == []
    assert pipeline._downsample([None, None], 100) == []
    # A short stream is returned as-is (rounded), preserving None gaps.
    assert pipeline._downsample([1.0, None, 3.0], 100) == [1.0, None, 3.0]


# ------------------------------------------------------ activity_detail()
def test_activity_detail_shape_and_downsample(user_id):
    aid = _insert(user_id, seconds=6000)
    d = pipeline.activity_detail(user_id, aid, max_points=1500)
    assert d["id"] == aid
    assert d["points"] <= 1600
    for k in ("power", "heartrate", "cadence", "altitude", "t"):
        assert len(d[k]) == d["points"]
    assert d["have"] == {"power": True, "heartrate": True,
                         "cadence": True, "altitude": True}
    # x axis is elapsed minutes, ascending from 0.
    assert d["t"][0] == 0.0 and d["t"][-1] > d["t"][0]


def test_activity_detail_missing_streams_graceful(user_id):
    # Power-only ride (old import without HR/cadence/altitude).
    aid = _insert(user_id, seconds=600, streams={
        "time": [None] * 600, "power": [180.0] * 600,
        "heartrate": [], "cadence": [], "altitude": []})
    d = pipeline.activity_detail(user_id, aid)
    assert d["have"] == {"power": True, "heartrate": False,
                         "cadence": False, "altitude": False}
    assert d["power"] and d["heartrate"] == [] and d["altitude"] == []


def test_activity_detail_no_streams_at_all(user_id):
    aid = _insert(user_id, seconds=0, streams={})
    d = pipeline.activity_detail(user_id, aid)
    assert d["points"] == 0
    assert not any(d["have"].values())


def test_activity_detail_user_scoped(user_id):
    other = db.create_user("other", auth.hash_password("password123"))
    aid = _insert(user_id, seconds=600)
    assert pipeline.activity_detail(user_id, aid) is not None
    assert pipeline.activity_detail(other, aid) is None


def test_activity_detail_missing_activity(user_id):
    assert pipeline.activity_detail(user_id, 999999) is None


# ----------------------------------------------------------------- routes
def test_activity_detail_page_and_link(client):
    _register(client)
    uid = db.get_user_by_username("rider")["id"]
    aid = _insert(uid, seconds=1200)
    # Activities list links to the detail page.
    assert f'/activity/{aid}' in client.get("/activities").text
    page = client.get(f"/activity/{aid}")
    assert page.status_code == 200
    assert "Ride graphs" in page.text
    assert "renderActivityDetail" in page.text


def test_activity_detail_api_json(client):
    _register(client)
    uid = db.get_user_by_username("rider")["id"]
    aid = _insert(uid, seconds=1200)
    r = client.get(f"/api/activity/{aid}")
    assert r.status_code == 200
    d = r.json()
    assert d["id"] == aid and d["have"]["power"] is True
    assert len(d["t"]) == d["points"]
    assert set(d["zones"]) == {"power", "heart_rate"}
    assert "covered_s" in d["zones"]["power"]


def test_activity_detail_page_renders_zone_summary_without_replacing_graph(client):
    _register(client)
    uid = db.get_user_by_username("rider")["id"]
    db.save_user_settings(uid, {"ftp": 200})
    db.set_user_hr_max(uid, 190)
    aid = _insert(uid, seconds=1200)
    text = client.get(f"/activity/{aid}").text
    assert "Time in zones" in text
    assert 'id="powerZoneSummary"' in text
    assert 'id="hrZoneSummary"' in text
    assert 'id="detailChart"' in text
    assert "renderActivityDetail" in text


def test_activity_detail_api_404_and_scoped(client):
    _register(client, "alice")
    uid = db.get_user_by_username("alice")["id"]
    aid = _insert(uid, seconds=600)
    client.post("/logout")
    _register(client, "bob")
    assert client.get(f"/api/activity/{aid}").status_code == 404


def test_activity_page_redirects_when_not_owner(client):
    _register(client, "alice")
    uid = db.get_user_by_username("alice")["id"]
    aid = _insert(uid, seconds=600)
    client.post("/logout")
    _register(client, "bob")
    r = client.get(f"/activity/{aid}", follow_redirects=False)
    assert r.status_code == 303
