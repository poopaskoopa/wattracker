"""Tests for the RideController state machine using simulated devices."""
import pytest

from tranalyzer import db
from tranalyzer.ble.devices import (
    SimulatedHeartRateSource,
    SimulatedPowerSource,
    SimulatedTrainer,
)
from tranalyzer.ble.runner import RideController
from tranalyzer.prescribe.planner import Segment, Session


def _two_block_session():
    return Session(
        name="Test Ride",
        description="",
        workout_type="custom",
        segments=[
            Segment(kind="steadystate", duration=10, power=0.5),  # 50% FTP
            Segment(kind="steadystate", duration=10, power=1.0),  # 100% FTP
        ],
    )


def test_pauses_at_zero_power_before_start():
    c = RideController(_two_block_session(), 200, trainer=SimulatedTrainer(), autosave=False)
    c.tick(power=0)
    c.tick(power=0)
    assert c.status == "idle"
    assert c.elapsed == 0.0
    assert c.trainer.targets == []  # no ERG target while idle


def test_starts_on_first_pedal_and_sets_erg_target():
    trainer = SimulatedTrainer()
    c = RideController(_two_block_session(), 200, trainer=trainer, autosave=False)
    st = c.tick(power=120)
    assert st["status"] == "running"
    assert c.elapsed == 1.0
    # elapsed 1 is in block 0 (50% x 200 = 100W)
    assert trainer.last_target == 100


def test_erg_target_follows_each_segment():
    trainer = SimulatedTrainer()
    c = RideController(_two_block_session(), 200, trainer=trainer, autosave=False)
    for _ in range(20):
        c.tick(power=150, dt=1)
    # Block 0 (50% -> 100W) then block 1 (100% -> 200W) must both appear.
    assert 100 in trainer.targets
    assert 200 in trainer.targets
    assert c.status == "finished"  # 20s session complete


def test_auto_pause_then_resume():
    c = RideController(_two_block_session(), 200, trainer=SimulatedTrainer(), autosave=False)
    c.tick(power=100)            # running, elapsed 1
    assert c.status == "running"
    c.tick(power=0)             # pause
    assert c.status == "paused"
    assert c.elapsed == 1.0     # clock did not advance
    c.tick(power=100)           # resume
    assert c.status == "running"
    assert c.elapsed == 2.0


def test_auto_stop_after_zero_power_grace():
    c = RideController(
        _two_block_session(), 200, trainer=SimulatedTrainer(),
        zero_grace_s=3, autosave=False,
    )
    c.tick(power=100)  # start
    c.tick(power=0)    # zero_run 1
    c.tick(power=0)    # zero_run 2
    assert c.status == "paused"
    c.tick(power=0)    # zero_run 3 >= grace -> finish
    assert c.status == "finished"


def test_zero_before_start_never_auto_stops():
    c = RideController(
        _two_block_session(), 200, zero_grace_s=1, autosave=False,
    )
    for _ in range(10):
        c.tick(power=0)
    assert c.status == "idle"  # still waiting to start, not finished


def test_finish_sets_trainer_to_zero():
    trainer = SimulatedTrainer()
    c = RideController(_two_block_session(), 200, trainer=trainer, autosave=False)
    for _ in range(20):
        c.tick(power=150)
    assert c.status == "finished"
    assert trainer.targets[-1] == 0  # ERG released on finish


def test_poll_drives_from_simulated_sources():
    # power script: idle, idle, pedal x3, then zeros to auto-stop (grace 2)
    ps = SimulatedPowerSource([0, 0, 120, 120, 120, 0, 0, 0], cadences=[0, 0, 85, 88, 90, 0, 0, 0])
    hrs = SimulatedHeartRateSource([0, 0, 140, 142, 145, 145, 145, 145])
    c = RideController(
        _two_block_session(), 200, trainer=SimulatedTrainer(),
        power_source=ps, hr_source=hrs, zero_grace_s=2, autosave=False,
    )
    statuses = []
    for _ in range(8):
        st = c.poll(dt=1)
        statuses.append(st["status"])
    assert statuses[0] == "idle"          # first 0W tick
    assert "running" in statuses          # started on pedal
    assert c.status == "finished"         # zeros after start -> auto-stop


def test_erg_started_once_and_stopped_on_finish():
    trainer = SimulatedTrainer()
    c = RideController(_two_block_session(), 200, trainer=trainer, autosave=False)
    c.tick(power=0)  # idle: no ERG commands yet
    assert trainer.commands == []
    c.tick(power=120)  # ride start -> Request Control + Start
    assert trainer.commands == ["request_control", "start"]
    c.tick(power=0)    # pause
    c.tick(power=120)  # resume must NOT re-send request control
    assert trainer.commands == ["request_control", "start"]
    for _ in range(20):
        c.tick(power=120)
    assert c.status == "finished"
    assert trainer.commands == ["request_control", "start", "stop"]
    assert trainer.targets[-1] == 0  # ERG target zeroed before stop


def test_erg_target_follows_ramp_each_second():
    # 10s warmup ramp 50% -> 100% at FTP 200: target steps up every tick.
    session = Session(
        name="Ramp", description="", workout_type="custom",
        segments=[Segment(kind="warmup", duration=10, power_low=0.5, power_high=1.0)],
    )
    trainer = SimulatedTrainer()
    c = RideController(session, 200, trainer=trainer, autosave=False)
    for _ in range(10):
        c.tick(power=150, dt=1)
    # Expected: fraction 0.5 + 0.05*t at t=1..10 -> 110,120,...,200 W, then the
    # finish zeroes the ERG target.
    assert c.status == "finished"
    assert trainer.targets == [110, 120, 130, 140, 150, 160, 170, 180, 190, 200, 0]


def test_ride_survives_failing_trainer():
    class FailingTrainer(SimulatedTrainer):
        def set_target_power(self, watts):
            raise RuntimeError("BLE write failed")

        def start_erg(self):
            raise RuntimeError("BLE write failed")

    c = RideController(
        _two_block_session(), 200, trainer=FailingTrainer(), autosave=False
    )
    for _ in range(20):
        c.tick(power=150)
    assert c.status == "finished"  # trainer errors never crash the ride


def test_no_trainer_power_display_still_works():
    c = RideController(_two_block_session(), 200, trainer=None, autosave=False)
    st = c.tick(power=180)
    assert st["status"] == "running"
    assert st["power"] == 180
    assert st["target_watts"] == 100  # target still computed for display


# ------------------------------------------- BleakTrainer over a fake client
class FakeBleakClient:
    """Records GATT writes/notify subscriptions; optionally fails writes."""

    def __init__(self, fail_writes=False):
        self.writes = []
        self.notify_subs = []
        self.fail_writes = fail_writes

    async def write_gatt_char(self, char, data, response=False):
        if self.fail_writes:
            raise RuntimeError("device gone")
        self.writes.append((char, bytes(data)))

    async def start_notify(self, char, callback):
        self.notify_subs.append((char, callback))


def test_bleak_trainer_sends_erg_command_bytes():
    import asyncio

    from tranalyzer.ble.devices import BleakTrainer
    from tranalyzer.ble.protocol import FITNESS_MACHINE_CONTROL_POINT

    client = FakeBleakClient()
    trainer = BleakTrainer(client)
    asyncio.run(trainer.prepare())
    asyncio.run(trainer.async_set_target_power(250))
    asyncio.run(trainer.async_stop())

    chars = {c for c, _ in client.writes}
    assert chars == {FITNESS_MACHINE_CONTROL_POINT}
    payloads = [d for _, d in client.writes]
    assert payloads == [
        bytes([0x00]),              # Request Control
        bytes([0x07]),              # Start/Resume
        bytes([0x05, 0xFA, 0x00]),  # Set Target Power 250W sint16 LE
        bytes([0x08, 0x01]),        # Stop
    ]
    # Subscribed to control-point indications for 0x80 responses.
    assert client.notify_subs and client.notify_subs[0][0] == FITNESS_MACHINE_CONTROL_POINT


def test_bleak_trainer_sync_entrypoints_drive_writes():
    from tranalyzer.ble.devices import BleakTrainer

    client = FakeBleakClient()
    trainer = BleakTrainer(client)
    trainer.start_erg()             # no running loop -> executed to completion
    trainer.set_target_power(180)
    trainer.stop_erg()
    payloads = [d for _, d in client.writes]
    assert bytes([0x05, 0xB4, 0x00]) in payloads  # 180W target
    assert payloads[0] == bytes([0x00]) and payloads[-1] == bytes([0x08, 0x01])


def test_bleak_trainer_handles_indication_responses():
    from tranalyzer.ble.devices import BleakTrainer

    trainer = BleakTrainer(FakeBleakClient())
    # Success response is recorded.
    trainer._on_control_point(None, bytearray([0x80, 0x05, 0x01]))
    assert trainer.last_response["success"] is True
    # Failure response is logged/recorded, never raised.
    trainer._on_control_point(None, bytearray([0x80, 0x05, 0x04]))
    assert trainer.last_response["success"] is False
    # Garbage indication is ignored.
    trainer._on_control_point(None, bytearray([0x42]))
    assert trainer.last_response["result"] == 0x04


def test_bleak_trainer_write_failure_degrades_gracefully():
    import asyncio

    from tranalyzer.ble.devices import BleakTrainer

    trainer = BleakTrainer(FakeBleakClient(fail_writes=True))
    asyncio.run(trainer.prepare())              # must not raise
    asyncio.run(trainer.async_set_target_power(200))
    trainer.set_target_power(150)               # sync path must not raise either


def test_finished_ride_saves_activity(user_id):
    trainer = SimulatedTrainer()
    c = RideController(
        _two_block_session(), 200, trainer=trainer, user_id=user_id, autosave=True,
    )
    for _ in range(20):
        c.tick(power=180, cadence=90, hr=150, dt=1)
    assert c.status == "finished"
    assert c.activity_id is not None
    acts = db.list_activities(user_id)
    assert len(acts) == 1
    assert acts[0]["filename"].startswith("Ride ")
    assert acts[0]["tss"] >= 0


def test_saved_ride_isolated_per_user(user_id):
    from tranalyzer import auth
    other = db.create_user("someone_else", auth.hash_password("password123"))
    c = RideController(
        _two_block_session(), 200, user_id=user_id, autosave=True,
    )
    for _ in range(20):
        c.tick(power=150, dt=1)
    assert len(db.list_activities(user_id)) == 1
    assert db.list_activities(other) == []
