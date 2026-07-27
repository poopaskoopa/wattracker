"""Tests for the token-authenticated /calendar.ics feed.

The security-critical claims exercised here are: a token names exactly one
user and never reaches another's rows; every rejection looks identical (404);
rotation kills the previous token; and user-supplied workout names cannot
break out of an iCalendar TEXT value.
"""
import datetime as dt
import logging
import sqlite3
import time

import pytest

pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

import wattracker.server as servermod  # noqa: E402
from wattracker import auth, calendarfeed, config, db  # noqa: E402
from wattracker.server import create_app  # noqa: E402


TODAY = dt.date(2026, 7, 27)


@pytest.fixture()
def client():
    app = create_app()
    with TestClient(app) as c:
        yield c


def _user(username="rider"):
    db.init_db()
    return db.create_user(username, auth.hash_password("password123"))


def _plan_workout(user_id, date=None, name="Sweet Spot 2x20", kind="sweetspot",
                  duration_s=5400, tss=85.0, plan_name="Block"):
    date = (date or TODAY).isoformat() if not isinstance(date, str) else date
    plan_id = db.create_plan(user_id, plan_name, date, 1)
    return db.add_plan_workout(
        plan_id, user_id, date, name, kind, duration_s, tss, "<workout_file/>"
    )


def _standalone(user_id, key="one", date=None, name="Zone 2 Cruise",
                kind="endurance", duration_s=3600, tss=50.0):
    date = (date or TODAY).isoformat() if not isinstance(date, str) else date
    return db.add_standalone_workout(
        user_id, key, date, name, kind, duration_s, tss, "<workout_file/>", 250.0
    )


def _unfold(ics: str) -> str:
    """Reverse RFC 5545 line folding so assertions can match whole values."""
    return ics.replace("\r\n ", "")


# --------------------------------------------------------- token storage
def test_token_is_random_and_only_its_hash_is_stored():
    uid = _user()
    token = calendarfeed.generate_token(uid)
    assert token and len(token) >= 40

    stored = db.get_calendar_token_hash(uid)
    assert stored == calendarfeed.hash_token(token)
    assert token not in stored

    # And the plaintext is nowhere on disk - main file or WAL sidecar.
    import os
    for suffix in ("", "-wal", "-shm"):
        candidate = config.db_path() + suffix
        if os.path.exists(candidate):
            with open(candidate, "rb") as fh:
                assert token.encode() not in fh.read(), candidate

    # Independently random, not derived from the user id or username: distinct
    # users get unrelated tokens, and so does the same user twice.
    tokens = {calendarfeed.generate_token(uid) for _ in range(20)}
    tokens.add(calendarfeed.generate_token(_user("rider2")))
    assert len(tokens) == 21
    assert token not in tokens


def test_generate_token_reports_missing_user():
    db.init_db()
    assert calendarfeed.generate_token(999_999) is None


def test_user_for_token_rejects_junk():
    uid = _user()
    token = calendarfeed.generate_token(uid)
    assert calendarfeed.user_for_token(token)["id"] == uid
    for bad in ("", None, 123, "   ", "short", "!" * 43, token + "x",
                token[:-1], "a" * 5000, calendarfeed.hash_token(token)):
        assert calendarfeed.user_for_token(bad) is None


def test_rotation_invalidates_the_previous_token():
    uid = _user()
    first = calendarfeed.generate_token(uid)
    second = calendarfeed.generate_token(uid)
    assert first != second
    assert calendarfeed.user_for_token(first) is None
    assert calendarfeed.user_for_token(second)["id"] == uid


def test_token_hash_column_is_unique_across_users():
    a = _user("a")
    b = _user("b")
    digest = calendarfeed.hash_token("shared")
    db.set_calendar_token_hash(a, digest)
    with pytest.raises(sqlite3.IntegrityError):
        db.set_calendar_token_hash(b, digest)


def test_null_token_hash_is_never_matched():
    uid = _user()
    assert db.get_calendar_token_hash(uid) is None
    assert db.user_by_calendar_token_hash(None) is None
    assert db.user_by_calendar_token_hash("") is None


# ------------------------------------------------------- route: rejection
@pytest.mark.parametrize("query", [
    "",                       # no token parameter at all
    "?token=",                # present but empty
    "?token=   ",             # whitespace
    "?token=not-a-real-token-not-a-real-token-not",
    "?token=" + "A" * 43,     # right shape, unknown
    "?token=a&token=b",       # repeated parameter
])
def test_feed_rejects_bad_tokens_with_404(client, query):
    _user()
    r = client.get("/calendar.ics" + query)
    assert r.status_code == 404
    assert "BEGIN:VCALENDAR" not in r.text
    # 404, not 401/403: the response must not confirm that a token exists or
    # that this endpoint takes one.
    assert r.status_code not in (401, 403)


def test_feed_rejects_another_users_hash_as_a_token(client):
    uid = _user()
    token = calendarfeed.generate_token(uid)
    # Presenting the stored hash must not authenticate - the hash is not a
    # credential, or the database would be a credential store again.
    r = client.get("/calendar.ics", params={"token": calendarfeed.hash_token(token)})
    assert r.status_code == 404


def test_feed_does_not_require_a_session(client):
    uid = _user()
    token = calendarfeed.generate_token(uid)
    _plan_workout(uid)
    r = client.get("/calendar.ics", params={"token": token})
    assert r.status_code == 200
    assert "BEGIN:VCALENDAR" in r.text


def test_calendar_page_still_requires_a_session(client):
    """Exempting /calendar.ics must not have exempted the /calendar page."""
    _user()
    r = client.get("/calendar", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


def test_failures_never_lock_out_a_valid_token(client):
    """Regression: the failure counter must not be able to break the feed.

    Every caller collapses onto one source address on a loopback bind (and
    behind the tunnel a phone would actually use), so a gate keyed on that
    address let anyone's bad guesses silently 404 the real subscriber - and a
    calendar client answers a 404 by quietly showing a stale calendar.
    """
    uid = _user()
    token = calendarfeed.generate_token(uid)
    _plan_workout(uid, name="StillThere")

    # Well past the threshold, from the same client, interleaved with the
    # subscriber's own polling.
    for _ in range(servermod.CALENDAR_TOKEN_FAILURE_THRESHOLD * 3):
        assert client.get(
            "/calendar.ics", params={"token": "B" * 43}
        ).status_code == 404
        ok = client.get("/calendar.ics", params={"token": token})
        assert ok.status_code == 200
        assert "StillThere" in _unfold(ok.text)

    # Still served after the burst ends, with no cooldown needed.
    assert client.get("/calendar.ics", params={"token": token}).status_code == 200


def test_a_rotated_tokens_stale_subscriber_cannot_lock_out_the_new_one(client):
    """The realistic self-DoS: an old subscription still polling a dead URL."""
    uid = _user()
    old = calendarfeed.generate_token(uid)
    new = calendarfeed.generate_token(uid)
    for _ in range(servermod.CALENDAR_TOKEN_FAILURE_THRESHOLD * 2):
        assert client.get("/calendar.ics", params={"token": old}).status_code == 404
    assert client.get("/calendar.ics", params={"token": new}).status_code == 200


def test_failures_are_counted_and_logged(client, caplog):
    _user()
    counter = client.app.state.calendar_failures
    with caplog.at_level(logging.WARNING, logger="wattracker.server"):
        for _ in range(servermod.CALENDAR_TOKEN_FAILURE_THRESHOLD):
            client.get("/calendar.ics", params={"token": "C" * 43})
    assert counter.count == servermod.CALENDAR_TOKEN_FAILURE_THRESHOLD
    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("rejected calendar-feed tokens" in m for m in warnings)
    # The rejected token itself must never reach the log.
    assert not any("CCC" in m for m in warnings)


def test_forged_forwarded_headers_cannot_silence_the_counter(client, caplog):
    """Regression: the count must not be keyed on anything a caller controls.

    uvicorn ships proxy_headers=True with a trusted range of 127.0.0.1, and
    this app binds loopback - so every caller is a "trusted proxy" and can set
    request.client.host to whatever it likes via X-Forwarded-For. A per-address
    count let 400 guesses under 400 forged addresses all sit at 1, firing no
    threshold at all. The count is now unkeyed, so the forgery is inert.
    """
    _user()
    counter = client.app.state.calendar_failures
    n = servermod.CALENDAR_TOKEN_FAILURE_THRESHOLD * 4
    with caplog.at_level(logging.WARNING, logger="wattracker.server"):
        for i in range(n):
            r = client.get(
                "/calendar.ics",
                params={"token": "E" * 43},
                headers={
                    "X-Forwarded-For": f"10.{i // 256}.{i % 256}.7",
                    "X-Real-IP": f"10.{i // 256}.{i % 256}.7",
                },
            )
            assert r.status_code == 404
    assert counter.count == n
    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING
                and "rejected calendar-feed tokens" in r.getMessage()]
    assert len(warnings) == n // servermod.CALENDAR_TOKEN_FAILURE_THRESHOLD == 4
    # No client-controlled value is echoed into the log.
    assert not any("10." in m for m in warnings)
    assert not any("EEE" in m for m in warnings)


def test_counter_is_unkeyed_and_monotonic():
    counter = servermod.CalendarFeedFailureCounter()
    assert counter.count == 0
    assert [counter.record_failure() for _ in range(5)] == [1, 2, 3, 4, 5]
    assert counter.count == 5
    # No key parameter to attack, and no eviction sweep that could wipe a real
    # tally while forged keys flood it.
    assert not hasattr(counter, "reset")
    assert not hasattr(counter, "_counts")


def test_launcher_disables_proxy_headers():
    """uvicorn's default would make every loopback caller a trusted proxy."""
    import inspect

    from wattracker import __main__ as launcher

    source = inspect.getsource(launcher.main)
    assert "proxy_headers=False" in source


# ------------------------------------------------------------ HEAD probes
def test_head_returns_200_and_get_headers_for_a_valid_token(client):
    uid = _user()
    token = calendarfeed.generate_token(uid)
    _plan_workout(uid, name="HeadProbe")

    get = client.get("/calendar.ics", params={"token": token})
    head = client.head("/calendar.ics", params={"token": token})

    assert head.status_code == 200
    assert head.content == b""
    assert head.headers["content-type"] == get.headers["content-type"]
    assert head.headers["cache-control"] == "private, no-store"
    assert head.headers["x-content-type-options"] == "nosniff"
    # RFC 9110 8.6: the Content-Length a HEAD reports is the GET's.
    assert head.headers["content-length"] == str(len(get.content))


@pytest.mark.parametrize("params", [
    {}, {"token": ""}, {"token": "D" * 43}, {"token": "malformed!"},
])
def test_head_rejects_bad_tokens_identically_to_get(client, params):
    _user()
    head = client.head("/calendar.ics", params=params)
    get = client.get("/calendar.ics", params=params)
    assert head.status_code == get.status_code == 404
    assert head.content == b""
    # Same rejection as a GET, body aside - a HEAD must not become the oracle
    # a GET refuses to be.
    assert head.headers["cache-control"] == get.headers["cache-control"]
    assert head.headers["content-length"] == str(len(get.content))


def test_head_does_not_leak_workout_data(client):
    uid = _user()
    token = calendarfeed.generate_token(uid)
    _plan_workout(uid, name="SecretSession")
    head = client.head("/calendar.ics", params={"token": token})
    assert b"SecretSession" not in head.content
    assert head.content == b""


def test_only_read_methods_are_allowed(client):
    """The feed is read-only; widening to HEAD must not have widened further."""
    uid = _user()
    token = calendarfeed.generate_token(uid)
    for method in ("POST", "PUT", "DELETE", "PATCH"):
        r = client.request(method, "/calendar.ics", params={"token": token})
        assert r.status_code == 405, method
        assert set(r.headers["allow"].replace(" ", "").split(",")) == {"GET", "HEAD"}
        assert "BEGIN:VCALENDAR" not in r.text


# --------------------------------------------------------- route: headers
def test_feed_response_headers(client):
    uid = _user()
    token = calendarfeed.generate_token(uid)
    r = client.get("/calendar.ics", params={"token": token})
    assert r.headers["content-type"] == "text/calendar; charset=utf-8"
    assert r.headers["cache-control"] == "private, no-store"
    assert r.text.endswith("END:VCALENDAR\r\n")


# ------------------------------------------------------ cross-user safety
def test_a_token_returns_only_its_owners_workouts(client):
    alice = _user("alice")
    bob = _user("bob")
    alice_token = calendarfeed.generate_token(alice)
    bob_token = calendarfeed.generate_token(bob)

    _plan_workout(alice, name="AlicePlanWorkout", plan_name="AlicePlan")
    _standalone(alice, key="alice-1", name="AliceSoloWorkout")
    _plan_workout(bob, name="BobPlanWorkout", plan_name="BobPlan")
    _standalone(bob, key="bob-1", name="BobSoloWorkout")

    a = _unfold(client.get("/calendar.ics", params={"token": alice_token}).text)
    b = _unfold(client.get("/calendar.ics", params={"token": bob_token}).text)

    assert "AlicePlanWorkout" in a and "AliceSoloWorkout" in a
    assert "BobPlanWorkout" not in a and "BobSoloWorkout" not in a
    assert "BobPlanWorkout" in b and "BobSoloWorkout" in b
    assert "AlicePlanWorkout" not in b and "AliceSoloWorkout" not in b
    assert f"-{alice}-" not in b and f"-{bob}-" not in a


def test_build_ics_is_scoped_even_when_ids_line_up():
    """Same row ids in both users' tables must not bleed across."""
    alice = _user("alice")
    bob = _user("bob")
    _plan_workout(alice, name="AliceOnly", plan_name="AlicePlan")
    _plan_workout(bob, name="BobOnly", plan_name="BobPlan")
    ics = calendarfeed.build_ics(alice, today=TODAY)
    assert "AliceOnly" in ics and "BobOnly" not in ics


# --------------------------------------------------------- ICS structure
def test_both_workout_tables_appear_in_the_feed():
    uid = _user()
    plan_id = _plan_workout(uid, name="PlanSession")
    solo_id = _standalone(uid, name="SoloSession")
    ics = _unfold(calendarfeed.build_ics(uid, today=TODAY))
    assert "PlanSession" in ics and "SoloSession" in ics
    assert f"UID:wattracker-plan-{uid}-{plan_id}@wattracker.local" in ics
    assert f"UID:wattracker-standalone-{uid}-{solo_id}@wattracker.local" in ics
    assert ics.count("BEGIN:VEVENT") == 2


def test_uids_cannot_collide_between_the_two_tables():
    uid = _user()
    plan_id = _plan_workout(uid, name="PlanSession")
    solo_id = _standalone(uid, name="SoloSession")
    # The two tables have independent AUTOINCREMENT sequences, so the same id
    # really does occur in both. Same id, same user, different table => still
    # different UIDs.
    assert calendarfeed.event_uid("plan", uid, 7) != calendarfeed.event_uid(
        "standalone", uid, 7
    )
    assert calendarfeed.event_uid("plan", uid, plan_id) != calendarfeed.event_uid(
        "standalone", uid, plan_id
    )
    assert calendarfeed.event_uid("plan", uid, solo_id) != calendarfeed.event_uid(
        "standalone", uid, solo_id
    )
    ics = _unfold(calendarfeed.build_ics(uid, today=TODAY))
    uids = [ln for ln in ics.split("\r\n") if ln.startswith("UID:")]
    assert len(uids) == len(set(uids)) == 2
    # Stable across fetches, so a re-fetch updates rather than duplicates.
    again = _unfold(calendarfeed.build_ics(uid, today=TODAY))
    assert uids == [ln for ln in again.split("\r\n") if ln.startswith("UID:")]


def test_calendar_header_is_rfc5545():
    uid = _user()
    ics = calendarfeed.build_ics(uid, today=TODAY)
    lines = ics.split("\r\n")
    assert lines[0] == "BEGIN:VCALENDAR"
    assert "VERSION:2.0" in lines
    assert any(ln.startswith("PRODID:-//wattracker//") for ln in lines)
    # Every line CRLF-terminated, including the last.
    assert ics.endswith("\r\n")
    assert "\n" not in ics.replace("\r\n", "")


def test_all_day_events_use_date_values():
    uid = _user()
    _plan_workout(uid, date="2026-07-27")
    ics = calendarfeed.build_ics(uid, today=TODAY)
    assert "DTSTART;VALUE=DATE:20260727" in ics
    # DTEND is the exclusive next day.
    assert "DTEND;VALUE=DATE:20260728" in ics
    # No DTSTART/DTEND may carry a time-of-day: the schema stores a bare date
    # (plan_workouts.date / standalone_workouts.scheduled_date), so anything
    # else would be an invented hour.
    for line in ics.split("\r\n"):
        if line.startswith(("DTSTART", "DTEND")):
            assert line.startswith(("DTSTART;VALUE=DATE:", "DTEND;VALUE=DATE:"))
            assert "T" not in line.split(":", 1)[1]


def test_summary_carries_name_and_duration():
    uid = _user()
    _plan_workout(uid, name="Sweet Spot 2x20", duration_s=5400)
    ics = _unfold(calendarfeed.build_ics(uid, today=TODAY))
    assert "SUMMARY:Sweet Spot 2x20 (1h30m)" in ics


@pytest.mark.parametrize("seconds,expected", [
    (5400, "1h30m"), (3600, "1h"), (2700, "45m"), (0, "0m"),
    (None, "0m"), (7260, "2h1m"),
])
def test_duration_formatting(seconds, expected):
    assert calendarfeed.format_duration(seconds) == expected


def test_description_has_tss_and_type():
    uid = _user()
    _plan_workout(uid, kind="sweetspot", tss=85.0)
    ics = _unfold(calendarfeed.build_ics(uid, today=TODAY))
    description = [ln for ln in ics.splitlines() if ln.startswith("DESCRIPTION:")][0]
    assert "Type: sweetspot" in description
    assert "TSS: 85" in description
    # Multi-line description is one folded/escaped TEXT value.
    assert "\\n" in description


def test_completed_workouts_are_marked():
    uid = _user()
    wid = _plan_workout(uid, name="Done Session")
    solo = _standalone(uid, name="Open Session")
    assert db.mark_plan_workout_completed(uid, wid, 4242, TODAY.isoformat())
    ics = _unfold(calendarfeed.build_ics(uid, today=TODAY))
    assert "SUMMARY:✓ Done Session" in ics
    assert "SUMMARY:Open Session" in ics
    assert solo


# ------------------------------------------------------------- escaping
HOSTILE_NAME = 'Ride, "hard"; 5x3\\\n#2'


def test_hostile_workout_name_is_escaped():
    uid = _user()
    _plan_workout(uid, name=HOSTILE_NAME)
    ics = calendarfeed.build_ics(uid, today=TODAY)
    unfolded = _unfold(ics)
    summary = [ln for ln in unfolded.split("\r\n")
               if ln.startswith("SUMMARY:")][0]

    # Comma, semicolon and backslash escaped; the newline became a literal \n.
    assert "\\," in summary and "\\;" in summary and "\\\\" in summary
    assert "SUMMARY:Ride\\, \"hard\"\\; 5x3\\\\\\n#2 (1h30m)" == summary

    # Crucially: exactly one SUMMARY line and no injected property. A raw
    # newline in the name would have started a new content line.
    assert unfolded.count("SUMMARY:") == 1
    assert "#2" in summary  # the tail stayed inside the value
    assert not any(ln.startswith("#2") for ln in unfolded.split("\r\n"))


def test_escape_text_rules():
    assert calendarfeed.escape_text("a,b") == "a\\,b"
    assert calendarfeed.escape_text("a;b") == "a\\;b"
    assert calendarfeed.escape_text("a\\b") == "a\\\\b"
    assert calendarfeed.escape_text("a\nb") == "a\\nb"
    assert calendarfeed.escape_text("a\r\nb") == "a\\nb"
    assert calendarfeed.escape_text("a\rb") == "a\\nb"
    # Backslash escaped before the characters whose escapes use it.
    assert calendarfeed.escape_text("\\,") == "\\\\\\,"
    # Stray control characters are dropped, not emitted raw.
    assert calendarfeed.escape_text("a\x00\x07b") == "ab"
    assert calendarfeed.escape_text(None) == ""


def test_injection_attempt_via_name_cannot_add_properties():
    uid = _user()
    _plan_workout(uid, name="x\r\nEND:VEVENT\r\nBEGIN:VEVENT\r\nSUMMARY:pwned")
    ics = calendarfeed.build_ics(uid, today=TODAY)
    # The payload survives *inside* the SUMMARY value as escaped text; what
    # matters is that it never becomes a content line of its own.
    lines = _unfold(ics).split("\r\n")
    assert lines.count("BEGIN:VEVENT") == 1
    assert lines.count("END:VEVENT") == 1
    assert not any(ln.startswith("SUMMARY:pwned") for ln in lines)
    assert len([ln for ln in lines if ln.startswith("SUMMARY:")]) == 1


# -------------------------------------------------------------- folding
def test_lines_are_folded_at_75_octets():
    uid = _user()
    _plan_workout(uid, name="A" * 400)
    ics = calendarfeed.build_ics(uid, today=TODAY)
    for line in ics.split("\r\n"):
        assert len(line.encode("utf-8")) <= 75, line
    assert "A" * 400 in _unfold(ics)


def test_folding_never_splits_a_multibyte_character():
    # Every character is 3 octets, so a naive 75-byte slice lands mid-character.
    line = "SUMMARY:" + "✓" * 200
    folded = calendarfeed.fold(line)
    for piece in folded.split("\r\n"):
        assert len(piece.encode("utf-8")) <= 75
        piece.encode("utf-8").decode("utf-8")  # must be valid UTF-8 on its own
    assert folded.replace("\r\n ", "") == line


def test_short_lines_are_not_folded():
    assert calendarfeed.fold("SUMMARY:short") == "SUMMARY:short"
    exact = "X" * 75
    assert calendarfeed.fold(exact) == exact
    assert "\r\n " in calendarfeed.fold("X" * 76)


def test_feed_bytes_decode_as_utf8(client):
    uid = _user()
    token = calendarfeed.generate_token(uid)
    wid = _plan_workout(uid, name="Café ✓ Ride")
    db.mark_plan_workout_completed(uid, wid, 1, TODAY.isoformat())
    r = client.get("/calendar.ics", params={"token": token})
    assert r.content.decode("utf-8")
    assert "Caf" in _unfold(r.content.decode("utf-8"))


# ---------------------------------------------------------- date window
def test_only_workouts_inside_the_window_are_included():
    uid = _user()
    inside_past = (TODAY - dt.timedelta(days=29)).isoformat()
    edge_past = (TODAY - dt.timedelta(days=30)).isoformat()
    too_old = (TODAY - dt.timedelta(days=31)).isoformat()
    edge_future = (TODAY + dt.timedelta(days=180)).isoformat()
    too_far = (TODAY + dt.timedelta(days=181)).isoformat()

    _plan_workout(uid, date=inside_past, name="InsidePast", plan_name="p1")
    _plan_workout(uid, date=edge_past, name="EdgePast", plan_name="p2")
    _plan_workout(uid, date=too_old, name="TooOld", plan_name="p3")
    _standalone(uid, key="edge", date=edge_future, name="EdgeFuture")
    _standalone(uid, key="far", date=too_far, name="TooFar")

    ics = _unfold(calendarfeed.build_ics(uid, today=TODAY))
    assert "InsidePast" in ics and "EdgePast" in ics and "EdgeFuture" in ics
    assert "TooOld" not in ics and "TooFar" not in ics


def test_range_queries_are_user_scoped():
    alice = _user("alice")
    bob = _user("bob")
    _plan_workout(alice, name="AliceRow", plan_name="ap")
    _standalone(bob, key="b", name="BobRow")
    start, end = "2000-01-01", "2100-01-01"
    assert [w["name"] for w in db.plan_workouts_in_range(alice, start, end)] == ["AliceRow"]
    assert db.plan_workouts_in_range(bob, start, end) == []
    assert [w["name"] for w in db.standalone_workouts_in_range(bob, start, end)] == ["BobRow"]
    assert db.standalone_workouts_in_range(alice, start, end) == []


def test_window_follows_the_users_timezone(monkeypatch):
    uid = _user()
    db.save_user_settings(uid, {"timezone": "Pacific/Kiritimati"})  # UTC+14
    monkeypatch.setattr(
        calendarfeed, "utc_now", lambda: dt.datetime(2026, 7, 27, 23, 0, 0)
    )
    _plan_workout(uid, date="2026-07-28", name="TomorrowInUtc")
    ics = calendarfeed.build_ics(uid)
    assert "TomorrowInUtc" in _unfold(ics)


# ------------------------------------------------------------ settings UI
def _register(client, username="rider", password="password123"):
    return client.post(
        "/register", data={"username": username, "password": password}
    )


def test_settings_page_offers_the_feed(client):
    _register(client)
    body = client.get("/settings").text
    assert "Calendar feed" in body
    assert "/settings/calendar-feed" in body
    assert "anyone who has the link" in body.lower()


def test_generate_shows_the_url_once_then_never_again(client):
    _register(client)
    r = client.post("/settings/calendar-feed")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "private, no-store"
    assert "/calendar.ics?token=" in r.text
    token = r.text.split("/calendar.ics?token=")[1].split('"')[0]

    # The URL works ...
    assert client.get("/calendar.ics", params={"token": token}).status_code == 200
    # ... and is not rendered again on a later visit.
    later = client.get("/settings").text
    assert token not in later
    assert "A calendar link is active" in later


def test_rotating_from_the_ui_kills_the_old_url(client):
    _register(client)
    first = client.post("/settings/calendar-feed").text
    old = first.split("/calendar.ics?token=")[1].split('"')[0]
    second = client.post("/settings/calendar-feed").text
    new = second.split("/calendar.ics?token=")[1].split('"')[0]

    assert old != new
    assert "previous link has stopped working" in second
    assert client.get("/calendar.ics", params={"token": old}).status_code == 404
    assert client.get("/calendar.ics", params={"token": new}).status_code == 200


def test_feed_generation_requires_a_session(client):
    r = client.post("/settings/calendar-feed", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


def test_feed_generation_rejects_cross_origin(client):
    _register(client)
    r = client.post(
        "/settings/calendar-feed", headers={"origin": "http://evil.example"}
    )
    assert r.status_code == 403


# ---------------------------------------------------------------- logging
def test_access_log_redacts_the_token():
    calendarfeed.install_access_log_redaction()
    logger = logging.getLogger("uvicorn.access")
    filters = [f for f in logger.filters
               if isinstance(f, calendarfeed._TokenRedactingFilter)]
    assert len(filters) == 1
    # Idempotent - create_app() runs per test process many times over.
    calendarfeed.install_access_log_redaction()
    assert len([f for f in logger.filters
                if isinstance(f, calendarfeed._TokenRedactingFilter)]) == 1

    record = logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 1,
        '%s - "%s %s HTTP/%s" %d', ("127.0.0.1:1", "GET",
                                    "/calendar.ics?token=SUPERSECRETVALUE",
                                    "1.1", 200), None,
    )
    assert filters[0].filter(record)
    assert "SUPERSECRETVALUE" not in record.getMessage()
    assert "[REDACTED]" in record.getMessage()


@pytest.mark.parametrize("target", [
    "/calendar.ics?token=SUPERSECRETVALUE",
    "/calendar.ics?token[]=SUPERSECRETVALUE",
    "/calendar.ics?token%5B%5D=SUPERSECRETVALUE",
    "/calendar.ics?TOKEN=SUPERSECRETVALUE",
    "/calendar.ics?tokens=SUPERSECRETVALUE",
    "/calendar.ics?a=1&token=SUPERSECRETVALUE&b=2",
    "/calendar.ics?a=1&token[]=SUPERSECRETVALUE&b=2",
    "/calendar.ics;token=SUPERSECRETVALUE",
    "/calendar.ics?a=1;token=SUPERSECRETVALUE",
])
def test_redaction_covers_every_token_parameter_form(target):
    """A guard narrower than the secret it hides is worse than none.

    Only "token=" is ever minted, but a client or framework can present the
    same parameter as "token[]="; each of those still reaches the handler and
    each would otherwise be logged in plaintext.
    """
    flt = calendarfeed._TokenRedactingFilter()
    record = logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 1,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:1", "GET", target, "1.1", 404), None,
    )
    assert flt.filter(record)
    message = record.getMessage()
    assert "SUPERSECRETVALUE" not in message, message
    assert "[REDACTED]" in message


def test_redaction_leaves_unrelated_text_alone():
    flt = calendarfeed._TokenRedactingFilter()
    for benign in ("/x?mytoken=KEEPME", "no query string here",
                   "a log line that merely says token"):
        record = logging.LogRecord(
            "uvicorn.access", logging.INFO, __file__, 1, "%s", (benign,), None,
        )
        assert flt.filter(record)
        assert record.getMessage() == benign


# The access logger runs inside the request's own task, so time spent in the
# redaction filter is time the event loop is not serving anyone. 100ms is ~200x
# the corrected pattern's real cost at this size (0.5ms) and ~20x below the
# 1.96s the quadratic version took, so it cannot flake on a loaded box while
# still failing loudly on any reintroduced blowup.
REDACTION_BUDGET_S = 0.1
# 64,800 chars: the largest pathological line that fits under httptools' ~65KB
# request-line cap, i.e. one the server accepts and logs rather than rejecting.
REDACTION_ADVERSARIAL_LEN = 64_800


@pytest.mark.parametrize("payload", [
    # id= keeps the payload out of the test name; these are ~64KB strings.
    pytest.param("?token" * 10_800, id="question-repeat"),
    pytest.param("&token" * 10_800, id="ampersand-repeat"),
    pytest.param(";token" * 10_800, id="semicolon-repeat"),
    pytest.param("?token" + "a" * (REDACTION_ADVERSARIAL_LEN - 6),
                 id="one-long-run"),
    pytest.param("?token=" + "a" * (REDACTION_ADVERSARIAL_LEN - 7),
                 id="long-value"),
    pytest.param("?token" * 5_400 + "=x", id="repeat-then-equals"),
    pytest.param("?" + "token" * 10_800, id="name-repeat"),
    pytest.param("?token" * 5_400 + "&token" * 5_400, id="mixed-separators"),
])
def test_redaction_is_linear_on_adversarial_input(payload):
    """Regression: the redaction pattern must not be a ReDoS.

    Reachable unauthenticated - this filter runs on the access-log line of
    every request, including the 404s a bad token produces - so a quadratic
    pattern is a remote event-loop stall, not just a slow test. An earlier
    widening of the parameter-name class forgot to exclude '?', which made
    "?token?token?..." scan to end-of-string from each of n/6 start positions:
    1.96s of CPU for one 64,800-char request line, during which a legitimate
    subscriber's fetch went from 0.01s to 1.58s.

    The existing redaction tests are correctness-only, which is exactly why
    they stayed green through that regression.
    """
    flt = calendarfeed._TokenRedactingFilter()
    record = logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 1,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:1", "GET", "/calendar.ics" + payload, "1.1", 404), None,
    )
    start = time.perf_counter()
    assert flt.filter(record)
    record.getMessage()
    elapsed = time.perf_counter() - start
    assert elapsed < REDACTION_BUDGET_S, f"{elapsed:.3f}s for {len(payload)} chars"


def test_redaction_stays_linear_as_input_grows():
    """Quadratic growth is the signature; assert it is absent directly.

    A 4x longer input took 16x longer before the fix. Allowing 6x here leaves
    room for timer noise on a busy machine while still catching any return to
    quadratic (which would need ~16x).
    """
    flt = calendarfeed._TokenRedactingFilter()

    def cost(n):
        line = "/calendar.ics" + "?token" * n
        best = min(
            _elapsed(flt.redact, line) for _ in range(5)
        )
        return best

    small = cost(2_700)
    large = cost(10_800)  # 4x the length
    floor = 1e-5  # don't divide by a timer-resolution artifact
    assert large < max(small, floor) * 6, f"{small:.6f}s -> {large:.6f}s"


def _elapsed(fn, *args):
    start = time.perf_counter()
    fn(*args)
    return time.perf_counter() - start


# -------------------------------------------------------------- migration
def test_migration_adds_the_column_without_losing_data():
    """A live v25 database must gain the column, not be dropped and recreated."""
    db.init_db()
    uid = db.create_user("legacy", auth.hash_password("password123"))
    _plan_workout(uid, name="PreExisting")

    path = config.db_path()
    conn = db.connect(path)
    try:
        conn.execute("DROP INDEX IF EXISTS idx_users_calendar_token_hash")
        conn.execute("ALTER TABLE users DROP COLUMN calendar_token_hash")
        conn.execute("PRAGMA user_version = 25")
        conn.commit()
    finally:
        conn.close()

    db.init_db()

    conn = db.connect(path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
        cols = {r[1] for r in conn.execute("PRAGMA table_info(users)")}
        assert "calendar_token_hash" in cols
    finally:
        conn.close()

    assert db.get_user_by_id(uid)["username"] == "legacy"
    assert [w["name"] for w in db.plan_workouts_in_range(
        uid, "2000-01-01", "2100-01-01")] == ["PreExisting"]
    # And the freshly migrated column is immediately usable.
    token = calendarfeed.generate_token(uid)
    assert calendarfeed.user_for_token(token)["id"] == uid


# ------------------------------------------- reaching the feed over a tailnet
# The app stays bound to loopback; a tailnet proxy (tailscale serve) forwards
# to it and passes the original Host through. WATTRACKER_PUBLIC_HOST names the
# one extra Host the server will answer to, and the host the subscription link
# is minted from. Unset, everything below must behave exactly as before.
PUBLIC_HOST = "laptop.tail1234.ts.net"


def _app_with_public_host(monkeypatch, host=None, scheme=None):
    if host is None:
        monkeypatch.delenv("WATTRACKER_PUBLIC_HOST", raising=False)
    else:
        monkeypatch.setenv("WATTRACKER_PUBLIC_HOST", host)
    if scheme is None:
        monkeypatch.delenv("WATTRACKER_PUBLIC_SCHEME", raising=False)
    else:
        monkeypatch.setenv("WATTRACKER_PUBLIC_SCHEME", scheme)
    return create_app()


def test_unset_public_host_still_rejects_a_tailnet_host(monkeypatch):
    """Default posture is unchanged: only loopback names are answered."""
    with TestClient(_app_with_public_host(monkeypatch)) as c:
        r = c.get("/login", headers={"host": PUBLIC_HOST})
    assert r.status_code == 400
    assert r.text == "Invalid host header"


def test_configured_public_host_is_accepted(monkeypatch):
    with TestClient(_app_with_public_host(monkeypatch, PUBLIC_HOST)) as c:
        r = c.get("/login", headers={"host": PUBLIC_HOST})
        assert r.status_code == 200
        # Host matching is case-insensitive on both sides.
        assert c.get("/login", headers={"host": PUBLIC_HOST.upper()}).status_code == 200
        # ... and the loopback names still work, so the desktop is unaffected.
        assert c.get("/login", headers={"host": "127.0.0.1:8000"}).status_code == 200


@pytest.mark.parametrize("host", [
    "other.tail1234.ts.net",
    "evil.ts.net",
    "ts.net",
    "laptop.tail1234.ts.net.evil.example",
    "evil.example",
])
def test_configured_public_host_is_not_a_wildcard(monkeypatch, host):
    """One exact name is allowed - no sibling, suffix, or parent of it."""
    with TestClient(_app_with_public_host(monkeypatch, PUBLIC_HOST)) as c:
        r = c.get("/login", headers={"host": host})
    assert r.status_code == 400


def test_public_host_with_a_port_matches_the_host_portion(monkeypatch):
    """Starlette compares the host portion, so a ':port' value must still match."""
    with TestClient(_app_with_public_host(monkeypatch, f"{PUBLIC_HOST}:8443")) as c:
        assert c.get(
            "/login", headers={"host": f"{PUBLIC_HOST}:8443"}
        ).status_code == 200
        assert c.get("/login", headers={"host": PUBLIC_HOST}).status_code == 200
        assert c.get(
            "/login", headers={"host": "other.ts.net:8443"}
        ).status_code == 400


class _FakeRequest:
    """Just enough Request for the base-URL decision."""

    def __init__(self, base_url="http://127.0.0.1:8000/"):
        self.base_url = base_url


def test_feed_base_url_falls_back_to_the_request(monkeypatch):
    monkeypatch.delenv("WATTRACKER_PUBLIC_HOST", raising=False)
    assert servermod._feed_base_url(_FakeRequest()) == "http://127.0.0.1:8000/"


def test_feed_base_url_uses_the_configured_host(monkeypatch):
    monkeypatch.setenv("WATTRACKER_PUBLIC_HOST", PUBLIC_HOST)
    monkeypatch.delenv("WATTRACKER_PUBLIC_SCHEME", raising=False)
    # https by default: tailscale serve terminates TLS.
    assert servermod._feed_base_url(_FakeRequest()) == f"https://{PUBLIC_HOST}"
    monkeypatch.setenv("WATTRACKER_PUBLIC_SCHEME", "http")
    assert servermod._feed_base_url(_FakeRequest()) == f"http://{PUBLIC_HOST}"
    monkeypatch.setenv("WATTRACKER_PUBLIC_HOST", f"{PUBLIC_HOST}:8443")
    assert servermod._feed_base_url(_FakeRequest()) == f"http://{PUBLIC_HOST}:8443"


def test_minted_url_uses_the_request_when_unconfigured(monkeypatch):
    with TestClient(_app_with_public_host(monkeypatch)) as c:
        _register(c)
        body = c.post("/settings/calendar-feed").text
    assert "http://testserver/calendar.ics?token=" in body


def test_minted_url_uses_the_configured_host(monkeypatch):
    with TestClient(_app_with_public_host(monkeypatch, f"{PUBLIC_HOST}:8443")) as c:
        _register(c)
        body = c.post("/settings/calendar-feed").text
        token = body.split("/calendar.ics?token=")[1].split('"')[0]
        # The link is for the phone; the token itself is unchanged and still
        # resolves on the loopback request that minted it.
        assert c.get("/calendar.ics", params={"token": token}).status_code == 200
    assert f"https://{PUBLIC_HOST}:8443/calendar.ics?token=" in body
    assert "testserver" not in body.split("/calendar.ics?token=")[0].split("value=")[-1]
