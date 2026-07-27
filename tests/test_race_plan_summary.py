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


# What counts as "past" - and therefore as a row reflow may not rewrite - is
# the whole subject here, so the clock is pinned rather than left to the day
# the suite happens to run.
TODAY = dt.date(2026, 7, 27)
NOW_UTC = dt.datetime(2026, 7, 27, 0, 10)


@pytest.fixture(autouse=True)
def frozen_clock(monkeypatch):
    import wattracker.server as servermod
    from wattracker.prescribe import reflow as reflowmod

    monkeypatch.setattr(servermod, "utc_today", lambda: TODAY)
    monkeypatch.setattr(reflowmod, "utc_now", lambda: NOW_UTC)


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


def _sentences(html):
    """The block as the rider reads it: tags stripped, whitespace collapsed."""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()


def _seed_plan(uid, weeks=WEEKS, active=True, races=None, name="Race Plan",
               start=MONDAY):
    """A stored plan with a recipe, generated around ``races`` (default: the
    rider's current races, the way /generate/plan does it)."""
    recipe = reflow.build_recipe(RIDE_DAYS, HOURS, 2)
    generated = planmod.generate_plan(
        name, start, weeks, recipe["days_of_week"], recipe["hours_per_week"],
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


def _describe(uid, plan_id, today=None, start=MONDAY):
    """Exactly what the plan page renders from: the stored rows are the
    evidence for every claim, so they are always passed."""
    plan = db.get_plan(uid, plan_id)
    return planmod.describe_races(
        db.list_race_dates(uid), plan["name"], start, plan["weeks"],
        days_of_week=RIDE_DAYS, hours_per_week=HOURS, hit_days_per_week=2,
        stored=_stored(uid, plan_id), today=today,
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

    assert r["shorter"]
    # Every date handed to the rider is a date whose stored session really did
    # get shorter, and every number beside it comes off the row itself.
    for x in r["shorter"]:
        assert stored[x["date"]]["duration_s"] < base[x["date"]]["duration_s"]
        assert x["minutes"] == round(stored[x["date"]]["duration_s"] / 60)
        assert x["was"] == round(base[x["date"]]["duration_s"] / 60)
    # No date that really shortened is left out, and none precedes the plan.
    tapered = [d for d in stored
               if d < A_RACE and stored[d]["duration_s"] < base[d]["duration_s"]]
    assert [x["date"] for x in r["shorter"]] == sorted(tapered)
    assert r["shorter"][0]["date"] >= MONDAY.isoformat()


def test_no_taper_is_claimed_when_the_fortnight_before_is_outside_the_plan(user_id):
    """An A race on the plan's first day tapers nothing - the days it would
    have cut are all before the plan starts."""
    db.add_race_date(user_id, MONDAY.isoformat(), "A", "Opener", 60)
    plan_id = _seed_plan(user_id)
    r = _by_date(_describe(user_id, plan_id), MONDAY.isoformat())
    assert r["shorter"] == []
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
    assert r["shorter"] and r["shorter"][0]["date"] <= "2026-08-30"
    stored, base = _stored(user_id, plan_id), _raceless_rows(user_id)
    first = r["shorter"][0]["date"]
    assert stored[first]["duration_s"] < base[first]["duration_s"]


def test_a_race_the_plan_does_nothing_about_is_dropped(user_id):
    """Far past the plan, with no reach into it: not this plan's business."""
    db.add_race_date(user_id, "2026-12-05", "A", "Winter champs", 60)
    plan_id = _seed_plan(user_id)
    assert _describe(user_id, plan_id) == []


# ------------------------------------------ what landed vs what is still owed
def test_a_plan_never_recomputed_for_a_race_claims_nothing(user_id):
    """Only the ACTIVE plan is reflowed when a race changes, so another plan's
    rows do not contain the race at all - and must not claim to."""
    plan_id = _seed_plan(user_id, active=False, races=[])   # born race-blind
    db.add_race_date(user_id, A_RACE, "A", "Nationals", 120)

    r = _by_date(_describe(user_id, plan_id), A_RACE)
    assert r["affects"] == []
    assert r["shorter"] == [] and r["displaces_workout"] is False
    assert r["recovery_dates"] == []
    # Everything the race wants is still owed, and nothing is explained away.
    assert r["pending"] and r["left_alone"] == []
    # The stored plan really does still have a full session on race day.
    stored, base = _stored(user_id, plan_id), _raceless_rows(user_id)
    assert stored[A_RACE]["duration_s"] == base[A_RACE]["duration_s"]


def test_an_unrecomputed_plan_page_describes_no_effects(client):
    uid = _register(client)
    client.post("/generate/plan", data=PLAN_FORM)          # race-blind plan
    plan_id = db.list_plans(uid)[0]["id"]
    db.add_race_date(uid, A_RACE, "A", "Nationals", 120)

    section = _races_section(client.get(f"/plan?plan_id={plan_id}").text)
    assert "does not reflect this race yet" in section
    assert "Shorter sessions before the race" not in section
    assert "No workout is scheduled on race day" not in section


def test_the_effects_are_described_once_the_plan_is_recomputed(user_id):
    plan_id = _seed_plan(user_id, races=[])
    db.add_race_date(user_id, A_RACE, "A", "Nationals", 120)
    assert _by_date(_describe(user_id, plan_id), A_RACE)["affects"] == []

    reflow.reflow_plan(user_id, plan_id, now=dt.datetime(2026, 8, 1, 9, 0))
    r = _by_date(_describe(user_id, plan_id), A_RACE)
    assert r["pending"] == []
    assert r["shorter"] and r["displaces_workout"] is True
    assert A_RACE not in _stored(user_id, plan_id)


def test_a_completed_row_inside_a_taper_is_explained_not_suppressed(user_id):
    """Reflow never rewrites a completed row. The rest of the taper still
    landed and is still described; the completed day is explained."""
    plan_id = _seed_plan(user_id, races=[])
    db.add_race_date(user_id, A_RACE, "A", "Nationals", 120)
    locked = "2026-08-14"          # an easy day inside the taper window
    db.mark_plan_workout_completed(
        user_id, _stored(user_id, plan_id)[locked]["id"], 999, locked)
    reflow.reflow_plan(user_id, plan_id, now=dt.datetime(2026, 8, 1, 9, 0))

    stored, base = _stored(user_id, plan_id), _raceless_rows(user_id)
    assert stored[locked]["duration_s"] == base[locked]["duration_s"]  # untouched
    r = _by_date(_describe(user_id, plan_id), A_RACE)
    assert locked in r["left_alone"]
    assert locked not in r["affects"]
    assert r["pending"] == []
    # The dates that DID taper are still described, and every one of them is a
    # date whose stored session really is shorter than the baseline.
    assert r["shorter"] and locked not in [x["date"] for x in r["shorter"]]
    assert all(stored[d]["duration_s"] < base[d]["duration_s"]
               for d in r["affects"] if d in stored and d < A_RACE)


def test_a_partly_applied_race_describes_the_part_that_landed(user_id):
    """The verifier's counterexample: a plan already under way, an A race two
    weeks out, so the first days of the taper are in the past. Four rows really
    changed and all four must be described."""
    start = dt.date(2026, 7, 20)          # already started
    today = "2026-07-27"
    plan_id = _seed_plan(user_id, races=[], start=start)
    db.add_race_date(user_id, "2026-08-01", "A", "Nats", 120)
    reflow.reflow_plan(user_id, plan_id, now=NOW_UTC)

    stored = _stored(user_id, plan_id)
    base = {w["date"]: w for w in planmod.generate_plan(
        "Race Plan", start, WEEKS, RIDE_DAYS, HOURS, 2)["workouts"]}
    changed = sorted(d for d in set(base) | set(stored)
                     if (d in stored) != (d in base)
                     or (d in stored and d in base
                         and (stored[d]["type"], stored[d]["duration_s"])
                         != (base[d]["type"], base[d]["duration_s"])))
    assert changed == ["2026-07-31", "2026-08-01", "2026-08-03", "2026-08-05"]

    r = _by_date(_describe(user_id, plan_id, today=TODAY.isoformat(),
                           start=start),
                 "2026-08-01")
    # Every changed row is described, and nothing else is.
    assert r["affects"] == changed
    assert [x["date"] for x in r["shorter"]] == ["2026-07-31"]  # the row that shrank
    assert stored["2026-07-31"]["duration_s"] < base["2026-07-31"]["duration_s"]
    assert r["displaces_workout"] is True and "2026-08-01" not in stored
    assert r["recovery_dates"] == ["2026-08-03", "2026-08-05"]
    assert all(stored[d]["type"] == "recovery" for d in r["recovery_dates"])
    # The taper days that fall in the past were left alone, and say so.
    assert r["left_alone"] == ["2026-07-20", "2026-07-24", "2026-07-27"]
    assert all(d <= today for d in r["left_alone"])
    assert r["pending"] == []


def test_the_partly_applied_race_reads_true_on_the_page(client):
    """The same case through the real routes, rendered."""
    uid = _register(client)
    form = dict(PLAN_FORM, start_date="2026-07-20")
    client.post("/generate/plan", data=form)
    plan_id = db.list_plans(uid)[0]["id"]
    client.post("/race/add", data={"date": "2026-08-01", "priority": "A",
                                   "name": "Nats", "duration_min": "120"},
                follow_redirects=False)

    read = _sentences(_races_section(client.get(f"/plan?plan_id={plan_id}").text))
    assert "Shorter sessions before the race: 2026-07-31" in read
    assert "No workout is scheduled on race day" in read
    assert "Easy sessions afterwards on 2026-08-03, 2026-08-05" in read
    assert ("2026-07-20, 2026-07-24, 2026-07-27 stayed as they were: past and "
            "completed workouts are never rewritten") in read
    assert "does not reflect this race yet" not in read
    # And the stored rows back every word of it.
    stored = _stored(uid, plan_id)
    assert "2026-08-01" not in stored
    assert stored["2026-08-03"]["type"] == "recovery"


def test_a_claim_needs_attribution_as_well_as_evidence(user_id):
    """A stored row that differs from the baseline for a reason the race does
    not predict is never claimed - which is what keeps this honest when the
    baseline drifts (a re-measured profile moves every duration)."""
    plan_id = _seed_plan(user_id, races=[])
    db.add_race_date(user_id, A_RACE, "A", "Nationals", 120)
    reflow.reflow_plan(user_id, plan_id, now=dt.datetime(2026, 8, 1, 9, 0))
    # Shorten a day the race has no opinion about at all.
    untouched = "2026-08-28"
    row = _stored(user_id, plan_id)[untouched]
    db.replace_plan_workout_content(
        user_id, row["id"], row["name"], row["type"], 600, row["tss"], "<x/>",
        "2026-08-01", variant=row.get("variant"))

    r = _by_date(_describe(user_id, plan_id), A_RACE)
    stored, base = _stored(user_id, plan_id), _raceless_rows(user_id)
    assert stored[untouched]["duration_s"] < base[untouched]["duration_s"]
    assert untouched not in r["affects"]      # evidence without attribution


# --------------------------------------- the taper is dates, not a shape claim
def _long_plan(client, weeks, races, days=RIDE_DAYS):
    """A plan created through the real route, around ``races`` (date, prio,
    name, minutes). Returns (uid, plan_id, stored rows, raceless baseline)."""
    uid = _register(client)
    for date, prio, name, minutes in races:
        db.add_race_date(uid, date, prio, name, minutes)
    client.post("/generate/plan", data=dict(PLAN_FORM, weeks=str(weeks),
                                            days=[str(d) for d in days]))
    plan_id = db.list_plans(uid)[0]["id"]
    baseline = {w["date"]: w for w in planmod.generate_plan(
        "Race Plan", MONDAY, weeks, days, HOURS, 2)["workouts"]}
    return uid, plan_id, _stored(uid, plan_id), baseline


def test_a_taper_never_claims_the_previous_races_recovery_days(client):
    """Race Two's fortnight opens on race One's post-race easy days. They are
    shorter than the baseline, but they are recovery, not a taper - One reports
    them itself, and Two must not claim them."""
    uid, plan_id, stored, base = _long_plan(
        client, 12, [("2026-08-22", "A", "One", 300),
                     ("2026-09-12", "A", "Two", 120)])
    described = _describe(uid, plan_id, today=TODAY.isoformat())
    one = _by_date(described, "2026-08-22")
    two = _by_date(described, "2026-09-12")

    for d in ("2026-08-29", "2026-08-31"):
        # Stored as recovery, against an interval day in the raceless plan.
        assert stored[d]["type"] == "recovery"
        assert base[d]["type"] in planmod.HARD_KINDS
        assert d in one["recovery_dates"]     # race One's, and it says so
        assert d not in two["affects"]        # never race Two's taper
    assert [x["date"] for x in two["shorter"]][0] == "2026-09-04"
    # Every number Two reports comes off a row that really is shorter.
    for x in two["shorter"]:
        assert stored[x["date"]]["duration_s"] < base[x["date"]]["duration_s"]


def test_every_rendered_taper_number_comes_off_a_stored_row(client):
    """The block quantifies nothing about the taper - it lists the dates that
    are actually shorter. Each pair of numbers must match its row exactly."""
    uid, plan_id, stored, base = _long_plan(
        client, 8, [("2026-09-05", "A", "Nats", 120)])
    read = _sentences(_races_section(client.get(f"/plan?plan_id={plan_id}").text))

    listed = re.findall(r"(\d{4}-\d\d-\d\d) (\d+) min \(was (\d+)\)", read)
    assert listed
    for date, minutes, was in listed:
        assert int(minutes) == round(stored[date]["duration_s"] / 60)
        assert int(was) == round(base[date]["duration_s"] / 60)
        assert stored[date]["duration_s"] < base[date]["duration_s"]
    # Nothing that really shortened before the race is left unmentioned.
    really = sorted(d for d in stored if "2026-08-22" <= d < "2026-09-05"
                    and stored[d]["duration_s"] < base[d]["duration_s"])
    assert [d for d, _, _ in listed] == really
    # And none of the discarded shape claims came back.
    for gone in ("shorter still", "intensity hold", "Taper from"):
        assert gone not in read


def test_a_long_taper_list_is_capped_and_says_how_many_more(client):
    """Six dates, then a count - a wall of dates is not a fact anyone reads.
    Six ride days a week gives a fortnight with more shortened days than that."""
    days = [0, 1, 2, 3, 4, 5]
    uid, plan_id, stored, base = _long_plan(
        client, 8, [("2026-09-05", "A", "Nats", 120)], days=days)
    r = planmod.describe_races(
        db.list_race_dates(uid), "Race Plan", MONDAY, 8, days_of_week=days,
        hours_per_week=HOURS, hit_days_per_week=2, stored=stored,
        today=TODAY.isoformat())[0]
    assert len(r["shorter"]) > 6
    read = _sentences(_races_section(client.get(f"/plan?plan_id={plan_id}").text))
    shown = re.findall(r"\d{4}-\d\d-\d\d \d+ min \(was", read)
    assert len(shown) == min(6, len(r["shorter"]))
    if len(r["shorter"]) > 6:
        assert f"and {len(r['shorter']) - 6} more" in read


def test_two_a_races_on_one_date_name_each_other_not_none(client):
    """The resolver demotes the second of two same-date A races. Re-deriving
    the demoting race as 'the latest A race strictly before this one' found
    nothing and rendered the string 'None'."""
    uid = _register(client)
    db.add_race_date(uid, "2026-09-05", "A", "Morning crit", 60)
    db.add_race_date(uid, "2026-09-05", "A", "Afternoon road race", 120)
    client.post("/generate/plan", data=dict(PLAN_FORM, weeks="8"))
    plan_id = db.list_plans(uid)[0]["id"]

    described = _describe(uid, plan_id, today=TODAY.isoformat())
    demoted = [r for r in described if r["demoted"]]
    assert len(demoted) == 1
    assert demoted[0]["conflicts_with"] == "2026-09-05"   # not None

    section = _races_section(client.get(f"/plan?plan_id={plan_id}").text)
    calendar = client.get("/calendar?year=2026&month=9").text
    assert "None" not in section
    assert "your other" in _sentences(section)
    assert "None" not in calendar
    assert "cal-race-demoted" in calendar
    # The stored rows keep both at A - the demotion is a planning decision.
    assert [r["priority"] for r in db.list_race_dates(uid)] == ["A", "A"]


def test_a_date_with_no_row_cannot_have_stayed_as_it_was(user_id):
    """`left_alone` explains rows the reflow left in place. A date that has no
    row has nothing that stayed, whatever its position in time."""
    plan_id = _seed_plan(user_id, races=[])
    db.add_race_date(user_id, A_RACE, "A", "Nationals", 120)
    stored = _stored(user_id, plan_id)
    gone = "2026-08-14"          # a taper-window row, deleted outright
    db.delete_generated_plan_workout(user_id, stored[gone]["id"], "2026-08-01")

    r = _by_date(_describe(user_id, plan_id, today="2026-08-20"), A_RACE)
    assert gone not in r["left_alone"]
    assert all(d in _stored(user_id, plan_id) for d in r["left_alone"])


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
    assert late["shorter"] == []
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
