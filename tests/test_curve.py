"""Tests for the CP / W' model fit."""
import pytest

from wattracker.metrics import curve


def test_fit_recovers_planted_cp_wprime():
    cp_true = 250.0
    wprime_true = 20000.0
    durations = [60, 180, 300, 600, 1200]
    mmp = {t: cp_true + wprime_true / t for t in durations}
    cp, wprime = curve.fit_cp_wprime(mmp)
    assert cp == pytest.approx(cp_true, abs=1.0)
    assert wprime == pytest.approx(wprime_true, abs=50.0)


def test_fit_requires_enough_points_inside_the_window():
    with pytest.raises(ValueError):
        curve.fit_cp_wprime({300: 300.0})
    # Two in-window points fit two parameters exactly - no residual degrees of
    # freedom, so a single noisy sample lands entirely in CP and W'.
    with pytest.raises(ValueError):
        curve.fit_cp_wprime({300: 300.0, 600: 260.0})
    # Three is enough.
    cp, wprime = curve.fit_cp_wprime({300: 300.0, 600: 260.0, 1200: 240.0})
    assert cp > 0 and wprime > 0


# --------------------------------------------- the CP model's valid domain
def test_fit_ignores_points_outside_the_valid_duration_window():
    # A clean planted hyperbola inside the window, plus sprint and multi-hour
    # points that the 2-parameter model does not describe. The fit must be
    # identical with and without them: they are dropped, not down-weighted.
    cp_true, wprime_true = 250.0, 20000.0
    inside = {t: cp_true + wprime_true / t for t in (120, 180, 300, 600, 1200)}
    polluted = dict(inside)
    polluted.update({1: 1200.0, 5: 1000.0, 30: 600.0, 3600: 210.0, 5400: 180.0})

    assert curve.fit_cp_wprime(polluted) == curve.fit_cp_wprime(inside)
    cp, wprime = curve.fit_cp_wprime(polluted)
    assert cp == pytest.approx(cp_true, abs=0.5)
    assert wprime == pytest.approx(wprime_true, abs=50.0)


def test_fit_window_is_the_classical_two_to_twenty_minutes():
    assert curve.CP_FIT_MIN_S == 120
    assert curve.CP_FIT_MAX_S == 1200
    # The window must not be a subset of the sampling grid by accident: the grid
    # has to offer enough points inside it to over-determine a 2-parameter fit.
    in_window = [d for d in curve.MMP_DURATIONS
                 if curve.CP_FIT_MIN_S <= d <= curve.CP_FIT_MAX_S]
    assert len(in_window) > curve.CP_FIT_MIN_POINTS


def test_fitted_cp_never_exceeds_the_riders_own_five_minute_power():
    # THE regression. CP is the asymptote of the power-duration curve: a rider
    # sustains it far longer than five minutes, so a CP at or above their best
    # 300s power is impossible by definition. The pre-fix code fitted all 17
    # grid durations and returned CP=321.8 W for a rider whose 5-minute power
    # was 241.3 W (and whose hour power was 169.7 W).
    real_mmp = {
        1: 953.0, 5: 910.2, 10: 850.9, 15: 786.9, 30: 492.7, 60: 311.3,
        120: 293.3, 180: 277.3, 300: 241.3, 600: 223.4, 900: 220.2,
        1200: 193.0, 1800: 183.6, 2400: 179.0, 2700: 178.0, 3600: 169.7,
        5400: 147.1,
    }
    try:
        cp, _wprime = curve.fit_cp_wprime(real_mmp)
    except ValueError:
        pass  # refusing to fit is also a valid answer - it stores no CP at all
    else:
        assert cp < real_mmp[300]


def test_fit_rejects_a_cp_at_or_above_the_longest_in_window_power():
    # CP is an asymptote, so P(t) = CP + W'/t must sit strictly ABOVE CP at
    # every measured duration. Data whose long point falls below what the short
    # points imply is not hyperbolic, and no CP should be stored for it: better
    # a blank profile than a number the rider's own 20-minute effort refutes.
    not_hyperbolic = {120: 293.3, 180: 277.3, 300: 241.3, 600: 223.4,
                      900: 220.2, 1200: 193.0}
    with pytest.raises(ValueError):
        curve.fit_cp_wprime(not_hyperbolic)


def test_pipeline_leaves_cp_unknown_rather_than_storing_an_impossible_fit():
    from wattracker.analysis.pipeline import _safe_cp

    assert _safe_cp({120: 293.3, 180: 277.3, 300: 241.3, 600: 223.4,
                     900: 220.2, 1200: 193.0}) == (None, None)
    assert _safe_cp({1: 953.0, 5: 910.2, 15: 786.9}) == (None, None)  # sprints only
    cp, wprime = _safe_cp({t: 250.0 + 20000.0 / t
                           for t in (120, 300, 600, 1200)})
    assert cp == pytest.approx(250.0, abs=0.5)
    assert wprime == pytest.approx(20000.0, abs=50.0)


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
