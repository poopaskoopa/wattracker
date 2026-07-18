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
    # Semantics changed: instead of a hard cutoff, old efforts are detraining-
    # decayed. A recent 300W effort still beats a 400W effort from 100 days ago
    # once the latter is decayed (400 * factor(100) ~= 275 < 300), so the
    # estimate is 300 * 0.95.
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
    assert est == pytest.approx(285.0, abs=1e-6)


# ----------------------------------------------- detraining decay model
def test_detraining_factor_flat_through_grace():
    assert power.detraining_factor(0) == 1.0
    assert power.detraining_factor(5) == 1.0
    assert power.detraining_factor(power.FTP_DECAY_GRACE_DAYS) == 1.0


def test_detraining_factor_monotonically_decreasing_after_grace():
    vals = [power.detraining_factor(d) for d in range(11, 200)]
    assert all(b < a for a, b in zip(vals, vals[1:]))
    assert all(0.0 < v < 1.0 for v in vals)


def test_detraining_factor_about_0875_at_six_weeks():
    # ~12% loss after a six-week (42-day) break.
    assert power.detraining_factor(42) == pytest.approx(0.875, abs=0.01)


def test_estimate_ftp_decays_over_layoff_not_collapse():
    # Regression for the reported bug: a ~216W best-20 effort six weeks ago
    # (a ~205 baseline) followed by easy return rides must decay smoothly to
    # ~178-180, NOT collapse toward ~143 as the old hard window did.
    import datetime as dt

    now = dt.datetime(2026, 7, 1, 12, 0, 0)
    hard = {
        "start_time": (now - dt.timedelta(days=42)).isoformat(),
        "streams": {"power": [216.0] * 1200},
    }
    easy = [
        {
            "start_time": (now - dt.timedelta(days=d)).isoformat(),
            "streams": {"power": [150.0] * 1200},
        }
        for d in (1, 3, 5)
    ]
    est = power.estimate_ftp([hard] + easy, now=now)
    # 216 * factor(42) * 0.95 ~= 179.6; well above the collapsed ~143.
    assert est == pytest.approx(179.6, abs=1.5)
    assert est > 170.0


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
    assert est == pytest.approx(304.0, abs=1e-6)  # 320 * 0.95, decay-free (grace)
