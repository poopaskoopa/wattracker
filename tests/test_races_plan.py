"""Tests for planned race dates: generation, reflow, adaptation and export.

This covers `race_dates` (races the rider INTENDS to do, which the plan bends
around). `race_results` (past results cached from ZwiftPower) is a different
table with a different lifecycle and is covered by tests/test_races.py.
"""
import datetime as dt
import os
import sqlite3

import pytest

from wattracker import db, exporter
from wattracker.ingest import importer
from wattracker.prescribe import adapt, plan as planmod, reflow, zwo

MONDAY = dt.date(2026, 7, 6)          # plan starts here
NOW = dt.datetime(2026, 7, 8, 9, 0)   # a Wednesday inside week 1

RIDE_DAYS = [0, 2, 4, 5]              # Mon/Wed/Fri/Sat
HOURS = 8.0


def _seed_plan(user_id, recipe=None, name="Base", start=MONDAY, weeks=10,
               active=True):
    """Create a plan the way the /generate/plan route does: rows + recipe."""
    recipe = recipe or reflow.build_recipe(RIDE_DAYS, HOURS, 1)
    generated = planmod.generate_plan(
        name, start, weeks, recipe["days_of_week"], recipe["hours_per_week"],
        recipe["hit_days_per_week"], hard_days=recipe["hard_days"] or None,
        model=recipe["model"],
    )
    plan_id = db.create_plan(
        user_id, name, generated["start_date"], generated["weeks"],
        model=generated["model"], recipe=recipe,
    )
    for w in generated["workouts"]:
        db.add_plan_workout(
            plan_id, user_id, w["date"], w["name"], w["type"], w["duration_s"],
            w["tss"], zwo.zwo_string(w["session"]), variant=w.get("variant"),
            origin=reflow.GENERATED, export_ftp=importer.current_ftp(user_id),
        )
    if active:
        db.set_active_plan(user_id, plan_id)
    return plan_id


def _gen(races=None, weeks=10, **kw):
    return planmod.generate_plan(
        "P", MONDAY, weeks, RIDE_DAYS, HOURS, 1, races=races, **kw)


def _by_date(generated):
    return {w["date"]: w for w in generated["workouts"]}


def _minutes(w):
    return round(w["duration_s"] / 60)


# Everything a plan_workouts row stores about a session. Comparing on `type`
# alone hides a whole class of bug: a variant rotation knocked one step out of
# sync rewrites the name, variant and TSS of every later session while leaving
# every type and duration untouched.
_IDENTITY = ("name", "type", "variant", "duration_s", "tss")


def _identity(generated):
    return {w["date"]: tuple(w[k] for k in _IDENTITY)
            for w in generated["workouts"]}


def _changed_dates(base, raced):
    """Dates whose session is not byte-identical between two generated plans."""
    a, b = _identity(base), _identity(raced)
    return {d for d in set(a) | set(b) if a.get(d) != b.get(d)}


def _rows(user_id, plan_id):
    return db.plan_workouts_for_plan(user_id, plan_id, include_zwo=True)


def _weekly_minutes(generated):
    """Total planned minutes per Mon-Sun week, keyed by the week's Monday."""
    out = {}
    for w in generated["workouts"]:
        d = dt.date.fromisoformat(w["date"])
        monday = (d - dt.timedelta(days=d.weekday())).isoformat()
        out[monday] = out.get(monday, 0) + w["duration_s"] / 60.0
    return out


# ------------------------------------------------------------- race day
@pytest.mark.parametrize("priority", ["A", "B"])
def test_no_workout_is_generated_on_a_race_date(priority):
    race_day = "2026-08-10"  # a Monday, a ride day
    assert race_day in _by_date(_gen())
    generated = _gen([{"id": 1, "date": race_day, "priority": priority}])
    assert race_day not in _by_date(generated)


def test_a_race_on_a_rest_day_removes_nothing():
    """Not every race lands on a ride day; the rest of the plan is unaffected."""
    rest_day = "2026-08-11"  # a Tuesday - never a ride day here
    generated = _gen([{"id": 1, "date": rest_day, "priority": "B"}])
    assert rest_day not in _by_date(generated)
    # The hard day either side is still softened, but nothing else moved.
    base, raced = _by_date(_gen()), _by_date(generated)
    assert set(base) == set(raced)


# ------------------------------------------------------------- B races
def test_b_race_softens_the_hard_day_after_it():
    # Fri 2026-08-14 is a threshold day; a Thursday race sits right before it.
    base = _by_date(_gen())
    hard_after = "2026-08-14"
    assert base[hard_after]["type"] in planmod.HARD_KINDS

    raced = _by_date(_gen([{"id": 1, "date": "2026-08-13", "priority": "B"}]))
    assert raced[hard_after]["type"] == "endurance"
    assert raced[hard_after]["duration_s"] == base[hard_after]["duration_s"]

    # A B race touches exactly one day here and NOTHING else - not the type,
    # not the duration, and not the variant/name/TSS either. Comparing full
    # row identity is the point: a rotation that drifts one step preserves
    # every type and duration in the plan while renaming half the sessions.
    assert _changed_dates(_gen(), _gen([{"id": 1, "date": "2026-08-13",
                                         "priority": "B"}])) == {hard_after}


def test_b_race_softens_the_hard_day_before_it():
    # Sat 2026-08-15 is a sweet-spot day; a Sunday race sits right after it.
    base = _by_date(_gen())
    hard_before = "2026-08-15"
    assert base[hard_before]["type"] in planmod.HARD_KINDS

    raced = _by_date(_gen([{"id": 1, "date": "2026-08-16", "priority": "B"}]))
    assert raced[hard_before]["type"] == "endurance"
    assert raced[hard_before]["duration_s"] == base[hard_before]["duration_s"]


def test_b_race_leaves_an_easy_neighbour_alone():
    """The rule targets INTERVAL days; an endurance day next to a B race stays."""
    base = _by_date(_gen())
    race = "2026-08-11"  # Tuesday: Mon 10th and Wed 12th are its neighbours
    monday = "2026-08-10"
    assert base[monday]["type"] == "endurance"
    raced = _by_date(_gen([{"id": 1, "date": race, "priority": "B"}]))
    assert raced[monday]["type"] == "endurance"
    assert raced[monday]["duration_s"] == base[monday]["duration_s"]


def test_b_race_has_no_taper_and_no_recovery():
    base = _by_date(_gen())
    raced = _by_date(_gen([{"id": 1, "date": "2026-08-10", "priority": "B",
                            "duration_min": 300}]))
    for date, w in raced.items():
        if date in ("2026-08-09", "2026-08-11"):
            continue  # neighbours may have been softened
        assert w["duration_s"] == base[date]["duration_s"], date
        assert w["type"] == base[date]["type"], date


# ------------------------------------------------------------- A races
def test_a_race_taper_multipliers_land_on_the_right_dates():
    race_day = dt.date(2026, 8, 16)
    base = _by_date(_gen())
    raced = _by_date(_gen([{"id": 1, "date": race_day.isoformat(),
                            "priority": "A", "duration_min": 60}]))
    for offset in range(1, 15):
        date = (race_day - dt.timedelta(days=offset)).isoformat()
        if date not in raced or date not in base:
            continue
        expected_mult = (planmod.TAPER_NEAR_MULT if offset <= 7
                         else planmod.TAPER_FAR_MULT)
        floor = planmod._feasible_min(
            raced[date]["type"],
            raced[date]["type"] in ("vo2max", "threshold"),
        )
        want = max(base[date]["duration_s"] / 60.0 * expected_mult, floor)
        # +/-1 min: build_workout rounds the requested minutes to whole ones.
        assert abs(_minutes(raced[date]) - want) <= 1, (date, offset)

    # Day -15 is outside the taper entirely.
    outside = (race_day - dt.timedelta(days=15)).isoformat()
    assert raced[outside]["duration_s"] == base[outside]["duration_s"]


def test_a_race_taper_keeps_intensity_intact():
    """Bosquet: cut duration, hold frequency and intensity. Types must not move."""
    race_day = dt.date(2026, 8, 16)
    base = _by_date(_gen())
    raced = _by_date(_gen([{"id": 1, "date": race_day.isoformat(),
                            "priority": "A", "duration_min": 60}]))
    for offset in range(1, 15):
        date = (race_day - dt.timedelta(days=offset)).isoformat()
        if date not in base:
            continue
        assert date in raced, f"{date}: a taper must not drop a training day"
        assert raced[date]["type"] == base[date]["type"], date


def test_a_race_taper_clamps_hard_sessions_to_the_feasible_minimum():
    race_day = dt.date(2026, 8, 16)
    raced = _by_date(_gen([{"id": 1, "date": race_day.isoformat(),
                            "priority": "A", "duration_min": 60}]))
    hard_in_taper = [
        w for d, w in raced.items()
        if w["type"] in ("vo2max", "threshold")
        and (race_day - dt.timedelta(days=7)).isoformat() <= d < race_day.isoformat()
    ]
    assert hard_in_taper, "expected at least one hard day in the final week"
    for w in hard_in_taper:
        # 0.45x of a ~86 min session is 39 min, which build_workout cannot fit;
        # it is clamped up to HIT_MIN_MIN rather than raising.
        assert _minutes(w) == planmod.HIT_MIN_MIN


def test_post_race_recovery_days_scale_with_duration():
    race_day = "2026-08-16"  # a Sunday, not a ride day here

    def recovery_dates(duration_min):
        raced = _by_date(_gen([{"id": 1, "date": race_day, "priority": "A",
                                "duration_min": duration_min}]))
        return sorted(d for d, w in raced.items()
                      if d > race_day and w["type"] == "recovery")

    assert len(recovery_dates(45)) == 2       # floored at 2
    assert len(recovery_dates(150)) == 3      # ceil(2.5h) = 3
    assert len(recovery_dates(600)) == 5      # capped at 5
    assert len(recovery_dates(None)) == 2     # unknown duration -> the floor
    # They are the first ride days AFTER the race, in order.
    assert recovery_dates(150) == ["2026-08-17", "2026-08-19", "2026-08-21"]


def test_post_race_recovery_keeps_the_planned_duration():
    base = _by_date(_gen())
    raced = _by_date(_gen([{"id": 1, "date": "2026-08-16", "priority": "A",
                            "duration_min": 60}]))
    day = "2026-08-17"
    assert raced[day]["type"] == "recovery"
    assert raced[day]["duration_s"] == base[day]["duration_s"]


# ------------------------------------------- the blast radius of a race
# A race must change the days it is ABOUT and nothing else. The rotation
# counters are the trap here: they are driven by the raceless schedule, so
# every day - skipped, substituted or untouched - has to consume exactly one
# slot of its ORIGINAL kind. Getting that wrong desyncs the rotation from the
# race onwards and silently rewrites months of sessions.

LONG_START = dt.date(2026, 3, 2)
LONG_DAYS = [0, 2, 4, 6]


def _long(races=None):
    return planmod.generate_plan("t", LONG_START, 26, LONG_DAYS, 8.0, 2,
                                 model="polarized", races=races)


def test_a_b_race_changes_one_day_of_a_26_week_plan():
    """2026-04-04 is a Saturday - not even a ride day. Only Sunday changes."""
    base = _long()
    raced = _long([{"id": 1, "date": "2026-04-04", "priority": "B"}])

    assert _changed_dates(base, raced) == {"2026-04-05"}
    assert len(base["workouts"]) == len(raced["workouts"]) == 104


def test_an_a_race_changes_only_its_taper_and_recovery_days():
    race = dt.date(2026, 4, 4)
    base = _long()
    raced = _long([{"id": 1, "date": race.isoformat(), "priority": "A",
                    "duration_min": 180}])

    changed = _changed_dates(base, raced)
    taper = {(race - dt.timedelta(days=o)).isoformat() for o in range(1, 15)}
    recovery = {"2026-04-05", "2026-04-06", "2026-04-08"}  # ceil(3h) = 3 days
    assert changed <= taper | recovery | {race.isoformat()}
    assert changed >= recovery
    # Nothing at all after the last recovery day - that is the regression.
    assert max(changed) == max(recovery)


def test_nothing_changes_after_the_last_affected_day():
    """The tail of a long plan is bit-identical with and without a race."""
    base, raced = _identity(_long()), _identity(
        _long([{"id": 1, "date": "2026-04-04", "priority": "A",
                "duration_min": 180}]))
    tail = {d: v for d, v in base.items() if d > "2026-04-08"}
    assert tail and {d: raced[d] for d in tail} == tail


# --------------------------------------------------- the volume invariant
def test_weekly_volume_never_exceeds_the_users_hours():
    """Hard requirement: races only ever REDUCE volume, never add to it."""
    races = [
        {"id": 1, "date": "2026-08-16", "priority": "A", "duration_min": 180},
        {"id": 2, "date": "2026-07-18", "priority": "B"},
        {"id": 3, "date": "2026-09-12", "priority": "A", "duration_min": 90},
    ]
    base = _weekly_minutes(_gen(weeks=12))
    raced = _weekly_minutes(_gen(races, weeks=12))
    cap = HOURS * 60
    for monday, minutes in raced.items():
        # No tolerance: the cap is exact, in both the raceless plan and the
        # raced one. See test_plan.py for the property sweep over the grid.
        assert minutes <= cap, f"week of {monday}: {minutes} > {cap}"
        assert base[monday] <= cap, f"baseline week of {monday} already over"
        assert minutes <= base[monday], f"week of {monday} grew"


def test_taper_actually_reduces_volume():
    race_day = dt.date(2026, 8, 16)
    base = _by_date(_gen())
    raced = _by_date(_gen([{"id": 1, "date": race_day.isoformat(),
                            "priority": "A", "duration_min": 60}]))
    window = [(race_day - dt.timedelta(days=o)).isoformat() for o in range(1, 15)]
    before = sum(base[d]["duration_s"] for d in window if d in base)
    after = sum(raced[d]["duration_s"] for d in window if d in raced)
    # Bosquet's band is a 41-60% cut; the feasibility clamp on hard days keeps
    # us at the conservative end, which is the intended trade.
    assert 0.25 <= 1 - after / before <= 0.60


# ------------------------------------------------------- A/A conflicts
def test_two_close_a_races_demote_the_later_one():
    races = [
        {"id": 1, "date": "2026-08-16", "priority": "A", "duration_min": 60},
        {"id": 2, "date": "2026-08-26", "priority": "A", "duration_min": 60},
    ]
    generated = _gen(races)
    planned = {r["date"]: r["priority"] for r in generated["races"]["planned"]}
    assert planned == {"2026-08-16": "A", "2026-08-26": "B"}

    conflicts = generated["races"]["conflicts"]
    assert len(conflicts) == 1
    assert conflicts[0]["date"] == "2026-08-26"
    assert conflicts[0]["conflicts_with"] == "2026-08-16"
    assert conflicts[0]["planned_as"] == "B"

    # The demoted race gets no taper: the day before it keeps its duration.
    base, raced = _by_date(_gen()), _by_date(generated)
    assert raced["2026-08-24"]["duration_s"] == base["2026-08-24"]["duration_s"]


def test_a_races_far_enough_apart_both_taper():
    races = [
        {"id": 1, "date": "2026-07-19", "priority": "A", "duration_min": 60},
        {"id": 2, "date": "2026-08-16", "priority": "A", "duration_min": 60},
    ]
    generated = _gen(races)
    assert generated["races"]["conflicts"] == []
    assert all(r["priority"] == "A" for r in generated["races"]["planned"])


def test_conflict_resolution_does_not_mutate_the_stored_rows(user_id):
    """Demotion is a planning decision - the database keeps both races at A."""
    db.add_race_date(user_id, "2026-08-16", "A", "Nationals", 60)
    db.add_race_date(user_id, "2026-08-26", "A", "State champs", 60)
    plan_id = _seed_plan(user_id)
    result = reflow.reflow_plan(user_id, plan_id, now=NOW)
    assert len(result["race_conflicts"]) == 1
    assert [r["priority"] for r in db.list_race_dates(user_id)] == ["A", "A"]


# ------------------------------------------------------------- reflow
def test_adding_then_deleting_a_race_restores_the_plan_exactly(user_id):
    """The headline property: a race is fully reversible."""
    plan_id = _seed_plan(user_id)
    before = _rows(user_id, plan_id)

    race_id = db.add_race_date(user_id, "2026-08-16", "A", "Nationals", 120)
    added = reflow.reflow_plan(user_id, plan_id, now=NOW)
    assert added["status"] == "ok"
    assert added["updated"] + added["deleted"] > 0
    assert _rows(user_id, plan_id) != before

    db.delete_race_date(user_id, race_id)
    removed = reflow.reflow_plan(user_id, plan_id, now=NOW)
    assert removed["status"] == "ok"
    assert _rows(user_id, plan_id) == before  # byte-identical, .zwo included


def test_reflow_of_a_b_race_touches_only_the_intended_rows(user_id):
    """At DB level: one B race must not rewrite (and re-export) the whole plan."""
    plan_id = _seed_plan(user_id, weeks=12)
    before = {r["date"]: r for r in _rows(user_id, plan_id)}
    # Thu 2026-08-13 is not a ride day; only Fri 2026-08-14 (a hard day) is
    # adjacent to it and future-dated, so exactly one row may change.
    db.add_race_date(user_id, "2026-08-13", "B", "Local crit", 60)

    result = reflow.reflow_plan(user_id, plan_id, now=NOW)

    assert (result["updated"], result["inserted"], result["deleted"]) == (1, 0, 0)
    after = {r["date"]: r for r in _rows(user_id, plan_id)}
    changed = {d for d in after if after[d] != before[d]}
    assert changed == {"2026-08-14"}


def test_reflowing_a_race_twice_is_a_no_op_the_second_time(user_id):
    plan_id = _seed_plan(user_id)
    db.add_race_date(user_id, "2026-08-16", "A", "Nationals", 120)
    reflow.reflow_plan(user_id, plan_id, now=NOW)
    settled = _rows(user_id, plan_id)

    again = reflow.reflow_plan(user_id, plan_id, now=NOW)
    assert (again["updated"], again["inserted"], again["deleted"]) == (0, 0, 0)
    assert _rows(user_id, plan_id) == settled


def test_moving_a_race_reflows_and_is_idempotent(user_id):
    plan_id = _seed_plan(user_id)
    # Both dates are Mondays, i.e. ride days, so the move is visible as a
    # workout disappearing from one date and reappearing on the other.
    race_id = db.add_race_date(user_id, "2026-08-17", "A", "Nationals", 120)
    reflow.reflow_plan(user_id, plan_id, now=NOW)
    assert "2026-08-17" not in {r["date"] for r in _rows(user_id, plan_id)}

    db.update_race_date(user_id, race_id, "2026-08-31", "A", "Nationals", 120)
    moved = reflow.reflow_plan(user_id, plan_id, now=NOW)
    assert moved["status"] == "ok"
    by_date = {r["date"]: r for r in _rows(user_id, plan_id)}
    assert "2026-08-31" not in by_date          # new race day cleared
    assert "2026-08-17" in by_date              # old race day back in the plan
    settled = _rows(user_id, plan_id)

    again = reflow.reflow_plan(user_id, plan_id, now=NOW)
    assert (again["updated"], again["inserted"], again["deleted"]) == (0, 0, 0)
    assert _rows(user_id, plan_id) == settled


def _mark_adapted(user_id, plan_id, date):
    """Pretend adapt.py rewrote the row on `date` (its own once-only path)."""
    row = next(r for r in _rows(user_id, plan_id) if r["date"] == date)
    session = planmod.build_workout("recovery", 30, "classic")
    ok = db.update_plan_workout_content(
        user_id, row["id"], session.name, "recovery", session.total_duration(),
        session.estimated_tss, zwo.zwo_string(session), "recovery",
        "2026-07-08T09:00:00", variant="classic",
    )
    assert ok
    return row["id"]


def test_reflow_skips_an_adapted_row_outside_any_race_window(user_id):
    plan_id = _seed_plan(user_id)
    # Far from the race so no taper or recovery reaches it.
    adapted_id = _mark_adapted(user_id, plan_id, "2026-07-13")
    db.add_race_date(user_id, "2026-09-13", "A", "Nationals", 120)

    result = reflow.reflow_plan(user_id, plan_id, now=NOW)

    assert result["skipped_locked"] >= 1
    row = next(r for r in _rows(user_id, plan_id) if r["id"] == adapted_id)
    assert row["adapted"] == "recovery"  # the considered adjustment survived


def test_reflow_claims_an_adapted_row_inside_a_race_window(user_id):
    plan_id = _seed_plan(user_id)
    # 2026-09-11 is two days before the race: squarely inside the taper.
    adapted_id = _mark_adapted(user_id, plan_id, "2026-09-11")
    db.add_race_date(user_id, "2026-09-13", "A", "Nationals", 120)

    result = reflow.reflow_plan(user_id, plan_id, now=NOW)

    assert result["updated"] >= 1
    row = next(r for r in _rows(user_id, plan_id) if r["id"] == adapted_id)
    assert row["adapted"] is None       # the race outranks the adaptation
    assert row["type"] != "recovery"


# ------------------------------------------------- adaptation suppression
class _State:
    overreach = True
    plateau = False
    alerts = []


def test_adaptation_is_suppressed_after_a_race(user_id):
    plan_id = _seed_plan(user_id, active=False)
    db.add_race_date(user_id, "2026-07-06", "A", "Nationals", 120)

    # Four days after the race: fatigue is expected, not overreaching.
    summary = adapt.apply_adaptations(user_id, _State(), now=NOW)

    assert summary["status"] == adapt.POST_RACE
    assert summary["adjusted"] == 0
    assert all(r["adapted"] is None for r in _rows(user_id, plan_id))


def test_adaptation_resumes_once_the_quiet_period_passes(user_id):
    _seed_plan(user_id, active=False)
    db.add_race_date(user_id, "2026-07-06", "A", "Nationals", 120)

    on_the_edge = dt.datetime(2026, 7, 16, 9, 0)   # race + 10 days
    assert adapt.apply_adaptations(
        user_id, _State(), now=on_the_edge)["status"] == adapt.POST_RACE

    after = dt.datetime(2026, 7, 17, 9, 0)         # race + 11 days
    summary = adapt.apply_adaptations(user_id, _State(), now=after)
    assert summary["status"] == adapt.OVERREACH
    assert summary["adjusted"] > 0


def test_suppression_banner_renders(user_id):
    db.add_race_date(user_id, "2026-07-06", "B", None, 60)
    summary = adapt.apply_adaptations(user_id, _State(), now=NOW)
    banner = adapt.banner_for(_State(), summary)
    assert banner["status"] == adapt.POST_RACE
    assert banner["headline"]


# ------------------------------------------------------------- export
def test_race_days_are_not_exported_and_a_stale_zwo_is_removed(user_id, tmp_path):
    out = tmp_path / "zwo"
    out.mkdir()
    db.save_user_settings(user_id, {"workouts_dir": str(out), "zwift_id": "123"})
    plan_id = _seed_plan(user_id)
    race_day = "2026-08-10"
    row = next(r for r in _rows(user_id, plan_id) if r["date"] == race_day)

    exporter.sync_plan_exports(user_id)
    fname = zwo.plan_filename(race_day, row["name"])
    assert os.path.exists(out / fname)

    db.add_race_date(user_id, race_day, "A", "Nationals", 120)
    result = exporter.sync_plan_exports(user_id)

    assert result["removed"] >= 1
    assert not os.path.exists(out / fname)


# ------------------------------------------------------------- CRUD scoping
def test_race_crud_is_user_scoped(user_id):
    from wattracker import auth

    other = db.create_user("intruder", auth.hash_password("password123"))
    race_id = db.add_race_date(user_id, "2026-08-16", "A", "Nationals", 60)

    assert db.update_race_date(other, race_id, "2026-09-01", "B") is False
    assert db.delete_race_date(other, race_id) is False
    assert db.list_race_dates(other) == []
    kept = db.list_race_dates(user_id)
    assert len(kept) == 1 and kept[0]["date"] == "2026-08-16"


def test_unknown_priority_is_stored_as_b(user_id):
    """'B' is the safe default: a wrong 'A' would rewrite three weeks of plan."""
    db.add_race_date(user_id, "2026-08-16", "sorta important")
    assert db.list_race_dates(user_id)[0]["priority"] == "B"


def test_race_on_returns_the_race_for_a_date(user_id):
    db.add_race_date(user_id, "2026-08-16", "A", "Nationals", 60)
    assert db.race_on(user_id, "2026-08-16")["name"] == "Nationals"
    assert db.race_on(user_id, "2026-08-17") is None


# ------------------------------------------------------------- routes
@pytest.fixture()
def client():
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    from wattracker.server import create_app

    with TestClient(create_app()) as c:
        yield c


def _register(client, username="rider"):
    client.post("/register", data={"username": username, "password": "password123"})
    return db.get_user_by_username(username)["id"]


def test_race_routes_add_reflow_and_delete(client):
    uid = _register(client)
    plan_id = _seed_plan(uid)
    before = _rows(uid, plan_id)

    resp = client.post("/race/add", data={
        "date": "2026-08-17", "priority": "A", "name": "Nationals",
        "duration_min": "120"}, follow_redirects=False)
    assert resp.status_code == 303
    assert "workouts%20changed" in resp.headers["location"]
    races = db.list_race_dates(uid)
    assert len(races) == 1 and races[0]["priority"] == "A"
    assert _rows(uid, plan_id) != before

    resp = client.post(f"/race/{races[0]['id']}/delete", follow_redirects=False)
    assert resp.status_code == 303
    assert db.list_race_dates(uid) == []
    # Deleting the race undoes it exactly. Row ids are excluded: the race day
    # itself was DELETED and is re-inserted here, so it comes back with a new
    # id but identical content.
    def _content(rows):
        return [{k: v for k, v in r.items() if k != "id"} for r in rows]

    assert _content(_rows(uid, plan_id)) == _content(before)


def test_race_update_route_reflows(client):
    uid = _register(client)
    _seed_plan(uid)
    race_id = db.add_race_date(uid, "2026-08-17", "B", "Local crit", 60)

    resp = client.post(f"/race/{race_id}/update", data={
        "date": "2026-08-24", "priority": "A", "name": "Local crit",
        "duration_min": "90"}, follow_redirects=False)

    assert resp.status_code == 303
    race = db.list_race_dates(uid)[0]
    assert (race["date"], race["priority"]) == ("2026-08-24", "A")


def test_race_routes_reject_another_users_race(client):
    from wattracker import auth

    uid = _register(client)
    other = db.create_user("intruder", auth.hash_password("password123"))
    race_id = db.add_race_date(other, "2026-08-17", "A", "Theirs", 60)

    assert client.post(f"/race/{race_id}/delete").status_code == 404
    assert client.post(f"/race/{race_id}/update",
                       data={"date": "2026-09-01"}).status_code == 404
    assert db.list_race_dates(other)[0]["name"] == "Theirs"
    assert db.list_race_dates(uid) == []


def test_race_add_rejects_a_bad_date(client):
    uid = _register(client)
    resp = client.post("/race/add", data={"date": "not-a-date"},
                       follow_redirects=False)
    assert resp.status_code == 303
    assert db.list_race_dates(uid) == []


def _create_plan_via_route(client, weeks=10, start=MONDAY):
    resp = client.post("/generate/plan", data={
        "name": "Base", "weeks": str(weeks), "hours_per_week": str(HOURS),
        "hit_days_per_week": "1", "start_date": start.isoformat(),
        "days": [str(d) for d in RIDE_DAYS],
    })
    assert resp.status_code == 200
    return resp


def test_plan_created_with_races_is_born_race_aware(client):
    """Regression: a plan created while races are on the calendar was built
    race-blind, so the very first nightly reflow rewrote it end to end and told
    the rider their brand-new plan had "changed overnight"."""
    uid = _register(client)
    db.add_race_date(uid, "2026-08-17", "A", "Nationals", 120)
    db.add_race_date(uid, "2026-09-05", "B", "Local crit", 60)
    _create_plan_via_route(client)
    plan_id = db.list_plans(uid)[0]["id"]

    # Race day itself carries no workout: proof the races reached the generator.
    assert [w for w in _rows(uid, plan_id) if w["date"] == "2026-08-17"] == []

    # The first reflow is a no-op, and therefore leaves no notice.
    result = reflow.reflow_plan(uid, plan_id, now=NOW)
    assert result["status"] == "ok"
    for key in ("updated", "inserted", "deleted", "skipped_locked", "failed"):
        assert result[key] == 0, (key, result)
    assert db.get_plan(uid, plan_id)["reflow_notice"] is None


def test_calendar_shows_race_days_and_the_panel(client):
    uid = _register(client)
    db.add_race_date(uid, "2026-08-17", "A", "Nationals", 120)
    body = client.get("/calendar?year=2026&month=8").text
    assert "cal-race-day" in body
    assert "Nationals" in body
    assert 'action="/race/add"' in body


# ------------------------------------------------------------- migration
def _make_v20_db(path):
    """A v20 database (pre race_dates) holding a user and a plan workout."""
    conn = sqlite3.connect(path)
    conn.executescript(db._SCHEMA)
    conn.executescript(
        "DROP TABLE race_dates;"
        "INSERT INTO users (id, username, password_hash, created) "
        "VALUES (1, 'rider', 'x', '2026-01-01T00:00:00');"
        "INSERT INTO plans (id, user_id, name, start_date, weeks, created) "
        "VALUES (1, 1, 'Base', '2026-07-06', 4, '2026-07-01T00:00:00');"
    )
    conn.execute("PRAGMA user_version = 20")
    conn.commit()
    conn.close()


def test_migration_20_to_21_preserves_data(tmp_path):
    path = str(tmp_path / "v20.db")
    _make_v20_db(path)

    db.init_db(path)

    conn = sqlite3.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1
    assert conn.execute("SELECT name FROM plans").fetchone()[0] == "Base"
    assert conn.execute("SELECT COUNT(*) FROM race_dates").fetchone()[0] == 0
    conn.close()

    # The table works after migrating.
    assert db.add_race_date(1, "2026-08-16", "A", "Nationals", 60, path=path)


def _schema_of(path):
    conn = sqlite3.connect(path)
    try:
        return sorted(
            r[0] for r in conn.execute(
                "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL")
        )
    finally:
        conn.close()


def test_migrated_schema_matches_a_fresh_one(tmp_path):
    migrated = str(tmp_path / "v20.db")
    _make_v20_db(migrated)
    db.init_db(migrated)

    fresh = str(tmp_path / "fresh.db")
    db.init_db(fresh)

    assert _schema_of(migrated) == _schema_of(fresh)
