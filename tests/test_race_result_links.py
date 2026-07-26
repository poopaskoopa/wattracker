"""Tests for linking planned races (`race_dates`) to cached results
(`race_results`) at read time.

The association is deliberately not a stored foreign key: `replace_race_results`
deletes and re-inserts every row on each refresh, so a persisted
`race_results.id` goes stale immediately. These tests pin the resolver's
semantics (which result wins, when none does), the batched query the calendar
render depends on, and the calendar markup that surfaces it.
"""
import pytest

from wattracker import db, races

pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from wattracker.server import create_app  # noqa: E402

PAST = "2026-05-10"
FUTURE = "2999-01-01"


@pytest.fixture()
def client():
    app = create_app()
    with TestClient(app) as c:
        yield c


def _register(client, username="rider"):
    client.post("/register", data={"username": username, "password": "password123"})
    return db.get_user_by_username(username)["id"]


def _seed_results(user_id, rows, source="zwiftpower"):
    """Write cached results the way a refresh does (whole-source replace)."""
    return db.replace_race_results(
        user_id, source,
        [dict({"fetched_at": "2026-05-11T00:00:00"}, **r) for r in rows],
    )


def _planned(date=PAST, name=None, duration_min=None, priority="B"):
    return {"id": 1, "date": date, "priority": priority, "name": name,
            "duration_min": duration_min}


# ------------------------------------------------------- single-race matching
def test_result_matches_a_planned_race_on_the_same_date(user_id):
    _seed_results(user_id, [{"event_date": PAST, "event_title": "Zwift Crit"}])
    match = races.match_result_for_race_date(user_id, _planned())
    assert match is not None
    assert match["event_title"] == "Zwift Crit"


def test_no_result_on_the_date_yields_none(user_id):
    _seed_results(user_id, [{"event_date": "2026-05-09",
                             "event_title": "Zwift Crit"}])
    assert races.match_result_for_race_date(user_id, _planned()) is None


def test_a_future_race_never_resolves_a_result(user_id):
    # A same-dated result can only be a previous edition of the event; a race
    # the rider has not ridden yet must never claim to have an outcome.
    _seed_results(user_id, [{"event_date": FUTURE, "event_title": "Zwift Crit"}])
    assert races.match_result_for_race_date(
        user_id, _planned(date=FUTURE)) is None


def test_a_race_date_with_no_date_yields_none(user_id):
    assert races.match_result_for_race_date(user_id, _planned(date="")) is None


# ------------------------------------------------------------ tie-breaking
def test_title_overlap_wins_over_a_closer_duration(user_id):
    _seed_results(user_id, [
        {"event_date": PAST, "event_title": "Local Chase", "duration_s": 1800},
        {"event_date": PAST, "event_title": "Tuesday Zwift Crit Series",
         "duration_s": 5400},
    ])
    match = races.match_result_for_race_date(
        user_id, _planned(name="Zwift Crit", duration_min=30))
    assert match["event_title"] == "Tuesday Zwift Crit Series"


def test_closest_duration_breaks_a_title_tie(user_id):
    _seed_results(user_id, [
        {"event_date": PAST, "event_title": "Race A", "duration_s": 5400},
        {"event_date": PAST, "event_title": "Race B", "duration_s": 2700},
    ])
    match = races.match_result_for_race_date(
        user_id, _planned(name="Something Else", duration_min=45))
    assert match["event_title"] == "Race B"


def test_a_result_without_a_duration_never_wins_by_default(user_id):
    _seed_results(user_id, [
        {"event_date": PAST, "event_title": "Race A"},
        {"event_date": PAST, "event_title": "Race B", "duration_s": 5400},
    ])
    match = races.match_result_for_race_date(
        user_id, _planned(duration_min=45))
    assert match["event_title"] == "Race B"


def test_lowest_id_breaks_a_total_tie(user_id):
    _seed_results(user_id, [
        {"event_date": PAST, "event_title": "Race A"},
        {"event_date": PAST, "event_title": "Race B"},
    ])
    ids = {r["event_title"]: r["id"]
           for r in db.race_results_on_date(user_id, PAST)}
    match = races.match_result_for_race_date(user_id, _planned())
    assert match["id"] == min(ids.values())


def test_a_lookup_failure_degrades_to_no_link(user_id, monkeypatch):
    monkeypatch.setattr(
        db, "race_results_on_date",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db gone")))
    assert races.match_result_for_race_date(user_id, _planned()) is None


# --------------------------------------------------------- the batched path
def test_batch_attach_matches_the_per_race_path(user_id):
    _seed_results(user_id, [
        {"event_date": PAST, "event_title": "Tuesday Zwift Crit",
         "duration_s": 2700},
        {"event_date": PAST, "event_title": "Other Race", "duration_s": 5400},
        {"event_date": "2026-05-03", "event_title": "Opening Race"},
    ])
    planned = [
        {"id": 1, "date": PAST, "priority": "A", "name": "Zwift Crit",
         "duration_min": 45},
        {"id": 2, "date": "2026-05-03", "priority": "B", "name": None,
         "duration_min": None},
        {"id": 3, "date": "2026-06-01", "priority": "B", "name": None,
         "duration_min": None},
        {"id": 4, "date": FUTURE, "priority": "A", "name": None,
         "duration_min": None},
    ]
    attached = races.attach_results_to_race_dates(user_id, planned)
    assert [a["result"] for a in attached] == [
        races.match_result_for_race_date(user_id, p) for p in planned
    ]
    assert attached[0]["result"]["event_title"] == "Tuesday Zwift Crit"
    assert attached[2]["result"] is None and attached[3]["result"] is None


def test_batch_attach_opens_one_connection_for_many_races(user_id, monkeypatch):
    _seed_results(user_id, [{"event_date": PAST, "event_title": "Race"}])
    planned = [{"id": i, "date": f"2026-05-{i:02d}", "priority": "B",
                "name": None, "duration_min": None}
               for i in range(1, 21)]
    opened = []
    real_connect = db.connect

    def counting_connect(*a, **k):
        opened.append(1)
        return real_connect(*a, **k)

    monkeypatch.setattr(db, "connect", counting_connect)
    races.attach_results_to_race_dates(user_id, planned)
    assert len(opened) == 1


def test_batch_attach_leaves_the_input_rows_untouched(user_id):
    _seed_results(user_id, [{"event_date": PAST, "event_title": "Race"}])
    planned = [_planned()]
    races.attach_results_to_race_dates(user_id, planned)
    assert "result" not in planned[0]


def test_batch_lookup_failure_degrades_to_no_link(user_id, monkeypatch):
    monkeypatch.setattr(
        db, "race_results_on_dates",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db gone")))
    attached = races.attach_results_to_race_dates(user_id, [_planned()])
    assert attached[0]["result"] is None


def test_results_are_scoped_to_the_user(user_id):
    from wattracker import auth

    other = db.create_user("other", auth.hash_password("password123"))
    _seed_results(other, [{"event_date": PAST, "event_title": "Their Race"}])
    assert races.match_result_for_race_date(user_id, _planned()) is None
    assert db.race_results_on_dates(user_id, [PAST]) == {}


# ------------------------------------------------------------- the calendar
def test_calendar_marks_a_raced_planned_race(client):
    uid = _register(client)
    db.add_race_date(uid, PAST, "A", "Zwift Crit", 45)
    _seed_results(uid, [{"event_date": PAST, "event_title": "Tuesday Zwift Crit",
                         "position": "3", "category": "B", "duration_s": 2700,
                         "avg_power": 250.0, "zp_event_id": "4242"}])
    text = client.get("/calendar?year=2026&month=5").text
    assert "cal-race-result" in text
    assert "https://zwiftpower.com/events.php?zid=4242" in text
    assert "Tuesday Zwift Crit — raced." in text
    assert "P3" in text


def test_calendar_leaves_an_unmatched_race_unmarked(client):
    uid = _register(client)
    db.add_race_date(uid, PAST, "A", "Zwift Crit", 45)
    text = client.get("/calendar?year=2026&month=5").text
    assert "cal-race-tag cal-race-A" in text     # the race itself still shows
    assert "cal-race-result" not in text


def test_calendar_leaves_a_future_race_unmarked(client):
    uid = _register(client)
    db.add_race_date(uid, FUTURE, "A", "Zwift Crit", 45)
    _seed_results(uid, [{"event_date": FUTURE, "event_title": "Zwift Crit"}])
    text = client.get("/calendar?year=2999&month=1").text
    assert "cal-race-tag cal-race-A" in text
    assert "cal-race-result" not in text


def test_calendar_escapes_a_hostile_event_title(client):
    uid = _register(client)
    db.add_race_date(uid, PAST, "B", "Crit", None)
    _seed_results(uid, [{"event_date": PAST,
                         "event_title": '<img src=x onerror=alert(1)>Crit'}])
    text = client.get("/calendar?year=2026&month=5").text
    assert "<img src=x" not in text
    assert "&lt;img src=x" in text
