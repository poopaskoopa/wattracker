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
    assert 'id="rideChartPanel"' in r.text
    assert 'id="rideChart"' in r.text
    assert 'id="rideHrChart"' not in r.text
    assert 'id="hrChartBlock"' not in r.text
    assert 'id="rideChartSummary"' in r.text
    assert 'aria-label="Target and measured power, cadence, and heart rate over workout time"' in r.text
    assert 'id="chartPowerValue"' in r.text
    assert 'id="chartCadenceValue"' in r.text
    assert 'id="chartHrValue"' in r.text
    assert "Cadence (power sensor)" in r.text
    assert "devices.slice().sort" in r.text
    assert "playCue(\"scan\")" in r.text
    assert "finally" in r.text
    assert r.text.index('id="scanBusy"') > r.text.index('id="connectBtn"')
    assert 'className = "device-disconnect button-secondary"' in r.text
    assert 'requestDisconnect(deviceAddress);' in r.text
    assert 'text: "Workout time (minutes)"' in r.text
    assert "min: 0, max: duration" in r.text
    assert "var minutes = Number(value) / 60;" in r.text
    assert '{label: "Target power", data: prescribed, yAxisID: "y"' in r.text
    assert '{label: "Measured power", data: livePower, yAxisID: "y"' in r.text
    assert 'borderColor: "#f2a900"' in r.text
    assert 'borderColor: "rgba(255, 209, 102, 0.7)", backgroundColor: "rgba(255, 209, 102, 0.7)"' in r.text
    assert 'borderColor: "rgba(87, 199, 255, 0.7)"' in r.text
    assert 'borderColor: "rgba(255, 77, 141, 0.7)"' in r.text
    assert 'id="rideChartTitle"' in r.text
    assert 'workout.name || "Workout metrics"' in r.text
    assert 'id="ergIndicator"' in r.text
    assert 'indicator.classList.toggle("erg-lit", ergEnabled)' in r.text
    assert "function fmtHms(sec)" in r.text
    assert "function blockRemaining(elapsed)" in r.text
    assert 'id="clockElapsed"' in r.text
    assert 'id="clockBlock"' in r.text
    assert 'id="clockTotal"' in r.text
    assert "grid: {drawTicks: true, tickLength: 8, tickColor: tickMark}" in r.text
    assert "grid: {drawOnChartArea: false, drawTicks: true, tickLength: 8," in r.text
    assert "borderDash: [8, 4]" in r.text
    assert '{label: "Cadence", data: liveCadence, yAxisID: "metrics"' in r.text
    assert '{label: "Heart rate", data: liveHr, yAxisID: "metrics"' in r.text
    assert 'metrics: {beginAtZero: true, position: "right"' in r.text
    assert 'text: "Cadence (rpm) / Heart rate (bpm)"' in r.text
    assert "appendOrReplace(liveHr" in r.text
    assert "if (rideChart) {" in r.text
    assert "rideChart.destroy();" in r.text
    assert "pointRadius: 2.5" in r.text
    assert "borderWidth: 3" in r.text
    assert 'id="chartFullscreenBtn"' in r.text
    assert 'aria-controls="rideChartPanel"' in r.text
    assert "panel.requestFullscreen || panel.webkitRequestFullscreen" in r.text
    assert '"ride-chart-fullscreen-fallback"' in r.text
    assert 'event.key === "Escape"' in r.text
    assert 'button.setAttribute("aria-pressed", active ? "true" : "false")' in r.text
    assert "rideChart.resize();" in r.text
    assert 'return number == null ? "—" : String(Math.round(number));' in r.text
    assert 'id="ergBtn"' in r.text
    assert '{action: "set_erg", enabled: !ergEnabled}' in r.text


def test_ride_chart_styles_support_live_metrics_and_fullscreen(client):
    r = client.get("/static/style.css")
    assert r.status_code == 200
    assert ".ride-chart-metrics strong" in r.text
    assert "font-size: 28px" in r.text
    assert ".ride-chart-metrics .metric-power { color: #ffd166; }" in r.text
    assert ".ride-chart-metrics .metric-cadence { color: #57c7ff; }" in r.text
    assert ".ride-chart-metrics .metric-heart-rate { color: #ff4d8d; }" in r.text
    assert ".ride-chart-block:fullscreen" in r.text
    assert ".ride-chart-fullscreen-fallback" in r.text
    assert "body.ride-chart-fullscreen-open" in r.text
    assert ".ride-chart-clocks" in r.text
    assert ".ride-chart-erg.erg-lit .erg-led" in r.text


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


def test_ride_ws_selected_plan_workout_links_saved_activity(client):
    _register(client)
    uid = db.get_user_by_username("rider")["id"]
    plan_id = db.create_plan(uid, "Ride selection", "2026-07-10", 1)
    workout_id = db.add_plan_workout(
        plan_id,
        uid,
        "2026-07-10",
        "Selected endurance",
        "endurance",
        60,
        1.0,
        "<workout_file/>",
    )

    frames = []
    with client.websocket_connect(
        f"/ride/ws?sim=1&workout_id={workout_id}"
    ) as ws:
        first = ws.receive_json()
        assert first["status"] == "workout"
        try:
            while True:
                frames.append(ws.receive_json())
        except Exception:
            pass

    assert frames[-1]["status"] == "finished"
    assert frames[-1]["workout_id"] == workout_id
    assert frames[-1]["activity_id"] is not None
    linked = db.get_plan_workout(uid, workout_id)
    assert linked["completed_activity_id"] == frames[-1]["activity_id"]


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


class _AckingFtmsClient:
    def __init__(self, results=None, delay=0.001):
        self.results = results or {}
        self.delay = delay
        self.callback = None
        self.active_procedure = False
        self.events = []

    async def start_notify(self, _char, callback):
        self.callback = callback
        self.events.append("notify")

    async def write_gatt_char(self, char, data, response=False):
        assert response is True
        assert self.active_procedure is False, "FTMS procedures overlapped"
        self.active_procedure = True
        op = data[0]
        self.events.append(("write", op))

        def acknowledge():
            self.active_procedure = False
            self.callback(
                char, bytearray([0x80, op, self.results.get(op, 0x01)])
            )

        asyncio.get_running_loop().call_later(self.delay, acknowledge)

    async def disconnect(self):
        assert self.active_procedure is False
        self.events.append("disconnect")


def test_ride_ws_real_path_erg_drives_trainer(client, monkeypatch):
    from wattracker.ble.devices import SimulatedTrainer

    _register(client)
    trainer = SimulatedTrainer()
    # connect_sensors prepares a real FTMS trainer before handing it to the
    # route; represent that state so initial targeting must not re-request
    # control/start.
    trainer.start_erg()
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
    assert trainer.commands.count("request_control") == 1
    assert trainer.commands[-1] == "stop"
    assert any(t > 0 for t in trainer.targets)
    assert trainer.targets[0] > 0  # prescribed target applied before pedal gate
    assert trainer.targets[-1] == 0
    # The ride was saved for the user.
    uid = db.get_user_by_username("rider")["id"]
    assert len(db.list_activities(uid)) == 1


def test_ride_ws_awaits_ftms_commands_and_stops_before_disconnect(
    client, monkeypatch
):
    from wattracker import server as servermod
    from wattracker.ble.devices import BleakTrainer, SimulatedPowerSource

    _register(client)
    hardware = _AckingFtmsClient()
    connected_trainers = []
    clock_calls = 0
    requested_sleeps = []
    real_sleep = asyncio.sleep

    def fake_ride_time():
        nonlocal clock_calls
        tick = clock_calls // 2
        processing = 0.02 if clock_calls % 2 else 0.0
        clock_calls += 1
        return float(tick) + processing

    async def capture_ride_sleep(delay):
        requested_sleeps.append(delay)
        await real_sleep(0)

    async def fake_connect(timeout=6.0, selected=None):
        trainer = BleakTrainer(hardware, response_timeout_s=0.1)
        await trainer.prepare()
        connected_trainers.append(trainer)
        return {
            "trainer": trainer,
            "power_source": SimulatedPowerSource(
                [150, 150, 150, 150] + [0] * 10
            ),
            "hr_source": None,
            "clients": [hardware],
            "clients_by_address": {"TRAINER": hardware},
            "bindings": {
                "TRAINER": {"name": "Kickr", "roles": {"trainer": trainer}}
            },
            "names": {"trainer": "Kickr", "power": "Pedals"},
            "errors": [],
        }

    monkeypatch.setattr(servermod.bledevices, "bluetooth_available", lambda: (True, "ok"))
    monkeypatch.setattr(servermod.bledevices, "connect_sensors", fake_connect)
    monkeypatch.setattr(servermod, "RIDE_POLL_INTERVAL_S", 0.05)
    monkeypatch.setattr(servermod, "RIDE_INACTIVITY_TIMEOUT_S", 5)
    monkeypatch.setattr(servermod, "_ride_loop_time", fake_ride_time)
    monkeypatch.setattr(servermod, "_ride_sleep", capture_ride_sleep)

    with client.websocket_connect("/ride/ws") as ws:
        assert _receive_after_workout(ws)["status"] == "connected"
        try:
            while True:
                ws.receive_json()
        except Exception:
            pass

    trainer = connected_trainers[0]
    stop_index = max(
        i for i, event in enumerate(hardware.events)
        if event == ("write", 0x08)
    )
    assert stop_index < hardware.events.index("disconnect")
    assert trainer._pending_response is None
    assert trainer._tasks == set()
    # Request Control and Start happened once during prepare; initial/per-tick
    # target procedures never queued another start sequence.
    assert hardware.events.count(("write", 0x00)) == 1
    assert hardware.events.count(("write", 0x07)) == 1
    # Deterministic clock models 20 ms of command/processing time per tick.
    # The server requests only the remaining 30 ms of its 50 ms cadence.
    assert requested_sleeps
    assert requested_sleeps == pytest.approx([0.03] * len(requested_sleeps))


def test_ride_ws_target_rejection_reports_erg_off_without_aborting(
    client, monkeypatch
):
    from wattracker import server as servermod
    from wattracker.ble.devices import BleakTrainer, SimulatedPowerSource

    _register(client)
    hardware = _AckingFtmsClient(results={0x05: 0x04})
    connected_trainers = []

    async def fake_connect(timeout=6.0, selected=None):
        trainer = BleakTrainer(hardware, response_timeout_s=0.1)
        await trainer.prepare()
        connected_trainers.append(trainer)
        return {
            "trainer": trainer,
            "power_source": SimulatedPowerSource(
                [150, 150, 150, 150] + [0] * 10
            ),
            "hr_source": None,
            "clients": [hardware],
            "clients_by_address": {"TRAINER": hardware},
            "bindings": {
                "TRAINER": {"name": "Kickr", "roles": {"trainer": trainer}}
            },
            "names": {"trainer": "Kickr", "power": "Pedals"},
            "errors": [],
        }

    monkeypatch.setattr(servermod.bledevices, "bluetooth_available", lambda: (True, "ok"))
    monkeypatch.setattr(servermod.bledevices, "connect_sensors", fake_connect)
    monkeypatch.setattr(servermod, "RIDE_POLL_INTERVAL_S", 0)
    monkeypatch.setattr(servermod, "RIDE_INACTIVITY_TIMEOUT_S", 5)

    frames = []
    with client.websocket_connect("/ride/ws") as ws:
        assert _receive_after_workout(ws)["status"] == "connected"
        try:
            while True:
                frames.append(ws.receive_json())
        except Exception:
            pass

    failure = next(frame for frame in frames if frame.get("status") == "erg")
    assert failure["enabled"] is False
    assert "operation failed" in failure["error"]
    assert connected_trainers[0].erg_enabled is False
    assert any(frame.get("status") == "inactivity_timeout" for frame in frames)
    assert frames[-1]["status"] == "finished"
    assert hardware.events[-1] == "disconnect"


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


def test_ride_ws_prepared_stop_cleans_hardware_before_server_close(
    client, monkeypatch
):
    from starlette.datastructures import QueryParams
    from wattracker import server as servermod
    from wattracker.ble.devices import SimulatedPowerSource

    _register(client)
    uid = db.get_user_by_username("rider")["id"]
    events = []

    class AwaitedTrainer:
        erg_available = True
        erg_enabled = True

        async def async_set_target_power(self, watts):
            events.append(("target", watts))

        async def async_stop(self):
            events.append("stop")
            self.erg_enabled = False

    class AwaitedClient:
        async def disconnect(self):
            events.append("disconnect")

    trainer = AwaitedTrainer()
    ble_client = AwaitedClient()

    async def fake_connect(timeout=6.0, selected=None):
        return {
            "trainer": trainer,
            "power_source": SimulatedPowerSource([0]),
            "hr_source": None,
            "clients": [ble_client],
            "clients_by_address": {"TRAINER": ble_client},
            "bindings": {
                "TRAINER": {"name": "Kickr", "roles": {"trainer": trainer}}
            },
            "names": {"trainer": "Kickr", "power": "Pedals"},
            "errors": [],
        }

    monkeypatch.setattr(servermod.bledevices, "bluetooth_available", lambda: (True, "ok"))
    monkeypatch.setattr(servermod.bledevices, "connect_sensors", fake_connect)

    class FakeWebSocket:
        headers = {}
        session = {"user_id": uid}
        query_params = QueryParams("prepare=1")

        def __init__(self):
            self.receive_count = 0
            self.messages = []

        async def accept(self):
            pass

        async def send_json(self, message):
            self.messages.append(message)

        async def receive_json(self):
            self.receive_count += 1
            if self.receive_count == 1:
                return {"action": "stop"}
            await asyncio.Future()

        async def close(self, code=None):
            events.append("close")

    endpoint = next(
        route.endpoint
        for route in client.app.routes
        if getattr(route, "path", None) == "/ride/ws"
    )
    websocket = FakeWebSocket()
    asyncio.run(endpoint(websocket))

    assert websocket.messages[0]["status"] == "workout"
    assert websocket.messages[1]["status"] == "connected"
    assert events == [("target", 0), "stop", "disconnect", "close"]
    assert db.list_activities(uid) == []


def test_ride_ws_prepared_actions_toggle_erg_and_disconnect_one_device(
    client, monkeypatch
):
    from wattracker import server as servermod
    from wattracker.ble.devices import (
        AggregatePowerSource,
        SimulatedPowerSource,
        SimulatedTrainer,
    )

    _register(client)
    trainer = SimulatedTrainer()
    trainer.start_erg()
    left = SimulatedPowerSource([100])
    right = SimulatedPowerSource([120])

    class FakeClient:
        def __init__(self, address):
            self.address = address
            self.disconnected = False

        async def disconnect(self):
            self.disconnected = True

    left_client = FakeClient("LEFT")
    right_client = FakeClient("RIGHT")
    trainer_client = FakeClient("TRAINER")
    conn = {
        "trainer": trainer,
        "power_source": AggregatePowerSource([left, right]),
        "hr_source": None,
        "clients": [left_client, right_client, trainer_client],
        "clients_by_address": {
            "LEFT": left_client, "RIGHT": right_client, "TRAINER": trainer_client,
        },
        "bindings": {
            "LEFT": {"name": "Left", "roles": {"power": left}},
            "RIGHT": {"name": "Right", "roles": {"power": right}},
            "TRAINER": {"name": "Kickr", "roles": {"trainer": trainer}},
        },
        "names": {"power": ["Left", "Right"], "trainer": "Kickr"},
        "errors": [],
    }

    async def fake_connect(timeout=6.0, selected=None):
        return conn

    monkeypatch.setattr(servermod.bledevices, "bluetooth_available", lambda: (True, "ok"))
    monkeypatch.setattr(servermod.bledevices, "connect_sensors", fake_connect)
    monkeypatch.setattr(servermod, "RIDE_POLL_INTERVAL_S", 0)

    with client.websocket_connect("/ride/ws?prepare=1") as ws:
        connected = _receive_after_workout(ws)
        assert connected["erg_available"] is True
        assert connected["erg_enabled"] is True

        ws.send_json({"action": "set_erg", "enabled": "false"})
        invalid = ws.receive_json()
        assert invalid == {
            "status": "erg",
            "available": True,
            "enabled": True,
            "error": "ERG enabled must be a boolean.",
        }

        ws.send_json({"action": "set_erg", "enabled": False})
        disabled = ws.receive_json()
        assert disabled["status"] == "erg"
        assert disabled["available"] is True
        assert disabled["enabled"] is False
        assert disabled["error"] is None

        ws.send_json({"action": "disconnect", "address": "LEFT"})
        disconnected = ws.receive_json()
        assert disconnected["status"] == "device_disconnected"
        assert disconnected["address"] == "LEFT"
        assert disconnected["devices"]["power"] == "Right"
        assert disconnected["erg_available"] is True
        assert left_client.disconnected is True

        ws.send_json({"action": "start"})
        assert ws.receive_json()["status"] == "started"

    assert right_client.disconnected is True
    assert trainer_client.disconnected is True


def test_ride_ws_active_actions_toggle_erg_and_validate_disconnect(
    client, monkeypatch
):
    from wattracker import server as servermod
    from wattracker.ble.devices import SimulatedPowerSource, SimulatedTrainer

    _register(client)
    trainer = SimulatedTrainer()

    async def fake_connect(timeout=6.0, selected=None):
        return {
            "trainer": trainer,
            "power_source": SimulatedPowerSource([150] * 100),
            "hr_source": None,
            "clients": [],
            "clients_by_address": {},
            "bindings": {},
            "names": {"power": "Pedals", "trainer": "Kickr"},
            "errors": [],
        }

    monkeypatch.setattr(servermod.bledevices, "bluetooth_available", lambda: (True, "ok"))
    monkeypatch.setattr(servermod.bledevices, "connect_sensors", fake_connect)
    monkeypatch.setattr(servermod, "RIDE_POLL_INTERVAL_S", 0.001)

    with client.websocket_connect("/ride/ws") as ws:
        assert _receive_after_workout(ws)["status"] == "connected"
        ws.send_json({"action": "set_erg", "enabled": False})
        messages = []
        while not any(message.get("status") == "erg" for message in messages):
            messages.append(ws.receive_json())
        result = next(message for message in messages if message.get("status") == "erg")
        assert result["enabled"] is False

        ws.send_json({"action": "disconnect", "address": 123})
        messages = []
        while not any(
            message.get("status") == "error"
            and message.get("action") == "disconnect"
            for message in messages
        ):
            messages.append(ws.receive_json())
        error = next(
            message for message in messages
            if message.get("status") == "error"
            and message.get("action") == "disconnect"
        )
        assert error["error"] == "Invalid device address."


def test_ride_ws_erg_action_reports_unavailable_without_trainer(
    client, monkeypatch
):
    from wattracker import server as servermod
    from wattracker.ble.devices import SimulatedPowerSource

    _register(client)

    async def fake_connect(timeout=6.0, selected=None):
        return {
            "trainer": None,
            "power_source": SimulatedPowerSource([0]),
            "hr_source": None,
            "clients": [],
            "clients_by_address": {},
            "bindings": {},
            "names": {"power": "Pedals"},
            "errors": [],
        }

    monkeypatch.setattr(servermod.bledevices, "bluetooth_available", lambda: (True, "ok"))
    monkeypatch.setattr(servermod.bledevices, "connect_sensors", fake_connect)

    with client.websocket_connect("/ride/ws?prepare=1") as ws:
        connected = _receive_after_workout(ws)
        assert connected["erg_available"] is False
        assert connected["erg_enabled"] is False
        ws.send_json({"action": "set_erg", "enabled": True})
        response = ws.receive_json()
        assert response["status"] == "erg"
        assert response["available"] is False
        assert response["enabled"] is False
        assert "No controllable FTMS trainer" in response["error"]
        ws.send_json({"action": "stop"})


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
