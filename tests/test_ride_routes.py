"""Route + WebSocket tests for the Ride page (no hardware; availability is
forced via monkeypatch so results don't depend on whether bleak is installed)."""
import pytest

pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from wattracker import db  # noqa: E402
from wattracker.ble import devices as bledevices  # noqa: E402
from wattracker.server import create_app  # noqa: E402


@pytest.fixture()
def client():
    app = create_app()
    with TestClient(app) as c:
        yield c


def _register(client, username="rider"):
    client.post("/register", data={"username": username, "password": "password123"})


def _force_bt_unavailable(monkeypatch):
    # Force the "no Bluetooth" branch regardless of whether the [ble] extra
    # (bleak) is installed in the test environment, so the suite is deterministic
    # for developers who have installed real-hardware support.
    monkeypatch.setattr(
        bledevices,
        "bluetooth_available",
        lambda: (False, "bleak not installed (ModuleNotFoundError)"),
    )


def test_ride_page_renders_unavailable(client, monkeypatch):
    # Bluetooth unavailable -> page still loads and offers Simulate.
    _force_bt_unavailable(monkeypatch)
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


def test_ride_status_endpoint(client, monkeypatch):
    _force_bt_unavailable(monkeypatch)
    _register(client)
    data = client.get("/ride/status").json()
    assert data["available"] is False
    assert "bleak" in data["reason"]


def test_ride_scan_unavailable(client, monkeypatch):
    _force_bt_unavailable(monkeypatch)
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


def test_ride_ws_unavailable_without_sim(client, monkeypatch):
    _force_bt_unavailable(monkeypatch)
    _register(client)
    with client.websocket_connect("/ride/ws?type=endurance&minutes=30") as ws:
        msg = ws.receive_json()
        assert msg["status"] == "unavailable"


# ------------------------------------------------ real-hardware path (mocked)
def _patch_real_ride(monkeypatch, trainer, power_script):
    from wattracker import server as servermod
    from wattracker.ble.devices import (
        SimulatedHeartRateSource,
        SimulatedPowerSource,
    )

    ps = SimulatedPowerSource(power_script)
    hr = SimulatedHeartRateSource(fixed=142)
    names = {"power": "FakePM"}
    if trainer is not None:
        names["trainer"] = "FakeKickr"

    async def fake_connect(timeout=6.0):
        return {
            "trainer": trainer, "power_source": ps, "hr_source": hr,
            "clients": [], "names": names,
        }

    monkeypatch.setattr(servermod.bledevices, "bluetooth_available", lambda: (True, "ok"))
    monkeypatch.setattr(servermod.bledevices, "connect_sensors", fake_connect)
    monkeypatch.setattr(servermod, "RIDE_POLL_INTERVAL_S", 0)


def test_ride_ws_real_path_erg_drives_trainer(client, monkeypatch):
    from wattracker.ble.devices import SimulatedTrainer

    _register(client)
    trainer = SimulatedTrainer()
    # Pedal 3s, then stop pedalling until the zero-power grace auto-stops.
    _patch_real_ride(monkeypatch, trainer, [150, 150, 150] + [0] * 10)

    frames = []
    with client.websocket_connect("/ride/ws?type=endurance&minutes=30") as ws:
        first = ws.receive_json()
        assert first["status"] == "connected"
        assert first["erg"] is True
        assert first["devices"]["trainer"] == "FakeKickr"
        try:
            while True:
                frames.append(ws.receive_json())
        except Exception:
            pass
    assert frames and frames[-1]["status"] == "finished"
    # ERG lifecycle: Request Control + Start at ride start, Stop at the end,
    # with real workout targets in between and a zeroed target on finish.
    assert trainer.commands[:2] == ["request_control", "start"]
    assert trainer.commands[-1] == "stop"
    assert any(t > 0 for t in trainer.targets)
    assert trainer.targets[-1] == 0
    # The ride was saved for the user.
    uid = db.get_user_by_username("rider")["id"]
    assert len(db.list_activities(uid)) == 1


def test_ride_ws_real_path_degrades_without_trainer(client, monkeypatch):
    _register(client)
    _patch_real_ride(monkeypatch, None, [150, 150, 150] + [0] * 10)

    frames = []
    with client.websocket_connect("/ride/ws?type=endurance&minutes=30") as ws:
        first = ws.receive_json()
        assert first["status"] == "connected"
        assert first["erg"] is False  # no FTMS trainer: read-only ride
        try:
            while True:
                frames.append(ws.receive_json())
        except Exception:
            pass
    # Power display still works without a controllable trainer.
    assert any(f["power"] == 150 for f in frames)
    assert frames[-1]["status"] == "finished"


def test_ride_ws_real_path_no_devices(client, monkeypatch):
    from wattracker import server as servermod

    _register(client)

    async def fake_connect(timeout=6.0):
        return {"trainer": None, "power_source": None, "hr_source": None,
                "clients": [], "names": {}}

    monkeypatch.setattr(servermod.bledevices, "bluetooth_available", lambda: (True, "ok"))
    monkeypatch.setattr(servermod.bledevices, "connect_sensors", fake_connect)
    with client.websocket_connect("/ride/ws?type=endurance&minutes=30") as ws:
        msg = ws.receive_json()
        assert msg["status"] == "error"
        assert "No power meter" in msg["error"]
