"""Tests for the CTL/ATL/TSB EWMA training-load model."""
import datetime as dt

import pytest

from wattracker.metrics import load


def test_daily_series_fills_gaps():
    d0 = dt.date(2026, 1, 1)
    d3 = dt.date(2026, 1, 4)
    series = load.daily_tss_series({d0: 50.0, d3: 80.0})
    # Four days total (Jan 1..4), gaps filled with 0.
    assert [tss for _, tss in series] == [50.0, 0.0, 0.0, 80.0]


def test_constant_tss_converges_to_that_value():
    # A long run of constant daily TSS: CTL and ATL both converge to it,
    # and TSB converges to 0.
    start = dt.date(2026, 1, 1)
    daily = 60.0
    series = [(start + dt.timedelta(days=i), daily) for i in range(400)]
    result = load.compute_load(series)
    last = result[-1]
    assert last["ctl"] == pytest.approx(daily, abs=0.5)
    assert last["atl"] == pytest.approx(daily, abs=0.5)
    assert last["tsb"] == pytest.approx(0.0, abs=0.5)


def test_ewma_first_day_seeded_from_zero():
    series = [(dt.date(2026, 1, 1), 42.0)]
    result = load.compute_load(series)
    # CTL[0] = 0*(1-1/42) + 42*(1/42) = 1.0
    assert result[0]["ctl"] == pytest.approx(1.0)
    # ATL[0] = 42 * (1/7) = 6.0
    assert result[0]["atl"] == pytest.approx(6.0)
    assert result[0]["tsb"] == pytest.approx(1.0 - 6.0)
