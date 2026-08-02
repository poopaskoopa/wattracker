"""Tests for NP / IF / TSS and the FTP estimator."""
import numpy as np
import pytest

from wattracker.metrics import power


def test_np_constant_equals_mean():
    # Constant power -> NP equals that power.
    stream = [200.0] * 3600
    assert power.normalized_power(stream) == pytest.approx(200.0, abs=1e-6)


def test_np_short_series_falls_back_to_mean():
    stream = [100.0, 200.0, 300.0]  # < 30 samples
    assert power.normalized_power(stream) == pytest.approx(200.0)


def test_np_hand_computed():
    # 60 samples: first 30 at 100W, next 30 at 300W.
    # 30s rolling means range from 100 -> 300 as the window slides.
    stream = [100.0] * 30 + [300.0] * 30
    roll = power.rolling_mean(stream, 30)
    expected = float(np.power(np.mean(np.power(roll, 4)), 0.25))
    assert power.normalized_power(stream) == pytest.approx(expected)


def test_if():
    assert power.intensity_factor(250.0, 250.0) == pytest.approx(1.0)
    assert power.intensity_factor(200.0, 250.0) == pytest.approx(0.8)


def test_one_hour_at_ftp_is_100_tss():
    ftp = 250.0
    stream = [ftp] * 3600  # 1 hour exactly at FTP
    tss = power.tss_from_stream(stream, ftp)
    assert tss == pytest.approx(100.0, abs=0.5)


def test_tss_formula_direct():
    # 30 min (1800s) at NP=FTP -> IF=1 -> TSS = 50.
    assert power.training_stress_score(1800, 250.0, 250.0) == pytest.approx(50.0)


def test_estimate_ftp_recovers_20min_effort():
    # A 20-min (1200s) effort at 300W -> FTP = 300 * 0.95 = 285.
    effort = [300.0] * 1200
    est = power.estimate_ftp([effort])
    assert est == pytest.approx(285.0, abs=1e-6)


@pytest.mark.parametrize("corrupt", [
    42,
    "300",
    b"300",
    bytearray(b"300"),
    {"sample": 300},
])
def test_estimate_ftp_skips_corrupt_dict_power_and_uses_valid_effort(corrupt):
    activities = [
        {"start_time": "2026-06-01T10:00:00", "streams": {"power": corrupt}},
        {
            "start_time": "2026-06-02T10:00:00",
            "streams": {"power": [300.0] * 1200},
        },
    ]
    assert power.estimate_ftp(activities) == pytest.approx(285.0)


def test_estimate_ftp_preserves_tuple_generator_and_numpy_raw_streams():
    assert power.estimate_ftp([(300.0,) * 1200]) == pytest.approx(285.0)
    assert power.estimate_ftp((iter([300.0] * 1200),)) == pytest.approx(285.0)
    assert power.estimate_ftp([np.full(1200, 300.0)]) == pytest.approx(285.0)


def test_estimate_ftp_override_wins():
    assert power.estimate_ftp([[300.0] * 1200], override=250.0) == 250.0


def test_estimate_ftp_recent_effort_dominates_decayed_old():
    # A recent 300W effort beats a 400W effort from 100 days ago once the latter
    # is detraining-decayed across the ~95-day idle gap between them.
    import datetime as dt

    now = dt.datetime(2026, 7, 1, 12, 0, 0)
    recent = {
        "start_time": (now - dt.timedelta(days=5)).isoformat(),
        "streams": {"power": [300.0] * 1200},
    }
    old = {
        "start_time": (now - dt.timedelta(days=100)).isoformat(),
        "streams": {"power": [400.0] * 1200},
    }
    est = power.estimate_ftp([recent, old], now=now)
    # Recent wins: 300 * factor(idle~0, active~5) * 0.95 ~= 284. The
    # recent-evidence floor sees the same effort but is decayed by the same
    # factor, so on a plain 20-minute effort it agrees exactly rather than
    # cancelling the decay.
    assert est == pytest.approx(284.0, abs=0.5)


# ----------------------------------------------- gap-aware detraining model
def test_idle_active_split_continuous_training_is_all_active():
    # An effort followed by riding every 5 days accrues no idle excess: the
    # whole span is "active", so decay stays minimal (this is the v1 bug case).
    import datetime as dt

    t = dt.datetime(2026, 1, 1, 10, 0)
    days = [t + dt.timedelta(days=5 * i) for i in range(8)]  # t .. t+35
    anchor = t + dt.timedelta(days=35)
    idle, active = power._idle_active_days(t, anchor, days)
    assert idle == pytest.approx(0.0, abs=1e-9)
    assert active == pytest.approx(35.0, abs=1e-9)


def test_idle_active_split_lone_effort_then_layoff():
    # A single effort with no rides until the anchor: the whole gap past the
    # grace window is idle excess.
    import datetime as dt

    t = dt.datetime(2026, 1, 1, 10, 0)
    anchor = t + dt.timedelta(days=40)
    idle, active = power._idle_active_days(t, anchor, [t])
    assert idle == pytest.approx(40 - power.FTP_DECAY_GRACE_DAYS, abs=1e-9)
    assert active == pytest.approx(power.FTP_DECAY_GRACE_DAYS, abs=1e-9)


def test_detraining_factor_no_decay_when_no_idle_no_active():
    assert power.detraining_factor(0.0, 0.0) == 1.0


def test_detraining_factor_active_days_decay_slowly():
    # 30 continuously-trained days barely touch the effort (~0.98).
    assert power.detraining_factor(0.0, 30.0) == pytest.approx(0.979, abs=0.005)


def test_detraining_factor_monotonic_in_both_terms():
    base = power.detraining_factor(20.0, 20.0)
    assert power.detraining_factor(40.0, 20.0) < base   # more idle -> lower
    assert power.detraining_factor(20.0, 40.0) < base   # more active -> lower
    assert power.detraining_factor(10.0, 20.0) > base   # less idle -> higher


def test_detraining_factor_about_088_before_six_week_stop():
    # Effort ridden immediately before a six-week full stop, evaluated at return:
    # idle excess ~28, active ~14 -> ~0.88.
    assert power.detraining_factor(28.0, 14.0) == pytest.approx(0.88, abs=0.01)


def test_estimate_ftp_decays_over_layoff_not_collapse():
    # Regression for the reported live-data bug: a ~216W best-20 effort at
    # the START of a six-week break, with regular training before it and easy
    # return rides after, must decay by ~0.87 (only the 44-day gap counts, NOT
    # the weeks of active training) -> ~179, NOT collapse to ~143/159.
    import datetime as dt

    now = dt.datetime(2026, 7, 1, 12, 0, 0)
    # Regular training every 4 days ending just before the break start.
    train = [
        {
            "start_time": (now - dt.timedelta(days=d)).isoformat(),
            "streams": {"power": [180.0] * 1200},
        }
        for d in range(93, 49, -4)  # 93, 89, ... 53
    ]
    hard = {
        "start_time": (now - dt.timedelta(days=49)).isoformat(),  # break start
        "streams": {"power": [216.0] * 1200},
    }
    easy = [
        {
            "start_time": (now - dt.timedelta(days=d)).isoformat(),
            "streams": {"power": [150.0] * 1200},
        }
        for d in (5, 3, 1)  # return rides after the 44-day gap
    ]
    est = power.estimate_ftp(train + [hard] + easy, now=now)
    # 216 * 0.95 = 205.2 baseline; * ~0.87 -> ~178.7.
    assert est == pytest.approx(178.7, abs=2.0)
    assert est > 170.0  # nowhere near the collapsed ~143/159


def test_estimate_ftp_continuous_training_barely_decays_old_effort():
    # The v1 bug: an effort ridden while the rider KEEPS training must not be
    # charged detraining. 250W effort 35 days ago, riding every 5 days since.
    import datetime as dt

    now = dt.datetime(2026, 7, 1, 12, 0, 0)
    acts = [
        {
            "start_time": (now - dt.timedelta(days=d)).isoformat(),
            "streams": {"power": ([250.0] if d == 35 else [180.0]) * 1200},
        }
        for d in range(35, -1, -5)  # 35, 30, ... 0
    ]
    est = power.estimate_ftp(acts, now=now)
    baseline = 250.0 * 0.95  # 237.5
    # Almost no decay: ~0.976 -> ~231.8, far above v1's effort-age ~214.
    assert est == pytest.approx(231.8, abs=1.5)
    assert est > 0.97 * baseline


def test_estimate_ftp_fresh_hard_effort_dominates_history():
    import datetime as dt

    now = dt.datetime(2026, 7, 1, 12, 0, 0)
    old = {
        "start_time": (now - dt.timedelta(days=200)).isoformat(),
        "streams": {"power": [250.0] * 1200},
    }
    fresh = {
        "start_time": (now - dt.timedelta(days=1)).isoformat(),
        "streams": {"power": [320.0] * 1200},
    }
    est = power.estimate_ftp([old, fresh], now=now)
    # 320 * factor(idle~0, active~1) * 0.95 ~= 303.8.
    assert est == pytest.approx(303.8, abs=0.5)


# ------------------------------------------------- recent-evidence FTP floor
# A rider who only does structured ERG work never rides a maximal 20 minutes,
# so the decayed best-20 estimate reads 15-20% low. The floor reads FTP off the
# long efforts they DO ride (>= 13min) instead.
import datetime as _dt  # noqa: E402


def _act(now, days_ago, watts, seconds, rpe=None):
    a = {
        "start_time": (now - _dt.timedelta(days=days_ago)).isoformat(),
        "streams": {"power": [float(watts)] * seconds},
    }
    if rpe is not None:
        a["rpe"] = rpe
    return a


def _decay(acts, now, days_ago):
    """Detraining factor for an effort ``days_ago`` old against ``acts``.

    The floor decays each contribution exactly as the estimator decays each
    effort, so every expectation below carries this term rather than pretending
    a recent effort is worth its raw watts forever.
    """
    _, calendar = power._split_activities(acts)
    idle, active = power._idle_active_days(
        now - _dt.timedelta(days=days_ago), now, calendar
    )
    return power.detraining_factor(idle, active)


def test_recent_effort_floor_maps_each_duration_to_its_ftp_fraction():
    now = _dt.datetime(2026, 8, 1, 12, 0, 0)
    for seconds, fraction in power.FTP_FLOOR_DURATIONS:
        acts = [_act(now, 3, 200.0, seconds)]
        floor = power.recent_effort_floor(acts, now)
        assert floor == pytest.approx(200.0 * fraction * _decay(acts, now, 3))


def test_recent_effort_floor_takes_the_best_across_durations_and_activities():
    now = _dt.datetime(2026, 8, 1, 12, 0, 0)
    acts = [
        _act(now, 10, 200.0, 780),    # -> 184.0 before decay
        _act(now, 4, 195.0, 1800),    # -> 191.1 before decay
    ]
    assert power.recent_effort_floor(acts, now) == pytest.approx(
        195.0 * 0.98 * _decay(acts, now, 4)
    )


def test_recent_effort_floor_ignores_efforts_shorter_than_thirteen_minutes():
    now = _dt.datetime(2026, 8, 1, 12, 0, 0)
    # A 5-minute maximal effort says nothing about the hour and must not floor.
    assert power.recent_effort_floor([_act(now, 1, 400.0, 300)], now) == 0.0


def test_recent_effort_floor_ignores_activities_outside_the_window():
    now = _dt.datetime(2026, 8, 1, 12, 0, 0)
    inside_acts = [_act(now, 41, 200.0, 1200)]
    outside_acts = [_act(now, 43, 200.0, 1200)]
    inside = power.recent_effort_floor(inside_acts, now)
    outside = power.recent_effort_floor(outside_acts, now)
    assert inside == pytest.approx(190.0 * _decay(inside_acts, now, 41))
    assert outside == 0.0
    # The window is a parameter, not a constant.
    assert power.recent_effort_floor(
        outside_acts, now, window_days=60
    ) == pytest.approx(190.0 * _decay(outside_acts, now, 43))


def test_recent_effort_floor_credits_a_submaximal_rpe():
    now = _dt.datetime(2026, 8, 1, 12, 0, 0)

    def floor(rpe):
        acts = [_act(now, 2, 200.0, 1200, rpe=rpe)]
        return power.recent_effort_floor(acts, now) / (
            200.0 * 0.95 * _decay(acts, now, 2)
        )

    # rpe 6 -> +5%, rpe 4 -> +10% (the cap), rpe 1 -> still the +10% cap.
    assert floor(6) == pytest.approx(1.05)
    assert floor(4) == pytest.approx(1.10)
    assert floor(1) == pytest.approx(1.10)


def test_recent_effort_floor_does_not_adjust_maximal_or_missing_rpe():
    now = _dt.datetime(2026, 8, 1, 12, 0, 0)
    for rpe in (None, 8, 9, 10):
        acts = [_act(now, 2, 200.0, 1200, rpe=rpe)]
        assert power.recent_effort_floor(acts, now) == pytest.approx(
            200.0 * 0.95 * _decay(acts, now, 2)
        )


def test_recent_effort_floor_decays_when_the_rider_stops_riding():
    """The floor must not cancel detraining inside its own window.

    Same 20-minute effort 40 days ago in both cases. The rider who kept riding
    every 5 days keeps essentially all of it - that is what the floor exists
    for. The rider who did the effort and then stopped loses real watts.
    """
    now = _dt.datetime(2026, 8, 1, 12, 0, 0)
    stopped = [_act(now, 40, 200.0, 1200)]
    kept = [_act(now, 40, 200.0, 1200)] + [
        _act(now, d, 120.0, 1200) for d in range(35, -1, -5)
    ]
    assert power.recent_effort_floor(stopped, now) < 0.92 * 190.0
    assert power.recent_effort_floor(kept, now) > 0.97 * 190.0


def test_recent_effort_floor_is_zero_without_now():
    now = _dt.datetime(2026, 8, 1, 12, 0, 0)
    assert power.recent_effort_floor([_act(now, 1, 300.0, 1200)], None) == 0.0


def test_estimate_ftp_with_now_none_ignores_the_floor():
    # The raw-stream contract is unchanged: no decay, no floor, just 0.95 x
    # best-20. A 13-minute effort at 300W would floor at 276 if it applied.
    now = _dt.datetime(2026, 8, 1, 12, 0, 0)
    acts = [_act(now, 1, 300.0, 780, rpe=5)]
    assert power.estimate_ftp(acts) == 0.0
    assert power.estimate_ftp(acts, now=None) == 0.0
    mixed = acts + [_act(now, 1, 200.0, 1200)]
    assert power.estimate_ftp(mixed) == pytest.approx(190.0)


def test_estimate_floored_by_recent_effort_inside_window():
    """The live case: structured ERG work only, no maximal 20 minutes.

    A rider whose best 13-minute power is 197.8W at RPE 6, with no maximal
    20-minute effort anywhere, read ~177W before the floor existed.
    """
    now = _dt.datetime(2026, 8, 1, 12, 0, 0)
    # An ERG session: 3 x 13min at ~198W with easy recoveries between them.
    stream = ([198.0] * 780 + [110.0] * 240) * 3
    acts = [{"start_time": (now - _dt.timedelta(days=1)).isoformat(),
             "rpe": 6,
             "streams": {"power": stream}}]
    decayed_only = power.estimate_ftp(acts, now=None)
    est = power.estimate_ftp(acts, now=now)
    # 197.8 * 0.92 * 1.05 = 191.0, well above the diluted best-20 estimate.
    assert est == pytest.approx(
        198.0 * 0.92 * 1.05 * _decay(acts, now, 1), abs=0.5
    )
    assert est > decayed_only * 1.05


def test_estimate_ftp_floor_never_lowers_the_estimate():
    now = _dt.datetime(2026, 8, 1, 12, 0, 0)
    # A genuine 20-minute test dominates; the floor agrees rather than cutting.
    acts = [_act(now, 2, 300.0, 1200), _act(now, 1, 120.0, 3600)]
    assert power.estimate_ftp(acts, now=now) == pytest.approx(
        285.0 * _decay(acts, now, 2), abs=0.5
    )


def test_estimate_ftp_accepts_a_generator_with_the_floor_active():
    now = _dt.datetime(2026, 8, 1, 12, 0, 0)
    acts = [_act(now, 2, 200.0, 1200)]
    assert power.estimate_ftp(iter(acts), now=now) == pytest.approx(
        190.0 * _decay(acts, now, 2)
    )


def test_split_activities_carries_rpe_through():
    now = _dt.datetime(2026, 8, 1, 12, 0, 0)
    efforts, _ = power._split_activities(
        [_act(now, 1, 200.0, 60, rpe=7), _act(now, 2, 200.0, 60), [1.0, 2.0]]
    )
    assert [e[2] for e in efforts] == [7, None, None]
