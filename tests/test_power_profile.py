"""Record-power computation, phenotype, and profile presentation."""
import datetime as dt
import re

import pytest

from wattracker import auth, db
from wattracker.analysis import power_profile

pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from wattracker.server import create_app  # noqa: E402


def _ride(start, power):
    return {"start_time": start, "streams": {"power": power}}


def _insert(uid, key, start, power):
    try:
        duration = len(power)
    except TypeError:
        duration = 1
    return db.insert_activity(uid, {
        "dedup_hash": key,
        "filename": f"{key}.fit",
        "start_time": start,
        "duration_s": duration,
        "distance_m": 1000,
        "avg_power": 200,
        "avg_hr": None,
        "np": 200,
        "if_": 0.8,
        "tss": 30,
        "streams": {"power": power, "time": []},
    })


def _row(result, seconds):
    return next(row for row in result["rows"] if row["duration"] == seconds)


def test_exact_rolling_maxima_every_duration():
    durations = [seconds for seconds, _ in power_profile.DURATIONS]
    stream = [100.0] * 3600 + [200.0] * 3600
    maxima = power_profile.rolling_maxima(stream)
    assert set(maxima) == set(durations)
    assert maxima[1] == 200
    assert maxima[15] == 200
    assert maxima[3600] == 200

    exact = power_profile.rolling_maxima([1, 2, 9, 3], [1, 2, 3])
    assert exact == {1: 9, 2: 6, 3: pytest.approx(14 / 3)}


def test_recent_boundary_future_and_malformed_are_conservative():
    now = dt.datetime(2026, 7, 25, 12)
    result = power_profile.compute([
        _ride("2026-05-26T12:00:00", [200] * 60),  # inclusive boundary
        _ride("2026-07-26T12:00:00", [900] * 60),  # future: all time only
        _ride("not-a-date", [800] * 60),            # malformed: all time only
        _ride("2026-05-26T11:59:59", [700] * 60),  # one second too old
    ], now=now)
    one_minute = _row(result, 60)
    assert one_minute["all_time"] == 900
    assert one_minute["recent_60d"] == 200
    assert one_minute["all_time_rides"] == 4
    assert one_minute["recent_60d_rides"] == 1
    assert result["cutoff_date"] == "2026-05-26"


def test_missing_zero_corrupt_and_short_streams():
    result = power_profile.compute([
        _ride("2026-07-01T00:00:00", []),
        _ride("2026-07-02T00:00:00", [0] * 60),
        _ride("2026-07-03T00:00:00", [100, "bad", 500]),
        {"start_time": "2026-07-04T00:00:00", "streams": "corrupt"},
    ], now=dt.datetime(2026, 7, 25))
    assert _row(result, 1)["all_time"] == 500
    assert _row(result, 15)["all_time"] is None
    assert result["coverage"]["all_time_durations"] == 1


@pytest.mark.parametrize("payload", [
    42,
    object(),
    "250,250",
    b"250",
    bytearray(b"250"),
    {"sample": 250},
])
def test_corrupt_nonsequence_power_payload_is_unavailable(payload):
    assert power_profile.rolling_maxima(payload) == {}
    result = power_profile.compute([
        {"start_time": "2026-07-01T00:00:00", "streams": {"power": payload}}
    ], now=dt.datetime(2026, 7, 25))
    assert result["available"] is False


def test_generators_and_numpy_arrays_are_supported():
    np = pytest.importorskip("numpy")
    assert power_profile.rolling_maxima((value for value in [100, 300]), [1, 2]) == {
        1: 300,
        2: 200,
    }
    assert power_profile.rolling_maxima(np.array([100, 300]), [1, 2]) == {
        1: 300,
        2: 200,
    }


def test_weight_and_chart_nulls():
    activities = [_ride("2026-01-01T00:00:00", [350] * 60)]
    weighted = power_profile.compute(
        activities, weight_kg=70, now=dt.datetime(2026, 7, 25)
    )
    assert _row(weighted, 60)["all_time_wkg"] == 5.0
    assert _row(weighted, 60)["recent_60d_wkg"] is None
    assert weighted["chart"]["recent_60d"][3] is None
    assert weighted["chart"]["recent_60d_watts"][3] is None
    assert power_profile.compute(
        activities, weight_kg=float("nan"), now=dt.datetime(2026, 7, 25)
    )["rows"][0]["all_time_wkg"] is None


@pytest.mark.parametrize(("curve", "expected"), [
    ({1: 1300, 15: 900, 30: 780, 60: 500, 120: 450, 300: 390,
      1200: 300, 2400: 260}, "Sprinter"),
    ({1: 1100, 15: 700, 30: 650, 60: 600, 120: 560, 300: 500,
      1200: 300, 2400: 240}, "Puncheur"),
    ({1: 950, 15: 500, 30: 450, 60: 390, 120: 370, 300: 350,
      1200: 300, 2400: 280, 3600: 270},
     "Endurance specialist (climber/time-trialist)"),
    ({1: 1200, 15: 560, 30: 520, 60: 410, 120: 390, 300: 360,
      1200: 300, 2400: 250}, "All-rounder"),
])
def test_phenotype_shape_cases(curve, expected):
    assert power_profile.classify_phenotype(curve)["label"] == expected


# Textbook Coggan-style balanced curve, as multiples of FTP.
BALANCED = {
    1: 6.4, 15: 2.9, 30: 2.2, 60: 2.0, 120: 1.60, 300: 1.27,
    600: 1.15, 1200: 1.05, 2400: 1.00, 3600: 0.97,
}


def _curve(ftp, overrides=None):
    multiples = {**BALANCED, **(overrides or {})}
    return {
        duration: round(multiple * ftp) for duration, multiple in multiples.items()
    }


@pytest.mark.parametrize("ftp", [180, 250, 320, 400])
def test_balanced_reference_curve_is_an_all_rounder(ftp):
    assert power_profile.classify_phenotype(_curve(ftp))["label"] == "All-rounder"


def test_leaning_variants_of_the_balanced_curve_get_specialty_labels():
    sprinty = _curve(250, {15: 3.6, 30: 2.9})
    punchy = _curve(250, {60: 2.4, 120: 2.0, 300: 1.55})
    assert power_profile.classify_phenotype(sprinty)["label"] == "Sprinter"
    assert power_profile.classify_phenotype(punchy)["label"] == "Puncheur"


def test_phenotype_key_and_indices_shape():
    balanced = power_profile.classify_phenotype(_curve(250))
    assert balanced["key"] == "all_rounder"
    assert set(balanced["indices"]) == {"short", "punch", "retention"}
    assert balanced["indices"]["short"] == pytest.approx(1.012, abs=0.01)

    sprinter = power_profile.classify_phenotype({
        1: 1300, 15: 900, 30: 780, 60: 500, 120: 450, 300: 390,
        1200: 300, 2400: 260,
    })
    assert sprinter["key"] == "sprinter"
    assert sprinter["indices"]["retention"] is not None

    no_long = power_profile.classify_phenotype({
        1: 1200, 15: 560, 30: 520, 60: 410, 120: 390, 300: 360, 1200: 300,
    })
    assert no_long["indices"]["retention"] is None

    sparse = power_profile.classify_phenotype({1: 900, 1200: 300})
    assert sparse["key"] == "insufficient_data"
    assert sparse["indices"] is None


def test_phenotype_requires_three_domains_and_is_scale_invariant():
    incomplete = power_profile.classify_phenotype({1: 900, 1200: 300})
    assert incomplete["label"] == "Insufficient data"
    curve = {
        1: 1300, 15: 900, 30: 780, 60: 500, 120: 450, 300: 390,
        1200: 300, 2400: 260,
    }
    scaled = {duration: watts * 0.5 for duration, watts in curve.items()}
    assert power_profile.classify_phenotype(curve)["label"] == \
        power_profile.classify_phenotype(scaled)["label"]


@pytest.mark.parametrize("curve", [
    # One record in each broad domain is not enough.
    {15: 800, 60: 500, 1200: 300, 2400: 270},
    # Both short records, but only one punch-domain record.
    {1: 1200, 15: 850, 30: 750, 60: 480, 1200: 300, 2400: 270},
    # Required domains exist but fewer than six requested durations are valid.
    {15: 850, 30: 750, 60: 500, 120: 450, 1200: 300},
])
def test_sparse_curves_are_insufficient(curve):
    result = power_profile.classify_phenotype(curve)
    assert result["label"] == "Insufficient data"
    assert "Broader record coverage" in result["rationale"]


def _dropout_stream(length=4000, gap_at=500, gap=1):
    stream = [200.0 + index % 5 for index in range(length)]
    for offset in range(gap):
        stream[gap_at + offset] = None
    return stream


def test_single_sample_dropout_is_bridged_and_keeps_the_hour_best():
    clean = [200.0 + index % 5 for index in range(4000)]
    bridged = power_profile.rolling_maxima(_dropout_stream(), [3600])
    assert 3600 in bridged
    assert bridged[3600] == pytest.approx(
        power_profile.rolling_maxima(clean, [3600])[3600], abs=1.0
    )


def test_dropout_longer_than_the_bridge_limit_still_invalidates():
    long_gap = power_profile.rolling_maxima(_dropout_stream(gap=10), [3600])
    assert 3600 not in long_gap
    # Four is one more than MAX_BRIDGED_GAP_S and is already too long.
    assert power_profile.MAX_BRIDGED_GAP_S == 3
    assert power_profile.rolling_maxima([100] + [None] * 4 + [300], [6]) == {}


def test_unanchored_dropouts_at_stream_edges_are_never_bridged():
    leading = power_profile.rolling_maxima([None, 100, 200], [1, 2, 3])
    assert leading == {1: 200, 2: 150}

    trailing = power_profile.rolling_maxima([100, 200, None], [1, 2, 3])
    assert trailing == {1: 200, 2: 150}

    assert power_profile.rolling_maxima([None], [1]) == {}
    assert power_profile.rolling_maxima([None, None, None], [1, 2, 3]) == {}


def test_bridged_samples_are_linearly_interpolated():
    assert power_profile.rolling_maxima([100, None, 300], [1, 2, 3]) == {
        1: 300,
        2: 250,
        3: 200,
    }
    assert power_profile.rolling_maxima(
        [100, None, None, None, 500], [5]
    ) == {5: 300}


NOW = dt.datetime(2026, 7, 25, 12)


def _at(days_ago):
    return (NOW - dt.timedelta(days=days_ago)).isoformat()


def _punchy():
    return _curve(250, {60: 2.4, 120: 2.0, 300: 1.55})


def _sprinty():
    return _curve(250, {15: 4.2, 30: 3.4})


def test_recent_window_wins_over_an_old_sprint_peak():
    records = [(_at(30), _punchy()), (_at(700), _sprinty())]
    result = power_profile.classify_with_recency(records, now=NOW)
    assert result["label"] == "Puncheur"
    assert result["window_days"] == 90
    assert result["stale"] is False
    # The bug being fixed: all-time bests would have called this a sprinter.
    all_time = power_profile._best_within(records, None, NOW)
    assert power_profile.classify_phenotype(all_time)["label"] == "Sprinter"


def test_ladder_falls_back_to_the_widest_window_with_coverage():
    result = power_profile.classify_with_recency(
        [(_at(300), _punchy())], now=NOW
    )
    assert result["window_days"] == 365
    assert result["stale"] is True
    assert result["key"] == "puncheur"


def test_full_recent_coverage_is_not_stale():
    result = power_profile.classify_with_recency(
        [(_at(10), _curve(250))], now=NOW
    )
    assert result["key"] == "all_rounder"
    assert result["window_days"] == 90
    assert result["stale"] is False


@pytest.mark.parametrize("start_time", [None, "", "not-a-date", 12345])
def test_undated_activities_classify_all_time_only(start_time):
    result = power_profile.classify_with_recency(
        [(start_time, _sprinty())], now=NOW
    )
    assert result["label"] == "Sprinter"
    assert result["window_days"] is None
    assert result["stale"] is True


def test_future_dated_activities_are_all_time_only():
    result = power_profile.classify_with_recency(
        [(_at(-5), _sprinty())], now=NOW
    )
    assert result["label"] == "Sprinter"
    assert result["window_days"] is None


def test_insufficient_data_branches_carry_window_fields():
    empty = power_profile.classify_with_recency([], now=NOW)
    assert empty["key"] == "insufficient_data"
    assert empty["window_days"] is None
    assert empty["stale"] is True

    direct = power_profile.classify_phenotype({1: 900, 1200: 300}, 180)
    assert direct["window_days"] == 180 and direct["stale"] is True

    denormal = power_profile.classify_phenotype(
        {**_curve(250), 1200: 5e-324}, 90
    )
    assert denormal["key"] == "insufficient_data"
    # Even from the primary window: there is no classification to be fresh
    # about, so a consumer gating on staleness alone still rejects it.
    assert denormal["window_days"] == 90 and denormal["stale"] is True


def test_compute_classifies_from_the_recent_window_only():
    result = power_profile.compute([
        _ride(_at(20), [250] * 60),
        _ride(_at(900), [900] * 60),
    ], now=NOW)
    # The old ride still owns the all-time row, but not the phenotype window.
    assert _row(result, 60)["all_time"] == 900
    assert result["phenotype"]["window_days"] in (90, 180, 365, None)
    assert "stale" in result["phenotype"]


@pytest.fixture()
def client():
    with TestClient(create_app()) as value:
        yield value


def _register(client, username):
    client.post("/register", data={"username": username, "password": "password123"})
    return db.get_user_by_username(username)["id"]


def test_profile_render_chart_table_validation_and_user_isolation(client, monkeypatch):
    monkeypatch.setattr(
        power_profile, "utc_now", lambda: dt.datetime(2026, 7, 25, 12)
    )
    alice = _register(client, "power-alice")
    db.save_user_settings(alice, {"weight_kg": 70})
    _insert(alice, "alice-power", "2026-07-01T10:00:00", [350] * 1200)

    page = client.get("/profile")
    assert page.status_code == 200
    assert "Power profile" in page.text
    assert "All time" in page.text and "Last 60 days" in page.text
    assert "350 W" in page.text and "5.0 W/kg" in page.text
    assert 'type: "radar"' in page.text
    assert '"recent_60d":' in page.text
    assert "Each spoke is normalized" in page.text
    assert 'aria-label="Radar chart' in page.text
    assert "Computed from the last 90 days." in page.text

    bad = client.post("/profile/ftp", data={"ftp": "bad", "action": "save"})
    assert bad.status_code == 200 and "350 W" in bad.text

    client.post("/logout")
    _register(client, "power-bob")
    bob_page = client.get("/profile")
    assert "Import Zwift or FIT rides with power" in bob_page.text
    assert "350 W" not in bob_page.text
    assert "powerProfileRadar" not in bob_page.text


def test_duplicate_secondary_is_excluded_from_user_profile(user_id):
    primary = _insert(user_id, "primary", "2026-07-01T10:00:00", [200] * 60)
    duplicate = _insert(user_id, "secondary", "2026-07-01T10:01:00", [900] * 60)
    conn = db.connect()
    conn.execute("UPDATE activities SET duplicate_of = ? WHERE id = ?", (primary, duplicate))
    conn.commit()
    conn.close()
    result = power_profile.for_user(user_id, now=dt.datetime(2026, 7, 25))
    assert _row(result, 60)["all_time"] == 200


def test_profile_and_validation_post_survive_scalar_power_payload(client, monkeypatch):
    frozen = dt.datetime(2026, 7, 25, 12)
    monkeypatch.setattr(power_profile, "utc_now", lambda: frozen)
    monkeypatch.setattr("wattracker.analysis.zones.importer.utc_now", lambda: frozen)
    uid = _register(client, "corrupt-power")
    _insert(uid, "corrupt", "2026-07-01T10:00:00", 42)
    _insert(uid, "valid", frozen.isoformat(), [250] * 1200)

    page = client.get("/profile")
    assert page.status_code == 200
    assert "Training FTP: 237.5 W" in page.text
    assert "250 W" in page.text
    bad = client.post("/profile/ftp", data={"ftp": "bad", "action": "save"})
    assert bad.status_code == 200
    assert "FTP must be a whole number" in bad.text
