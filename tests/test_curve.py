"""Tests for the CP / W' model fit and dashboard curve payload."""
import datetime as dt
import sqlite3

import pytest

from wattracker import db
from wattracker.analysis import pipeline
from wattracker.metrics import curve
from wattracker.metrics import curve_store
from wattracker.timeutil import utc_now


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


def test_curve_payload_keeps_90_day_measured_and_adds_effective_variants(user_id):
    def add(key, when, watts):
        return db.insert_activity(user_id, {
            "dedup_hash": key, "filename": f"{key}.fit", "start_time": when.isoformat(),
            "duration_s": 60, "avg_power": watts, "np": watts, "if_": 1.0,
            "tss": 10.0, "streams": {"power": [watts] * 60},
        })

    now = utc_now()
    add("old", now - dt.timedelta(days=91), 400.0)
    usable = add("usable", now - dt.timedelta(days=10), 200.0)
    corrected = add("corrected", now - dt.timedelta(days=2), 500.0)
    duplicate = add("duplicate", now - dt.timedelta(days=1), 900.0)
    assert db.set_duplicate_of(user_id, duplicate, usable)
    assert db.apply_power_correction(
        user_id, corrected, 0, 59, 250.0, None,
        {"avg_power": None, "np": None, "if_": None, "tss": 0.0},
    ) is not None

    payload = pipeline.curve_points(user_id)

    assert payload["measured"] == [{"t": 1, "power": 200.0}, {"t": 5, "power": 200.0},
                                   {"t": 10, "power": 200.0}, {"t": 15, "power": 200.0},
                                   {"t": 30, "power": 200.0}, {"t": 60, "power": 200.0}]
    assert payload["all_time"][0] == {"t": 1, "power": 400.0}
    assert payload["last_ride"] == payload["measured"]


def _add_curve_activity(user_id, key, when, watts, seconds=60):
    return db.insert_activity(user_id, {
        "dedup_hash": key, "filename": f"{key}.fit", "start_time": when.isoformat(),
        "duration_s": seconds, "avg_power": watts, "np": watts, "if_": 1.0,
        "tss": 10.0, "streams": {"power": [watts] * seconds},
    })


def test_all_time_curve_rebuilds_once_then_reuses_persisted_cache(user_id, monkeypatch):
    _add_curve_activity(user_id, "cache", utc_now(), 250.0)
    conn = db.connect()
    try:
        conn.execute("DELETE FROM curve_cache WHERE user_id = ?", (user_id,))
        conn.commit()
    finally:
        conn.close()

    assert curve_store.all_time(user_id)[60] == 250.0
    monkeypatch.setattr(
        curve_store, "_rebuild_locked",
        lambda *_args: pytest.fail("clean cache rebuilt again"),
    )
    assert curve_store.all_time(user_id)[60] == 250.0


def test_clean_curve_cache_merges_new_stronger_ride_without_history_rebuild(user_id, monkeypatch):
    _add_curve_activity(user_id, "first", utc_now() - dt.timedelta(days=1), 200.0)
    assert curve_store.all_time(user_id)[60] == 200.0
    monkeypatch.setattr(
        curve_store, "_rebuild_locked",
        lambda *_args: pytest.fail("insert should merge clean cache"),
    )
    _add_curve_activity(user_id, "stronger", utc_now(), 300.0)
    assert curve_store.all_time(user_id)[60] == 300.0


def test_activity_insert_merges_curve_cache_before_commit(user_id, monkeypatch):
    _add_curve_activity(user_id, "first", utc_now() - dt.timedelta(days=1), 200.0)
    observed = {}
    merge = curve_store.merge_activity_in_transaction

    def wrapped(conn, uid, record):
        observed["in_transaction"] = conn.in_transaction
        return merge(conn, uid, record)

    monkeypatch.setattr(curve_store, "merge_activity_in_transaction", wrapped)
    _add_curve_activity(user_id, "stronger", utc_now(), 300.0)

    assert observed["in_transaction"]
    assert curve_store.all_time(user_id)[60] == 300.0


def test_duplicate_and_correction_invalidation_rebuild_effective_curve(user_id):
    now = utc_now()
    primary = _add_curve_activity(user_id, "primary", now - dt.timedelta(days=2), 200.0)
    secondary = _add_curve_activity(user_id, "secondary", now - dt.timedelta(days=1), 500.0)
    assert curve_store.all_time(user_id)[60] == 500.0
    assert db.set_duplicate_of(user_id, secondary, primary)
    assert curve_store.all_time(user_id)[60] == 200.0
    assert db.apply_power_correction(
        user_id, primary, 0, 59, 200.0, None,
        {"avg_power": 0.0, "np": 0.0, "if_": 0.0, "tss": 0.0},
    ) is not None
    assert curve_store.all_time(user_id) == {}


def test_last_ride_skips_newest_unusable_power(user_id):
    now = utc_now()
    _add_curve_activity(user_id, "usable", now - dt.timedelta(days=1), 225.0)
    db.insert_activity(user_id, {
        "dedup_hash": "empty", "filename": "empty.fit", "start_time": now.isoformat(),
        "duration_s": 60, "avg_power": 0.0, "np": 0.0, "if_": 0.0,
        "tss": 0.0, "streams": {"power": [0.0] * 60},
    })
    assert curve_store.last_ride(user_id)[60] == 225.0


def test_v31_migrates_curve_cache_table_in_place(tmp_path):
    path = str(tmp_path / "v31.db")
    db.init_db(path)
    conn = sqlite3.connect(path)
    try:
        conn.execute("DROP TABLE curve_cache")
        conn.execute("PRAGMA user_version = 31")
        conn.commit()
    finally:
        conn.close()
    db.init_db(path)
    conn = sqlite3.connect(path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 32
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'curve_cache'"
        ).fetchone() is not None
    finally:
        conn.close()
