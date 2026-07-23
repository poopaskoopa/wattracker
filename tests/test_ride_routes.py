"""Route + WebSocket tests for the Ride page (no hardware; availability is
forced via monkeypatch so results don't depend on whether bleak is installed)."""
import asyncio

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


def _receive_after_workout(ws):
    workout = ws.receive_json()
    assert workout["status"] == "workout"
    assert workout["workout"]["name"]
    assert workout["workout"]["duration_s"] > 0
    assert workout["workout"]["profile"]
    return ws.receive_json()


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
    assert "Connect selected sensors" in r.text
    assert 'input[data-role="power"]:checked' in r.text
    assert "replaceChildren" in r.text
    assert "innerHTML" not in r.text
    assert 'id="connectionStatus"' in r.text
    assert "deviceNames[deviceAddress]" in r.text
    assert 'setLive(document.getElementById("connectionStatus"), message' in r.text
    assert "preferredPower" in r.text
    assert "No HR" in r.text
    assert "No trainer" in r.text
    assert 'q.push("prepare=1")' in r.text
    assert 'ws.send(JSON.stringify({action: "start"}))' in r.text
    assert 'id="scanBusy"' in r.text
    assert "localStorage.getItem(cacheKey)" in r.text
    assert "new Chart" in r.text
    assert "MAX_CHART_POINTS" in r.text
    assert "MAX_CHART_POINTS = 30000" in r.text
    assert "appendOrReplace(livePower" in r.text
    assert "appendOrReplace(liveHr" in r.text
    assert "normalized: true" not in r.text
    assert "hasWarnings ? \"failure\" : \"success\"" in r.text
    assert "primeAudio();" in r.text
    assert "function resetConnectedRows()" in r.text
    assert "if (ws !== socket) return" in r.text
    assert 'ws = null;' in r.text
    assert '"Disconnected.", null' in r.text
    assert "Bluetooth connection failed. Check the device and try again." in r.text
    assert 'socket.onmessage = function (ev) {\n            if (ws !== socket) return;' in r.text
    assert 'decimation: {enabled: true, algorithm: "lttb", samples: 1000' in r.text
    assert r.text.count("var wid") == 1
    assert 'getElementById("workoutSelect").disabled = active' in r.text
    assert 'id="ridePowerChart"' in r.text
    assert 'id="rideHrChart"' in r.text
    assert "Cadence (power sensor)" in r.text
    assert "devices.slice().sort" in r.text
    assert "playCue(\"scan\")" in r.text
    assert "finally" in r.text


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


def test_ride_scan_returns_role_and_rssi_contract(client, monkeypatch):
    _register(client)
    monkeypatch.setattr(bledevices, "bluetooth_available", lambda: (True, "ok"))

    async def fake_scan():
        return [{
            "address": "OPAQUE-UUID",
            "name": "<unknown & untrusted>",
            "services": [],
            "roles": ["power"],
            "rssi": -55,
        }]

    monkeypatch.setattr(bledevices, "scan", fake_scan)
    data = client.post("/ride/scan").json()

    assert data["available"] is True
    assert data["devices"][0]["roles"] == ["power"]
    assert data["devices"][0]["rssi"] == -55


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
    assert any(f.get("target_watts", 0) > 0 for f in frames)
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
        msg = _receive_after_workout(ws)
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
    monkeypatch.setattr(servermod, "RIDE_INACTIVITY_TIMEOUT_S", 5)


def test_ride_ws_real_path_erg_drives_trainer(client, monkeypatch):
    from wattracker.ble.devices import SimulatedTrainer

    _register(client)
    trainer = SimulatedTrainer()
    # Pedal through the 3s start gate plus 1s of ride time, then stop until the
    # shortened inactivity timeout finalizes.
    _patch_real_ride(monkeypatch, trainer, [150, 150, 150, 150] + [0] * 10)

    frames = []
    with client.websocket_connect("/ride/ws?type=endurance&minutes=30") as ws:
        first = _receive_after_workout(ws)
        assert first["status"] == "connected"
        assert first["erg"] is True
        assert first["devices"]["trainer"] == "FakeKickr"
        try:
            while True:
                frames.append(ws.receive_json())
        except Exception:
            pass
    assert frames and frames[-1]["status"] == "finished"
    assert any(f.get("status") == "inactivity_timeout" and f["saved"] for f in frames)
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
    _patch_real_ride(monkeypatch, None, [150, 150, 150, 150] + [0] * 10)

    frames = []
    with client.websocket_connect("/ride/ws?type=endurance&minutes=30") as ws:
        first = _receive_after_workout(ws)
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
        msg = _receive_after_workout(ws)
        assert msg["status"] == "error"
        assert "No power meter" in msg["error"]


def test_ride_ws_propagates_exact_explicit_sensor_selection(client, monkeypatch):
    from wattracker import server as servermod
    from wattracker.ble.devices import SimulatedPowerSource

    _register(client)
    captured = []

    async def fake_connect(timeout=6.0, selected=None):
        captured.append(selected)
        return {
            "trainer": None,
            "power_source": SimulatedPowerSource([0, 0, 0, 0, 0, 0]),
            "hr_source": None,
            "clients": [],
            "names": {"power": ["LEFT", "RIGHT"]},
            "errors": [],
        }

    monkeypatch.setattr(servermod.bledevices, "bluetooth_available", lambda: (True, "ok"))
    monkeypatch.setattr(servermod.bledevices, "connect_sensors", fake_connect)
    monkeypatch.setattr(servermod, "RIDE_POLL_INTERVAL_S", 0)

    url = (
        "/ride/ws?selected=1&power=LEFT-UUID&power=RIGHT-UUID"
        "&power=LEFT-UUID&hr=HR-UUID&trainer=TRAINER-UUID"
    )
    with client.websocket_connect(url) as ws:
        assert _receive_after_workout(ws)["status"] == "connected"

    assert captured == [{
        "power": ["LEFT-UUID", "RIGHT-UUID"],
        "hr": ["HR-UUID"],
        "trainer": ["TRAINER-UUID"],
    }]


def test_ride_ws_rejects_unbounded_power_selection(client, monkeypatch):
    from wattracker import server as servermod

    _register(client)
    monkeypatch.setattr(servermod.bledevices, "bluetooth_available", lambda: (True, "ok"))
    query = "&".join("power=P" + str(i) for i in range(9))
    with client.websocket_connect("/ride/ws?selected=1&" + query) as ws:
        msg = ws.receive_json()
    assert msg["status"] == "error"
    assert "at most 8 power sensors" in msg["error"]


def test_ride_ws_prepare_waits_for_start_action(client, monkeypatch):
    from wattracker import server as servermod
    from wattracker.ble.devices import SimulatedPowerSource, SimulatedTrainer

    _register(client)
    trainer = SimulatedTrainer()

    async def fake_connect(timeout=6.0, selected=None):
        return {
            "trainer": trainer,
            "power_source": SimulatedPowerSource([150, 150, 150, 150, 0, 0, 0, 0]),
            "hr_source": None,
            "clients": [],
            "names": {"power": "Pedals", "trainer": "Trainer"},
            "errors": [],
        }

    monkeypatch.setattr(servermod.bledevices, "bluetooth_available", lambda: (True, "ok"))
    monkeypatch.setattr(servermod.bledevices, "connect_sensors", fake_connect)
    monkeypatch.setattr(servermod, "RIDE_POLL_INTERVAL_S", 0)
    monkeypatch.setattr(servermod, "RIDE_INACTIVITY_TIMEOUT_S", 5)

    with client.websocket_connect("/ride/ws?prepare=1") as ws:
        connected = _receive_after_workout(ws)
        assert connected["status"] == "connected"
        assert connected["prepared"] is True
        uid = db.get_user_by_username("rider")["id"]
        assert db.list_activities(uid) == []

        ws.send_json({"action": "start"})
        assert ws.receive_json()["status"] == "started"
        frames = []
        try:
            while True:
                frames.append(ws.receive_json())
        except Exception:
            pass

    assert frames[-1]["status"] == "finished"
    assert len(db.list_activities(uid)) == 1


@pytest.mark.parametrize("prepare", [True, False])
def test_ride_ws_close_before_pedaling_does_not_save_activity(
    client, monkeypatch, prepare
):
    from wattracker import server as servermod
    from wattracker.ble.devices import SimulatedPowerSource, SimulatedTrainer

    _register(client)
    trainer = SimulatedTrainer()

    class FakeClient:
        disconnected = False

        async def disconnect(self):
            self.disconnected = True

    ble_client = FakeClient()

    async def fake_connect(timeout=6.0, selected=None):
        return {
            "trainer": trainer,
            "power_source": SimulatedPowerSource([0]),
            "hr_source": None,
            "clients": [ble_client],
            "names": {"power": "Pedals", "trainer": "Trainer"},
            "errors": [],
        }

    monkeypatch.setattr(servermod.bledevices, "bluetooth_available", lambda: (True, "ok"))
    monkeypatch.setattr(servermod.bledevices, "connect_sensors", fake_connect)
    monkeypatch.setattr(servermod, "RIDE_POLL_INTERVAL_S", 0)
    monkeypatch.setattr(servermod, "RIDE_INACTIVITY_TIMEOUT_S", 5)
    url = "/ride/ws?prepare=1" if prepare else "/ride/ws"

    with client.websocket_connect(url) as ws:
        connected = _receive_after_workout(ws)
        assert connected["status"] == "connected"
        assert connected["prepared"] is prepare

    uid = db.get_user_by_username("rider")["id"]
    assert db.list_activities(uid) == []
    assert trainer.commands[-1] == "stop"
    assert ble_client.disconnected is True


@pytest.mark.parametrize("prepare", [True, False])
def test_ride_ws_inactivity_disconnects_without_saving_never_started_ride(
    client, monkeypatch, prepare
):
    from wattracker import server as servermod
    from wattracker.ble.devices import SimulatedPowerSource, SimulatedTrainer

    _register(client)
    trainer = SimulatedTrainer()

    class FakeClient:
        disconnected = False

        async def disconnect(self):
            self.disconnected = True

    ble_client = FakeClient()

    async def fake_connect(timeout=6.0, selected=None):
        return {"trainer": trainer, "power_source": SimulatedPowerSource([0]),
                "hr_source": None, "clients": [ble_client],
                "names": {"power": "Pedals"}, "errors": []}

    monkeypatch.setattr(servermod.bledevices, "bluetooth_available", lambda: (True, "ok"))
    monkeypatch.setattr(servermod.bledevices, "connect_sensors", fake_connect)
    monkeypatch.setattr(servermod, "RIDE_POLL_INTERVAL_S", 0)
    monkeypatch.setattr(servermod, "RIDE_INACTIVITY_TIMEOUT_S", 0.01 if prepare else 2)

    with client.websocket_connect("/ride/ws" + ("?prepare=1" if prepare else "")) as ws:
        assert _receive_after_workout(ws)["status"] == "connected"
        timeout = ws.receive_json()
        while timeout["status"] != "inactivity_timeout":
            timeout = ws.receive_json()

    assert timeout["status"] == "inactivity_timeout"
    assert timeout["saved"] is False
    assert "No activity was saved" in timeout["message"]
    uid = db.get_user_by_username("rider")["id"]
    assert db.list_activities(uid) == []
    assert trainer.commands[-1] == "stop"
    assert ble_client.disconnected is True


def test_ride_ws_base_exception_finalizes_active_ride_and_cleans_every_client(
    client, monkeypatch
):
    from wattracker import server as servermod
    from wattracker.ble.devices import SimulatedTrainer
    from starlette.datastructures import QueryParams

    _register(client)

    class RideCancelled(BaseException):
        pass

    class CleanupCancelled(BaseException):
        pass

    class CancellingPower:
        calls = 0

        def advance(self):
            self.calls += 1
            if self.calls == 5:
                raise RideCancelled("ride task cancelled")

        def latest_power(self):
            return 150

        def latest_cadence(self):
            return 90

    disconnects = []

    class FakeClient:
        def __init__(self, name, fail=False):
            self.name = name
            self.fail = fail

        async def disconnect(self):
            disconnects.append(self.name)
            if self.fail:
                raise CleanupCancelled("cleanup cancelled")

    trainer = SimulatedTrainer()

    async def fake_connect(timeout=6.0, selected=None):
        return {
            "trainer": trainer,
            "power_source": CancellingPower(),
            "hr_source": None,
            "clients": [FakeClient("first", fail=True), FakeClient("second")],
            "names": {"power": "Pedals", "trainer": "Trainer"},
            "errors": [],
        }

    monkeypatch.setattr(servermod.bledevices, "bluetooth_available", lambda: (True, "ok"))
    monkeypatch.setattr(servermod.bledevices, "connect_sensors", fake_connect)
    monkeypatch.setattr(servermod, "RIDE_POLL_INTERVAL_S", 0)

    uid = db.get_user_by_username("rider")["id"]

    class FakeWebSocket:
        headers = {}
        session = {"user_id": uid}
        query_params = QueryParams("")

        def __init__(self):
            self.messages = []

        async def accept(self):
            pass

        async def send_json(self, message):
            self.messages.append(message)

        async def close(self, code=None):
            pass

    endpoint = next(
        route.endpoint
        for route in client.app.routes
        if getattr(route, "path", None) == "/ride/ws"
    )
    websocket = FakeWebSocket()
    with pytest.raises(RideCancelled):
        asyncio.run(endpoint(websocket))

    assert websocket.messages[0]["status"] == "workout"
    assert websocket.messages[1]["status"] == "connected"
    assert websocket.messages[-1]["status"] == "running"
    assert len(db.list_activities(uid)) == 1
    assert "stop" in trainer.commands
    assert disconnects == ["first", "second"]


def test_ride_ws_close_during_start_countdown_never_saves_activity(
    client, monkeypatch
):
    from starlette.datastructures import QueryParams
    from wattracker import server as servermod
    from wattracker.ble.devices import SimulatedPowerSource, SimulatedTrainer

    _register(client)
    uid = db.get_user_by_username("rider")["id"]
    trainer = SimulatedTrainer()

    class FakeClient:
        disconnected = False

        async def disconnect(self):
            self.disconnected = True

    ble_client = FakeClient()

    async def fake_connect(timeout=6.0, selected=None):
        return {"trainer": trainer, "power_source": SimulatedPowerSource([150]),
                "hr_source": None, "clients": [ble_client],
                "names": {"power": "Pedals"}, "errors": []}

    monkeypatch.setattr(servermod.bledevices, "bluetooth_available", lambda: (True, "ok"))
    monkeypatch.setattr(servermod.bledevices, "connect_sensors", fake_connect)
    monkeypatch.setattr(servermod, "RIDE_POLL_INTERVAL_S", 0)

    class FakeWebSocket:
        headers = {}
        session = {"user_id": uid}
        query_params = QueryParams("")

        def __init__(self):
            self.messages = []

        async def accept(self):
            pass

        async def send_json(self, message):
            self.messages.append(message)
            if sum(item.get("status") == "starting" for item in self.messages) == 2:
                raise RuntimeError("client closed")

        async def close(self, code=None):
            pass

    endpoint = next(
        route.endpoint for route in client.app.routes
        if getattr(route, "path", None) == "/ride/ws"
    )
    websocket = FakeWebSocket()
    asyncio.run(endpoint(websocket))

    assert [message["status"] for message in websocket.messages[-2:]] == [
        "starting", "starting"
    ]
    assert db.list_activities(uid) == []
    assert trainer.commands[-1] == "stop"
    assert ble_client.disconnected is True
