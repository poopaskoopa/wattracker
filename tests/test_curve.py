"""Tests for the CP / W' model fit."""
import pytest

from tranalyzer.metrics import curve


def test_fit_recovers_planted_cp_wprime():
    cp_true = 250.0
    wprime_true = 20000.0
    durations = [60, 180, 300, 600, 1200]
    mmp = {t: cp_true + wprime_true / t for t in durations}
    cp, wprime = curve.fit_cp_wprime(mmp)
    assert cp == pytest.approx(cp_true, abs=1.0)
    assert wprime == pytest.approx(wprime_true, abs=50.0)


def test_fit_requires_two_points():
    with pytest.raises(ValueError):
        curve.fit_cp_wprime({300: 300.0})


def test_mean_maximal_power_picks_best():
    # One activity: 600s at 250W then 600s at 350W (1200 total).
    stream = [250.0] * 600 + [350.0] * 600
    mmp = curve.mean_maximal_power([stream], durations=[60, 300])
    # Best 60s window is entirely within the 350W block.
    assert mmp[60] == pytest.approx(350.0, abs=1e-6)


def test_default_duration_grid_is_dense():
    # The dashboard puts an axis tick (and a measured dot) at every sampled
    # duration; the default grid must cover short/medium/long durations.
    for d in (5, 10, 15, 30, 60, 300, 1200, 3600):
        assert d in curve.MMP_DURATIONS
    assert list(curve.MMP_DURATIONS) == sorted(curve.MMP_DURATIONS)


def test_mmp_has_a_point_at_every_grid_duration_with_data():
    # A 1200s stream yields a measured point at EVERY default grid duration
    # that fits within the stream (so every chart tick gets its dot).
    stream = [250.0] * 1200
    mmp = curve.mean_maximal_power([stream])
    expected = [d for d in curve.MMP_DURATIONS if d <= 1200]
    assert sorted(mmp.keys()) == expected
