"""Tests for late-ride five-minute power durability."""
import pytest

from wattracker.metrics import durability


def _ride(early_watts, late_watts, *, work_seconds=3750, late_seconds=300):
    return [early_watts] * work_seconds + [late_watts] * late_seconds


def test_strong_late_effort_retains_nearly_all_fresh_power():
    rides = [_ride(300.0, late) for late in (294.0, 296.0, 297.0)]
    result = durability.compute_durability(rides, weight_kg=75.0)
    assert result.retention_ratio == pytest.approx(297.0 / 300.0)
    assert result.fresh_5min_power == pytest.approx(300.0)
    assert result.late_5min_power == pytest.approx(297.0)
    assert result.qualifying_rides == 3


def test_fading_late_effort_has_low_retention():
    rides = [_ride(300.0, late) for late in (175.0, 180.0, 185.0)]
    result = durability.compute_durability(rides, weight_kg=75.0)
    assert result.retention_ratio == pytest.approx(185.0 / 300.0)
    assert result.retention_ratio < 0.7


def test_short_rides_are_excluded():
    rides = [_ride(300.0, 290.0) for _ in range(3)]
    rides.append([500.0] * 300)
    result = durability.compute_durability(rides, weight_kg=75.0)
    assert result.qualifying_rides == 3
    assert result.fresh_5min_power == pytest.approx(500.0)
    assert result.late_5min_power == pytest.approx(290.0)


def test_below_minimum_ride_count_has_no_retention():
    result = durability.compute_durability(
        [_ride(300.0, 280.0), _ride(300.0, 275.0)],
        weight_kg=75.0,
    )
    assert result.retention_ratio is None
    assert result.late_5min_power is None
    assert result.qualifying_rides == 2


@pytest.mark.parametrize(
    "activities",
    [
        [],
        [[None] * 5000],
        [["not-power"] * 5000],
        None,
        42,
    ],
)
def test_empty_none_and_nonnumeric_inputs_are_survived(activities):
    assert durability.compute_durability(activities) == durability.DurabilityResult()


def test_missing_weight_uses_absolute_kj_fallback():
    # 3,750 seconds at 300 W reaches the 1,125 kJ fallback exactly.
    rides = [_ride(300.0, 285.0) for _ in range(3)]
    result = durability.compute_durability(rides)
    assert result.retention_ratio == pytest.approx(285.0 / 300.0)
    assert result.qualifying_rides == 3


def test_no_data_returns_all_none():
    result = durability.compute_durability([])
    assert result.retention_ratio is None
    assert result.fresh_5min_power is None
    assert result.late_5min_power is None
    assert result.qualifying_rides is None
