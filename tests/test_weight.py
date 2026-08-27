"""Body weight as history, not a setting (schema v34).

A rider's weight changes; a single ``user_settings.weight_kg`` scalar turns
every W/kg label into a statement about a weight the rider no longer has. This
feature stores weight per date, in ``weight_history``, with three capture
sources - manual (highest), the weight a Zwift ride was ridden at
(``race_results.weight_kg``, per event date), and the weight of a profile
sync (local today) - and resolves "what did this ride happen at" per record.

Two invariants this module pins:

1. The v33 -> v34 migration backfills from ``race_results`` and promotes a
   rider's typed scalar to a manual weigh-in for the upgrade day, so it stays
   the latest word rather than being shadowed by an archived ride weight.
2. ``record_weight`` is the only writer that arbitrates source priority, and
   a skipped write changes nothing.
"""
import datetime as dt
import re
import sqlite3

import pytest

from wattracker import auth, db
from wattracker.timeutil import local_today

pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from wattracker.server import create_app  # noqa: E402


@pytest.fixture()
def client():
    with TestClient(create_app()) as value:
        yield value


def _register(client, username="rider"):
    assert client.post(
        "/register", data={"username": username, "password": "password123"}
    ).status_code == 200
    return db.get_user_by_username(username)["id"]


# --------------------------------------------------------------- the policy

def test_the_input_window_is_the_window_onboarding_has_always_used():
    """A guard: it pins WHY the bounds are these (the module is new, so there
    is no unfixed code for it to fail against).

    Weight is a display value, not a scoring basis, so the policy is a single
    hard range with no confirmation band - there is nothing scoring-related to
    confirm against.
    """
    from wattracker.weight_input import (
        WEIGHT_INPUT_MAX_KG,
        WEIGHT_INPUT_MIN_KG,
        parse_weight_input,
    )

    assert (WEIGHT_INPUT_MIN_KG, WEIGHT_INPUT_MAX_KG) == (20.0, 300.0)
    assert parse_weight_input("72").kg == 72.0
    assert parse_weight_input("72.5").kg == 72.5
    assert parse_weight_input("20").kg == 20.0
    assert parse_weight_input("300").kg == 300.0
    # Out of range, non-numeric, non-finite: refused.
    for raw in ("19.9", "300.1", "-3", "0", "abc", "", "  ", "nan", "inf", "-inf"):
        assert parse_weight_input(raw).kg is None, raw
    assert parse_weight_input(1e309).kg is None  # overflow to inf
    # bool is an int; a weight of True/False is a bug, not a number.
    assert parse_weight_input(True).kg is None
    assert parse_weight_input(False).kg is None
    assert parse_weight_input(None).kg is None
    # There is no confirmation band: nothing in this module asks for one.
    assert parse_weight_input("21").needs_confirmation is False
    assert parse_weight_input("19.9", confirmed=True).kg is None
    assert parse_weight_input("abc").needs_confirmation is False


def test_the_onboarding_constants_are_the_input_policy():
    """A guard: it pins that the wizard's range and the shared policy cannot
    drift apart, the way the FTP bounds did before issue #64."""
    from wattracker.server import ONBOARDING_WEIGHT_MAX_KG, ONBOARDING_WEIGHT_MIN_KG
    from wattracker.weight_input import (
        WEIGHT_INPUT_MAX_KG,
        WEIGHT_INPUT_MIN_KG,
    )

    assert ONBOARDING_WEIGHT_MIN_KG is WEIGHT_INPUT_MIN_KG
    assert ONBOARDING_WEIGHT_MAX_KG is WEIGHT_INPUT_MAX_KG


# ------------------------------------------------------------------- schema

def test_v33_migrates_weight_history_in_place_and_backfills(tmp_path):
    """The v33 -> v34 step: the table appears, per-date rows are backfilled
    from race_results, and the typed scalar is filed as a manual weigh-in for
    the upgrade day (and still readable as the scalar).

    A typed scalar is a measurement the rider made recently; the backfill is a
    one-time import of historical ride weights. Promoting the scalar to a
    dated row is what keeps it the rider's latest word: without it an archived
    ride weight would become the divisor for today's rides the moment history
    existed, and today's W/kg would change at upgrade.
    """
    path = str(tmp_path / "v33.db")
    db.init_db(path)
    uid = db.create_user("migrator", "password123", path)
    db.save_user_settings(uid, {"weight_kg": 66.0}, path)
    conn = sqlite3.connect(path)
    try:
        for event_date, weight in (
            ("2026-06-01", 71.0),
            ("2026-06-02", None),
            ("2026-05-01", 73.0),
        ):
            conn.execute(
                "INSERT INTO race_results (user_id, source, event_date, event_title,"
                " weight_kg, fetched_at) VALUES (?, 'zwiftpower', ?, 'Race', ?,"
                " '2026-06-02')",
                (uid, event_date, weight),
            )
        conn.execute("DROP TABLE weight_history")
        conn.execute("PRAGMA user_version = 33")
        conn.commit()
    finally:
        conn.close()
    db.init_db(path)
    conn = sqlite3.connect(path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
        rows = conn.execute(
            "SELECT date, weight_kg, source FROM weight_history"
            " WHERE user_id = ? ORDER BY date",
            (uid,),
        ).fetchall()
    finally:
        conn.close()
    today = local_today(db.get_user_settings(uid, path).get("timezone")).isoformat()
    assert rows == [
        ("2026-05-01", 73.0, "zwift_ride"),
        ("2026-06-01", 71.0, "zwift_ride"),
        (today, 66.0, "manual"),
    ]
    assert db.get_user_settings(uid, path)["weight_kg"] == 66.0

    # Idempotency: running the step again changes nothing.
    db.init_db(path)
    conn = sqlite3.connect(path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
        again = conn.execute(
            "SELECT date, weight_kg, source FROM weight_history"
            " WHERE user_id = ? ORDER BY date",
            (uid,),
        ).fetchall()
    finally:
        conn.close()
    assert again == rows


def test_a_fresh_database_has_the_table_at_the_current_version(tmp_path):
    path = str(tmp_path / "fresh.db")
    db.init_db(path)
    conn = sqlite3.connect(path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
        assert (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table'"
                " AND name = 'weight_history'"
            ).fetchone()
            is not None
        )
    finally:
        conn.close()


# ---------------------------------------------------------------- the writer

def test_record_weight_priority_matrix(user_id):
    """manual > zwift_ride > zwift_profile, same source replaces itself."""
    assert db.record_weight(user_id, "2026-08-01", 70.0, "manual") is True
    assert db.record_weight(user_id, "2026-08-01", 71.0, "zwift_ride") is False
    assert db.record_weight(user_id, "2026-08-01", 72.0, "zwift_profile") is False
    assert db.weight_entry(user_id, "2026-08-01") == {
        "date": "2026-08-01", "weight_kg": 70.0, "source": "manual",
    }

    assert db.record_weight(user_id, "2026-07-01", 74.0, "zwift_profile") is True
    assert db.record_weight(user_id, "2026-07-01", 73.0, "zwift_ride") is True
    assert db.record_weight(user_id, "2026-07-01", 75.0, "zwift_profile") is False
    assert db.weight_entry(user_id, "2026-07-01")["source"] == "zwift_ride"

    # Manual replaces manual; a source replaces itself on refresh.
    assert db.record_weight(user_id, "2026-08-01", 69.5, "manual") is True
    assert db.record_weight(user_id, "2026-07-01", 72.5, "zwift_ride") is True
    assert db.weight_entry(user_id, "2026-08-01")["weight_kg"] == 69.5
    assert db.weight_entry(user_id, "2026-07-01")["weight_kg"] == 72.5


def test_a_skipped_weight_write_changes_nothing(user_id):
    assert db.record_weight(user_id, "2026-08-01", 70.0, "manual")
    before = db.weight_entry(user_id, "2026-08-01")
    assert db.record_weight(user_id, "2026-08-01", 71.0, "zwift_ride") is False
    assert db.weight_entry(user_id, "2026-08-01") == before


def test_record_weight_refuses_non_numeric_values(user_id):
    """The db guard is a type guard, not the range rail (the same split the
    FTP write guard makes, see test_ftp_input_policy.py): the 20-300 kg window
    is the input policy's, enforced at every route before record_weight is
    ever called. Storing a positive finite weight a route let through is not
    a corruption the db should second-guess."""
    for raw in (None, "abc", float("inf"), float("-inf"), -5.0, 0.0, True):
        assert db.record_weight(user_id, "2026-08-01", raw, "manual") is False, raw
    assert db.weight_entry(user_id, "2026-08-01") is None
    # A positive finite value outside the INPUT window still stores: the
    # window belongs to the routes, and this keeps a future range tightening
    # from silently discarding history.
    assert db.record_weight(user_id, "2026-08-01", 19.9, "manual") is True


def test_the_scalar_tracks_the_latest_dated_row(user_id):
    """Going-forward writes maintain the scalar invariant: the rider's
    "current weight" is the most recent thing we know, whatever its source."""
    assert db.record_weight(user_id, "2026-08-01", 70.0, "manual")
    assert db.get_user_settings(user_id)["weight_kg"] == pytest.approx(70.0)

    # An older-dated write must not regress it.
    assert db.record_weight(user_id, "2026-07-01", 75.0, "manual")
    assert db.get_user_settings(user_id)["weight_kg"] == pytest.approx(70.0)

    # A newer-dated write from any source moves it.
    assert db.record_weight(user_id, "2026-09-01", 69.0, "zwift_profile")
    assert db.get_user_settings(user_id)["weight_kg"] == pytest.approx(69.0)


# ----------------------------------------------------------------- resolvers

def test_weight_as_of_steps_to_the_latest_row_on_or_before(user_id):
    db.record_weight(user_id, "2026-07-01", 74.0, "zwift_profile")
    db.record_weight(user_id, "2026-07-15", 72.0, "zwift_ride")
    db.record_weight(user_id, "2026-08-01", 70.0, "manual")
    assert db.weight_as_of(user_id, "2026-07-15") == 72.0
    assert db.weight_as_of(user_id, "2026-07-20") == 72.0
    assert db.weight_as_of(user_id, "2026-08-15") == 70.0
    # Nothing on or before: the earliest known after is better than nothing.
    assert db.weight_as_of(user_id, "2026-06-01") == 74.0


def test_weight_as_of_falls_back_to_the_scalar(user_id):
    db.save_user_settings(user_id, {"weight_kg": 77.0})
    assert db.weight_as_of(user_id, "2026-08-01") == 77.0
    # A row, however old, beats the scalar fallback.
    db.record_weight(user_id, "2026-07-01", 74.0, "zwift_profile")
    assert db.weight_as_of(user_id, "2026-08-01") == 74.0


def test_weight_as_of_is_none_when_nothing_exists(user_id):
    assert db.weight_as_of(user_id, "2026-08-01") is None


def test_weight_resolution_reports_provenance(user_id):
    db.record_weight(user_id, "2026-08-01", 70.0, "manual")
    assert db.weight_resolution(user_id, "2026-08-01") == {
        "date": "2026-08-01", "weight_kg": 70.0, "source": "manual",
    }
    # The scalar fallback is labelled as such, so a UI can say where the
    # number came from.
    uid2 = db.create_user("second", "password123")
    db.save_user_settings(uid2, {"weight_kg": 77.0})
    assert db.weight_resolution(uid2, "2026-08-01") == {
        "date": "2026-08-01", "weight_kg": 77.0, "source": "settings",
    }
    uid3 = db.create_user("third", "password123")
    assert db.weight_resolution(uid3, "2026-08-01") is None


def test_weight_history_list_is_date_asc_and_delete_removes(user_id):
    db.record_weight(user_id, "2026-08-01", 70.0, "manual")
    db.record_weight(user_id, "2026-07-15", 72.0, "zwift_ride")
    db.record_weight(user_id, "2026-07-01", 74.0, "zwift_profile")
    assert [
        (e["date"], e["weight_kg"], e["source"])
        for e in db.weight_history_list(user_id)
    ] == [
        ("2026-07-01", 74.0, "zwift_profile"),
        ("2026-07-15", 72.0, "zwift_ride"),
        ("2026-08-01", 70.0, "manual"),
    ]
    assert db.delete_weight_entry(user_id, "2026-07-15") is True
    assert db.delete_weight_entry(user_id, "2026-07-15") is False
    assert db.weight_entry(user_id, "2026-07-15") is None
    # The resolution now steps to the next row on or before the date.
    assert db.weight_as_of(user_id, "2026-07-20") == 74.0


# ------------------------------------------------------ capture: the routes

def test_settings_weight_becomes_a_manual_log_for_today(client):
    uid = _register(client)

    response = client.post("/settings", data={"weight_kg": "72.5"})

    assert response.status_code == 200
    assert "Weight not saved" not in response.text
    today = local_today(db.get_user_settings(uid).get("timezone")).isoformat()
    assert db.weight_entry(uid, today) == {
        "date": today, "weight_kg": 72.5, "source": "manual"}
    # The scalar re-syncs to the fresh log: readers that still use it see
    # what the rider just said.
    assert db.get_user_settings(uid)["weight_kg"] == pytest.approx(72.5)


@pytest.mark.parametrize("value", ["19.9", "301", "abc", "-5", "0", "nan", ""])
def test_settings_refuses_a_bad_weight_and_keeps_the_rest(client, value):
    """A refused weight is reported and left unsaved; the rest of the form
    still lands (the ftp_message pattern). An empty field means 'untouched'."""
    uid = _register(client, "r" + value.replace(".", "n").replace("-", "n").replace("nan", "x"))
    db.save_user_settings(uid, {"weight_kg": 70.0})
    payload = {"timezone": "Europe/Paris"}
    if value:
        payload["weight_kg"] = value

    response = client.post("/settings", data=payload)

    if value:
        assert "Weight not saved" in response.text
        assert db.weight_entry(
            uid, local_today(None).isoformat()) is None
    assert db.get_user_settings(uid)["timezone"] == "Europe/Paris"
    assert db.get_user_settings(uid)["weight_kg"] == pytest.approx(70.0)
    assert db.weight_history_list(uid) == []


def test_settings_a_new_manual_log_beats_an_old_zwift_row_for_its_date(client):
    uid = _register(client)
    db.record_weight(uid, "2026-06-01", 74.0, "zwift_ride")
    today = local_today(db.get_user_settings(uid).get("timezone")).isoformat()

    client.post("/settings", data={"weight_kg": "72.0"})

    assert db.weight_entry(uid, today)["source"] == "manual"
    # Older rows are history, not overwritten current values.
    assert db.weight_entry(uid, "2026-06-01") == {
        "date": "2026-06-01", "weight_kg": 74.0, "source": "zwift_ride"}
    assert db.get_user_settings(uid)["weight_kg"] == pytest.approx(72.0)


def test_onboarding_weight_becomes_a_manual_log_for_today(client):
    uid = _register(client)

    response = client.post("/setup/complete", data={
        "weight_kg": "72.5", "ftp_choice": "manual", "manual_ftp": "245",
        "zwiftpower": "no",
    })

    assert response.status_code == 200
    today = local_today(db.get_user_settings(uid).get("timezone")).isoformat()
    assert db.weight_entry(uid, today) == {
        "date": today, "weight_kg": 72.5, "source": "manual"}
    assert db.get_user_settings(uid)["weight_kg"] == pytest.approx(72.5)


@pytest.mark.parametrize("value", ["19.9", "300.5", "abc"])
def test_onboarding_refuses_an_out_of_window_weight(client, value):
    uid = _register(client, "s" + value.replace(".", "n"))

    response = client.post("/setup/complete", data={
        "weight_kg": value, "ftp_choice": "manual", "manual_ftp": "245",
        "zwiftpower": "no",
    })

    assert response.status_code == 400
    assert db.get_user_settings(uid)["weight_kg"] is None
    assert db.weight_history_list(uid) == []


# ------------------------------- routes: the activity weight API (fetch)

def _insert_activity(uid, key, start, seconds=60):
    return db.insert_activity(uid, {
        "dedup_hash": key,
        "filename": f"{key}.fit",
        "start_time": start,
        "duration_s": seconds,
        "distance_m": 1000,
        "avg_power": 250,
        "avg_hr": None,
        "np": 250,
        "if_": 0.8,
        "tss": 30,
        "streams": {"power": [250] * seconds, "time": []},
    })


def test_activity_weight_defaults_to_the_ride_local_date(client):
    uid = _register(client)
    aid = _insert_activity(uid, "ride-w1", "2026-06-10T15:00:00")

    response = client.post(
        f"/api/activity/{aid}/weight", json={"weight_kg": 71.25}
    )

    assert response.status_code == 200
    assert response.json() == {
        "date": "2026-06-10", "weight_kg": 71.25, "source": "manual",
    }
    assert db.weight_entry(uid, "2026-06-10")["source"] == "manual"


def test_activity_weight_date_is_local_not_utc(client):
    """The default date follows the rider's timezone, so a ride late on one
    UTC day that is morning of the next local day logs to the local day."""
    uid = _register(client)
    db.save_user_settings(uid, {"timezone": "Pacific/Kiritimati"})
    aid = _insert_activity(uid, "ride-w2", "2026-06-10T15:00:00")

    response = client.post(
        f"/api/activity/{aid}/weight", json={"weight_kg": 70.0}
    )

    assert response.status_code == 200
    # 15:00 UTC is 05:00 the next day at UTC+14.
    assert response.json()["date"] == "2026-06-11"


def test_activity_weight_accepts_an_explicit_past_date(client):
    uid = _register(client)
    aid = _insert_activity(uid, "ride-w3", "2026-06-10T15:00:00")
    db.record_weight(uid, "2026-06-01", 74.0, "zwift_ride")

    response = client.post(
        f"/api/activity/{aid}/weight",
        json={"weight_kg": 71.0, "date": "2026-06-01"},
    )

    assert response.status_code == 200
    # Manual wins for the date it is logged under.
    assert db.weight_entry(uid, "2026-06-01") == {
        "date": "2026-06-01", "weight_kg": 71.0, "source": "manual",
    }


def test_activity_weight_refuses_bad_values(client):
    uid = _register(client)
    aid = _insert_activity(uid, "ride-w4", "2026-06-10T15:00:00")
    today = local_today(None)

    for payload in (
        {"weight_kg": "19.9"},
        {"weight_kg": "300.5"},
        {"weight_kg": "abc"},
        {"weight_kg": True},
        {"weight_kg": None},
        {},
        {"weight_kg": 70.0, "date": "2026-13-40"},
        {"weight_kg": 70.0, "date": (today + dt.timedelta(days=1)).isoformat()},
    ):
        response = client.post(
            f"/api/activity/{aid}/weight", json=payload
        )
        assert response.status_code == 400, payload
        assert db.weight_history_list(uid) == []


def test_activity_weight_404_for_foreign_or_unknown_activities(client):
    uid = _register(client)
    # A second user created directly in the db: registering through the
    # client would replace this session with theirs.
    other = db.create_user("other", auth.hash_password("password123"))
    foreign = _insert_activity(other, "ride-wx", "2026-06-10T15:00:00")

    assert client.post(
        f"/api/activity/{foreign}/weight", json={"weight_kg": 70.0}
    ).status_code == 404
    assert client.post(
        "/api/activity/9999999/weight", json={"weight_kg": 70.0}
    ).status_code == 404
    # No write leaked through either way.
    assert db.weight_history_list(other) == []
    assert db.weight_history_list(uid) == []


# ------------------------------- routes: the Profile log forms

def test_profile_form_logs_a_weight_for_a_date(client):
    uid = _register(client)

    response = client.post(
        "/weight",
        data={"date": "2026-06-01", "weight_kg": "70.5"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/profile?saved=weight"
    assert db.weight_entry(uid, "2026-06-01") == {
        "date": "2026-06-01", "weight_kg": 70.5, "source": "manual",
    }


def test_profile_form_defaults_to_local_today(client):
    uid = _register(client)

    response = client.post(
        "/weight", data={"weight_kg": "70.5"}, follow_redirects=False
    )

    assert response.status_code == 303
    today = local_today(
        db.get_user_settings(uid).get("timezone")
    ).isoformat()
    assert db.weight_entry(uid, today)["weight_kg"] == 70.5


def test_profile_form_rejects_bad_weight_and_renders(client):
    uid = _register(client)

    response = client.post(
        "/weight", data={"date": "2026-06-01", "weight_kg": "19.9"}
    )

    assert response.status_code == 200
    assert "Enter weight in kilograms" in response.text
    assert db.weight_history_list(uid) == []


def test_profile_form_rejects_bad_and_future_dates(client):
    uid = _register(client)
    today = local_today(db.get_user_settings(uid).get("timezone"))

    for date in ("nope", (today + dt.timedelta(days=1)).isoformat()):
        response = client.post(
            "/weight", data={"date": date, "weight_kg": "70.5"}
        )
        assert response.status_code == 200, date
        assert db.weight_history_list(uid) == []
    assert "in the future" in client.post(
        "/weight",
        data={"date": (today + dt.timedelta(days=1)).isoformat(),
              "weight_kg": "70.5"},
    ).text


def test_profile_form_deletes_one_entry_only(client):
    uid = _register(client)
    db.record_weight(uid, "2026-06-01", 70.0, "manual")
    db.record_weight(uid, "2026-06-02", 71.0, "zwift_ride")

    response = client.post(
        "/weight/delete", data={"date": "2026-06-01"}, follow_redirects=False
    )

    assert response.status_code == 303
    assert db.weight_entry(uid, "2026-06-01") is None
    assert db.weight_entry(uid, "2026-06-02")["weight_kg"] == 71.0
    # Deleting a Zwift-derived row is allowed; the next refresh re-derives it.


def test_profile_form_delete_of_unknown_date_renders_error(client):
    uid = _register(client)

    response = client.post("/weight/delete", data={"date": "2020-01-01"})

    assert response.status_code == 200
    assert "No weight is logged" in response.text


# ------------------------------------------------------- pages: the UI

def test_profile_page_renders_the_weight_log(client):
    uid = _register(client)
    db.record_weight(uid, "2026-07-01", 74.0, "zwift_profile")
    db.record_weight(uid, "2026-07-15", 72.0, "zwift_ride")
    today = local_today(None).isoformat()
    db.record_weight(uid, today, 70.0, "manual")

    text = client.get("/profile").text

    assert "Body weight" in text
    # The "current" line resolves to today's manual entry.
    assert "70.0 kg" in text
    # The chart and its data are present only when there is history.
    assert 'id="bodyWeightChart"' in text
    assert 'id="bodyWeightData"' in text
    # The log table is newest first.
    assert text.index("2026-07-15") < text.index("2026-07-01")
    assert "Zwift ride" in text
    # The form defaults: the date to local today, the value to the current.
    date_input = re.search(
        r'<input id="weight-log-date"[^>]*>', text).group(0)
    assert f'max="{today}"' in date_input
    assert f'value="{today}"' in date_input
    kg_input = re.search(r'<input id="weight-log-kg"[^>]*>', text).group(0)
    assert 'value="70.0"' in kg_input


def test_profile_page_weight_empty_state(client):
    """No entries and no scalar: a dash for the value, no chart, and a log
    form that carries nothing to re-save."""
    uid = _register(client)

    text = client.get("/profile").text

    assert "Body weight" in text
    assert 'id="bodyWeightChart"' not in text
    assert 'id="bodyWeightData"' not in text
    assert "No weigh-ins yet" in text
    kg_input = re.search(r'<input id="weight-log-kg"[^>]*>', text).group(0)
    assert 'value=""' in kg_input


def test_profile_page_scalar_only_resolves_with_settings_provenance(client):
    uid = _register(client)
    db.save_user_settings(uid, {"weight_kg": 77.5})

    text = client.get("/profile").text

    assert "77.5 kg" in text
    assert "from your profile setting" in text
    # The scalar alone is not history: no table, no chart.
    assert "No weigh-ins yet" in text


def test_dashboard_weight_card_follows_the_resolution(client):
    uid = _register(client)

    # Nothing known: the card is absent, like the other conditional cards.
    body = client.get("/").text
    assert "Body Weight" not in body

    db.save_user_settings(uid, {"weight_kg": 72.0})
    body = client.get("/").text
    assert "Body Weight" in body
    assert "72.0 kg" in body


# ------------------------- the resolution date is LOCAL, everywhere it is used

def _race_activity(uid, key, start, seconds=1800, watts=250):
    """A ride that reads as a race effort (IF and duration inside the window)."""
    return db.insert_activity(uid, {
        "dedup_hash": key,
        "filename": f"{key}.fit",
        "start_time": start,
        "duration_s": seconds,
        "distance_m": 20000,
        "avg_power": watts,
        "avg_hr": 150.0,
        "np": watts,
        "if_": 0.95,
        "tss": 45,
        "streams": {"power": [watts] * seconds, "time": []},
    })


def test_every_reader_divides_one_ride_by_one_weight_across_the_date_line(client):
    """The ride page, the power profile and the races page must agree.

    A 05:30Z ride from Los Angeles is UTC June 2 but LOCAL June 1. The races
    page used to resolve on ``race_results.event_date`` (UTC), so the same
    ride was divided by two different weights on two pages - exactly the drift
    dated weigh-ins exist to remove.
    """
    from wattracker import races
    from wattracker.analysis import pipeline, power_profile

    uid = _register(client)
    db.save_user_settings(uid, {"timezone": "America/Los_Angeles"})
    aid = _race_activity(uid, "boundary-ride", "2026-06-02T05:30:00")
    db.record_weight(uid, "2026-06-01", 70.0, "manual")
    db.record_weight(uid, "2026-06-02", 80.0, "manual")

    races.refresh_race_results(uid, "")
    result = races.race_page_data(uid)["results"][0]
    detail = pipeline.activity_detail(uid, aid)
    profile_row = power_profile.for_user(uid)["rows"][0]

    # The row is still filed on its UTC event date - only the weight lookup
    # converts - so this pins the boundary the bug lived on.
    assert result["event_date"] == "2026-06-02"
    assert detail["weight_kg"] == 70.0
    assert result["resolved_weight_kg"] == 70.0
    assert profile_row["all_time_wkg"] == round(
        profile_row["all_time"] / 70.0, 2
    )


def test_a_zwift_race_weight_is_filed_on_the_local_date(client, monkeypatch):
    """A UTC-evening race writes the LOCAL day's row, not the UTC day's.

    ``weight_history`` is keyed on local dates; ZwiftPower reports a UTC
    instant. Filing 23:30Z on the UTC date puts a rider at UTC+12 a day behind
    every other weigh-in they make.
    """
    from wattracker import races

    uid = _register(client)
    db.save_user_settings(uid, {"timezone": "Pacific/Auckland"})
    _race_activity(uid, "evening-ride", "2026-06-01T23:30:00")
    doc = {"data": [{
        "event_date": int(
            dt.datetime(2026, 6, 1, 23, 30, tzinfo=dt.timezone.utc).timestamp()
        ),
        "event_title": "Late Race", "f_t": "TYPE_RACE",
        "time": 1800, "weight": 71.5,
    }]}
    monkeypatch.setattr(
        races, "fetch_zwiftpower_results",
        lambda rider_id: races.parse_zwiftpower_profile(doc),
    )

    races.refresh_race_results(uid, "1234567")

    # 23:30 UTC on June 1 is 11:30 on June 2 at UTC+12.
    assert db.weight_entry(uid, "2026-06-02") == {
        "date": "2026-06-02", "weight_kg": 71.5, "source": "zwift_ride",
    }
    assert db.weight_entry(uid, "2026-06-01") is None


# ------------------------ the typed scalar becomes a dated weigh-in at upgrade

def _v33_database(tmp_path, name="v33.db", scalar=None, race=None, timezone=None):
    """A v33 database with an optional typed scalar and one weighted race."""
    path = str(tmp_path / name)
    db.init_db(path)
    uid = db.create_user("migrator", "password123", path)
    settings = {}
    if scalar is not None:
        settings["weight_kg"] = scalar
    if timezone is not None:
        settings["timezone"] = timezone
    if settings:
        db.save_user_settings(uid, settings, path)
    conn = sqlite3.connect(path)
    try:
        if race is not None:
            conn.execute(
                "INSERT INTO race_results (user_id, source, event_date,"
                " event_title, weight_kg, fetched_at) VALUES (?, 'zwiftpower',"
                " ?, 'Race', ?, '2026-06-02')",
                (uid, race[0], race[1]),
            )
        conn.execute("DROP TABLE weight_history")
        conn.execute("PRAGMA user_version = 33")
        conn.commit()
    finally:
        conn.close()
    return path, uid


def _history(path, uid):
    conn = sqlite3.connect(path)
    try:
        return conn.execute(
            "SELECT date, weight_kg, source FROM weight_history"
            " WHERE user_id = ? ORDER BY date",
            (uid,),
        ).fetchall()
    finally:
        conn.close()


def test_the_upgrade_files_the_typed_scalar_as_todays_manual_weigh_in(tmp_path):
    """The rider typed 70; an old race says 80. Today must still be 70.

    History outranks the scalar, so leaving the scalar as a scalar would hand
    every current ride the archived race weight - today's W/kg would change
    the moment the rider upgraded, which is the opposite of what dated
    weigh-ins are for.
    """
    path, uid = _v33_database(tmp_path, scalar=70.0, race=("2026-01-15", 80.0))

    db.init_db(path)

    today = local_today(db.get_user_settings(uid, path).get("timezone")).isoformat()
    assert _history(path, uid) == [
        ("2026-01-15", 80.0, "zwift_ride"),
        (today, 70.0, "manual"),
    ]
    assert db.weight_as_of(uid, today, path) == 70.0
    assert db.weight_as_of(uid, "2026-01-15", path) == 80.0
    # Manual, so a later Zwift-derived write cannot take the day back.
    assert db.record_weight(uid, today, 80.0, "zwift_ride", path) is False
    assert db.weight_as_of(uid, today, path) == 70.0


def test_the_promoted_weigh_in_wins_a_race_ridden_the_same_day(tmp_path):
    """Collision on the upgrade day: manual beats zwift_ride, as everywhere."""
    today = local_today(None).isoformat()
    path, uid = _v33_database(tmp_path, scalar=70.0, race=(today, 80.0))

    db.init_db(path)

    assert _history(path, uid) == [(today, 70.0, "manual")]


def test_upgrading_again_adds_nothing(tmp_path):
    path, uid = _v33_database(tmp_path, scalar=70.0, race=("2026-01-15", 80.0))

    db.init_db(path)
    before = _history(path, uid)
    for _ in range(3):
        db.init_db(path)

    assert _history(path, uid) == before


@pytest.mark.parametrize("scalar", [None, 0.0])
def test_no_usable_scalar_promotes_no_row(tmp_path, scalar):
    """Nothing to promote is not a reason to invent an empty weigh-in."""
    path, uid = _v33_database(tmp_path, scalar=scalar)

    db.init_db(path)

    assert _history(path, uid) == []


def test_the_promoted_weigh_in_is_dated_the_riders_local_day(tmp_path):
    """The table is local-dated, so the upgrade day is the rider's, not UTC."""
    path, uid = _v33_database(tmp_path, scalar=70.0, timezone="Pacific/Kiritimati")

    db.init_db(path)

    rows = _history(path, uid)
    assert [(r[1], r[2]) for r in rows] == [(70.0, "manual")]
    assert rows[0][0] == local_today("Pacific/Kiritimati").isoformat()


def test_the_backfill_files_a_race_on_its_local_date(tmp_path):
    """The backfill inherits the same UTC/local skew, via race_results.

    A date cannot be converted on its own, so the matching imported ride
    supplies the instant; a race with no local ride keeps its own date.
    """
    path, uid = _v33_database(
        tmp_path, scalar=None, race=("2026-06-02", 80.0),
        timezone="America/Los_Angeles",
    )
    db.insert_activity(uid, {
        "dedup_hash": "backfill-ride",
        "filename": "backfill-ride.fit",
        "start_time": "2026-06-02T05:30:00",
        "duration_s": 1800,
        "distance_m": 20000,
        "avg_power": 250,
        "avg_hr": 150.0,
        "np": 250,
        "if_": 0.95,
        "tss": 45,
        "streams": {"power": [250] * 60, "time": []},
    }, path)

    db.init_db(path)

    assert _history(path, uid) == [("2026-06-01", 80.0, "zwift_ride")]
