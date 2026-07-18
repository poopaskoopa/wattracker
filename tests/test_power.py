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


def test_estimate_ftp_windowed_activity_dicts():
    # Dated activity dict inside the window contributes; the estimate is best20*0.95.
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
    est = power.estimate_ftp([recent, old], window_days=42, now=now)
    # Old effort excluded; only the 300W recent effort counts.
    assert est == pytest.approx(285.0, abs=1e-6)
