"""The plan says what it DID about each race.

A demoted A race - one inside an earlier A race's taper - used to be silently
planned as a B race: the rider marked two A races, one stopped getting a taper,
and nothing anywhere said so. These tests pin the three places that now speak:
the plan summary, the overnight notice, and the calendar badge.
"""
import datetime as dt
import re

import pytest

pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from wattracker import db  # noqa: E402
from wattracker.prescribe import plan as planmod, reflow  # noqa: E402
from wattracker.server import create_app  # noqa: E402

MONDAY = dt.date(2026, 8, 3)
NOW = dt.datetime(2026, 8, 5, 9, 0)   # a Wednesday inside week 1
RIDE_DAYS = ["0", "2", "4", "5"]      # Mon/Wed/Fri/Sat

PLAN_FORM = {
    "name": "Race Plan",
    "weeks": "4",
    "hours_per_week": "8",
    "hit_days_per_week": "2",
    "start_date": MONDAY.isoformat(),
    "days": RIDE_DAYS,
}

A_RACE = "2026-08-17"       # Monday of week 3
B_RACE = "2026-08-20"       # Thursday - both neighbours are ride days
LATE_A = "2026-08-24"       # 7 days after A_RACE -> demoted


@pytest.fixture()
def client():
    app = create_app()
    with TestClient(app) as c:
        yield c


def _register(client, username="rider"):
    client.post("/register", data={"username": username, "password": "password123"})
    return db.get_user_by_username(username)["id"]


def _races_section(html):
    """The plan summary's races block, or None when it is not rendered."""
    m = re.search(r'<section class="plan-races">.*?</section>', html, re.S)
    return m.group(0) if m else None


def _describe(races, **kw):
    return planmod.describe_races(races, MONDAY, 4, days_of_week=[0, 2, 4, 5],
                                  **kw)


# --------------------------------------------------------------- the summary
def test_a_demotion_is_described_with_its_reason():
    described = _describe([
        {"id": 1, "date": A_RACE, "priority": "A", "duration_min": 60},
        {"id": 2, "date": LATE_A, "priority": "A", "duration_min": 60},
    ])
    late = [r for r in described if r["date"] == LATE_A][0]
    assert late["priority"] == "B"          # EFFECTIVE, not stored
    assert late["demoted"] is True
    assert late["conflicts_with"] == A_RACE
    assert late["separation_days"] == planmod.A_RACE_SEPARATION_DAYS
    # The earlier race keeps its A and its taper.
    early = [r for r in described if r["date"] == A_RACE][0]
    assert (early["priority"], early["demoted"]) == ("A", False)
    assert early["taper_from"] == "2026-08-03"


def test_an_a_race_reports_its_taper_and_a_b_race_its_eased_neighbours():
    described = _describe([
        {"id": 1, "date": A_RACE, "priority": "A", "duration_min": 120},
        {"id": 2, "date": B_RACE, "priority": "B", "duration_min": 60},
    ])
    a, b = described[0], described[1]
    assert a["taper_from"] == "2026-08-03"      # 14 days before
    assert a["taper_hard_from"] == "2026-08-10"  # 7 days before
    assert a["recovery_dates"] == ["2026-08-19", "2026-08-21"]
    assert a["displaces_workout"] is True
    assert b["taper_from"] is None
    assert b["easy_dates"] == ["2026-08-19", "2026-08-21"]


def test_a_conflicting_race_outside_the_window_still_demotes():
    """Windowing is applied AFTER resolution: the A race that causes the
    demotion may sit before the plan even starts."""
    described = _describe([
        {"id": 1, "date": "2026-07-27", "priority": "A"},   # before the plan
        {"id": 2, "date": "2026-08-05", "priority": "A"},
    ])
    assert [r["date"] for r in described] == ["2026-08-05"]
    assert described[0]["priority"] == "B"
    assert described[0]["conflicts_with"] == "2026-07-27"


def test_generation_and_re_view_say_exactly_the_same_thing(client):
    """The whole point: the races block is computed in ONE place, at view time,
    so a freshly generated plan and the same plan re-opened cannot disagree."""
    uid = _register(client)
    db.add_race_date(uid, A_RACE, "A", "Nationals", 120)
    db.add_race_date(uid, LATE_A, "A", "State champs", 60)

    generated = client.post("/generate/plan", data=PLAN_FORM).text
    plan_id = db.list_plans(uid)[0]["id"]
    reviewed = client.get(f"/plan?plan_id={plan_id}").text

    section = _races_section(generated)
    assert section is not None
    assert _races_section(reviewed) == section
    assert "planned as a" in section and LATE_A in section
    assert A_RACE in section  # named as the race that took the taper


def test_a_plan_with_no_races_renders_no_block(client):
    _register(client)
    body = client.post("/generate/plan", data=PLAN_FORM).text
    assert _races_section(body) is None
    assert "Races in this plan" not in body


def test_a_race_name_is_escaped_everywhere_it_appears(client):
    uid = _register(client)
    hostile = '<script>alert("xss")</script>'
    db.add_race_date(uid, A_RACE, "A", hostile, 60)
    db.add_race_date(uid, LATE_A, "A", hostile, 60)

    body = client.post("/generate/plan", data=PLAN_FORM).text
    assert hostile not in body
    assert "&lt;script&gt;" in body

    cal = client.get("/calendar?year=2026&month=8").text
    assert hostile not in cal
    assert "&lt;script&gt;" in cal


# ----------------------------------------------------------- the calendar
def test_the_calendar_badges_the_effective_priority(client):
    uid = _register(client)
    db.add_race_date(uid, A_RACE, "A", "Nationals", 60)
    db.add_race_date(uid, LATE_A, "A", "State champs", 60)

    cal = client.get("/calendar?year=2026&month=8").text
    assert "cal-race-demoted" in cal
    # The stored row is untouched - only the badge changed.
    assert [r["priority"] for r in db.list_race_dates(uid)] == ["A", "A"]
    assert "planned as a B race" in cal
    assert f"A race on {A_RACE}" in cal


# ------------------------------------------------------- the nightly notice
def _seed_plan(uid, weeks=4):
    """A stored plan with a recipe, the way /generate/plan creates one."""
    from wattracker.prescribe import zwo

    recipe = reflow.build_recipe([0, 2, 4, 5], 8.0, 2)
    generated = planmod.generate_plan(
        "Race Plan", MONDAY, weeks, recipe["days_of_week"],
        recipe["hours_per_week"], recipe["hit_days_per_week"],
        races=db.list_race_dates(uid),
    )
    plan_id = db.create_plan(uid, "Race Plan", generated["start_date"],
                             generated["weeks"], model=generated["model"],
                             recipe=recipe)
    for w in generated["workouts"]:
        db.add_plan_workout(plan_id, uid, w["date"], w["name"], w["type"],
                            w["duration_s"], w["tss"],
                            zwo.zwo_string(w["session"]),
                            variant=w.get("variant"), origin=reflow.GENERATED)
    return plan_id


def test_the_overnight_notice_names_a_demotion(user_id):
    plan_id = _seed_plan(user_id)
    db.add_race_date(user_id, A_RACE, "A", "Nationals", 60)
    db.add_race_date(user_id, LATE_A, "A", "State champs", 60)

    result = reflow.reflow_plan(user_id, plan_id, now=NOW, notify=True)
    assert len(result["race_conflicts"]) == 1

    message = db.get_plan(user_id, plan_id)["reflow_notice"]["message"]
    # The counts sentence is untouched...
    assert "overnight" in message and "measured fitness" in message
    # ...and the demotion is named, with the race that outranked it.
    assert LATE_A in message and A_RACE in message
    assert "planned as a B race" in message
    assert "State champs" in message


def test_the_notice_stays_count_only_when_nothing_was_demoted(user_id):
    plan_id = _seed_plan(user_id)
    db.add_race_date(user_id, A_RACE, "A", "Nationals", 60)
    reflow.reflow_plan(user_id, plan_id, now=NOW, notify=True)
    message = db.get_plan(user_id, plan_id)["reflow_notice"]["message"]
    assert "planned as a B race" not in message
