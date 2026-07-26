"""The plan says what it DID about each race - and only what it really did.

A demoted A race - one inside an earlier A race's taper - used to be silently
planned as a B race: the rider marked two A races, one stopped getting a taper,
and nothing anywhere said so. These tests pin the three places that now speak:
the plan summary, the overnight notice, and the calendar badge.

Every claim about effects is checked against the rows the plan actually STORES,
never against the describing code's own output. The description is established
by diffing a with-races generation against a raceless one, so "tapered" means
the stored session really is shorter, not that a taper rule would have applied.
"""
import datetime as dt
import re

import pytest

pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from wattracker import db  # noqa: E402
from wattracker.prescribe import plan as planmod, reflow, zwo  # noqa: E402
from wattracker.server import create_app  # noqa: E402

MONDAY = dt.date(2026, 8, 3)
NOW = dt.datetime(2026, 8, 5, 9, 0)   # a Wednesday inside week 1
RIDE_DAYS = [0, 2, 4, 5]              # Mon/Wed/Fri/Sat
HOURS = 8.0
WEEKS = 4

PLAN_FORM = {
    "name": "Race Plan",
    "weeks": str(WEEKS),
    "hours_per_week": str(HOURS),
    "hit_days_per_week": "2",
    "start_date": MONDAY.isoformat(),
    "days": [str(d) for d in RIDE_DAYS],
}

A_RACE = "2026-08-17"       # Monday of week 3
# A Thursday whose ride-day neighbours differ in the raceless plan: Wed is a
# VO2max day (the race eases it), Fri is already endurance (nothing to ease).
B_RACE = "2026-08-06"
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


def _seed_plan(uid, weeks=WEEKS, active=True, races=None, name="Race Plan"):
    """A stored plan with a recipe, generated around ``races`` (default: the
    rider's current races, the way /generate/plan does it)."""
    recipe = reflow.build_recipe(RIDE_DAYS, HOURS, 2)
    generated = planmod.generate_plan(
        name, MONDAY, weeks, recipe["days_of_week"], recipe["hours_per_week"],
        recipe["hit_days_per_week"],
        races=db.list_race_dates(uid) if races is None else races,
    )
    plan_id = db.create_plan(uid, name, generated["start_date"],
                             generated["weeks"], model=generated["model"],
                             recipe=recipe)
    for w in generated["workouts"]:
        db.add_plan_workout(plan_id, uid, w["date"], w["name"], w["type"],
                            w["duration_s"], w["tss"],
                            zwo.zwo_string(w["session"]),
                            variant=w.get("variant"), origin=reflow.GENERATED)
    if active:
        db.set_active_plan(uid, plan_id)
    return plan_id


def _stored(uid, plan_id):
    return {w["date"]: w for w in db.plan_workouts_for_plan(uid, plan_id)}


def _describe(uid, plan_id):
    """Exactly what the plan page renders from, including the stored-row check."""
    plan = db.get_plan(uid, plan_id)
    return planmod.describe_races(
        db.list_race_dates(uid), plan["name"], MONDAY, plan["weeks"],
        days_of_week=RIDE_DAYS, hours_per_week=HOURS, hit_days_per_week=2,
        stored=_stored(uid, plan_id),
    )


def _raceless_rows(uid):
    """What this recipe generates with no races at all - the baseline every
    claim about an effect is measured against."""
    generated = planmod.generate_plan("Race Plan", MONDAY, WEEKS, RIDE_DAYS,
                                      HOURS, 2, races=None)
    return {w["date"]: w for w in generated["workouts"]}


def _by_date(described, date):
    return [r for r in described if r["date"] == date][0]


# ------------------------------------------------- effects are facts, not rules
def test_a_taper_is_claimed_only_where_the_stored_session_is_shorter(user_id):
    db.add_race_date(user_id, A_RACE, "A", "Nationals", 120)
    plan_id = _seed_plan(user_id)
    r = _by_date(_describe(user_id, plan_id), A_RACE)
    stored, base = _stored(user_id, plan_id), _raceless_rows(user_id)

    assert r["taper_from"] is not None
    # The date handed to the rider is a date whose stored session really did
    # get shorter, and it is the FIRST such date - not a rule-derived one.
    assert stored[r["taper_from"]]["duration_s"] < base[r["taper_from"]]["duration_s"]
    tapered = [d for d in stored
               if d < A_RACE and stored[d]["duration_s"] < base[d]["duration_s"]]
    assert r["taper_from"] == min(tapered)
    assert r["taper_from"] >= MONDAY.isoformat()


def test_no_taper_is_claimed_when_the_fortnight_before_is_outside_the_plan(user_id):
    """An A race on the plan's first day tapers nothing - the days it would
    have cut are all before the plan starts."""
    db.add_race_date(user_id, MONDAY.isoformat(), "A", "Opener", 60)
    plan_id = _seed_plan(user_id)
    r = _by_date(_describe(user_id, plan_id), MONDAY.isoformat())
    assert r["taper_from"] is None
    assert r["taper_hard_from"] is None
    # It still displaces the session that would have been ridden that day.
    assert r["displaces_workout"] is True
    assert MONDAY.isoformat() not in _stored(user_id, plan_id)


def test_a_b_race_names_only_neighbours_whose_kind_actually_changed(user_id):
    """A neighbouring day that was already endurance is not 'eased'."""
    db.add_race_date(user_id, B_RACE, "B", "Club crit", 60)
    plan_id = _seed_plan(user_id)
    r = _by_date(_describe(user_id, plan_id), B_RACE)
    stored, base = _stored(user_id, plan_id), _raceless_rows(user_id)

    for d in ("2026-08-05", "2026-08-07"):
        changed = stored[d]["type"] != base[d]["type"]
        assert (d in r["easy_dates"]) is changed
    # Concretely: the Wednesday was VO2max and really did drop to endurance;
    # the Friday was already endurance and is byte-identical to the baseline.
    assert r["easy_dates"] == ["2026-08-05"]
    assert base["2026-08-05"]["type"] == "vo2max"
    assert stored["2026-08-05"]["type"] == "endurance"
    assert base["2026-08-07"]["type"] == stored["2026-08-07"]["type"] == "endurance"
    assert stored["2026-08-07"]["duration_s"] == base["2026-08-07"]["duration_s"]


def test_recovery_dates_are_the_days_that_really_became_recovery(user_id):
    db.add_race_date(user_id, A_RACE, "A", "Nationals", 180)
    plan_id = _seed_plan(user_id)
    r = _by_date(_describe(user_id, plan_id), A_RACE)
    stored = _stored(user_id, plan_id)

    assert r["recovery_dates"]
    assert all(stored[d]["type"] == "recovery" for d in r["recovery_dates"])
    # Nothing that became recovery is left out (deriving the count from the
    # race's duration used to drop one date per intervening race).
    became = sorted(d for d in stored
                    if d > A_RACE and stored[d]["type"] == "recovery")
    assert r["recovery_dates"] == became


def test_a_race_just_outside_the_plan_that_cuts_it_is_still_described(user_id):
    """Its taper reaches inside, so the plan does have something to say."""
    after_end = "2026-09-03"        # the plan's last day is 2026-08-30
    db.add_race_date(user_id, after_end, "A", "Late nationals", 60)
    plan_id = _seed_plan(user_id)
    described = _describe(user_id, plan_id)
    assert [r["date"] for r in described] == [after_end]
    r = described[0]
    assert r["outside_plan"] is True
    assert r["taper_from"] and r["taper_from"] <= "2026-08-30"
    stored, base = _stored(user_id, plan_id), _raceless_rows(user_id)
    assert stored[r["taper_from"]]["duration_s"] < base[r["taper_from"]]["duration_s"]


def test_a_race_the_plan_does_nothing_about_is_dropped(user_id):
    """Far past the plan, with no reach into it: not this plan's business."""
    db.add_race_date(user_id, "2026-12-05", "A", "Winter champs", 60)
    plan_id = _seed_plan(user_id)
    assert _describe(user_id, plan_id) == []


# ------------------------------------------------------------------ staleness
def test_a_plan_never_recomputed_for_a_race_says_so(user_id):
    """Only the ACTIVE plan is reflowed when a race changes, so another plan's
    rows do not contain the race at all - and must not claim to."""
    plan_id = _seed_plan(user_id, active=False, races=[])   # born race-blind
    db.add_race_date(user_id, A_RACE, "A", "Nationals", 120)

    r = _by_date(_describe(user_id, plan_id), A_RACE)
    assert r["stale"] is True
    # The stored plan really does still have a full session on race day.
    stored = _stored(user_id, plan_id)
    assert A_RACE in stored
    assert stored[A_RACE]["duration_s"] == _raceless_rows(user_id)[A_RACE]["duration_s"]


def test_a_stale_plan_page_describes_no_effects(client):
    uid = _register(client)
    client.post("/generate/plan", data=PLAN_FORM)          # race-blind plan
    plan_id = db.list_plans(uid)[0]["id"]
    db.add_race_date(uid, A_RACE, "A", "Nationals", 120)

    section = _races_section(client.get(f"/plan?plan_id={plan_id}").text)
    assert "has not been recomputed for this race" in section
    assert "Taper from" not in section
    assert "No workout is scheduled on race day" not in section


def test_the_effects_are_described_once_the_plan_is_recomputed(user_id):
    plan_id = _seed_plan(user_id, races=[])
    db.add_race_date(user_id, A_RACE, "A", "Nationals", 120)
    assert _by_date(_describe(user_id, plan_id), A_RACE)["stale"] is True

    reflow.reflow_plan(user_id, plan_id, now=dt.datetime(2026, 8, 1, 9, 0))
    r = _by_date(_describe(user_id, plan_id), A_RACE)
    assert r["stale"] is False
    assert r["taper_from"] and r["displaces_workout"] is True
    assert A_RACE not in _stored(user_id, plan_id)


def test_a_locked_row_reflow_cannot_rewrite_keeps_the_race_stale(user_id):
    """Reflow never rewrites a completed row, so the plan does not contain the
    race's full effect and must not claim it does."""
    plan_id = _seed_plan(user_id, races=[])
    db.add_race_date(user_id, A_RACE, "A", "Nationals", 120)
    # Complete an easy day inside the taper - reflow will refuse to shorten it.
    locked = "2026-08-14"
    db.mark_plan_workout_completed(
        user_id, _stored(user_id, plan_id)[locked]["id"], 999, locked)
    reflow.reflow_plan(user_id, plan_id, now=dt.datetime(2026, 8, 1, 9, 0))

    stored, base = _stored(user_id, plan_id), _raceless_rows(user_id)
    assert stored[locked]["duration_s"] == base[locked]["duration_s"]  # untouched
    assert _by_date(_describe(user_id, plan_id), A_RACE)["stale"] is True


# --------------------------------------------------------------- the demotion
def test_a_demotion_is_described_with_its_reason(user_id):
    db.add_race_date(user_id, A_RACE, "A", "Nationals", 60)
    db.add_race_date(user_id, LATE_A, "A", "State champs", 60)
    plan_id = _seed_plan(user_id)

    late = _by_date(_describe(user_id, plan_id), LATE_A)
    assert late["priority"] == "B"          # EFFECTIVE, not stored
    assert late["demoted"] is True
    assert late["conflicts_with"] == A_RACE
    assert late["separation_days"] == planmod.A_RACE_SEPARATION_DAYS
    early = _by_date(_describe(user_id, plan_id), A_RACE)
    assert (early["priority"], early["demoted"]) == ("A", False)
    # The stored rows agree: the later race got no taper of its own.
    stored, base = _stored(user_id, plan_id), _raceless_rows(user_id)
    assert stored["2026-08-22"]["duration_s"] == base["2026-08-22"]["duration_s"]
    assert late["taper_from"] is None
    # ...and nothing was written back to the race rows.
    assert [r["priority"] for r in db.list_race_dates(user_id)] == ["A", "A"]


def test_a_conflicting_race_outside_the_window_still_demotes(user_id):
    """Resolution runs over the whole race calendar before any windowing: the
    A race that causes the demotion may sit before the plan even starts."""
    db.add_race_date(user_id, "2026-07-27", "A", "Early", 60)   # pre-plan
    db.add_race_date(user_id, "2026-08-05", "A", "Second", 60)
    plan_id = _seed_plan(user_id)

    inside = _by_date(_describe(user_id, plan_id), "2026-08-05")
    assert inside["priority"] == "B"
    assert inside["conflicts_with"] == "2026-07-27"


# ----------------------------------------------------------- the plan page
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
    assert "has not been recomputed" not in section  # it was born race-aware


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


def test_the_calendar_and_the_flash_use_one_wording(client):
    uid = _register(client)
    db.add_race_date(uid, A_RACE, "A", "Nationals", 60)
    r = client.post("/race/add", data={"date": LATE_A, "priority": "A",
                                       "name": "State champs",
                                       "duration_min": "60"},
                    follow_redirects=True)
    assert f"within {planmod.A_RACE_SEPARATION_DAYS} days" in r.text
    assert "within three weeks" not in r.text


# ------------------------------------------------------- the nightly notice
def test_the_overnight_notice_names_a_demotion(user_id):
    plan_id = _seed_plan(user_id, races=[])
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
    plan_id = _seed_plan(user_id, races=[])
    db.add_race_date(user_id, A_RACE, "A", "Nationals", 60)
    reflow.reflow_plan(user_id, plan_id, now=NOW, notify=True)
    message = db.get_plan(user_id, plan_id)["reflow_notice"]["message"]
    assert "planned as a B race" not in message


def test_the_notice_never_names_a_demotion_the_plan_is_not_about(user_id):
    """The generator resolves the whole race calendar; this plan ends in August
    and must not announce a demotion between two December races."""
    plan_id = _seed_plan(user_id, races=[])
    db.add_race_date(user_id, A_RACE, "A", "Nationals", 60)   # makes it change
    db.add_race_date(user_id, "2026-12-05", "A", "Winter", 60)
    db.add_race_date(user_id, "2026-12-12", "A", "Winter II", 60)

    result = reflow.reflow_plan(user_id, plan_id, now=NOW, notify=True)
    assert result["race_conflicts"] == []
    message = db.get_plan(user_id, plan_id)["reflow_notice"]["message"]
    assert "Winter II" not in message
    assert "planned as a B race" not in message
    # ...and the summary block for the same plan says nothing about them either.
    assert [r["date"] for r in _describe(user_id, plan_id)] == [A_RACE]
