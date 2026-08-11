"""Losing the connector mid-ride, and getting the ride back.

The behaviour these cover was found on real hardware and is the reason the
ride-buffering model exists at all: a transient loss of the server used to
kill the ride outright. 111 ms after the socket dropped the connector sent
FTMS Stop and disconnected both the trainer and the HRM, because the session's
``finally`` released the radio unconditionally.

Two requirements collide there - never hold the adapter across a reconnect,
and never lose a rider's workout to a wifi stutter - so these tests pin down
the reconciliation rather than either half of it:

* the radio is released when the socket drops **and no ride is running**;
* a ride keeps its devices, its sampler and its buffer;
* the server freezes rather than ending the ride, and replays the seconds it
  missed when the connector comes back;
* nobody is left holding a trainer forever if the other end never returns.

The connector half runs in-process here, reached through a session double
rather than a real websocket - the transport itself is covered by
tests/test_connector_transport.py, and what matters here is what survives a
session being replaced by a different one.
"""
from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from wattracker import connectorhub, db  # noqa: E402
from wattracker import server as servermod  # noqa: E402
from wattracker.backend import remote_ble  # noqa: E402
from wattracker.rpc import ConnectorUnavailable  # noqa: E402
from wattracker.server import create_app  # noqa: E402
from wattracker_connector import ble_handlers as blemod  # noqa: E402
from wattracker_connector.ble_handlers import BleState, build_ble_handlers  # noqa: E402
from wattracker_connector.buffer import RideBuffer  # noqa: E402


@pytest.fixture()
def client():
    app = create_app()
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def _clean_hub():
    connectorhub.reset()
    yield
    connectorhub.reset()


def _register(client, username="rider"):
    client.post("/register", data={"username": username, "password": "password123"})
    return db.get_user_by_username(username)["id"]


# --------------------------------------------------------------- doubles
class _Pedals:
    """A power meter and HRM whose readings the test sets directly."""

    def __init__(self, power=200, cadence=90.0, hr=140):
        self.power = power
        self.cadence = cadence
        self.hr = hr

    def latest_power(self):
        return self.power

    def latest_cadence(self):
        return self.cadence

    def latest_hr(self):
        return self.hr


class _Trainer:
    """An FTMS trainer that records which writes it was asked for.

    Distinguishing enable from set is the whole point: re-arming is three FTMS
    ops (0x00 Request Control, 0x07 Start, 0x05 target) and adjusting is one.
    """

    def __init__(self):
        self.calls = []
        self.erg_available = True
        self.erg_enabled = False

    async def async_enable_erg(self, watts=None):
        self.calls.append(("enable", watts))
        self.erg_enabled = True

    async def async_set_target_power(self, watts):
        self.calls.append(("set", watts))

    async def async_stop(self):
        self.calls.append(("stop", None))
        self.erg_enabled = False

    async def async_disable_erg(self):
        self.calls.append(("disable", None))
        self.erg_enabled = False


class _FakeBleDevices:
    """Stands in for wattracker.ble.devices on the connector side."""

    def __init__(self, pedals, trainer):
        self.pedals = pedals
        self.trainer = trainer
        self.disconnected = 0

    def bluetooth_available(self):
        return True, "ok"

    async def scan(self, timeout=5.0, attempts=2):
        return [{"address": "AA", "name": "FakeKickr", "roles": ["trainer"]}]

    async def connect_sensors(self, timeout=6.0, selected=None):
        client = _FakeClient(self)
        return {
            "trainer": self.trainer,
            "power_source": self.pedals,
            "hr_source": self.pedals,
            "clients": [client],
            "clients_by_address": {"AA": client},
            # Roles are a mapping here, exactly as devices.connect_sensors
            # builds them - _describe reads their keys.
            "bindings": {
                "AA": {"name": "FakeKickr",
                       "roles": {"power": None, "trainer": None}},
                "BB": {"name": "FakeHRM", "roles": {"hr": None}},
            },
            "names": {"power": "FakeKickr", "trainer": "FakeKickr"},
            "errors": [],
        }

    async def disconnect_sensor(self, conn, address):
        return conn


class _FakeClient:
    def __init__(self, owner):
        self._owner = owner

    async def disconnect(self):
        self._owner.disconnected += 1


class _LoopbackSession:
    """A connectorhub session that runs the real connector handlers in-process.

    Everything the ride path touches on a ConnectorSession - ``call``,
    ``ble_sink``, ``closed`` - with the RPC dispatch going straight to the
    genuine ``build_ble_handlers`` output instead of over a socket.
    """

    def __init__(self, user_id, handlers, label="Test PC"):
        self.user_id = user_id
        self.device_id = 1
        self.label = label
        self.handlers = handlers
        self.ble_sink = None
        self.closed = False
        self.calls = []

    async def call(self, method, params=None, *, timeout=None):
        if self.closed:
            raise ConnectorUnavailable("connector disconnected")
        params = params or {}
        self.calls.append((method, params))
        handler = self.handlers.get(method)
        if handler is None:
            raise ConnectorUnavailable(f"unknown method {method}")
        return await handler(**params)

    def close(self, reason="connector disconnected", code=1000):
        self.closed = True


class _Rig:
    """A connector holding a radio, attachable and detachable at will."""

    def __init__(self, uid, tmp_path, pedals=None, trainer=None):
        self.uid = uid
        self.pedals = pedals or _Pedals()
        self.trainer = trainer or _Trainer()
        self.devices = _FakeBleDevices(self.pedals, self.trainer)
        self.state = BleState(
            buffer=RideBuffer(str(tmp_path / "ride-buffer.jsonl"))
        )
        self.handlers = build_ble_handlers(self.state, self._send_event)
        self.session = None
        # Off, this connector never tells the server which sample it is on -
        # which is what a connector whose buffer failed to open looks like.
        self.report_index = True

    async def _send_event(self, event, **fields):
        session = connectorhub.get(self.uid)
        if session is None:
            raise ConnectorUnavailable("not connected")
        if event == "ble.sample" and session.ble_sink is not None:
            session.ble_sink.update(
                power=fields.get("power"), cadence=fields.get("cadence"),
                hr=fields.get("hr"),
                index=fields.get("n") if self.report_index else None,
            )

    def attach(self):
        """A connector dialling in - a brand-new session every time."""
        self.session = _LoopbackSession(self.uid, self.handlers)
        connectorhub.register(self.session)
        return self.session

    def detach(self):
        """The socket dropping. The connector's own state is untouched."""
        if self.session is not None:
            connectorhub.unregister(self.session)
            self.session = None


def _rig(uid, tmp_path, monkeypatch, **kwargs):
    rig = _Rig(uid, tmp_path, **kwargs)
    monkeypatch.setattr(blemod, "bledevices", rig.devices)
    return rig


# ------------------------------------------------- the release decision
def _run(coro):
    return asyncio.run(coro)


def test_a_dropped_socket_releases_the_radio_when_no_ride_is_running(tmp_path):
    """The original rule, which is still right when nothing is being ridden."""
    rig = _Rig(1, tmp_path)
    trainer = _Trainer()
    rig.state.conn = {"trainer": trainer, "clients": [_FakeClient(rig.devices)]}

    assert _run(rig.state.detach()) is True
    assert rig.state.conn is None
    assert trainer.calls == [("disable", None)]


def test_a_dropped_socket_keeps_the_radio_while_a_ride_is_running(tmp_path):
    """The bug this whole change exists for.

    A transient loss of the server used to send FTMS Stop and drop every
    device 111 ms after the socket went - so a wifi stutter ended the workout.
    """
    rig = _Rig(1, tmp_path)
    trainer = _Trainer()
    client = _FakeClient(rig.devices)
    rig.state.conn = {"trainer": trainer, "clients": [client]}
    rig.state.ride = {"started_at": "2026-08-01T10:00:00", "name": "VO2"}
    rig.state.buffer.start("2026-08-01T10:00:00", "VO2", 250.0, None)

    assert _run(rig.state.detach()) is False
    assert rig.state.conn is not None
    assert trainer.calls == []            # no FTMS stop
    assert rig.devices.disconnected == 0  # no device disconnect
    assert rig.state.buffer.recording is True   # still recording
    assert rig.state.claimed is False     # but nobody is driving it


def test_a_clean_release_drops_the_buffer_and_a_lost_socket_does_not(tmp_path):
    """Which end owns the ride depends on how it ended.

    ble.release only happens when the server is there, and a server that is
    there has recorded the ride itself. Leaving the file behind would mean the
    next reconnect uploading a ride that is already stored - and it would not
    even dedupe, because the hash is over (start, duration) and a controller's
    duration excludes the seconds the rider was paused.
    """
    rig = _Rig(1, tmp_path)
    rig.state.conn = {"trainer": _Trainer(), "clients": []}
    rig.state.ride = {"started_at": "2026-08-01T10:00:00", "name": "VO2"}
    rig.state.buffer.start("2026-08-01T10:00:00", "VO2", 250.0, None)
    rig.state.buffer.append(power=200)

    _run(rig.handlers["ble.release"]())
    assert rig.state.buffer.load() is None

    # Whereas losing the socket keeps it, because it is the only copy.
    rig.state.buffer.start("2026-08-01T11:00:00", "VO2", 250.0, None)
    rig.state.buffer.append(power=200)
    rig.state.ride = {"started_at": "2026-08-01T11:00:00", "name": "VO2"}
    _run(rig.state.detach())
    assert rig.state.buffer.load() is not None


# ------------------------------------------------------------- catch-up
def test_catchup_returns_only_what_was_missed_and_claims_the_ride(tmp_path):
    rig = _Rig(1, tmp_path)
    rig.state.ride = {"started_at": "2026-08-01T10:00:00", "name": "VO2"}
    rig.state.buffer.start("2026-08-01T10:00:00", "VO2", 250.0, None)
    for watts in (100, 150, 200, 250):
        rig.state.buffer.append(power=watts, cadence=90, hr=140)
    rig.state.claimed = False

    result = _run(rig.handlers["ble.catchup"](since=2))
    assert [row[0] for row in result["samples"]] == [200, 250]
    assert result["count"] == 4
    assert result["active"] is True
    assert rig.state.claimed is True


def test_the_buffer_indexes_every_sample_it_stores(tmp_path):
    buffer = RideBuffer(str(tmp_path / "ride.jsonl"))
    buffer.start("2026-08-01T10:00:00", "VO2", 250.0, None)
    assert [buffer.append(power=w) for w in (100, 110, 120)] == [0, 1, 2]
    assert buffer.count == 3
    assert buffer.samples_from(1) == [[110, None, None], [120, None, None]]
    assert buffer.samples_from(9) == []
    # A closed buffer indexes nothing, and says so rather than lying.
    buffer.finish()
    assert buffer.append(power=130) is None


# ------------------------------------------------------ ERG write count
def test_a_target_change_is_one_ftms_write_not_three(tmp_path):
    """B-2 from the hardware session: 46 x 0x00, 46 x 0x07, 46 x 0x05.

    server._set_connection_erg already draws the distinction between arming
    ERG and adjusting a target. The RPC used to collapse both onto the same
    call, so the connector always took the expensive path - three FTMS ops per
    1 Hz tick where local mode issues one.
    """
    rig = _Rig(1, tmp_path)
    rig.state.conn = {"trainer": rig.trainer, "clients": []}

    _run(rig.handlers["ble.set_erg"](enabled=True, watts=200, force_rearm=True))
    assert rig.trainer.calls == [("enable", 200)]

    rig.trainer.calls.clear()
    _run(rig.handlers["ble.set_erg"](enabled=True, watts=210, force_rearm=False))
    assert rig.trainer.calls == [("set", 210)]

    # ...but an unarmed trainer is armed regardless of what the server asked
    # for, because a bare target does not put a trainer back into ERG.
    rig.trainer.calls.clear()
    rig.trainer.erg_enabled = False
    _run(rig.handlers["ble.set_erg"](enabled=True, watts=220, force_rearm=False))
    assert rig.trainer.calls == [("enable", 220)]


def test_the_proxy_trainer_asks_for_a_rearm_only_when_it_means_it(tmp_path):
    """The server end of the same distinction."""
    sent = []

    class _Session:
        closed = False

        async def call(self, method, params=None, *, timeout=None):
            sent.append((method, params))
            return {"available": True, "enabled": True, "error": None}

    trainer = remote_ble.RemoteTrainer(lambda: _Session())
    _run(trainer.async_set_target_power(210))
    _run(trainer.async_enable_erg(210))
    assert [p["force_rearm"] for _m, p in sent] == [False, True]


def test_the_proxy_refuses_rather_than_commanding_a_dead_session():
    """A reconnect makes a brand-new session; the old one must not be used."""
    trainer = remote_ble.RemoteTrainer(lambda: None)
    with pytest.raises(ConnectorUnavailable):
        _run(trainer.async_set_target_power(200))


# ------------------------------------------------- the ride, end to end
def _drive_ride(client, monkeypatch, rig, offline_frames=6):
    """Ride, drop the connector, reconnect, and collect every frame."""
    monkeypatch.setenv("WATTRACKER_MODE", "server")
    monkeypatch.setattr(servermod, "RIDE_POLL_INTERVAL_S", 0.01)
    monkeypatch.setattr(blemod, "SAMPLE_INTERVAL_S", 0.005)
    rig.attach()

    frames = []
    with client.websocket_connect("/ride/ws?type=endurance&minutes=30") as ws:
        while True:
            try:
                message = ws.receive_json()
            except Exception:
                raise AssertionError(f"closed early; frames={frames}")
            frames.append(message)
            if message.get("status") == "connected":
                break

        # ERG does not auto-engage at ride start (pre-existing behaviour, and
        # not something the split caused), so ask for it - the point here is
        # what happens to a trainer that is actually holding a target.
        ws.send_json({"action": "set_erg", "enabled": True})

        # Ride normally for a moment so the connector's buffer has samples the
        # server has already seen - the ones it must NOT replay.
        while len([f for f in frames if f.get("status") == "running"]) < 5:
            frames.append(ws.receive_json())

        rig.detach()

        seen_offline = 0
        while seen_offline < offline_frames:
            message = ws.receive_json()
            frames.append(message)
            if (
                message.get("status") == "connector_offline"
                or message.get("connector_offline")
            ):
                seen_offline += 1

        rig.attach()

        while True:
            message = ws.receive_json()
            frames.append(message)
            if message.get("status") in ("connector_resumed", "connector_lost"):
                break
        # Everything the trainer was told up to and including the recovery.
        # The deliberate stop at the end of the ride comes after this point.
        rig.calls_through_recovery = list(rig.trainer.calls)
        rig.disconnects_through_recovery = rig.devices.disconnected

        for _ in range(3):
            frames.append(ws.receive_json())
        ws.send_json({"action": "stop"})
        for _ in range(5):
            try:
                frames.append(ws.receive_json())
            except Exception:
                break
    return frames


def test_a_ride_survives_losing_the_connector_and_picks_up_where_it_left_off(
    client, tmp_path, monkeypatch
):
    """The headline. The ride must not end, and the missed seconds must land.

    Before this, the server saw ConnectorUnavailable from the per-tick ERG
    call, that escaped into ride_ws's blanket except, and the ride was stopped
    and saved on the spot.
    """
    uid = _register(client)
    rig = _rig(uid, tmp_path, monkeypatch)
    frames = _drive_ride(client, monkeypatch, rig)

    statuses = [f.get("status") for f in frames]
    assert "connector_offline" in statuses
    resumed = next(f for f in frames if f.get("status") == "connector_resumed")
    assert resumed["replayed"] >= 1     # seconds recovered off the buffer
    assert "connector_lost" not in statuses
    # The ride was still going after the outage, not finished by it.
    assert any(
        f.get("status") in ("running", "starting")
        for f in frames[frames.index(resumed):]
    )


def test_the_trainer_is_never_told_to_stop_by_a_transient_drop(
    client, tmp_path, monkeypatch
):
    """111 ms after the socket went, the rider saw power 0 and status paused."""
    uid = _register(client)
    rig = _rig(uid, tmp_path, monkeypatch)
    _drive_ride(client, monkeypatch, rig)

    # Nothing between the drop and the recovery released the trainer. The one
    # at the end of the ride is deliberate and comes after this snapshot.
    during = rig.calls_through_recovery
    assert [c for c in during if c[0] in ("stop", "disable")] == []
    assert rig.disconnects_through_recovery == 0

    kinds = [c[0] for c in rig.trainer.calls]
    # Two arming sequences and no more: one to engage ERG at the start, one to
    # re-arm after the outage - a trainer that sat through one may have
    # dropped out of ERG on its own, and a bare target does not put it back.
    # Every other tick is a single write, which is B-2 from the hardware
    # session: remote mode used to issue three FTMS ops where local issues one.
    assert kinds.count("enable") == 2
    assert kinds.count("set") >= 1


# ------------------------------------------------------- the safety nets
def _connector(tmp_path, rig):
    from wattracker_connector.client import Connector
    from wattracker_connector.handlers import ConnectorConfig

    connector = Connector(
        server_url="http://server.invalid:8000", token="t",
        config=ConnectorConfig(activities_dir=None, workouts_dir=None),
    )
    connector.ble = rig.state
    return connector


def test_a_ride_still_being_ridden_is_never_uploaded(tmp_path, monkeypatch):
    """Reconnecting mid-ride must not post half a workout as a finished one.

    The buffer is discarded on a successful upload, so an early one would not
    just store a partial activity - it would throw away the half still to come.
    """
    rig = _Rig(1, tmp_path)
    connector = _connector(tmp_path, rig)
    rig.state.ride = {"started_at": "2026-08-01T10:00:00", "name": "VO2"}
    rig.state.buffer.start("2026-08-01T10:00:00", "VO2", 250.0, None)
    rig.state.buffer.append(power=200)

    uploads = []
    monkeypatch.setattr(
        "wattracker_connector.client.upload_pending",
        lambda *a, **k: uploads.append(a),
    )
    _run(connector._flush_buffered_ride())
    assert uploads == []

    # Once the ride is over it goes up on the next reconnect, as before.
    rig.state.ride = None
    _run(connector._flush_buffered_ride())
    assert len(uploads) == 1


def test_a_ride_nobody_claims_is_released_rather_than_held_forever(
    tmp_path, monkeypatch
):
    """The other end of holding the trainer across a reconnect.

    A closed ride page, or a server that timed the ride out while we were
    away, would otherwise leave a rider pushing against a workout nobody is
    running - and no page to stop it from.
    """
    rig = _Rig(1, tmp_path)
    connector = _connector(tmp_path, rig)
    rig.state.conn = {"trainer": rig.trainer, "clients": []}
    rig.state.ride = {"started_at": "2026-08-01T10:00:00", "name": "VO2"}
    rig.state.buffer.start("2026-08-01T10:00:00", "VO2", 250.0, None)
    rig.state.buffer.append(power=200)
    rig.state.claimed = False

    uploads = []
    monkeypatch.setattr(
        "wattracker_connector.client.CLAIM_TIMEOUT_S", 0.01
    )
    monkeypatch.setattr(
        "wattracker_connector.client.upload_pending",
        lambda *a, **k: uploads.append(a),
    )
    _run(connector._abandon_unclaimed_ride())

    assert rig.trainer.calls == [("disable", None)]
    assert rig.state.conn is None
    assert len(uploads) == 1        # ...and the ride is not thrown away


def test_a_claimed_ride_is_left_alone_by_the_watchdog(tmp_path, monkeypatch):
    rig = _Rig(1, tmp_path)
    connector = _connector(tmp_path, rig)
    rig.state.conn = {"trainer": rig.trainer, "clients": []}
    rig.state.ride = {"started_at": "2026-08-01T10:00:00", "name": "VO2"}
    rig.state.claimed = True

    monkeypatch.setattr("wattracker_connector.client.CLAIM_TIMEOUT_S", 0.01)
    _run(connector._abandon_unclaimed_ride())
    assert rig.state.conn is not None
    assert rig.trainer.calls == []


def test_a_ride_the_connector_already_ended_is_not_carried_on(
    client, tmp_path, monkeypatch
):
    """Both ends can give up, and they must agree on who owns the record.

    The connector ends an unattended ride on its own once the rider stops. If
    the server then came back and simply carried on, it would drive a trainer
    that has been released and save a copy beside the uploaded one.
    """
    uid = _register(client)
    rig = _rig(uid, tmp_path, monkeypatch)
    monkeypatch.setenv("WATTRACKER_MODE", "server")
    monkeypatch.setattr(servermod, "RIDE_POLL_INTERVAL_S", 0.01)
    monkeypatch.setattr(blemod, "SAMPLE_INTERVAL_S", 0.005)
    rig.attach()

    lost = None
    with client.websocket_connect("/ride/ws?type=endurance&minutes=30") as ws:
        while ws.receive_json().get("status") != "connected":
            pass
        for _ in range(8):
            ws.receive_json()
        rig.detach()
        for _ in range(20):
            if ws.receive_json().get("status") == "connector_offline":
                break
        # The connector's own idle rule fires while we are away.
        _run(rig.state.teardown())
        rig.attach()
        for _ in range(40):
            message = ws.receive_json()
            if message.get("status") == "connector_resumed":
                assert message["riding"] is False
            if message.get("status") == "connector_lost":
                lost = message
                break

    assert lost is not None
    assert db.list_activities(uid) == []
    assert rig.state.buffer.load() is not None


def test_quitting_releases_the_trainer_even_mid_ride(tmp_path):
    """Ctrl-C, the tray quitting, or being displaced is definitive.

    Holding the radio is only ever right when something is coming back for it.
    """
    rig = _Rig(1, tmp_path)
    connector = _connector(tmp_path, rig)
    rig.state.conn = {"trainer": rig.trainer, "clients": []}
    rig.state.ride = {"started_at": "2026-08-01T10:00:00", "name": "VO2"}
    rig.state.buffer.start("2026-08-01T10:00:00", "VO2", 250.0, None)
    rig.state.buffer.append(power=200)

    connector.stop()
    _run(connector.run_forever())

    assert rig.state.conn is None
    assert rig.trainer.calls == [("disable", None)]
    assert rig.state.buffer.load() is not None   # kept, for the next start


def test_an_unattended_ride_ends_itself_once_the_rider_stops(
    tmp_path, monkeypatch
):
    """A server that never comes back must not hold the trainer indefinitely.

    While the rider keeps pedalling the ride keeps recording, however long the
    outage runs. It is stopping that ends it - the same rule they are used to.
    """
    monkeypatch.setattr(blemod, "SAMPLE_INTERVAL_S", 0.001)
    monkeypatch.setattr(blemod, "UNATTENDED_IDLE_S", 0.01)
    rig = _Rig(1, tmp_path)
    monkeypatch.setattr(blemod, "bledevices", rig.devices)

    async def ride_with_nobody_listening():
        await rig.handlers["ble.connect"](
            started_at="2026-08-01T10:00:00", name="VO2", ftp=250.0
        )
        rig.state.claimed = False       # the socket has gone
        rig.pedals.power = 180
        await asyncio.sleep(0.05)
        assert rig.state.conn is not None, "still pedalling: keep recording"
        rig.pedals.power = 0
        for _ in range(200):
            if rig.state.conn is None:
                break
            await asyncio.sleep(0.005)

    _run(ride_with_nobody_listening())
    assert rig.state.conn is None
    assert rig.trainer.calls[-1] == ("disable", None)
    assert rig.state.buffer.load() is not None   # kept, to be uploaded


def test_giving_up_on_a_connector_saves_nothing_here(client, tmp_path, monkeypatch):
    """Because the connector still holds the whole ride, and will upload it.

    Writing a truncated copy first would not merely be worse data: the dedup
    hash is over (start, duration), so the short row and the complete one
    differ and both land - one ride stored as two activities.
    """
    uid = _register(client)
    rig = _rig(uid, tmp_path, monkeypatch)
    monkeypatch.setenv("WATTRACKER_MODE", "server")
    monkeypatch.setattr(servermod, "RIDE_POLL_INTERVAL_S", 0.01)
    monkeypatch.setattr(servermod, "CONNECTOR_OFFLINE_TIMEOUT_S", 0.05)
    monkeypatch.setattr(blemod, "SAMPLE_INTERVAL_S", 0.005)
    rig.attach()

    lost = None
    with client.websocket_connect("/ride/ws?type=endurance&minutes=30") as ws:
        while ws.receive_json().get("status") != "connected":
            pass
        for _ in range(8):
            ws.receive_json()
        rig.detach()
        for _ in range(40):
            message = ws.receive_json()
            if message.get("status") == "connector_lost":
                lost = message
                break

    assert lost is not None
    assert db.list_activities(uid) == []
    # ...and the connector still has it, ready to upload.
    assert rig.state.buffer.load() is not None


def test_a_connector_that_never_buffered_gets_saved_for_rather_than_trusted(
    client, tmp_path, monkeypatch
):
    """Deferring to a file that does not exist would lose the ride outright.

    Every sample carries the connector's index in its own buffer, so one
    having arrived is the proof there is something on the far end to defer
    to. Without that proof the truncated copy is the best there is.
    """
    uid = _register(client)
    rig = _rig(uid, tmp_path, monkeypatch)
    rig.report_index = False        # a buffer that never opened
    monkeypatch.setenv("WATTRACKER_MODE", "server")
    monkeypatch.setattr(servermod, "RIDE_POLL_INTERVAL_S", 0.01)
    monkeypatch.setattr(servermod, "CONNECTOR_OFFLINE_TIMEOUT_S", 0.05)
    monkeypatch.setattr(blemod, "SAMPLE_INTERVAL_S", 0.005)
    rig.attach()

    lost = None
    with client.websocket_connect("/ride/ws?type=endurance&minutes=30") as ws:
        while ws.receive_json().get("status") != "connected":
            pass
        for _ in range(10):
            ws.receive_json()
        rig.detach()
        for _ in range(40):
            message = ws.receive_json()
            if message.get("status") == "connector_lost":
                lost = message
                break

    assert lost is not None
    assert lost["buffered"] is False
    assert len(db.list_activities(uid)) == 1


def test_stopping_a_ride_while_the_connector_is_away_saves_nothing_either(
    client, tmp_path, monkeypatch
):
    """Every way a ride can end offline has to defer, not just the timeout.

    The rider pressing stop mid-outage would otherwise write the truncated
    copy the timeout path exists to avoid, and the connector's complete one
    would land beside it rather than dedupe against it.
    """
    uid = _register(client)
    rig = _rig(uid, tmp_path, monkeypatch)
    monkeypatch.setenv("WATTRACKER_MODE", "server")
    monkeypatch.setattr(servermod, "RIDE_POLL_INTERVAL_S", 0.01)
    monkeypatch.setattr(blemod, "SAMPLE_INTERVAL_S", 0.005)
    rig.attach()

    lost = None
    with client.websocket_connect("/ride/ws?type=endurance&minutes=30") as ws:
        while ws.receive_json().get("status") != "connected":
            pass
        for _ in range(8):
            ws.receive_json()
        rig.detach()
        for _ in range(20):
            if ws.receive_json().get("status") == "connector_offline":
                break
        ws.send_json({"action": "stop"})
        for _ in range(40):
            message = ws.receive_json()
            if message.get("status") == "connector_lost":
                lost = message
            if message.get("status") == "finished":
                break

    assert lost is not None
    assert db.list_activities(uid) == []
    assert rig.state.buffer.load() is not None


# ------------------------------------------------ mid-flight RPC failure
def test_connector_unavailable_during_erg_does_not_latch_erg_off(
    client, tmp_path, monkeypatch
):
    """B-6: a transport failure in the ERG call path must not disable ERG.

    Before the fix, _set_connection_erg's blanket except caught
    ConnectorUnavailable and returned command_enabled=False. The per-tick
    caller then cleared controller.erg_enabled, which was never set back on
    (the only line that could was inside the block it just turned off). On
    main in local mode a single transient BLE write failure would silently
    kill ERG for the rest of the ride.

    The fix lets ConnectorUnavailable through and the per-tick caller skips
    the tick rather than counting a trainer refusal.
    """
    uid = _register(client)
    rig = _rig(uid, tmp_path, monkeypatch)
    monkeypatch.setenv("WATTRACKER_MODE", "server")
    monkeypatch.setattr(servermod, "RIDE_POLL_INTERVAL_S", 0.01)
    monkeypatch.setattr(blemod, "SAMPLE_INTERVAL_S", 0.005)
    rig.attach()

    fail_erg_calls = 0
    failure_injected = False

    original_call = rig.session.call
    session = rig.session

    async def failing_call(method, params=None, *, timeout=None):
        nonlocal fail_erg_calls, failure_injected
        if method == "ble.set_erg":
            fail_erg_calls += 1
            if fail_erg_calls == 2 and not failure_injected:
                failure_injected = True
                session.close()
                raise ConnectorUnavailable("connector disconnected")
        return await original_call(method, params, timeout=timeout)

    rig.session.call = failing_call

    erg_frames = []
    with client.websocket_connect("/ride/ws?type=endurance&minutes=30") as ws:
        while ws.receive_json().get("status") != "connected":
            pass

        ws.send_json({"action": "set_erg", "enabled": True})
        for _ in range(5):
            ws.receive_json()

        for _ in range(30):
            message = ws.receive_json()
            erg_frames.append(message)
            if message.get("status") == "connector_offline":
                break

    erg_disabled_frames = [
        f for f in erg_frames
        if f.get("status") == "erg" and f.get("enabled") is False
    ]
    assert (
        len(erg_disabled_frames) == 0
    ), "ConnectorUnavailable during an ERG call must not produce an " \
       "erg-enabled=false frame -- that would latch ERG off"
