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
