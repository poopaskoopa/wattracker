"""Personal rider zones, HRmax estimation, migration, and profile routes."""
import datetime as dt
import math

import pytest

from wattracker import auth, db
from wattracker.analysis import pipeline, zones
from wattracker.metrics import profile_store

pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from wattracker.server import create_app  # noqa: E402


@pytest.fixture()
def client():
    with TestClient(create_app()) as value:
        yield value


def _register(client, username="rider"):
    client.post("/register", data={"username": username, "password": "password123"})
    return db.get_user_by_username(username)["id"]


def _activity(user_id, key, when, power=None, hr=None, times=None, np=200, if_=0.8):
    length = max(len(power or []), len(hr or []), len(times or []))
    return db.insert_activity(user_id, {
        "dedup_hash": key,
        "filename": f"{key}.fit",
        "start_time": when,
        "duration_s": length,
        "distance_m": 1000,
        "avg_power": 200,
        "avg_hr": 150,
        "np": np,
        "if_": if_,
        "tss": 40,
        "streams": {"time": times or [], "power": power or [], "heartrate": hr or []},
    })


def test_power_zone_exact_half_open_boundaries_and_zero():
    values = [0, 55.9, 56, 75.9, 76, 90.9, 91, 105.9, 106, 120.9, 121, 150.9, 151]
    result = zones.time_in_zones(values, [], 100, zones.POWER_ZONES, "power")
    assert [row["seconds"] for row in result["zones"]] == [2, 2, 2, 2, 2, 2, 1]
    assert result["covered_s"] == 13
    assert result["missing_s"] == 0


def test_hr_zone_boundaries_and_below_fifty_percent_goes_to_z1():
    result = zones.time_in_zones(
        [60, 119.9, 120, 139.9, 140, 159.9, 160, 179.9, 180, 200],
        [], 200, zones.HR_ZONES, "heart-rate",
    )
    assert [row["seconds"] for row in result["zones"]] == [2, 2, 2, 2, 2]


def test_time_accounting_gaps_invalid_values_and_unequal_lengths():
    result = zones.time_in_zones(
        [0, -1, 200, math.nan, 300], [0, 1, 7, 8, 9, 10],
        200, zones.POWER_ZONES, "power",
    )
    assert result["covered_s"] == 3  # zero, 200 W, and 300 W
    assert result["missing_s"] == 7  # six-second gap + NaN second
    assert result["coverage_pct"] == 30
    assert result["zones"][0]["duration"] == "0:01"
    assert result["zones"][5]["duration"] == "0:01"


def test_missing_timestamp_stream_uses_one_second_per_sample_and_one_sample_works():
    result = zones.time_in_zones([100], [], 200, zones.POWER_ZONES, "power")
    assert result["available"] is True
    assert result["covered_s"] == 1
    # A timestamped singleton has no measurable interval and is handled safely.
    timed = zones.time_in_zones([100], ["2026-01-01T00:00:00"], 200, zones.POWER_ZONES, "power")
    assert timed["available"] is False


def test_missing_stream_and_invalid_hr_are_unavailable_or_missing():
    assert zones.time_in_zones([], [], 190, zones.HR_ZONES, "heart-rate")["available"] is False
    result = zones.time_in_zones([29, 231, "bad", 150], [], 190, zones.HR_ZONES, "heart-rate")
    assert result["covered_s"] == 1
    assert result["missing_s"] == 3


def _hr_ride(date, peak, seconds=1800):
    return {
        "filename": f"{date[:10]}-{peak}.fit",
        "start_time": date,
        "streams": {"heartrate": [peak] * seconds, "time": []},
    }


def test_hrmax_estimate_requires_sufficient_corroborated_sustained_data():
    now = dt.datetime(2026, 7, 1)
    good = zones.estimate_hr_max([
        _hr_ride("2026-06-01T10:00:00", 185),
        _hr_ride("2026-06-15T10:00:00", 188),
    ], now=now)
    assert good["available"] is True
    assert good["value"] == 188
    assert good["confidence"] == "moderate"

    too_far_apart = zones.estimate_hr_max([
        _hr_ride("2026-06-01T10:00:00", 180),
        _hr_ride("2026-06-15T10:00:00", 190),
    ], now=now)
    assert too_far_apart["available"] is False
    assert too_far_apart["confidence"] == "insufficient"

    too_short = zones.estimate_hr_max([
        _hr_ride("2026-06-01T10:00:00", 185, 599),
        _hr_ride("2026-06-15T10:00:00", 186, 599),
    ], now=now)
    assert too_short["available"] is False


def test_hrmax_default_now_uses_utc_naive_fit_clock(monkeypatch):
    # At 03:00 UTC these FIT rides are fresh even though their naive UTC
    # timestamps are later than the prior evening's local wall clock.
    monkeypatch.setattr(zones, "utc_now", lambda: dt.datetime(2026, 7, 1, 3))
    result = zones.estimate_hr_max([
        _hr_ride("2026-07-01T02:00:00", 185),
        _hr_ride("2026-07-01T02:30:00", 188),
    ])
    assert result["available"] is True
    assert result["value"] == 188


def test_manual_hrmax_precedes_fit_estimate_and_can_be_cleared(
        user_id, monkeypatch):
    # The rides below are pinned to absolute dates and resolve_hr_max is called
    # without a `now`, so it windows them against the real clock: the HRmax
    # evidence window is 365 days, and on 2027-06-01 the 2026-06-01 ride ages
    # out and the fit estimate silently becomes None. Pin the clock the same
    # way test_hrmax_default_now_uses_utc_naive_fit_clock does, rather than let
    # the assertion below depend on the day the suite happens to run.
    monkeypatch.setattr(zones, "utc_now", lambda: dt.datetime(2026, 7, 1, 12))
    _activity(user_id, "a", "2026-06-01T10:00:00", hr=[185] * 1800)
    _activity(user_id, "b", "2026-06-10T10:00:00", hr=[187] * 1800)
    db.set_user_hr_max(user_id, 195)
    assert zones.resolve_hr_max(user_id)["value"] == 195
    assert zones.resolve_hr_max(user_id)["confidence"] == "manual"
    db.set_user_hr_max(user_id, None)
    assert zones.resolve_hr_max(user_id)["value"] == 187


def test_hrmax_cache_recomputes_when_evidence_ages_out(user_id):
    _activity(user_id, "old-a", "2026-06-01T10:00:00", hr=[185] * 1800)
    _activity(user_id, "old-b", "2026-06-02T10:00:00", hr=[187] * 1800)

    eligible = zones.resolve_hr_max(user_id, now=dt.datetime(2027, 5, 31, 12))
    assert eligible["available"] is True
    assert eligible["value"] == 187

    # The activity fingerprint is unchanged; only the rolling cutoff moved.
    expired = zones.resolve_hr_max(user_id, now=dt.datetime(2027, 6, 3, 12))
    assert expired["available"] is False
    assert expired["confidence"] == "insufficient"


def test_current_power_profile_never_uses_unexplained_200_default(user_id):
    empty = zones.resolve_current_ftp(user_id)
    assert empty == {
        "available": False, "value": None, "source": "No personalized FTP available"
    }
    db.save_user_settings(user_id, {"ftp": 247})
    manual = zones.resolve_current_ftp(user_id)
    assert manual["value"] == 247
    assert "Manual" in manual["source"]
    db.add_ftp_entry(user_id, "2026-07-01", 260, "estimated")
    assert zones.resolve_current_ftp(user_id)["value"] == 247


@pytest.mark.parametrize("payload", [42, "250", b"250", {"sample": 250}])
def test_current_ftp_skips_corrupt_nonsequence_power(user_id, monkeypatch, payload):
    monkeypatch.setattr(db, "full_activities", lambda _uid: [
        {"streams": {"power": payload}},
    ])
    assert zones.resolve_current_ftp(user_id) == {
        "available": False,
        "value": None,
        "source": "No personalized FTP available",
    }


def test_current_ftp_skips_persisted_corrupt_power_then_uses_valid_list(
    user_id, monkeypatch
):
    frozen = dt.datetime(2026, 7, 25, 12)
    monkeypatch.setattr(zones.importer, "utc_now", lambda: frozen)
    db.insert_activity(user_id, {
        "dedup_hash": "corrupt-scalar",
        "filename": "corrupt.fit",
        "start_time": "2026-06-01T10:00:00",
        "duration_s": 1,
        "streams": {"power": 42},
    })
    _activity(
        user_id, "valid-power", frozen.isoformat(), power=[250] * 1200
    )
    assert zones.resolve_current_ftp(user_id) == {
        "available": True,
        "value": pytest.approx(237.5),
        "source": "FIT power estimate",
    }


def test_activity_ftp_prefers_history_then_np_if_recovery(user_id):
    db.add_ftp_entry(user_id, "2026-01-01", 230, "estimated")
    db.add_ftp_entry(user_id, "2026-06-01", 250, "manual")
    db.set_user_ftp_override(user_id, 300)
    historical = zones.resolve_activity_ftp(user_id, {
        "start_time": "2026-03-01T09:00:00", "np": 300, "if_": 1.0,
    })
    assert historical["value"] == 230
    assert "2026-03-01" in historical["source"]

    other = db.create_user("np-if", auth.hash_password("password123"))
    recovered = zones.resolve_activity_ftp(other, {
        "start_time": "2026-03-01T09:00:00", "np": 205, "if_": 0.82,
    })
    assert recovered["value"] == pytest.approx(250)
    assert "NP" in recovered["source"]


def test_activity_detail_zone_api_uses_raw_stream_and_keeps_graph_shape(user_id):
    db.save_user_settings(user_id, {"ftp": 200})
    db.set_user_hr_max(user_id, 200)
    aid = _activity(
        user_id, "raw", "2026-06-01T10:00:00",
        power=[100] * 2000, hr=[190] * 2000,
    )
    detail = pipeline.activity_detail(user_id, aid, max_points=100)
    assert detail["points"] <= 110
    assert len(detail["power"]) == detail["points"]
    assert detail["zones"]["power"]["covered_s"] == 2000
    assert detail["zones"]["heart_rate"]["zones"][4]["duration"] == "33:20"


def test_profile_get_post_validation_reset_and_isolation(client):
    alice = _register(client, "alice")
    db.save_user_settings(alice, {"ftp": 240})
    page = client.get("/profile")
    assert page.status_code == 200
    assert "Power profile" in page.text
    assert "Power zones" in page.text and "Training FTP: 240.0 W" in page.text
    assert "Heart-rate zones" in page.text

    bad = client.post("/profile/hr-max", data={"hr_max": "231", "action": "save"})
    assert bad.status_code == 200
    assert "80 to 230" in bad.text
    assert db.get_user_settings(alice)["hr_max"] is None

    saved = client.post(
        "/profile/hr-max", data={"hr_max": "190", "action": "save"},
        follow_redirects=False,
    )
    assert saved.status_code == 303 and saved.headers["location"] == "/profile?saved=1"
    assert db.get_user_settings(alice)["hr_max"] == 190
    assert "Heart-rate setting saved." in client.get(saved.headers["location"]).text

    client.post("/logout")
    bob = _register(client, "bob")
    assert db.get_user_settings(bob)["hr_max"] is None
    assert "HRmax: 190 bpm" not in client.get("/profile").text
    client.post("/logout")
    client.post("/login", data={"username": "alice", "password": "password123"})
    reset = client.post("/profile/hr-max", data={"action": "reset"}, follow_redirects=False)
    assert reset.status_code == 303
    assert db.get_user_settings(alice)["hr_max"] is None


@pytest.mark.parametrize(
    "streams",
    [
        [1, 2, 3],
        42,
        {"heartrate": 180, "time": 0},
        {"heartrate": [10**1000], "time": []},
    ],
    ids=["top-level-list", "top-level-scalar", "scalar-hr-time", "huge-hr"],
)
def test_profile_ignores_malformed_persisted_activity_streams(client, streams):
    user_id = _register(client)
    db.insert_activity(user_id, {
        "dedup_hash": "malformed-streams",
        "filename": "malformed.fit",
        "start_time": "2026-06-01T10:00:00",
        "duration_s": 1,
        "streams": streams,
    })
    assert db.full_activities(user_id)[0]["streams"] == streams

    page = client.get("/profile")

    assert page.status_code == 200
    assert "Heart-rate zones" in page.text


def test_profile_ftp_save_validation_reset_and_isolation(client):
    alice = _register(client, "ftp-alice")
    db.add_ftp_entry(alice, "2026-06-01", 240)

    # The bounds are wattracker.ftp_input's, shared with /settings and the setup
    # wizard (#64); 2001 and 1001 used to be accepted here and by the wizard.
    for invalid in ("0", "2001", "1001", "", "abc"):
        bad = client.post("/profile/ftp", data={"ftp": invalid, "action": "save"})
        assert bad.status_code == 200
        assert 'role="alert"' in bad.text and "from 20 to 700" in bad.text
        assert db.get_user_settings(alice)["ftp"] is None

    # A fractional entry is fine - the app itself displays one-decimal FTPs, so
    # the field has to accept one back.
    fractional = client.post(
        "/profile/ftp", data={"ftp": "250.5", "action": "save"},
        follow_redirects=False,
    )
    assert fractional.status_code == 303
    assert db.get_user_settings(alice)["ftp"] == pytest.approx(250.5)
    assert 'value="250.5"' in client.get("/profile").text

    saved = client.post(
        "/profile/ftp", data={"ftp": "275", "action": "save"},
        follow_redirects=False,
    )
    assert saved.status_code == 303 and saved.headers["location"] == "/profile?saved=ftp"
    assert db.get_user_settings(alice)["ftp"] == pytest.approx(275)
    page = client.get(saved.headers["location"])
    assert "Training FTP setting saved." in page.text
    assert 'value="275"' in page.text
    assert "Use FTP history or FIT estimate" in page.text

    client.post("/logout")
    bob = _register(client, "ftp-bob")
    assert db.get_user_settings(bob)["ftp"] is None
    assert "Training FTP: 275.0 W" not in client.get("/profile").text

    client.post("/logout")
    client.post("/login", data={"username": "ftp-alice", "password": "password123"})
    reset = client.post("/profile/ftp", data={"action": "reset"}, follow_redirects=False)
    assert reset.status_code == 303
    assert db.get_user_settings(alice)["ftp"] is None
    assert zones.resolve_current_ftp(alice)["value"] == 240


def test_profile_and_api_require_authentication(client):
    assert client.get("/profile", follow_redirects=False).status_code == 303
    assert client.post("/profile/hr-max", data={"hr_max": "190"}, follow_redirects=False).status_code == 303
    assert client.post("/profile/ftp", data={"ftp": "250"}, follow_redirects=False).status_code == 303


def test_settings_save_refreshes_profile_weight(client):
    uid = _register(client, "weight-alice")
    client.post("/settings", data={"weight_kg": "68.5"})
    assert profile_store.for_user(uid).weight_kg == pytest.approx(68.5)
    client.post("/settings", data={"weight_kg": "70.2"})
    assert profile_store.for_user(uid).weight_kg == pytest.approx(70.2)


def test_ftp_override_set_and_clear_refreshes_profile(client):
    uid = _register(client, "ftp-refresh")
    client.post("/profile/ftp", data={"ftp": "280", "action": "save"})
    assert profile_store.for_user(uid).ftp == pytest.approx(280)
    client.post("/profile/ftp", data={"action": "reset"})
    assert profile_store.for_user(uid).ftp != pytest.approx(280)


def test_hr_max_set_and_clear_refreshes_profile_and_source(client):
    uid = _register(client, "hrmax-refresh")
    client.post("/profile/hr-max", data={"hr_max": "192", "action": "save"})
    metrics = profile_store.for_user(uid)
    assert metrics.hr_max == pytest.approx(192)
    assert metrics.hr_max_source == "manual"

    client.post("/profile/hr-max", data={"action": "reset"})
    metrics = profile_store.for_user(uid)
    assert metrics.hr_max_source != "manual"


def test_settings_save_succeeds_even_when_profile_refresh_raises(client, monkeypatch):
    uid = _register(client, "refresh-fails")

    def _boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(profile_store, "refresh", _boom)
    resp = client.post("/settings", data={"weight_kg": "72"})
    assert resp.status_code == 200
    assert db.get_user_settings(uid)["weight_kg"] == pytest.approx(72)


def test_v16_to_v17_migration_preserves_users_settings_and_races(tmp_path):
    path = str(tmp_path / "migration.db")
    db.init_db(path)
    uid = db.create_user("migrating", auth.hash_password("password123"), path)
    db.save_user_settings(uid, {"ftp": 245, "weight_kg": 70}, path)
    conn = db.connect(path)
    conn.execute(
        "INSERT INTO race_results (user_id, source, event_date, event_title, avg_hr, max_hr, weight_kg, fetched_at) "
        "VALUES (?, 'zwiftpower', '2026-06-01', 'Race', 170, 190, 70, '2026-06-02')",
        (uid,),
    )
    conn.execute("ALTER TABLE user_settings DROP COLUMN hr_max")
    conn.execute("PRAGMA user_version = 16")
    conn.commit()
    conn.close()

    db.init_db(path)
    conn = db.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    setting = conn.execute(
        "SELECT ftp, weight_kg, hr_max FROM user_settings WHERE user_id = ?", (uid,)
    ).fetchone()
    assert tuple(setting) == (245, 70, None)
    race = conn.execute("SELECT avg_hr, max_hr, weight_kg FROM race_results WHERE user_id = ?", (uid,)).fetchone()
    assert tuple(race) == (170, 190, 70)
    conn.close()
