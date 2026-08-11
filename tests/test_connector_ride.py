"""Riding through a connector, and surviving losing it mid-ride.

The connector stands in for the BLE radio; the workout, the state machine and
the saving all stay on the server. So these tests drive the real /ride/ws
handler with a real connector attached, whose bledevices module is swapped for
simulated hardware - which is the same substitution the local-mode ride tests
already make, one machine further away.
"""
import datetime as _dt

import pytest

pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from wattracker import connectorauth, connectorhub, db  # noqa: E402
from wattracker.backend.remote_ble import RemoteSampleSink  # noqa: E402
from wattracker.ingest import importer  # noqa: E402
from wattracker.server import create_app  # noqa: E402
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


# ------------------------------------------------------------ staleness
def test_a_silent_connector_reads_as_no_power_not_steady_power():
    """The trap this proxy exists to avoid.

    Without staleness, a dead link looks to the controller like a rider
    holding a perfectly steady wattage: the clock keeps running on a ride
    nobody is pedalling, and it never auto-pauses.
    """
    now = {"t": 1000.0}
    sink = RemoteSampleSink(clock=lambda: now["t"])
    sink.update(power=210, cadence=88.0, hr=142)

    assert sink.latest_power() == 210
    assert sink.latest_cadence() == 88.0
    assert sink.latest_hr() == 142

    now["t"] += 2.0          # inside the 3s window
    assert sink.latest_power() == 210

    now["t"] += 2.0          # 4s: stale
    assert sink.latest_power() is None
    assert sink.latest_cadence() is None
    assert sink.latest_hr() is None

    # A fresh frame revives all three at once - they share a connector.
    sink.update(power=0, cadence=None, hr=140)
    assert sink.latest_power() == 0
    assert sink.latest_hr() == 140


def test_a_sink_that_never_saw_a_frame_reports_nothing():
    sink = RemoteSampleSink()
    assert sink.latest_power() is None
    assert sink.fresh is False


# -------------------------------------------------------- erg proxying
def test_the_proxy_trainer_declares_erg_explicitly():
    """server._connection_erg_state reads these with getattr(..., True).

    A proxy that merely forgot to define them would silently claim ERG works
    on hardware that has none.
    """
    from wattracker.backend.remote_ble import RemoteTrainer

    trainer = RemoteTrainer(lambda: None)
    assert isinstance(type(trainer).erg_available, property)
    assert isinstance(type(trainer).erg_enabled, property)
    assert trainer.erg_enabled is False


# ------------------------------------------------------- buffered rides
def test_buffer_round_trips_a_ride(tmp_path):
    buffer = RideBuffer(str(tmp_path / "ride.jsonl"))
    buffer.start("2026-08-01T10:00:00", "VO2 5x4", 250.0, 7)
    for watts in (0, 150, 210, 205):
        buffer.append(power=watts, cadence=90.0, hr=145)
    buffer.finish()

    loaded = buffer.load()
    assert loaded["started_at"] == "2026-08-01T10:00:00"
    assert loaded["name"] == "VO2 5x4"
    assert loaded["ftp"] == 250.0
    assert loaded["workout_id"] == 7
    assert loaded["duration_s"] == 4
    assert loaded["samples"]["power"] == [0, 150, 210, 205]
    assert loaded["samples"]["heartrate"] == [145, 145, 145, 145]


def test_buffer_survives_a_torn_final_line(tmp_path):
    """Killed mid-write: lose the last second, not the whole ride."""
    path = tmp_path / "ride.jsonl"
    buffer = RideBuffer(str(path))
    buffer.start("2026-08-01T10:00:00", "VO2 5x4", 250.0, None)
    buffer.append(power=100, cadence=None, hr=None)
    buffer.append(power=200, cadence=None, hr=None)
    with open(path, "a") as handle:
        handle.write('[300, null, nu')  # torn

    loaded = buffer.load()
    assert loaded["samples"]["power"] == [100, 200]


def test_starting_a_ride_discards_the_previous_buffer(tmp_path):
    buffer = RideBuffer(str(tmp_path / "ride.jsonl"))
    buffer.start("2026-08-01T10:00:00", "Old", 250.0, None)
    buffer.append(power=100)
    buffer.start("2026-08-02T10:00:00", "New", 250.0, None)
    buffer.append(power=200)

    loaded = buffer.load()
    assert loaded["name"] == "New"
    assert loaded["samples"]["power"] == [200]


def test_an_empty_or_missing_buffer_loads_as_nothing(tmp_path):
    assert RideBuffer(str(tmp_path / "nope.jsonl")).load() is None
    empty = tmp_path / "empty.jsonl"
    empty.write_text("")
    assert RideBuffer(str(empty)).load() is None


# ------------------------------------------------- the upload endpoint
def _upload(client, token, **overrides):
    payload = {
        "started_at": "2026-08-01T10:00:00",
        "name": "VO2 5x4",
        "ftp": 250.0,
        "duration_s": 3,
        "samples": {"power": [100, 200, 210], "cadence": [90, 90, 90],
                    "heartrate": [140, 145, 150]},
    }
    payload.update(overrides)
    return client.post(
        "/api/connector/ride", json=payload,
        headers={"Authorization": f"Bearer {token}"},
    )


def test_a_buffered_ride_uploads_as_an_activity(client):
    uid = _register(client)
    _device_id, token = connectorauth.generate_token(uid, "Zwift PC")

    response = _upload(client, token)
    assert response.status_code == 200
    activity_id = response.json()["activity_id"]
    assert activity_id is not None

    activities = db.list_activities(uid)
    assert len(activities) == 1
    # Named like any other in-app ride, which is what marks it as one.
    assert activities[0]["filename"].startswith("Ride 2026-08-01 ")


def test_re_uploading_the_same_ride_is_reported_and_not_stored_twice(client):
    """The connector retries until it gets an answer, so this must be safe."""
    uid = _register(client)
    _device_id, token = connectorauth.generate_token(uid, "Zwift PC")

    first = _upload(client, token)
    second = _upload(client, token)
    assert first.json()["activity_id"] is not None
    assert second.json()["duplicate"] is True
    assert len(db.list_activities(uid)) == 1


def test_upload_requires_a_valid_token(client):
    _register(client)
    assert _upload(client, "A" * 43).status_code == 401
    response = client.post("/api/connector/ride", json={})
    assert response.status_code == 401


def test_upload_rejects_a_malformed_ride(client):
    uid = _register(client)
    _device_id, token = connectorauth.generate_token(uid, "Zwift PC")

    assert _upload(client, token, samples={}).status_code == 400
    assert _upload(client, token, started_at="not a date").status_code == 400
    over = {"power": [1] * 90000}
    assert _upload(client, token, samples=over).status_code == 413
    assert db.list_activities(uid) == []


def test_upload_cannot_attach_a_ride_to_another_users_workout(client):
    """workout_id is scoped, so a connector cannot complete someone else's plan."""
    uid = _register(client)
    other = db.create_user("other", "hash")
    plan_id = db.create_plan(other, "Theirs", "2026-08-01", 1)
    workout_id = db.add_plan_workout(
        plan_id, other, "2026-08-01", "Theirs", "endurance", 60, 1.0,
        "<workout_file/>",
    )
    _device_id, token = connectorauth.generate_token(uid, "Zwift PC")

    response = _upload(client, token, workout_id=workout_id)
    assert response.status_code == 200
    # Stored for the uploading user, and NOT linked to the other user's plan.
    assert db.get_plan_workout(other, workout_id)["completed_activity_id"] is None


def test_a_buffered_ride_lands_the_same_as_an_uninterrupted_one(client):
    """The point of routing both through importer.save_ride_record.

    A ride that happened to span a reconnect is still one ride, and must not
    be a second-class row.
    """
    uid = _register(client)
    _device_id, token = connectorauth.generate_token(uid, "Zwift PC")
    started = _dt.datetime(2026, 8, 1, 10, 0, 0)
    samples = {"power": [100, 200, 210], "cadence": [90, 90, 90],
               "heartrate": [140, 145, 150]}

    # In-process, as the controller would.
    direct_id, _record = importer.save_ride_record(
        uid, started, 3, samples, "VO2 5x4", 250.0, None
    )
    assert direct_id is not None

    # The same ride arriving as an upload dedupes against it...
    response = _upload(client, token)
    assert response.json()["duplicate"] is True

    # ...and a distinct one produces the same shape of row.
    other = _upload(client, token, started_at="2026-08-02T10:00:00")
    rows = {a["id"]: a for a in db.list_activities(uid)}
    uploaded_row = rows[other.json()["activity_id"]]
    direct_row = rows[direct_id]
    assert uploaded_row["duration_s"] == direct_row["duration_s"]
    assert uploaded_row["np"] == direct_row["np"]
    assert uploaded_row["tss"] == direct_row["tss"]


# --------------------------------------------------------------- teardown
class _RecordingTrainer:
    """Records which release command it was given, and in what order."""

    def __init__(self, *, offers=("async_disable_erg", "async_stop",
                                  "async_set_target_power")):
        self.calls = []
        for name in offers:
            setattr(self, name, self._recorder(name))

    def _recorder(self, name):
        async def _call(*args):
            self.calls.append((name, args))
        return _call


def _teardown_with(trainer):
    import asyncio

    from wattracker_connector.ble_handlers import BleState

    state = BleState()
    state.conn = {"trainer": trainer, "clients": []}
    asyncio.run(state.teardown())
    return trainer.calls


def test_teardown_releases_erg_rather_than_holding_it_at_zero():
    """A real Trainer defines all three, so order decides what it is told.

    async_set_target_power(0) does not release anything - it leaves ERG
    engaged, holding the wheel against the rider at 0 W. Trying it first meant
    a real trainer never received a stop at all.
    """
    calls = _teardown_with(_RecordingTrainer())
    assert calls == [("async_disable_erg", ())]


def test_teardown_falls_back_when_a_trainer_offers_less():
    """The chain is a fallback, not a sequence: first match wins, and stops."""
    assert _teardown_with(
        _RecordingTrainer(offers=("async_stop", "async_set_target_power"))
    ) == [("async_stop", ())]
    assert _teardown_with(
        _RecordingTrainer(offers=("async_set_target_power",))
    ) == [("async_set_target_power", (0,))]


def test_teardown_survives_a_trainer_with_nothing_to_offer():
    assert _teardown_with(_RecordingTrainer(offers=())) == []


# ----------------------------------------------------------- the cadence role
# A standalone cadence sensor is a role the local radio grew after this branch
# was cut (devices.BleakCadenceSource). It has to arrive through the connector
# too, or the same sensor works when the app owns the radio and silently does
# nothing when a connector does - the exact divergence the remote module exists
# to prevent.
class _AnsweringSession:
    """A connector that answers ble.connect/ble.disconnect with a device list."""

    def __init__(self, devices, errors=None):
        self.devices = devices
        self.errors = errors or []
        self.ble_sink = None
        self.calls = []

    async def call(self, method, params=None, *, timeout=None):
        self.calls.append((method, params or {}))
        return {"devices": self.devices, "errors": self.errors}


def _connect(devices):
    import asyncio

    from wattracker.backend import remote_ble

    session = _AnsweringSession(devices)
    conn = asyncio.run(remote_ble.connect_sensors(session))
    return session, conn


_CADENCE_ONLY = [{"address": "CC", "name": "FakeCadence", "roles": ["cadence"]}]
_POWER_AND_CADENCE = [
    {"address": "AA", "name": "FakePedals", "roles": ["power"]},
    {"address": "CC", "name": "FakeCadence", "roles": ["cadence"]},
]


def test_a_standalone_cadence_sensor_arrives_as_a_cadence_source():
    _, conn = _connect(_CADENCE_ONLY)

    assert conn["cadence_source"] is not None
    assert conn["names"]["cadence"] == "FakeCadence"
    conn.sink.update(power=None, cadence=91.0, hr=None)
    assert conn["cadence_source"].latest_cadence() == 91.0


def test_a_cadence_only_connection_is_not_mistaken_for_power():
    """server._connection_has_power tells the stand-in apart by identity.

    devices.connect_sensors aliases a lone cadence sensor into ``power_source``
    for legacy consumers, so the server distinguishes the alias from a real
    power meter by asking whether the two are the same object. Building a
    second, equal-looking RemoteCadenceSource here would defeat that check and
    start a ride on a rider who has no way to produce watts.
    """
    _, conn = _connect(_CADENCE_ONLY)

    assert conn["power_source"] is conn["cadence_source"]
    # And the alias must not report the frame's watts as its own measurement.
    conn.sink.update(power=210, cadence=91.0, hr=None)
    assert conn["power_source"].latest_power() is None


def test_a_power_meter_keeps_its_cadence_role_separate():
    _, conn = _connect(_POWER_AND_CADENCE)

    assert conn["power_source"] is not conn["cadence_source"]
    conn.sink.update(power=210, cadence=91.0, hr=None)
    assert conn["power_source"].latest_power() == 210
    assert conn["cadence_source"].latest_cadence() == 91.0


def test_dropping_the_power_meter_mid_ride_leaves_the_cadence_alias():
    """The rebind after a per-device disconnect must land in the same state.

    A rider who drops their power meter mid-ride has to end up where one who
    never selected it starts - otherwise the two paths disagree about what
    ``power_source`` means.
    """
    import asyncio

    from wattracker.backend import remote_ble

    session, conn = _connect(_POWER_AND_CADENCE)
    session.devices = _CADENCE_ONLY
    asyncio.run(remote_ble.disconnect_sensor(conn, "AA"))

    assert conn["power_source"] is conn["cadence_source"]
    assert conn["cadence_source"] is not None
    assert conn["names"]["cadence"] == "FakeCadence"


class _CadencelessPedals:
    """A power meter that measures watts but reports no cadence."""

    def latest_power(self):
        return 205

    def latest_cadence(self):
        return None


class _CadenceSensor:
    """A CSC sensor: cadence, and explicitly no power. Mirrors BleakCadenceSource."""

    def __init__(self, cadence=93.0):
        self.cadence = cadence

    def latest_power(self):
        return None

    def latest_cadence(self):
        return self.cadence


class _SplitRolesRadio:
    """A radio holding a cadence-less power meter and a separate cadence sensor."""

    def __init__(self):
        self.pedals = _CadencelessPedals()
        self.cadence = _CadenceSensor()

    def bluetooth_available(self):
        return True, "ok"

    async def scan(self, timeout=5.0, attempts=2):
        return []

    async def connect_sensors(self, timeout=6.0, selected=None):
        return {
            "trainer": None,
            "power_source": self.pedals,
            "cadence_source": self.cadence,
            "hr_source": None,
            "clients": [],
            "clients_by_address": {},
            "bindings": {
                "AA": {"name": "FakePedals", "roles": {"power": None}},
                "CC": {"name": "FakeCadence", "roles": {"cadence": None}},
            },
            "names": {"power": "FakePedals", "cadence": "FakeCadence"},
            "errors": [],
        }

    async def disconnect_sensor(self, conn, address):
        return conn


def test_the_connector_samples_a_standalone_cadence_sensor(tmp_path, monkeypatch):
    """The frame is the only thing the server ever gets to look at.

    RideController.poll prefers a power meter's own cadence and falls back to a
    standalone sensor. Up here that precedence has to be applied *before* the
    frame is sent, because the server has no second source to fall back to -
    without it the rider's cadence sensor reads as no cadence at all.
    """
    import asyncio

    from wattracker_connector import ble_handlers as blemod
    from wattracker_connector.ble_handlers import BleState, build_ble_handlers

    radio = _SplitRolesRadio()
    monkeypatch.setattr(blemod, "bledevices", radio)

    async def _drive():
        sent = []
        state = BleState(buffer=RideBuffer(str(tmp_path / "ride-buffer.jsonl")))

        async def _send_event(event, **fields):
            sent.append((event, fields))

        handlers = build_ble_handlers(state, _send_event)
        await handlers["ble.connect"](timeout=0.0)
        # The loop records and sends its first frame before it ever sleeps.
        for _ in range(10):
            await asyncio.sleep(0)
            if sent:
                break
        await state.teardown()
        return sent

    sent = asyncio.run(_drive())

    assert sent, "the connector sent no sample frame at all"
    event, fields = sent[0]
    assert event == "ble.sample"
    assert fields["power"] == 205
    assert fields["cadence"] == 93.0
