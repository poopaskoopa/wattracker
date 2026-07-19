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
    # Recent wins: 300 * factor(idle~0, active~5) * 0.95 ~= 284.
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
    # Regression for the reported live-data bug (rider): a ~216W best-20 effort at
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
