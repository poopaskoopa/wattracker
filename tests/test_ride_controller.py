"""Tests for the RideController state machine using simulated devices."""
import datetime as dt

import pytest

from wattracker import db
from wattracker.ble.devices import (
    SimulatedHeartRateSource,
    SimulatedPowerSource,
    SimulatedTrainer,
)
from wattracker.ble.runner import RideController
from wattracker.prescribe.planner import Segment, Session


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


def test_requires_three_continuous_power_seconds_to_start_and_sets_erg_target():
    trainer = SimulatedTrainer()
    c = RideController(_two_block_session(), 200, trainer=trainer, autosave=False)
    assert c.tick(power=120)["status"] == "starting"
    assert c.state()["start_countdown"] == 2
    assert c.tick(power=120)["status"] == "starting"
    assert c.state()["start_countdown"] == 1
    st = c.tick(power=120)
    assert st["status"] == "running"
    assert c.elapsed == 0.0
    assert c._samples["power"] == []
    # The initial target is armed at workout time zero.
    assert trainer.last_target == 100
    c.tick(power=120)
    assert c.elapsed == 1.0
    assert c._samples["power"] == [120]


def test_start_countdown_resets_when_positive_power_is_not_continuous():
    c = RideController(_two_block_session(), 200, autosave=False)
    assert c.tick(power=100)["start_countdown"] == 2
    assert c.tick(power=100)["start_countdown"] == 1
    assert c.tick(power=0)["status"] == "idle"
    assert c.tick(power=100)["start_countdown"] == 2
    assert c.has_started is False


def test_stopping_during_start_countdown_does_not_save(user_id):
    trainer = SimulatedTrainer()
    c = RideController(
        _two_block_session(), 200, trainer=trainer, user_id=user_id, autosave=True
    )
    c.tick(power=100)
    c.tick(power=100)
    assert c.status == "starting"
    c.stop()

    assert c.status == "finished"
    assert db.list_activities(user_id) == []
    assert trainer.commands[-1] == "stop"


def test_completing_start_gate_without_riding_does_not_save(user_id):
    trainer = SimulatedTrainer()
    c = RideController(
        _two_block_session(), 200, trainer=trainer, user_id=user_id, autosave=True
    )
    c.tick(power=100)
    c.tick(power=100)
    assert c.tick(power=100)["status"] == "running"  # start gate completed
    assert c.elapsed == 0.0
    assert c.has_started is False  # no ride time accumulated yet
    c.tick(power=0)
    c.stop()

    assert c.status == "finished"
    assert c.activity_id is None
    assert db.list_activities(user_id) == []


def test_erg_target_follows_each_segment():
    trainer = SimulatedTrainer()
    c = RideController(_two_block_session(), 200, trainer=trainer, start_grace_s=0, autosave=False)
    for _ in range(20):
        c.tick(power=150, dt=1)
    # Block 0 (50% -> 100W) then block 1 (100% -> 200W) must both appear.
    assert 100 in trainer.targets
    assert 200 in trainer.targets
    assert c.status == "cooldown"  # 20s session complete, rider still spinning
    for _ in range(3):
        c.tick(power=0, dt=1)
    assert c.status == "finished"


def test_auto_pause_then_resume():
    c = RideController(_two_block_session(), 200, trainer=SimulatedTrainer(), start_grace_s=0, autosave=False)
    c.tick(power=100)            # running, elapsed 1
    assert c.status == "running"
    c.tick(power=0)
    c.tick(power=0)
    assert c.status == "running"  # brief dropouts do not pause
    c.tick(power=0)             # pause after 3 continuous seconds
    assert c.status == "paused"
    assert c.elapsed == 1.0     # clock did not advance
    c.tick(power=100)           # resume
    assert c.status == "running"
    assert c.elapsed == 2.0


def test_pause_after_zero_power_grace_does_not_auto_finish():
    c = RideController(
        _two_block_session(), 200, trainer=SimulatedTrainer(),
        start_grace_s=0, zero_grace_s=3, autosave=False,
    )
    c.tick(power=100)  # start
    c.tick(power=0)    # zero_run 1
    c.tick(power=0)    # zero_run 2
    assert c.status == "running"
    c.tick(power=0)    # zero_run 3 >= grace -> pause
    assert c.status == "paused"
    for _ in range(20):
        c.tick(power=0)
    assert c.status == "paused"


def test_zero_before_start_never_auto_stops():
    c = RideController(
        _two_block_session(), 200, zero_grace_s=1, autosave=False,
    )
    for _ in range(10):
        c.tick(power=0)
    assert c.status == "idle"  # still waiting to start, not finished


def test_finish_sets_trainer_to_zero():
    trainer = SimulatedTrainer()
    c = RideController(_two_block_session(), 200, trainer=trainer, start_grace_s=0, autosave=False)
    for _ in range(20):
        c.tick(power=150)
    for _ in range(3):  # spin down out of the cooldown
        c.tick(power=0)
    assert c.status == "finished"
    assert trainer.targets[-1] == 0  # ERG released on finish


def test_poll_drives_from_simulated_sources():
    # power script: idle, idle, pedal x3, then zeros to pause (grace 2)
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
    assert c.status == "paused"           # zeros after start -> pause, not finish


def test_poll_uses_standalone_cadence_when_power_source_has_none():
    class CadenceOnly:
        def __init__(self, cadence):
            self.cadence = cadence

        def latest_cadence(self):
            return self.cadence

    power = SimulatedPowerSource([120], cadences=[None])
    c = RideController(
        _two_block_session(), 200, power_source=power,
        cadence_source=CadenceOnly(94), start_grace_s=0, autosave=False,
    )

    assert c.poll(dt=1)["cadence"] == 94

    power = SimulatedPowerSource([120], cadences=[82])
    c = RideController(
        _two_block_session(), 200, power_source=power,
        cadence_source=CadenceOnly(94), start_grace_s=0, autosave=False,
    )
    assert c.poll(dt=1)["cadence"] == 82


def test_erg_rearmed_on_resume_and_stopped_on_finish():
    trainer = SimulatedTrainer()
    c = RideController(_two_block_session(), 200, trainer=trainer, start_grace_s=0, autosave=False)
    c.tick(power=0)  # idle: no ERG commands yet
    assert trainer.commands == []
    c.tick(power=120)  # ride start -> Request Control + Start
    assert trainer.commands == ["request_control", "start"]
    for _ in range(3):
        c.tick(power=0)  # pause -> trainer may have dropped ERG
    c.tick(power=120)  # resume MUST re-arm ERG (Request Control + Start again)
    assert trainer.commands == ["request_control", "start", "request_control", "start"]
    for _ in range(20):
        c.tick(power=120)
    for _ in range(3):
        c.tick(power=0)
    assert c.status == "finished"
    assert trainer.commands[-1] == "stop"
    assert trainer.targets[-1] == 0  # ERG target zeroed before stop


def test_erg_reengaged_after_pause_regression():
    """Regression: stopping mid-ride drops the trainer out of ERG; on resume the
    controller must re-issue Request Control + Start so ERG is re-engaged rather
    than the trainer free-riding below target (observed 2026-07-21 .fit)."""
    trainer = SimulatedTrainer()
    c = RideController(_two_block_session(), 200, trainer=trainer, start_grace_s=0, autosave=False)
    c.tick(power=120)                      # start
    for _ in range(30):                    # long stop: rider off the pedals
        if c.status == "finished":
            break
        c.tick(power=0, dt=0.1)            # small dt so grace isn't tripped
    c.tick(power=120)                      # resume
    # ERG was re-armed on resume, not just re-targeted.
    assert trainer.commands.count("request_control") == 2
    assert trainer.commands.count("start") == 2


def test_erg_target_follows_ramp_each_second():
    # 10s warmup ramp 50% -> 100% at FTP 200: target steps up every tick.
    session = Session(
        name="Ramp", description="", workout_type="custom",
        segments=[Segment(kind="warmup", duration=10, power_low=0.5, power_high=1.0)],
    )
    trainer = SimulatedTrainer()
    c = RideController(session, 200, trainer=trainer, start_grace_s=0, autosave=False)
    for _ in range(10):
        c.tick(power=150, dt=1)
    for _ in range(3):  # cooldown sends no new targets, only the finish zero
        c.tick(power=0, dt=1)
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
        _two_block_session(), 200, trainer=FailingTrainer(),
        start_grace_s=0, autosave=False,
    )
    for _ in range(20):
        c.tick(power=150)
    for _ in range(3):
        c.tick(power=0)
    assert c.status == "finished"  # trainer errors never crash the ride


def test_no_trainer_power_display_still_works():
    c = RideController(_two_block_session(), 200, trainer=None, start_grace_s=0, autosave=False)
    st = c.tick(power=180)
    assert st["status"] == "running"
    assert st["power"] == 180
    assert st["target_watts"] == 100  # target still computed for display


def test_erg_toggle_suppresses_targets_then_releases_and_rearms():
    trainer = SimulatedTrainer()
    c = RideController(
        _two_block_session(), 200, trainer=trainer,
        start_grace_s=0, autosave=False,
    )
    c.tick(power=150)
    targets_before_disable = list(trainer.targets)

    assert c.set_erg_enabled(False) is False
    assert trainer.targets[-1] == 0
    assert trainer.commands[-1] == "stop"
    c.tick(power=150)
    assert trainer.targets == targets_before_disable + [0]
    assert c.state()["target_watts"] == 100
    assert c.state()["erg_available"] is True
    assert c.state()["erg_enabled"] is False

    assert c.set_erg_enabled(True) is True
    assert trainer.commands[-2:] == ["request_control", "start"]
    assert trainer.targets[-1] == 100
    assert c.state()["erg_enabled"] is True


def test_erg_toggle_is_unavailable_without_trainer_but_target_remains_visible():
    c = RideController(
        _two_block_session(), 200, trainer=None,
        start_grace_s=0, autosave=False,
    )
    c.tick(power=150)
    assert c.set_erg_enabled(True) is False
    assert c.state()["erg_available"] is False
    assert c.state()["erg_enabled"] is False
    assert c.state()["target_watts"] == 100


def test_prearmed_erg_sets_initial_target_without_duplicate_start():
    trainer = SimulatedTrainer()
    trainer.start_erg()
    c = RideController(
        _two_block_session(), 200, trainer=trainer, autosave=False
    )
    c.current_target = c.target_watts(0)
    trainer.set_target_power(c.current_target)
    for _ in range(3):
        c.tick(power=120)

    assert trainer.commands == ["request_control", "start"]
    assert trainer.targets[0] == 100
    assert trainer.targets[-1] == 100
    assert c.elapsed == 0
    assert c.has_started is False


def test_server_managed_controller_never_schedules_trainer_commands():
    trainer = SimulatedTrainer()
    c = RideController(
        _two_block_session(),
        200,
        trainer=trainer,
        start_grace_s=0,
        autosave=False,
        manage_trainer_commands=False,
    )
    state = c.tick(power=150)

    assert state["status"] == "running"
    assert state["target_watts"] == 100
    assert trainer.commands == []
    assert trainer.targets == []


# ------------------------------------------- BleakTrainer over a fake client
class FakeBleakClient:
    """Records GATT procedures and emits configurable FTMS indications."""

    def __init__(self, fail_writes=False, results=None, drop_ops=None):
        self.writes = []
        self.notify_subs = []
        self.fail_writes = fail_writes
        self.results = results or {}
        self.drop_ops = set(drop_ops or [])
        self._callback = None

    async def write_gatt_char(self, char, data, response=False):
        if self.fail_writes:
            raise RuntimeError("device gone")
        self.writes.append((char, bytes(data)))
        op = data[0]
        if op not in self.drop_ops:
            self._callback(
                char, bytearray([0x80, op, self.results.get(op, 0x01)])
            )

    async def start_notify(self, char, callback):
        self.notify_subs.append((char, callback))
        self._callback = callback


def test_bleak_trainer_sends_erg_command_bytes():
    import asyncio

    from wattracker.ble.devices import BleakTrainer
    from wattracker.ble.protocol import FITNESS_MACHINE_CONTROL_POINT

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
    from wattracker.ble.devices import BleakTrainer

    client = FakeBleakClient()
    trainer = BleakTrainer(client)
    trainer.start_erg()             # no running loop -> executed to completion
    trainer.set_target_power(180)
    trainer.stop_erg()
    payloads = [d for _, d in client.writes]
    assert bytes([0x05, 0xB4, 0x00]) in payloads  # 180W target
    assert payloads[0] == bytes([0x00]) and payloads[-1] == bytes([0x08, 0x01])


def test_bleak_trainer_handles_indication_responses():
    from wattracker.ble.devices import BleakTrainer

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


def test_bleak_trainer_write_failure_is_surfaced():
    import asyncio

    from wattracker.ble.devices import BleakTrainer

    trainer = BleakTrainer(FakeBleakClient(fail_writes=True))
    with pytest.raises(RuntimeError, match="device gone"):
        asyncio.run(trainer.prepare())
    assert trainer.erg_available is False
    assert "device gone" in trainer.last_error


def test_bleak_trainer_serializes_procedures_and_subscribes_once():
    import asyncio

    from wattracker.ble.devices import BleakTrainer

    async def exercise():
        client = FakeBleakClient()
        trainer = BleakTrainer(client)
        await asyncio.gather(
            trainer.async_enable_erg(210),
            trainer.async_set_target_power(220),
        )
        return client, trainer

    client, trainer = asyncio.run(exercise())
    assert [payload[0] for _, payload in client.writes] == [0x00, 0x07, 0x05, 0x05]
    assert len(client.notify_subs) == 1
    assert trainer.erg_available is True
    assert trainer.erg_enabled is True


def test_bleak_trainer_rejection_and_timeout_are_surfaced():
    import asyncio

    from wattracker.ble.devices import BleakTrainer

    rejected = BleakTrainer(FakeBleakClient(results={0x00: 0x05}))
    with pytest.raises(RuntimeError, match="control not permitted"):
        asyncio.run(rejected.prepare())
    assert "control not permitted" in rejected.last_error

    timed_out = BleakTrainer(
        FakeBleakClient(drop_ops={0x00}), response_timeout_s=0.01
    )
    with pytest.raises(TimeoutError, match="timed out"):
        asyncio.run(timed_out.prepare())
    assert "timed out" in timed_out.last_error

    target_rejected = BleakTrainer(FakeBleakClient(results={0x05: 0x04}))
    with pytest.raises(RuntimeError, match="operation failed"):
        asyncio.run(target_rejected.async_enable_erg(200))
    assert target_rejected.erg_available is True
    assert target_rejected.erg_enabled is False


def test_finished_ride_saves_activity(user_id):
    trainer = SimulatedTrainer()
    c = RideController(
        _two_block_session(), 200, trainer=trainer, user_id=user_id,
        start_grace_s=0, autosave=True,
    )
    for _ in range(20):
        c.tick(power=180, cadence=90, hr=150, dt=1)
    for _ in range(3):
        c.tick(power=0, dt=1)
    assert c.status == "finished"
    assert c.activity_id is not None
    acts = db.list_activities(user_id)
    assert len(acts) == 1
    assert acts[0]["filename"].startswith("Ride ")
    assert acts[0]["tss"] >= 0


def test_finished_selected_ride_links_saved_activity_to_plan_workout(user_id):
    started = dt.datetime(2026, 7, 10, 9, 0, 0)
    plan_id = db.create_plan(user_id, "Selected ride", "2026-07-10", 1)
    workout_id = db.add_plan_workout(
        plan_id,
        user_id,
        "2026-07-10",
        "Selected workout",
        "endurance",
        20,
        1.0,
        "<workout_file/>",
    )
    c = RideController(
        _two_block_session(),
        200,
        user_id=user_id,
        workout_id=workout_id,
        started_at=started,
        start_grace_s=0,
        autosave=True,
    )

    for _ in range(20):
        c.tick(power=150, dt=1)
    for _ in range(3):
        c.tick(power=0, dt=1)

    assert c.status == "finished"
    assert c.activity_id is not None
    workout = db.get_plan_workout(user_id, workout_id)
    assert workout["completed_activity_id"] == c.activity_id
    assert workout["completed_date"] == "2026-07-10"


def test_unsaved_selected_ride_does_not_complete_plan_workout(user_id):
    plan_id = db.create_plan(user_id, "Not started", "2026-07-10", 1)
    workout_id = db.add_plan_workout(
        plan_id, user_id, "2026-07-10", "W", "endurance", 20, 1.0, "<x/>"
    )
    c = RideController(
        _two_block_session(),
        200,
        user_id=user_id,
        workout_id=workout_id,
        autosave=True,
    )

    c.stop()

    assert c.activity_id is None
    assert db.get_plan_workout(user_id, workout_id)["completed_activity_id"] is None


def test_saved_ride_isolated_per_user(user_id):
    from wattracker import auth
    other = db.create_user("someone_else", auth.hash_password("password123"))
    c = RideController(
        _two_block_session(), 200, user_id=user_id, start_grace_s=0, autosave=True,
    )
    for _ in range(20):
        c.tick(power=150, dt=1)
    for _ in range(3):
        c.tick(power=0, dt=1)
    assert len(db.list_activities(user_id)) == 1
    assert db.list_activities(other) == []


# ------------------------------------------------------- cooldown (spin-down)
def _ridden_to_cooldown(user_id=None, trainer=None, autosave=False):
    """A controller that pedalled the 20s prescription and is now spinning down."""
    c = RideController(
        _two_block_session(), 200, trainer=trainer, user_id=user_id,
        start_grace_s=0, autosave=autosave,
    )
    for _ in range(20):
        c.tick(power=150, dt=1)
    assert c.status == "cooldown"
    return c


def test_workout_end_enters_cooldown_without_saving(user_id):
    trainer = SimulatedTrainer()
    c = _ridden_to_cooldown(user_id=user_id, trainer=trainer, autosave=True)
    assert c.elapsed == 20.0
    assert c.activity_id is None
    assert db.list_activities(user_id) == []
    assert trainer.commands[-1] != "stop"  # ERG still engaged for the spin-down


def test_cooldown_pedalling_extends_elapsed_and_records_samples():
    trainer = SimulatedTrainer()
    c = _ridden_to_cooldown(trainer=trainer)
    for _ in range(5):
        c.tick(power=90, cadence=70, hr=130, dt=1)
    assert c.status == "cooldown"
    assert c.elapsed == 25.0
    assert len(c._samples["power"]) == 25
    assert c._samples["power"][-5:] == [90] * 5
    # The final prescribed target is held; no cooldown target is invented.
    assert trainer.targets[-1] == 200
    assert c.state()["progress"] == 1.0


def test_cooldown_finishes_after_three_zero_power_seconds(user_id):
    trainer = SimulatedTrainer()
    c = _ridden_to_cooldown(user_id=user_id, trainer=trainer, autosave=True)
    c.tick(power=0, dt=1)
    c.tick(power=0, dt=1)
    assert c.status == "cooldown"
    assert c.state()["finish_countdown"] == 1.0
    c.tick(power=0, dt=1)
    assert c.status == "finished"
    assert trainer.targets[-1] == 0
    assert trainer.commands[-1] == "stop"
    assert c.activity_id is not None
    assert len(db.list_activities(user_id)) == 1


def test_short_zero_gap_in_cooldown_does_not_finish(user_id):
    c = _ridden_to_cooldown(user_id=user_id, autosave=True)
    c.tick(power=0, dt=1)
    c.tick(power=0, dt=1)
    assert c.status == "cooldown"
    c.tick(power=80, dt=1)  # back on the pedals: the grace restarts
    assert c.status == "cooldown"
    assert c.state()["no_power_s"] == 0.0
    c.tick(power=0, dt=1)
    c.tick(power=0, dt=1)
    assert c.status == "cooldown"
    assert db.list_activities(user_id) == []
    assert c.elapsed == 21.0


def test_stop_during_cooldown_saves_the_ride_including_the_spin_down(user_id):
    c = _ridden_to_cooldown(user_id=user_id, autosave=True)
    for _ in range(30):
        c.tick(power=100, dt=1)
    c.stop()

    assert c.status == "finished"
    acts = db.list_activities(user_id)
    assert len(acts) == 1
    assert acts[0]["duration_s"] == 50  # 20s prescribed + 30s cooldown
    assert len(c.saved_record["streams"]["power"]) == 50


def test_cooldown_never_returns_to_running_or_paused():
    c = _ridden_to_cooldown()
    for _ in range(2):
        c.tick(power=0, dt=1)  # under the grace: still cooling down
    c.tick(power=120, dt=1)
    assert c.status == "cooldown"
    c.tick(power=120, dt=1)
    assert c.status == "cooldown"
