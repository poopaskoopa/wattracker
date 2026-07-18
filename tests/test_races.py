"""Tests for race results: source fallback, heuristics, power table, routes."""
import datetime as dt

import pytest

from wattracker import db, races

pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from wattracker.server import create_app  # noqa: E402


@pytest.fixture()
def client():
    app = create_app()
    with TestClient(app) as c:
        yield c


def _register(client, username="rider"):
    client.post("/register", data={"username": username, "password": "password123"})


def _activity(user_id, start_time, seconds, watts, if_=0.9, tss=80.0):
    return db.insert_activity(
        user_id,
        {
            "dedup_hash": f"h-{start_time}-{seconds}",
            "filename": f"ride-{start_time[:10]}.fit",
            "start_time": start_time,
            "duration_s": seconds,
            "distance_m": 0.0,
            "avg_power": watts,
            "avg_hr": 0.0,
            "np": watts,
            "if_": if_,
            "tss": tss,
            "streams": {"power": [watts] * seconds},
        },
    )


# ------------------------------------------------------- power per period
def test_format_duration_under_1h():
    assert races.format_duration(46 * 60 + 12) == "46:12"


def test_format_duration_over_1h():
    assert races.format_duration(3600 + 2 * 60 + 7.25) == "1:02:07"


def test_format_duration_rounds_to_nearest_second():
    assert races.format_duration(12.6) == "00:13"


def test_format_duration_none():
    assert races.format_duration(None) is None


def test_power_per_period_exact_spec_durations():
    assert races.RACE_POWER_DURATIONS == (1, 5, 15, 30, 60, 120, 300, 600, 1200)
    stream = [400.0] * 15 + [200.0] * 1200  # sprint then steady
    p = races.power_per_period(stream)
    assert set(p.keys()) == {str(d) for d in races.RACE_POWER_DURATIONS}
    assert p["1"] == 400
    assert p["15"] == 400
    assert p["1200"] < 210  # includes the steady block


def test_power_per_period_omits_durations_longer_than_ride():
    p = races.power_per_period([250.0] * 700)
    assert "600" in p and "1200" not in p and "3600" not in p


def test_mmp_grid_includes_1s_and_40min():
    from wattracker.metrics.curve import MMP_DURATIONS

    assert 1 in MMP_DURATIONS and 2400 in MMP_DURATIONS


# --------------------------------------------------------- local heuristic
def test_local_race_detection_heuristic(user_id):
    _activity(user_id, "2026-06-01T10:00:00", 3600, 260.0, if_=0.9)   # race
    _activity(user_id, "2026-06-02T10:00:00", 3600, 180.0, if_=0.65)  # endurance
    _activity(user_id, "2026-06-03T10:00:00", 600, 300.0, if_=1.1)    # too short
    results = races.derive_local_results(user_id)
    assert len(results) == 1
    r = results[0]
    assert r["event_date"] == "2026-06-01"
    assert "Race effort" in r["event_title"]
    assert r["power"]["1200"] == 260


# ------------------------------------------------------------ refresh flow
def test_refresh_falls_back_to_local_when_source_unavailable(user_id, monkeypatch):
    def boom(url, timeout=0):
        raise races.RaceSourceUnavailable("non-JSON response (text/xml)")

    monkeypatch.setattr(races, "_http_get_json", boom)
    _activity(user_id, "2026-06-01T10:00:00", 3600, 260.0, if_=0.9)
    out = races.refresh_race_results(user_id, "1234567")
    assert out["source"] == "local"
    assert out["count"] == 1
    assert "non-JSON" in out["error"]
    sync = db.get_race_sync(user_id)
    assert sync["source"] == "local"
    assert sync["rider_id"] == "1234567"
    assert sync["bests"]["60"] == 260


def test_refresh_uses_zwiftpower_when_it_answers(user_id, monkeypatch):
    doc = {
        "data": [
            {
                "event_date": int(dt.datetime(2026, 6, 1, 10).timestamp()),
                "event_title": "3R Volcano Flat Race",
                "f_t": "TYPE_RACE TYPE_RACE ",
                "position_in_cat": 7,
                "category": "B",
                "avg_power": [251, 1],
                "np": [265, 1],
                "w15": [420, 0],  # ZwiftPower peak-power period
            }
        ]
    }
    monkeypatch.setattr(races, "_http_get_json", lambda url, timeout=0: doc)
    # A same-day imported ride supplies periods ZwiftPower doesn't carry (300s).
    _activity(user_id, "2026-06-01T10:00:00", 3600, 260.0, if_=0.9)
    out = races.refresh_race_results(user_id, "1234567")
    assert out["source"] == "zwiftpower"
    assert out["count"] == 1 and out["error"] is None
    rows = db.list_race_results(user_id)
    assert rows[0]["event_title"] == "3R Volcano Flat Race"
    assert rows[0]["position"] == "7"
    assert rows[0]["category"] == "B"
    assert rows[0]["avg_power"] == 251.0
    assert rows[0]["source_type"] == "TYPE_RACE TYPE_RACE"
    assert rows[0]["power"]["15"] == 420  # from ZwiftPower's w15
    assert rows[0]["power"]["300"] == 260  # filled from the local ride
    # The race resolves to its same-day imported ride for the graphs link.
    assert rows[0]["activity_id"] is not None


def test_race_links_to_matching_ride_and_none_when_absent(user_id, monkeypatch):
    doc = {"data": [
        {"event_date": int(dt.datetime(2026, 6, 1, 10).timestamp()),
         "event_title": "Has Ride", "f_t": "TYPE_RACE", "w15": [400, 0]},
        {"event_date": int(dt.datetime(2026, 5, 1, 10).timestamp()),
         "event_title": "No Ride", "f_t": "TYPE_RACE", "w15": [380, 0]},
    ]}
    monkeypatch.setattr(races, "_http_get_json", lambda url, timeout=0: doc)
    aid = _activity(user_id, "2026-06-01T10:00:00", 3600, 260.0, if_=0.9)
    races.refresh_race_results(user_id, "1234567")
    by_title = {r["event_title"]: r for r in races.race_page_data(user_id)["results"]}
    assert by_title["Has Ride"]["activity_id"] == aid
    assert by_title["No Ride"]["activity_id"] is None


def test_race_page_renders_period_columns_and_graph_links(client, monkeypatch):
    from wattracker import zwiftauth

    _register(client)
    uid = db.get_user_by_username("rider")["id"]
    aid = _activity(uid, "2026-06-01T10:00:00", 3600, 260.0, if_=0.9)
    doc = {"data": [{
        "event_date": int(dt.datetime(2026, 6, 1, 10).timestamp()),
        "event_title": "Crit City", "f_t": "TYPE_RACE",
        "position_in_cat": 2, "category": "A",
        "w5": [500, 0], "w15": [420, 0], "w30": [360, 0], "w60": [300, 0],
        "w120": [270, 0], "w300": [240, 0], "w1200": [210, 0],
    }]}
    from wattracker import credstore
    credstore.save_zwift_credentials(uid, "a@b.com", "pw")
    monkeypatch.setattr(
        zwiftauth, "fetch_results_authenticated",
        lambda email, password, rider_id=None: (doc, "1234567", 72.0))
    client.post("/races/refresh", data={"rider_id": "1234567"})
    text = client.get("/races").text
    # Per-period columns present with values (root-cause: were pushed off a
    # 960px page; page is now wide + first column compact).
    for label in ("1s", "5s", "15s", "30s", "1m", "5m", "20m"):
        assert f"<th>{label}</th>" in text
    assert 'data-w="420"' in text          # w15 value rendered
    assert f'href="/activity/{aid}"' in text  # race links to its ride graphs
    assert 'class="wide"' in text          # widened page


def test_period_columns_merged_after_np_no_separate_table(client, monkeypatch):
    from wattracker import credstore, zwiftauth

    _register(client)
    uid = db.get_user_by_username("rider")["id"]
    doc = {"data": [{
        "event_date": int(dt.datetime(2026, 6, 1, 10).timestamp()),
        "event_title": "Crit City", "f_t": "TYPE_RACE",
        "np": [270, 1], "w5": [500, 0], "w15": [420, 0],
    }]}
    credstore.save_zwift_credentials(uid, "a@b.com", "pw")
    monkeypatch.setattr(
        zwiftauth, "fetch_results_authenticated",
        lambda email, password, rider_id=None: (doc, "1234567", 72.0))
    client.post("/races/refresh", data={"rider_id": "1234567"})
    text = client.get("/races").text
    # Merged into ONE table: NP header then the period headers then IF, and the
    # old standalone "Average power per period" table is gone.
    assert "Average power per period" not in text
    head = text[text.index("<thead>"):text.index("</thead>")]
    assert (head.index("NP") < head.index(">1s<")
            < head.index(">20m<") < head.index(">IF<"))


def test_if_computed_from_np_and_ftp_as_of_date(client, monkeypatch):
    from wattracker import credstore, zwiftauth

    _register(client)
    uid = db.get_user_by_username("rider")["id"]
    # FTP history: 250 W from 2026-01-01. A race on 2026-06-01 with NP 275
    # -> IF = 275/250 = 1.10 (ZP provides no IF field).
    db.add_ftp_entry(uid, "2026-01-01", 250.0, "manual")
    doc = {"data": [{
        "event_date": int(dt.datetime(2026, 6, 1, 10).timestamp()),
        "event_title": "R", "f_t": "TYPE_RACE", "np": [275, 1],
    }]}
    credstore.save_zwift_credentials(uid, "a@b.com", "pw")
    monkeypatch.setattr(
        zwiftauth, "fetch_results_authenticated",
        lambda email, password, rider_id=None: (doc, "1234567", 72.0))
    client.post("/races/refresh", data={"rider_id": "1234567"})
    rows = db.list_race_results(uid)
    assert rows[0]["if_"] == pytest.approx(1.10, abs=0.001)
    assert "1.1" in client.get("/races").text  # rendered, no longer blank


def test_if_from_zwiftpower_race_ftp_field():
    # ZP carries the rider's FTP at the race; IF = NP / that FTP.
    doc = {"data": [{
        "event_date": int(dt.datetime(2026, 6, 1, 10).timestamp()),
        "event_title": "R", "f_t": "TYPE_RACE", "np": [240, 0], "ftp": "200",
    }]}
    row = races.parse_zwiftpower_profile(doc)[0]
    assert row["if_"] == pytest.approx(1.20, abs=0.001)


# --------------------------------------------------- ZwiftPower event id
def test_zp_event_id_parsed_from_zid_list_and_scalar():
    doc = {"data": [
        {  # [value, flag] encoding like other ZwiftPower fields
            "event_date": int(dt.datetime(2026, 6, 1, 10).timestamp()),
            "event_title": "With zid", "f_t": "TYPE_RACE", "zid": [3456789, 0],
        },
        {  # no zid at all -> None (field is optional in the payload)
            "event_date": int(dt.datetime(2026, 5, 1, 10).timestamp()),
            "event_title": "No zid", "f_t": "TYPE_RACE",
        },
    ]}
    rows = races.parse_zwiftpower_profile(doc)
    by_title = {r["event_title"]: r for r in rows}
    assert by_title["With zid"]["zp_event_id"] == "3456789"
    assert by_title["No zid"]["zp_event_id"] is None


def test_zp_event_id_backfilled_on_resync(user_id):
    db.init_db()
    base = {
        "event_date": "2026-06-01", "event_title": "R", "fetched_at": "t0",
        "power": {},
    }
    # First sync: ZwiftPower payload lacked the event id.
    db.replace_race_results(user_id, "zwiftpower", [dict(base)])
    assert db.list_race_results(user_id)[0]["zp_event_id"] is None
    # Re-sync: same race now carries a zid -> backfilled onto the row.
    db.replace_race_results(
        user_id, "zwiftpower", [dict(base, zp_event_id="3456789")]
    )
    assert db.list_race_results(user_id)[0]["zp_event_id"] == "3456789"
    # A later re-sync missing the id must not clobber the stored one.
    db.replace_race_results(user_id, "zwiftpower", [dict(base)])
    assert db.list_race_results(user_id)[0]["zp_event_id"] == "3456789"


def test_race_date_links_to_zwiftpower_when_event_id_present(client, monkeypatch):
    from wattracker import credstore, zwiftauth

    _register(client)
    uid = db.get_user_by_username("rider")["id"]
    doc = {"data": [{
        "event_date": int(dt.datetime(2026, 6, 1, 10).timestamp()),
        "event_title": "Crit City", "f_t": "TYPE_RACE",
        "position_in_cat": 2, "np": [270, 1], "zid": [3456789, 0],
    }]}
    credstore.save_zwift_credentials(uid, "a@b.com", "pw")
    monkeypatch.setattr(
        zwiftauth, "fetch_results_authenticated",
        lambda email, password, rider_id=None: (doc, "1234567", 72.0))
    client.post("/races/refresh", data={"rider_id": "1234567"})
    text = client.get("/races").text
    assert ('href="https://zwiftpower.com/events.php?zid=3456789"'
            in text)


def test_place_int_parsing():
    assert races._place_int("1") == 1
    assert races._place_int(3) == 3
    assert races._place_int("2nd") == 2
    assert races._place_int("15 /40") == 15
    assert races._place_int("") is None
    assert races._place_int(None) is None


# ------------------------------------------------------------- distance
def test_distance_parsed_from_zwiftpower_km(user_id, monkeypatch):
    doc = {"data": [{
        "event_date": int(dt.datetime(2026, 6, 1, 10).timestamp()),
        "event_title": "R", "f_t": "TYPE_RACE", "np": [240, 0],
        "distance": 30,  # ZwiftPower distance is in km
    }]}
    monkeypatch.setattr(races, "_http_get_json", lambda url, timeout=0: doc)
    races.refresh_race_results(user_id, "1234567")
    assert db.list_race_results(user_id)[0]["distance_km"] == 30.0


def test_distance_backfilled_from_local_ride(user_id, monkeypatch):
    # ZwiftPower omits distance -> backfill from the same-day ride (metres).
    doc = {"data": [{
        "event_date": int(dt.datetime(2026, 6, 1, 10).timestamp()),
        "event_title": "R", "f_t": "TYPE_RACE", "np": [240, 0],
    }]}
    monkeypatch.setattr(races, "_http_get_json", lambda url, timeout=0: doc)
    aid = _activity(user_id, "2026-06-01T10:00:00", 3600, 260.0, if_=0.9)
    db.insert_activity  # noqa: keep flake happy; activity already inserted
    # give the activity a real distance
    import wattracker.db as _db
    conn = _db.connect()
    conn.execute("UPDATE activities SET distance_m = ? WHERE id = ?", (27198.79, aid))
    conn.commit()
    conn.close()
    races.refresh_race_results(user_id, "1234567")
    row = db.list_race_results(user_id)[0]
    assert row["distance_km"] == pytest.approx(27.2, abs=0.05)


def test_distance_column_and_sort_attrs_rendered(client, monkeypatch):
    from wattracker import credstore, zwiftauth

    _register(client)
    uid = db.get_user_by_username("rider")["id"]
    doc = {"data": [{
        "event_date": int(dt.datetime(2026, 6, 1, 10).timestamp()),
        "event_title": "Crit City", "f_t": "TYPE_RACE",
        "position_in_cat": 2, "np": [270, 1], "distance": 24,
        "w5": [500, 0], "w15": [420, 0],
    }]}
    credstore.save_zwift_credentials(uid, "a@b.com", "pw")
    monkeypatch.setattr(
        zwiftauth, "fetch_results_authenticated",
        lambda email, password, rider_id=None: (doc, "1234567", 72.0))
    client.post("/races/refresh", data={"rider_id": "1234567"})
    text = client.get("/races").text
    # Distance column present, after Duration and before Avg.
    head = text[text.index("<thead>"):text.index("</thead>")]
    assert head.index("Duration") < head.index("Distance") < head.index("Avg")
    assert "24 km" in text
    # Sortable headers + numeric sort keys on cells.
    assert 'class="sortable"' in text and 'data-type="num"' in text
    assert 'data-sort="24.0"' in text    # distance sort key (km, parseFloat-safe)
    assert 'data-sort="270"' in text     # NP sort key (watts, unit-independent)
    assert 'data-sort="2"' in text       # place sort key (parsed position)
    assert 'id="raceTable"' in text


def test_podium_trophies_rendered(client, monkeypatch):
    from wattracker import credstore, zwiftauth

    _register(client)
    uid = db.get_user_by_username("rider")["id"]
    ts = lambda d: int(dt.datetime(2026, 6, d, 10).timestamp())  # noqa: E731
    doc = {"data": [
        {"event_date": ts(1), "event_title": "Win", "f_t": "TYPE_RACE",
         "position_in_cat": 1, "np": [270, 1]},
        {"event_date": ts(2), "event_title": "Third", "f_t": "TYPE_RACE",
         "position_in_cat": 3, "np": [260, 1]},
        {"event_date": ts(3), "event_title": "Pack", "f_t": "TYPE_RACE",
         "position_in_cat": 12, "np": [250, 1]},
    ]}
    credstore.save_zwift_credentials(uid, "a@b.com", "pw")
    monkeypatch.setattr(
        zwiftauth, "fetch_results_authenticated",
        lambda email, password, rider_id=None: (doc, "1234567", 72.0))
    client.post("/races/refresh", data={"rider_id": "1234567"})
    text = client.get("/races").text
    assert 'class="trophy"' in text
    assert 'fill="#d4af37"' in text       # gold for 1st
    assert 'fill="#cd7f32"' in text       # bronze for 3rd
    assert "1st place" in text and "3rd place" in text
    assert 'fill="#bfc1c2"' not in text   # no 2nd place in this set
    # 12th renders as plain text, not a trophy.
    assert ">12<" in text or ">12 " in text


def test_refresh_without_numeric_id_derives_locally(user_id):
    _activity(user_id, "2026-06-01T10:00:00", 3600, 260.0, if_=0.9)
    out = races.refresh_race_results(user_id, "not a number")
    assert out["source"] == "local"
    assert "no numeric" in out["error"]


def test_results_are_user_scoped(user_id, monkeypatch):
    from wattracker import auth

    other = db.create_user("other", auth.hash_password("password123"))
    _activity(user_id, "2026-06-01T10:00:00", 3600, 260.0, if_=0.9)
    races.refresh_race_results(user_id, "")
    assert len(db.list_race_results(user_id)) == 1
    assert db.list_race_results(other) == []
    assert db.get_race_sync(other) is None


# ----------------------------------------------------------------- routes
def test_races_page_explains_rider_id(client):
    _register(client)
    text = client.get("/races").text
    assert "zwiftpower.com/profile.php?z=" in text
    assert "Documents/Zwift/Workouts" in text
    assert 'name="rider_id"' in text


def test_races_page_prefills_numeric_zwift_id(client):
    _register(client)
    uid = db.get_user_by_username("rider")["id"]
    db.save_user_settings(uid, {"zwift_id": "1234567"})
    assert 'value="1234567"' in client.get("/races").text
    # Non-numeric saved id is NOT prefilled, and the page says why.
    db.save_user_settings(uid, {"zwift_id": "not a number"})
    text = client.get("/races").text
    assert 'value=""' in text
    assert "not numeric" in text


def test_races_refresh_route_persists_id_and_shows_results(client, monkeypatch):
    monkeypatch.setattr(
        races, "_http_get_json",
        lambda url, timeout=0: (_ for _ in ()).throw(
            races.RaceSourceUnavailable("403 MissingKey")),
    )
    _register(client)
    uid = db.get_user_by_username("rider")["id"]
    _activity(uid, "2026-06-01T10:00:00", 3600, 260.0, if_=0.9)
    r = client.post("/races/refresh", data={"rider_id": "1234567"})
    assert r.status_code == 200
    assert db.get_user_settings(uid)["zwift_id"] == "1234567"
    assert "Derived 1 race effort" in r.text
    assert "detected from your imported rides" in r.text  # source labeled
    assert "Power profile" in r.text and "all-time bests" in r.text
    # Power tables show the spec'd period columns (incl. 15s in the profile).
    for label in ("1s", "5s", "15s", "30s", "2m", "20m", "40m", "1h"):
        assert f"<th>{label}</th>" in r.text


def test_races_page_shows_stale_cache_without_network(client, monkeypatch):
    monkeypatch.setattr(
        races, "_http_get_json",
        lambda url, timeout=0: (_ for _ in ()).throw(
            races.RaceSourceUnavailable("unreachable: offline")),
    )
    _register(client)
    uid = db.get_user_by_username("rider")["id"]
    _activity(uid, "2026-06-01T10:00:00", 3600, 260.0, if_=0.9)
    races.refresh_race_results(uid, "123")
    # Later page loads read the cache only - no network call happens.
    def explode(url, timeout=0):
        raise AssertionError("page load must not hit the network")
    monkeypatch.setattr(races, "_http_get_json", explode)
    text = client.get("/races").text
    assert "Race effort" in text
    assert "Last refreshed" in text


# ----------------------------------------------- power profile + W/kg toggle
def test_profile_durations_spec_includes_15s():
    assert races.PROFILE_DURATIONS == (1, 5, 15, 30, 60, 120, 300, 600,
                                       1200, 2400, 3600)
    from wattracker.metrics.curve import MMP_DURATIONS

    assert 15 in MMP_DURATIONS


def test_bests_cover_profile_grid(user_id):
    _activity(user_id, "2026-06-01T10:00:00", 3600, 250.0, if_=0.9)
    bests = races.compute_bests(user_id)
    for d in races.PROFILE_DURATIONS:
        assert bests[str(d)] == 250
    # Per-race grid durations are also covered by the union bests.
    for d in races.RACE_POWER_DURATIONS:
        assert str(d) in bests


def test_power_profile_section_and_toggle_disabled_without_weight(client):
    _register(client)
    uid = db.get_user_by_username("rider")["id"]
    _activity(uid, "2026-06-01T10:00:00", 3600, 260.0, if_=0.9)
    client.post("/races/refresh", data={"rider_id": ""})
    text = client.get("/races").text
    assert "Power profile" in text and "<th>15s</th>" in text
    # No weight from any source: W/kg toggle disabled with a Settings hint.
    assert 'id="unitWkg"' in text and "disabled" in text
    assert "set it in" in text and "/settings" in text
    # Values carry machine-readable watts for the client-side conversion.
    assert 'data-w="260"' in text


def test_toggle_enabled_with_weight_and_settings_override(client):
    _register(client)
    uid = db.get_user_by_username("rider")["id"]
    _activity(uid, "2026-06-01T10:00:00", 3600, 260.0, if_=0.9)
    # Manual weight override via Settings.
    r = client.post("/settings", data={"weight_kg": "68.0"})
    assert r.status_code == 200
    assert db.get_user_settings(uid)["weight_kg"] == 68.0
    client.post("/races/refresh", data={"rider_id": ""})
    text = client.get("/races").text
    assert 'data-weight="68.0"' in text
    assert "(at 68.0 kg)" in text
    # Enabled toggle: the W/kg button carries no disabled attribute.
    import re

    btn = re.search(r'<button[^>]*id="unitWkg"[^>]*>', text).group(0)
    assert "disabled" not in btn
    # Preference persistence is client-side (localStorage) in the page script.
    assert "localStorage" in text and "tr_power_unit" in text
