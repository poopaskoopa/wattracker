"""Tests for time-range filtering and the rolling estimated-FTP series."""
import datetime as dt

import pytest

from wattracker import auth, db
from wattracker.analysis import pipeline


def _insert(user_id, when: dt.datetime, watts=300.0, seconds=1200):
    db.init_db()
    db.insert_activity(
        user_id,
        {
            "dedup_hash": f"h-{user_id}-{when.isoformat()}-{watts}",
            "filename": "a.fit",
            "start_time": when.isoformat(),
            "duration_s": seconds,
            "distance_m": 0.0,
            "avg_power": watts,
            "avg_hr": 0.0,
            "np": watts,
            "if_": 1.0,
            "tss": 100.0,
            "streams": {"power": [watts] * seconds},
        },
    )


# ------------------------------------------------------- months filter
def test_filter_by_months_keeps_trailing_window():
    now = dt.datetime(2026, 7, 1, 12, 0)
    series = [{"date": (now - dt.timedelta(days=d)).date().isoformat()} for d in
              (0, 20, 40, 200)]
    kept = pipeline._filter_by_months(series, months=1, now=now)
    dates = {s["date"] for s in kept}
    # 1 month ~= 30.44 days: only the 0- and 20-day-old points survive.
    assert (now.date()).isoformat() in dates
    assert (now - dt.timedelta(days=20)).date().isoformat() in dates
    assert (now - dt.timedelta(days=40)).date().isoformat() not in dates
    assert (now - dt.timedelta(days=200)).date().isoformat() not in dates


def test_filter_by_months_none_returns_all():
    series = [{"date": "2020-01-01"}, {"date": "2026-01-01"}]
    assert pipeline._filter_by_months(series, months=None) == series
    assert pipeline._filter_by_months(series, months=0) == series


# --------------------------------------------------- rolling FTP series
def test_rolling_ftp_varies_and_decays_through_gap(user_id):
    base = dt.datetime(2026, 1, 1, 10, 0)
    _insert(user_id, base, watts=300.0)                       # 20 min @ 300W
    _insert(user_id, base + dt.timedelta(days=60), watts=340.0)  # later, stronger

    now = base + dt.timedelta(days=60)
    result = pipeline.ftp_rolling_series(
        user_id, step_days=7, now=now
    )
    est = result["estimated"]
    values = [p["ftp"] for p in est]

    assert result["recorded"] == []  # no manual/monthly rows
    # First sample sees the fresh 300W effort -> 285.
    assert values[0] == pytest.approx(285.0, abs=0.5)
    # Semantics changed: no hard window/cliff. Through the 60-day gap (all of it
    # idle, since there are no rides between the two efforts) the 300W effort
    # decays smoothly with no None gaps, so the minimum is BELOW 285 rather than
    # pinned at it. Last pre-jump sample (day56): ~237.
    assert min(values) < 285.0
    assert min(values) == pytest.approx(236.9, abs=1.5)
    # The samples strictly decay until the stronger effort lands at the end.
    gap_vals = values[:-1]
    assert all(b <= a + 1e-6 for a, b in zip(gap_vals, gap_vals[1:]))
    # Final sample (at `now`) reflects the recent stronger 340W effort -> 323.
    assert max(values) == pytest.approx(323.0, abs=0.5)
    assert est[-1]["ftp"] == pytest.approx(323.0, abs=0.5)


def test_rolling_ftp_window_shrinks_series_with_months(user_id):
    base = dt.datetime(2026, 1, 1, 10, 0)
    _insert(user_id, base, watts=300.0)
    _insert(user_id, base + dt.timedelta(days=60), watts=340.0)
    now = base + dt.timedelta(days=60)

    full = pipeline.ftp_rolling_series(user_id, window_days=42, step_days=7, now=now)
    windowed = pipeline.ftp_rolling_series(
        user_id, months=1, window_days=42, step_days=7, now=now
    )
    assert len(windowed["estimated"]) < len(full["estimated"])
    cutoff = (now - dt.timedelta(days=int(round(30.44)))).date().isoformat()
    assert all(p["date"] >= cutoff for p in windowed["estimated"])


def test_rolling_ftp_empty_user_no_error():
    db.init_db()
    uid = db.create_user("nobody", auth.hash_password("password123"))
    result = pipeline.ftp_rolling_series(uid)
    assert result == {"estimated": [], "recorded": []}


def test_rolling_ftp_includes_recorded_points(user_id):
    base = dt.datetime(2026, 1, 1, 10, 0)
    _insert(user_id, base, watts=300.0)
    db.add_ftp_entry(user_id, base.date().isoformat(), 305.0, "manual")
    result = pipeline.ftp_rolling_series(
        user_id, now=base + dt.timedelta(days=1)
    )
    assert len(result["recorded"]) == 1
    assert result["recorded"][0]["ftp_watts"] == pytest.approx(305.0)
