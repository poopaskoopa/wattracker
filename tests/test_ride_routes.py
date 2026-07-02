"""Route + WebSocket tests for the Ride page (no hardware, bleak absent)."""
import pytest

pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from tranalyzer import db  # noqa: E402
from tranalyzer.ble import devices as bledevices  # noqa: E402
from tranalyzer.server import create_app  # noqa: E402


@pytest.fixture()
def client():
    app = create_app()
    with TestClient(app) as c:
        yield c


def _register(client, username="rider"):
    client.post("/register", data={"username": username, "password": "password123"})


def test_ride_page_renders_unavailable(client):
    # bleak is absent in the test env -> Bluetooth unavailable, page still loads.
    _register(client)
    r = client.get("/ride")
    assert r.status_code == 200
    assert "Bluetooth unavailable" in r.text
    assert "Simulate" in r.text


def test_ride_page_renders_available_when_monkeypatched(client, monkeypatch):
    _register(client)
    monkeypatch.setattr(bledevices, "bluetooth_available", lambda: (True, "ok"))
    r = client.get("/ride")
    assert r.status_code == 200
    assert "Bluetooth available" in r.text


def test_ride_status_endpoint(client):
    _register(client)
    data = client.get("/ride/status").json()
    assert data["available"] is False
    assert "bleak" in data["reason"]


def test_ride_scan_unavailable(client):
    _register(client)
    r = client.post("/ride/scan")
    data = r.json()
    assert data["available"] is False
    assert data["devices"] == []


def test_ride_requires_auth(client):
    assert client.get("/ride", follow_redirects=False).status_code == 303


def test_ride_ws_simulation_streams_and_saves(client):
    _register(client)
    frames = []
    with client.websocket_connect("/ride/ws?sim=1&type=endurance&minutes=30") as ws:
        try:
            while True:
                frames.append(ws.receive_json())
        except Exception:
            pass
    assert frames, "expected streamed frames"
    assert frames[-1]["status"] == "finished"
    assert any(f["target_watts"] > 0 for f in frames)
    # A ride activity was recorded for the user.
    uid = db.get_user_by_username("rider")["id"]
    assert len(db.list_activities(uid)) == 1


def test_ride_ws_unauthenticated_closes(client):
    # No login -> WS should report an auth error and close, not crash.
    with client.websocket_connect("/ride/ws?sim=1") as ws:
        msg = ws.receive_json()
        assert msg["status"] == "error"


def test_ride_ws_unavailable_without_sim(client):
    _register(client)
    with client.websocket_connect("/ride/ws?type=endurance&minutes=30") as ws:
        msg = ws.receive_json()
        assert msg["status"] == "unavailable"
