"""Tests for the per-user activity digest cache and its invalidation.

The dashboard pipeline memoizes expensive stream-derived data (per-effort best
20-min power, the activity calendar, decoupling) keyed by a cheap fingerprint of
the user's activity set. These tests pin the invalidation contract: the cache
must never serve stale results after activities change, and must reuse the
digest when they do not.
"""
import datetime as dt

import pytest

from wattracker import db
from wattracker.analysis import activity_cache, pipeline


def _insert(user_id, start_time, power=300.0, seconds=1500):
    db.init_db()
    db.insert_activity(
        user_id,
        {
            "dedup_hash": f"h-{start_time}-{power}",
            "filename": "a.fit",
            "start_time": start_time,
            "duration_s": seconds,
            "distance_m": 0.0,
            "avg_power": power,
            "avg_hr": 150.0,
            "np": power,
            "if_": 1.0,
            "tss": 100.0,
            "streams": {
                "power": [power] * seconds,
                "heartrate": [150.0] * seconds,
            },
        },
    )


@pytest.fixture(autouse=True)
def _clear_cache():
    activity_cache.invalidate()
    yield
    activity_cache.invalidate()


def test_digest_reused_when_activities_unchanged(user_id):
    _insert(user_id, "2026-01-01T10:00:00")
    d1 = activity_cache.get_digest(user_id)
    d2 = activity_cache.get_digest(user_id)
    assert d1 is d2  # same fingerprint -> same cached object, no rebuild


def test_digest_rebuilds_after_new_activity(user_id):
    _insert(user_id, "2026-01-01T10:00:00", power=300.0)
    d1 = activity_cache.get_digest(user_id)
    assert len(d1.effort_days) == 1

    _insert(user_id, "2026-02-01T10:00:00", power=320.0)
    d2 = activity_cache.get_digest(user_id)
    assert d2 is not d1  # count/max-id changed -> fingerprint miss -> rebuilt
    assert len(d2.effort_days) == 2


def test_explicit_invalidate_forces_rebuild(user_id):
    _insert(user_id, "2026-01-01T10:00:00")
    d1 = activity_cache.get_digest(user_id)
    activity_cache.invalidate(user_id)
    d2 = activity_cache.get_digest(user_id)
    assert d2 is not d1
    assert d2.effort_days == d1.effort_days  # same inputs -> same content


def test_ftp_series_not_stale_after_import(user_id):
    # A high early effort, then a new higher one imported later. The rolling
    # series must reflect the new effort (i.e. the cache invalidated).
    _insert(user_id, "2026-01-01T10:00:00", power=250.0)
    s1 = pipeline.ftp_rolling_series(user_id, now=dt.datetime(2026, 3, 1, 12, 0))
    top1 = max(p["ftp"] for p in s1["estimated"])

    _insert(user_id, "2026-02-15T10:00:00", power=400.0)
    s2 = pipeline.ftp_rolling_series(user_id, now=dt.datetime(2026, 3, 1, 12, 0))
    top2 = max(p["ftp"] for p in s2["estimated"])
    assert top2 > top1


def test_ftp_series_matches_unoptimized_reference(user_id):
    """The prefix-sum estimate must equal the O(effort*calendar) reference."""
    from wattracker.metrics.power import (
        best_20min_power,
        detraining_factor,
        _idle_active_days,
    )
    from wattracker.timeutil import parse_naive

    days = [
        ("2025-06-01T09:00:00", 280.0),
        ("2025-06-20T09:00:00", 300.0),
        ("2025-09-05T09:00:00", 260.0),  # a long gap in between
        ("2026-01-10T09:00:00", 330.0),
        ("2026-01-12T09:00:00", 0.0),    # a power-less ride (calendar only)
    ]
    for t, p in days:
        _insert(user_id, t, power=p, seconds=1500 if p > 0 else 600)

    now = dt.datetime(2026, 2, 1, 12, 0)
    got = pipeline.ftp_rolling_series(user_id, now=now)["estimated"]

    # Reference: replicate the original brute-force computation.
    acts = db.full_activities(user_id)
    dated = []
    cal = []
    for a in acts:
        w = parse_naive(a["start_time"])
        cal.append(w)
        pw = (a.get("streams") or {}).get("power") or []
        if pw:
            b = best_20min_power(pw)
            if b > 0:
                dated.append((w, b))
    cal.sort()
    start = min(d for d, _ in dated)
    last = max(d for d, _ in dated)
    end = now if now > last else last
    step = dt.timedelta(days=7)

    def est(anchor):
        best = 0.0
        for w, b in dated:
            if w > anchor:
                continue
            idle, active = _idle_active_days(w, anchor, cal)
            best = max(best, b * detraining_factor(idle, active))
        return round(best * 0.95, 1) if best > 0 else None

    ref = []
    cur = start
    while cur <= end:
        e = est(cur)
        if e is not None:
            ref.append({"date": cur.date().isoformat(), "ftp": e})
        cur += step
    end_iso = end.date().isoformat()
    if not ref or ref[-1]["date"] != end_iso:
        e = est(end)
        if e is not None:
            ref.append({"date": end_iso, "ftp": e})

    assert got == ref
