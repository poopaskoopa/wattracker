"""Endpoint tests for the dashboard time-series API (months param, ftp_series)."""
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


def _seed(uid, when, watts=300.0, seconds=1200):
    db.insert_activity(
        uid,
        {
            "dedup_hash": f"h-{uid}-{when.isoformat()}",
            "filename": "a.fit",
            "start_time": when.isoformat(),
            "duration_s": seconds,
            "distance_m": 0.0,
            "avg_power": watts,
            "avg_hr": 0.0,
            "np": watts,
            "if_": 1.0,
            "tss": 100.0,
            "streams": {"power": [watts] * seconds},
        },
    )


def _setup(client):
    client.post("/register", data={"username": "rider", "password": "password123"})
    uid = db.get_user_by_username("rider")["id"]
    now = dt.datetime.now().replace(microsecond=0)
    _seed(uid, now - dt.timedelta(days=60), watts=300.0)
    _seed(uid, now, watts=340.0)
    return uid


def test_api_load_months_filters(client):
    _setup(client)
    full = client.get("/api/load").json()
    filtered = client.get("/api/load?months=1").json()
    assert len(full) > 30  # ~61 days of gap-filled series
    assert len(filtered) < len(full)
    assert len(filtered) > 0


def test_api_ftp_series_shape_and_content(client):
    _setup(client)
    data = client.get("/api/ftp_series").json()
    assert set(data.keys()) == {"estimated", "recorded"}
    assert isinstance(data["estimated"], list)
    assert len(data["estimated"]) > 0
    for p in data["estimated"]:
        assert "date" in p and "ftp" in p


def test_api_ftp_series_months_param_accepted(client):
    _setup(client)
    r = client.get("/api/ftp_series?months=1")
    assert r.status_code == 200
    assert set(r.json().keys()) == {"estimated", "recorded"}


def test_api_ftp_still_returns_recorded_list(client):
    uid = _setup(client)
    db.add_ftp_entry(uid, dt.date.today().isoformat(), 300.0, "manual")
    data = client.get("/api/ftp").json()
    assert isinstance(data, list)
    assert len(data) == 1
    assert data[0]["source"] == "manual"


def test_api_endpoints_empty_user_no_error(client):
    client.post("/register", data={"username": "fresh", "password": "password123"})
    assert client.get("/api/load").json() == []
    assert client.get("/api/ftp_series").json() == {"estimated": [], "recorded": []}
    assert client.get("/api/ftp").json() == []
