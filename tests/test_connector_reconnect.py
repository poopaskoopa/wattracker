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
import threading
import time

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

from conftest import _receive_until  # noqa: E402


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
        # Every wait below is bounded via `_receive_until`: a regression that
        # stops a frame from ever arriving must fail this test, not hang the
        # whole run. `_appending` folds each received frame into `frames` (the
        # assertions after this helper inspect the full stream) while still
        # deferring to `_receive_until`'s cap-and-fail behaviour.
        def _appending(predicate):
            def wrapped(message):
                frames.append(message)
                return predicate(message)
            return wrapped

        try:
            _receive_until(
                ws,
                _appending(lambda m: m.get("status") == "connected"),
                "a 'connected' frame",
            )
        except Exception:
            raise AssertionError(f"closed early; frames={frames}")

        # ERG does not auto-engage at ride start (pre-existing behaviour, and
        # not something the split caused), so ask for it - the point here is
        # what happens to a trainer that is actually holding a target.
        ws.send_json({"action": "set_erg", "enabled": True})

        # Ride normally for a moment so the connector's buffer has samples the
        # server has already seen - the ones it must NOT replay.
        _receive_until(
            ws,
            _appending(
                lambda m: len([f for f in frames if f.get("status") == "running"]) >= 5
            ),
            "5 'running' frames",
        )

        rig.detach()

        seen_offline = 0

        def _offline_predicate(message):
            nonlocal seen_offline
            if message.get("status") == "connector_offline" or message.get(
                "connector_offline"
            ):
                seen_offline += 1
            return seen_offline >= offline_frames

        _receive_until(
            ws, _appending(_offline_predicate), f"{offline_frames} connector_offline frames"
        )

        rig.attach()

        _receive_until(
            ws,
            _appending(lambda m: m.get("status") in ("connector_resumed", "connector_lost")),
            "a 'connector_resumed' or 'connector_lost' frame",
        )
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
        _receive_until(ws, lambda m: m.get("status") == "connected", "a 'connected' frame")
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
    # And the tray is told it is over. "Not connected" and "not connected and
    # never again" want different icons, and only this says which one this is.
    assert connector.status.stopped is True


def test_being_displaced_leaves_a_reason_a_rider_can_act_on(tmp_path, monkeypatch):
    """The one stop that is nobody's decision and needs explaining.

    ``run_forever`` deliberately does not reconnect when another connector
    takes the account over. The close frame says "4409", the log says the rest,
    and the tray has no log to read - so the sentence it will show has to be
    left somewhere it can find.
    """
    from wattracker_connector import client as clientmod

    rig = _Rig(1, tmp_path)
    connector = _connector(tmp_path, rig)

    async def _displaced():
        raise clientmod._Replaced("sent 4409 (private) keepalive ping timeout")

    monkeypatch.setattr(connector, "_session", _displaced)
    _run(connector.run_forever())

    assert connector.status.connected is False
    assert connector.status.stopped is True
    assert "Only one connector may run per account" in connector.status.stopped_reason
    assert "4409" not in connector.status.stopped_reason


def test_a_revoked_device_is_terminal_and_says_so(tmp_path, monkeypatch):
    """The 403 at the handshake is not a network failure.

    Backing off and redialling the same revoked token forever is exactly what
    the rider should not see: it looks like an unreachable server, so someone
    who does not open the log chases a network problem. The correct shape is
    "not connected, and that is the end of it" - stop retrying, and leave the
    sentence the tray will show where the tray can find it.
    """
    from wattracker_connector import client as clientmod

    # Instant backoff: if the revoked path ever falls through to the generic
    # failure handler, a retrying loop would make many attempts in the sleep
    # below. The one it must make is the last.
    monkeypatch.setattr(clientmod, "_BACKOFF_START_S", 0.01)
    monkeypatch.setattr(clientmod, "_BACKOFF_MAX_S", 0.01)

    rig = _Rig(1, tmp_path)
    connector = _connector(tmp_path, rig)

    attempts = []

    async def _revoked():
        attempts.append(1)
        raise clientmod._Revoked("server rejected WebSocket connection: HTTP 403")

    monkeypatch.setattr(connector, "_session", _revoked)

    thread = threading.Thread(
        target=lambda: _run(connector.run_forever()), daemon=True
    )
    thread.start()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not attempts:
        time.sleep(0.01)
    assert attempts, "the first attempt never happened"

    # It went idle waiting to be re-paired, it did not retry.
    time.sleep(0.3)
    assert attempts == [1], f"a revoked token was retried: {attempts}"
    assert connector.status.connected is False
    # "Stopped" to the tray, not "offline and still trying" - and the reason
    # the rider can act on is left where the tray will read it.
    assert connector.status.stopped is True
    assert "revoked" in connector.status.stopped_reason.lower()
    assert "Pair..." in connector.status.stopped_reason

    # A quit is the other way out of the idle wait, and it must actually end.
    connector._loop.call_soon_threadsafe(connector.stop)
    thread.join(timeout=10)
    assert not thread.is_alive(), "the loop did not exit after the stop"
    assert connector.status.stopped is True


def test_repair_after_revocation_reconnects_with_the_new_token(tmp_path, monkeypatch):
    """Re-pairing is what un-sticks a revoked device, so the two must compose.

    The 403 path goes idle rather than ending the loop on purpose: reconnect()
    posts into the loop that is running run_forever, and a loop that has
    returned cannot be woken - its _loop is None and the repair is a no-op. So
    a revoked connector has to be sitting in the run loop when the tray's
    Pair... item saves a fresh token, or the repair never reaches a handshake.
    """
    from wattracker_connector import client as clientmod

    monkeypatch.setattr(clientmod, "_BACKOFF_START_S", 0.01)
    monkeypatch.setattr(clientmod, "_BACKOFF_MAX_S", 0.01)

    rig = _Rig(1, tmp_path)
    connector = _connector(tmp_path, rig)

    attempts = []

    async def _session():
        attempts.append(connector.token)
        if len(attempts) == 1:
            raise clientmod._Revoked(
                "server rejected WebSocket connection: HTTP 403"
            )
        # A fresh token proves itself: stay connected until we are told to stop.
        await connector._stop.wait()

    monkeypatch.setattr(connector, "_session", _session)

    thread = threading.Thread(
        target=lambda: _run(connector.run_forever()), daemon=True
    )
    thread.start()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not attempts:
        time.sleep(0.01)
    assert attempts == ["t"], "the first (revoked) attempt never happened"

    # Gone idle on the revoked token, not retrying it.
    time.sleep(0.3)
    assert attempts == ["t"], "a revoked token was retried instead of going idle"

    # The rider re-pairs: the token on the object is new, and the loop dials
    # with it - no process restart, no backoff to wait out.
    connector.token = "new-token"
    connector.reconnect()

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and len(attempts) < 2:
        time.sleep(0.01)
    connector._loop.call_soon_threadsafe(connector.stop)
    thread.join(timeout=10)
    assert not thread.is_alive(), "the loop did not exit after the stop"
    assert attempts == ["t", "new-token"], (
        f"the repair after revocation did not redial with the new token: "
        f"{attempts}"
    )
    assert connector.status.stopped is True


def test_a_403_at_the_handshake_is_a_revocation_not_a_blip(tmp_path, monkeypatch):
    """The wire to _Revoked, pinned to the one answer it is.

    The server refuses a revoked token before accept(); uvicorn turns that into
    an HTTP 403 on the raw socket, and websockets surfaces it as
    InvalidStatus - "server rejected WebSocket connection: HTTP 403", the line
    from the issue. A 403 is the refused-credential answer and becomes the
    terminal _Revoked; any other status is a transport failure left for the
    ordinary retry path.
    """
    import websockets

    from websockets.exceptions import InvalidStatus
    from wattracker_connector import client as clientmod

    rig = _Rig(1, tmp_path)
    connector = _connector(tmp_path, rig)

    def _refuse(status_code):
        from types import SimpleNamespace

        # websockets.connect is used as ``async with ... as connection``, so
        # the fake has to hand back an async context manager, not a coroutine;
        # the refusal lands in __aenter__, the way a real handshake fails.
        class _Rejecting:
            async def __aenter__(self):
                # All InvalidStatus.__str__ and the client read is status_code.
                raise InvalidStatus(SimpleNamespace(status_code=status_code))

            async def __aexit__(self, *_exc):
                return False

        def _connect(*_args, **_kwargs):
            return _Rejecting()

        monkeypatch.setattr(websockets, "connect", _connect)

    _refuse(403)
    with pytest.raises(clientmod._Revoked):
        _run(connector._session())

    _refuse(500)
    with pytest.raises(InvalidStatus):
        _run(connector._session())


def test_repair_drops_the_serving_session_and_dials_with_the_new_token(
    tmp_path, monkeypatch
):
    """A saved re-pair must not keep serving on the token it replaced.

    The socket stays open - a server that revoked the token has already closed
    this one, but the code cannot assume which it is - and the old credential
    keeps working on a session that was already accepted. The only way the new
    token is used at all is a fresh handshake, so the session that is serving
    has to go when the pairing changes, without waiting for the link to drop.
    """
    from websockets.exceptions import ConnectionClosedOK

    rig = _Rig(1, tmp_path)
    connector = _connector(tmp_path, rig)

    sessions = []

    class _Socket:
        def __init__(self, connector) -> None:
            self._connector = connector

        async def send_text(self, _text):
            pass

        async def receive_text(self):
            # No frame is coming. When the test stops the connector, close the
            # way a real socket would, so _serve has its ordinary exit.
            await self._connector._stop.wait()
            raise ConnectionClosedOK(None, None)

    class _Peer:
        def resolve(self, _message):
            return False

        async def serve(self, *args, **kwargs):
            pass

    async def _serving_session():
        sessions.append(connector.token)
        peer = _Peer()
        connector._peer = peer
        await connector._serve(_Socket(connector), peer)

    monkeypatch.setattr(connector, "_session", _serving_session)

    thread = threading.Thread(
        target=lambda: _run(connector.run_forever()), daemon=True
    )
    thread.start()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and len(sessions) != 1:
        time.sleep(0.01)
    assert sessions == ["t"], "the first session never started serving"

    connector.token = "new-token"
    connector.reconnect()

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and len(sessions) != 2:
        time.sleep(0.01)
    # stop() is a loop-thread call (the _ConnectorThread is what the tray
    # uses), so the request is posted the same way reconnect() posts its own.
    connector._loop.call_soon_threadsafe(connector.stop)
    thread.join(timeout=10)
    assert not thread.is_alive(), "the loop did not exit after the stop"
    assert sessions == ["t", "new-token"], (
        f"the second session did not go out with the new token: {sessions}"
    )
    assert connector.status.stopped is True


def test_repair_while_offline_wakes_the_backoff_instead_of_earning_it(
    tmp_path, monkeypatch
):
    """A saved re-pair is not an outage, and must not wait out one.

    The backoff is sized for a server that is rebooting or switched off - a
    ceiling of minutes, where a rider who just pasted a fresh token wants the
    answer now.
    """
    from wattracker_connector import client as clientmod

    monkeypatch.setattr(clientmod, "_BACKOFF_START_S", 1000.0)
    monkeypatch.setattr(clientmod, "_BACKOFF_MAX_S", 1000.0)

    rig = _Rig(1, tmp_path)
    connector = _connector(tmp_path, rig)

    attempts = []

    async def _always_down():
        attempts.append(time.monotonic())
        raise ConnectionRefusedError("server down")

    monkeypatch.setattr(connector, "_session", _always_down)

    thread = threading.Thread(
        target=lambda: _run(connector.run_forever()), daemon=True
    )
    thread.start()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not attempts:
        time.sleep(0.01)
    assert attempts, "the first attempt never happened"

    connector.reconnect()

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and len(attempts) < 2:
        time.sleep(0.01)
    connector._loop.call_soon_threadsafe(connector.stop)
    thread.join(timeout=10)
    assert len(attempts) == 2, "the repair never produced a retry"
    assert attempts[1] - attempts[0] < 2.0, (
        f"the retry waited out the backoff instead of landing on the repair: "
        f"{attempts[1] - attempts[0]:.1f}s"
    )
    assert connector.status.stopped is True


def test_a_repair_is_not_charged_an_outages_backoff(tmp_path, monkeypatch):
    """Re-pairing must not make the *next* real outage wait longer.

    The wait wakes on a repair either way, so a prompt retry does not prove
    this on its own. What does is the delay the retry *after* it is sized
    against: a rider who re-pairs three times while a server happens to be
    down must not have talked the connector into a three-times-longer wait,
    and the log must not announce a delay that is never served.
    """
    from wattracker_connector import client as clientmod

    # Long enough that nothing here times out naturally - every wait this test
    # sees ends because of a repair - and a factor big enough that a single
    # wrongly-charged repair is unmistakable.
    monkeypatch.setattr(clientmod, "_BACKOFF_START_S", 30.0)
    monkeypatch.setattr(clientmod, "_BACKOFF_MAX_S", 10000.0)
    monkeypatch.setattr(clientmod, "_BACKOFF_FACTOR", 10.0)
    # No jitter, so a recorded delay is the backoff itself and the assertion
    # is about escalation rather than about a random draw.
    monkeypatch.setattr(clientmod.random, "random", lambda: 0.5)

    rig = _Rig(1, tmp_path)
    connector = _connector(tmp_path, rig)

    delays = []
    real_wait = connector._wait_for_repair_or_stop

    async def _record(timeout=None):
        delays.append(timeout)
        # The return value is the whole point - it is what tells the loop the
        # wait was cut short - so the double has to pass it back.
        return await real_wait(timeout=timeout)

    monkeypatch.setattr(connector, "_wait_for_repair_or_stop", _record)

    attempts = []

    async def _always_down():
        attempts.append(1)
        raise ConnectionRefusedError("server down")

    monkeypatch.setattr(connector, "_session", _always_down)

    thread = threading.Thread(
        target=lambda: _run(connector.run_forever()), daemon=True
    )
    thread.start()

    # Three re-pairs in a row, each answered by a redial.
    for expected in (2, 3, 4):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and len(attempts) < expected - 1:
            time.sleep(0.01)
        connector.reconnect()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and len(attempts) < expected:
            time.sleep(0.01)
    assert len(attempts) >= 4, f"the repairs did not all redial: {attempts}"

    connector._loop.call_soon_threadsafe(connector.stop)
    thread.join(timeout=10)
    assert not thread.is_alive(), "the loop did not exit after the stop"

    # Every wait entered was still the first backoff step. Charged as outages
    # the three repairs would have taken this to 300s, then 3000s.
    assert all(d == 30.0 for d in delays), (
        f"a re-pair escalated the backoff: {delays}"
    )


def test_a_repair_does_not_announce_a_delay_it_will_not_serve(
    tmp_path, monkeypatch, caplog
):
    """The log is the only account of itself the connector keeps.

    A re-pair that lands while the session is still up ends that session and
    dials again immediately. Falling through to the backoff would print
    "reconnecting in 47.3s" and then reconnect at once - which is not what
    happened, and this log is what anyone diagnosing a connector reads first.
    """
    from websockets.exceptions import ConnectionClosedOK

    from wattracker_connector import client as clientmod

    # Large, so a delay that did get announced is unmistakable in the log.
    monkeypatch.setattr(clientmod, "_BACKOFF_START_S", 900.0)
    monkeypatch.setattr(clientmod, "_BACKOFF_MAX_S", 900.0)

    rig = _Rig(1, tmp_path)
    connector = _connector(tmp_path, rig)

    sessions = []

    class _Socket:
        def __init__(self, connector) -> None:
            self._connector = connector

        async def send_text(self, _text):
            pass

        async def receive_text(self):
            await self._connector._stop.wait()
            raise ConnectionClosedOK(None, None)

    class _Peer:
        def resolve(self, _message):
            return False

        async def serve(self, *args, **kwargs):
            pass

    async def _serving_session():
        sessions.append(connector.token)
        peer = _Peer()
        connector._peer = peer
        await connector._serve(_Socket(connector), peer)

    monkeypatch.setattr(connector, "_session", _serving_session)

    with caplog.at_level("INFO", logger=clientmod.log.name):
        thread = threading.Thread(
            target=lambda: _run(connector.run_forever()), daemon=True
        )
        thread.start()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and len(sessions) != 1:
            time.sleep(0.01)
        assert sessions == ["t"], "the first session never started serving"

        connector.token = "new-token"
        connector.reconnect()

        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and len(sessions) != 2:
            time.sleep(0.01)
        connector._loop.call_soon_threadsafe(connector.stop)
        thread.join(timeout=10)

    assert sessions == ["t", "new-token"], f"the repair did not redial: {sessions}"
    announced = [r.getMessage() for r in caplog.records
                 if "reconnecting in" in r.getMessage()]
    assert not announced, (
        f"a repair announced a backoff delay it then did not serve: {announced}"
    )


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
        _receive_until(ws, lambda m: m.get("status") == "connected", "a 'connected' frame")
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
        _receive_until(ws, lambda m: m.get("status") == "connected", "a 'connected' frame")
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
        _receive_until(ws, lambda m: m.get("status") == "connected", "a 'connected' frame")
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
        _receive_until(ws, lambda m: m.get("status") == "connected", "a 'connected' frame")

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
