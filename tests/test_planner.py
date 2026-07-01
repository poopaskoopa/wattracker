"""Tests for the rule-based workout planner."""
import pytest

from tranalyzer.analysis.state import TrainingState
from tranalyzer.prescribe.planner import plan_workout


def _neutral(tsb=0.0):
    return TrainingState(ftp=250.0, tsb=tsb, plateau=False, overreach=False)


def test_duration_below_minimum_raises():
    with pytest.raises(ValueError):
        plan_workout(_neutral(), 20)


def test_duration_above_maximum_raises():
    with pytest.raises(ValueError):
        plan_workout(_neutral(), 481)


def test_duration_480_allowed():
    session = plan_workout(_neutral(), 480)
    assert session.total_duration() == 480 * 60


@pytest.mark.parametrize("minutes", [45, 90, 300])
def test_total_duration_matches(minutes):
    session = plan_workout(_neutral(), minutes)
    assert session.total_duration() == minutes * 60


def test_45min_fresh_is_sweet_spot():
    session = plan_workout(_neutral(tsb=0.0), 45)
    assert session.workout_type == "sweet_spot"
    interval = next(s for s in session.segments if s.kind == "intervals")
    assert 0.88 <= interval.on_power <= 0.94


def test_90min_fresh_is_sweet_spot():
    session = plan_workout(_neutral(tsb=0.0), 90)
    assert session.workout_type == "sweet_spot"


def test_300min_is_z2_endurance():
    session = plan_workout(_neutral(tsb=0.0), 300)
    assert session.workout_type == "endurance"
    steady = next(s for s in session.segments if s.kind == "steadystate")
    # Zone 2 is <= 75% FTP.
    assert steady.power <= 0.75


def test_overreach_prescribes_easy():
    state = TrainingState(ftp=250.0, tsb=-30.0, overreach=True)
    session = plan_workout(state, 60)
    assert session.workout_type == "recovery"
    # All targets stay in Z1-2 (<= 75% FTP).
    for s in session.segments:
        assert (s.avg_fraction()) <= 0.75


def test_plateau_prescribes_vo2max():
    state = TrainingState(ftp=250.0, tsb=0.0, plateau=True)
    session = plan_workout(state, 75)
    assert session.workout_type == "vo2max"
    interval = next(s for s in session.segments if s.kind == "intervals")
    assert 1.10 <= interval.on_power <= 1.15


def test_estimated_tss_positive():
    session = plan_workout(_neutral(), 60)
    assert session.estimated_tss > 0
