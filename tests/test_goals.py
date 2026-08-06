"""Training goals: arcs, the recipe, the advisory length, and the UI slice.

Two properties carry the weight here.

INERTNESS: a plan with NO goal must still be byte-identical to the generator on
main. Goals are the first thing that passes a periodization arc to
``generate_plan`` at all, and the nightly sweep reflows every stored plan - so
if "no goal" drifted by one minute, every existing rider's workouts and .zwo
exports would be rewritten the first night after deploy. It is checked against
the real source pulled out of git, not against expectations written here.

THE WEEKLY-HOURS PROMISE: no week may exceed the hours the rider asked for, for
any goal x model x length x races. A phase redistributes intensity inside that
budget; it never buys more of it.
"""
import datetime as dt
import subprocess
import sys
import types
from pathlib import Path

import pytest

pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from wattracker import db  # noqa: E402
from wattracker.prescribe import duration, goals, plan, reflow, zwo  # noqa: E402
from wattracker.prescribe.phases import (  # noqa: E402
    MIN_VIABLE_PHASES, minimum_viable_weeks, resolve_phases,
)
from wattracker.prescribe.planner import build_workout  # noqa: E402
from wattracker.server import create_app  # noqa: E402

MONDAY = dt.date(2026, 7, 6)  # a Monday
NOW = dt.datetime(2026, 7, 15, 9, 0)
REPO = Path(__file__).resolve().parents[1]

# The last commit before goals existed. Pinned for the same reason
# test_plan_phases.py pins its own: comparing against whatever ``main`` happens
# to be would make this test compare the goal code with itself once it merges.
PRE_GOALS_REV = "62c7e53"

GOAL_KEYS = sorted(goals.GOALS)

_RACES = [
    {"id": 1, "date": "2026-08-16", "priority": "A", "duration_min": 180},
    {"id": 2, "date": "2026-07-18", "priority": "B"},
    {"id": 3, "date": "2026-07-27", "priority": "B", "duration_min": 60},
]


def _pre_goals_plan_module():
    """Import the pre-goals plan.py straight out of git history."""
    try:
        src = subprocess.run(
            ["git", "show", f"{PRE_GOALS_REV}:wattracker/prescribe/plan.py"],
            cwd=REPO, capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as e:  # pragma: no cover
        pytest.skip(f"git unavailable: {e}")
    if src.returncode != 0:  # pragma: no cover - shallow clone
        pytest.skip(f"commit {PRE_GOALS_REV} not available in this checkout")
    name = "wattracker.prescribe._plan_pre_goals"
    mod = types.ModuleType(name)
    mod.__package__ = "wattracker.prescribe"
    # dataclasses resolves string annotations through sys.modules, so the module
    # has to be registered before its body runs.
    sys.modules[name] = mod
    try:
        exec(compile(src.stdout, f"<{PRE_GOALS_REV}:plan.py>", "exec"),
             mod.__dict__)
    except Exception:  # pragma: no cover - a broken import must not linger
        sys.modules.pop(name, None)
        raise
    # ``hard_seconds`` is a shared reporting helper, not part of the goals
    # feature, and it has legitimately changed since this revision: it now also
    # counts the steady work block that interval sessions end on, and the
    # steady reps of _vo2max_descending, both of which it used to miss. This
    # test asks whether a goal alters plan GENERATION, so both sides must
    # measure the generated sessions with the same ruler - otherwise a fix to
    # the ruler reads as a goals regression. The comparison itself is
    # untouched: every field, including hard_s, is still compared.
    mod.hard_seconds = plan.hard_seconds
    return mod


@pytest.fixture(scope="module")
def pre_goals():
    return _pre_goals_plan_module()


@pytest.fixture()
def client():
    app = create_app()
    with TestClient(app) as c:
        yield c


def _register(client, username="rider"):
    client.post("/register", data={"username": username, "password": "password123"})


# ------------------------------------------------------------- the registry
def test_every_goal_is_well_formed_and_orthogonal_to_the_models():
    for key, goal in goals.GOALS.items():
        assert goal.key == key
        assert goal.label and goal.description
        # The goal nominates a model that actually exists - and only nominates
        # it. Model and goal stay separate choices.
        assert goal.default_model in plan.MODELS
        assert goal.signals
        assert goal.arc[-1].anchored_end, f"{key} must end on an anchored taper"


def test_goal_keys_match_the_length_recommender():
    """A goal's length recommendation is a lookup, not a second mapping."""
    for key in GOAL_KEYS:
        rec = duration.recommend_weeks(key)
        unknown = duration.recommend_weeks("no-such-goal")
        assert (rec.floor_weeks, rec.ideal_weeks, rec.rationale) != (
            unknown.floor_weeks, unknown.ideal_weeks, unknown.rationale
        ), f"{key} fell through to the unknown-goal default"


def test_an_unknown_or_malformed_goal_is_simply_no_goal():
    """A goal must never be the reason a plan cannot be built."""
    for value in (None, "", "  ", "nonsense", 7, object(), ["ftp"]):
        assert goals.normalize_key(value) is None
        assert goals.get(value) is None
        assert goals.arc_for(value) is None
        assert goals.resolve(value, 12) is None
        assert goals.block_summary(value, 12) is None
        assert goals.phase_by_date(MONDAY, 12, value) == {}


def test_goal_keys_are_normalized_not_trusted():
    assert goals.normalize_key(" FTP ") == "ftp"
    assert goals.normalize_key("Criterium") == "criterium"


# ------------------------------------------------------- arcs across 8-52 wk
@pytest.mark.parametrize("key", GOAL_KEYS)
def test_every_arc_is_a_sane_structure_from_8_to_52_weeks(key):
    """The sweep: at every length the form allows, the arc is real training."""
    arc = goals.GOALS[key].arc
    by_name = {p.name: p for p in arc}
    order = {p.name: i for i, p in enumerate(arc)}
    repeatable = [p.name for p in arc if p.repeatable]
    for weeks in range(8, 53):
        r = resolve_phases(weeks, arc)
        assert len(r.weeks) == weeks
        if r.unphased_reason:
            # Abandoned wholesale - never a fragment of an arc.
            assert r.blocks == () and all(p is None for p in r.weeks)
            assert weeks < minimum_viable_weeks(arc)
            continue
        names = [n for n, _ in r.blocks]
        # 1. Every block is a real mesocycle inside its own bounds - a phase
        #    that cannot reach its floor is dropped, never shrunk.
        for name, count in r.blocks:
            phase = by_name[name]
            assert count >= phase.min_weeks, (weeks, r.blocks)
            if phase.max_weeks is not None:
                assert count <= phase.max_weeks, (weeks, r.blocks)
        # 2. A viable structure that ends on the taper...
        assert len(set(names)) >= min(MIN_VIABLE_PHASES, len(arc))
        assert names[-1] == "taper"
        # 3. ...entered from the arc's last work block, never straight out of a
        #    base: the block before the taper is the sharpening one.
        assert names[-2] == arc[-2].name, (weeks, names)
        # 4. The progression climbs, wrapping only between whole cycles.
        for a, b in zip(names, names[1:]):
            assert order[b] > order[a] or (a == repeatable[-1]
                                           and b == repeatable[0]), (weeks, names)


@pytest.mark.parametrize("key", GOAL_KEYS)
def test_no_arc_ever_increases_weekly_volume(key):
    for phase in goals.GOALS[key].arc:
        assert 0.0 < phase.volume_multiplier <= 1.0


def test_the_criterium_goal_is_the_one_that_schedules_sprints():
    types_ = {t for p in goals.CRITERIUM_ARC for t in p.hard_types}
    assert "sprint" in types_
    for key in ("ftp", "long_ride"):
        others = {t for p in goals.GOALS[key].arc for t in p.hard_types}
        assert "sprint" not in others


def test_phase_by_date_covers_the_plan_and_nothing_else():
    weeks = 16
    mapping = goals.phase_by_date(MONDAY, weeks, "ftp")
    assert len(mapping) == weeks * 7
    assert mapping[MONDAY.isoformat()] == "base"
    last = MONDAY + dt.timedelta(days=7 * weeks - 1)
    assert mapping[last.isoformat()] == "taper"
    assert (last + dt.timedelta(days=1)).isoformat() not in mapping
    # A start mid-week still anchors to that week's Monday, exactly as the
    # generator does.
    assert goals.phase_by_date(MONDAY + dt.timedelta(days=3), weeks,
                               "ftp") == mapping


def test_phase_by_date_says_nothing_when_the_arc_was_abandoned():
    """Too short to periodize -> no phase labels at all, not blank ones."""
    short = minimum_viable_weeks(goals.FTP_ARC) - 1
    assert goals.phase_by_date(MONDAY, short, "ftp") == {}
    assert goals.phase_by_date(MONDAY, 0, "ftp") == {}
    assert goals.phase_by_date("not-a-date", 12, "ftp") == {}


# ----------------------------------------------- hard_seconds and sprints
def test_hard_seconds_counts_a_sprints_freeride_work():
    """The hazard that had to be fixed before the criterium arc could exist."""
    session = build_workout("sprint", 60)
    reps = [s for s in session.segments if s.kind == "freeride"]
    assert reps, "a sprint session is built from freeride efforts"
    assert plan.hard_seconds(session) == sum(s.duration for s in reps)
    assert plan.hard_seconds(session) > 0


def test_hard_seconds_ignores_a_sub_threshold_freeride_block():
    """Freeride means 'no target', not 'hard'. The load fraction decides."""
    from wattracker.prescribe.planner import Segment, Session

    easy = Session(name="x", description="", workout_type="endurance",
                   segments=[Segment(kind="freeride", duration=600,
                                     load_fraction=0.6)])
    assert plan.hard_seconds(easy) == 0


@pytest.mark.parametrize("minutes", list(range(plan.HIT_MIN_MIN,
                                               plan.HIT_MAX_MIN + 1)))
def test_a_sprint_builds_at_every_duration_a_hit_slot_can_produce(minutes):
    """_sprint has its own rep math; a HIT slot must never break it."""
    session = build_workout("sprint", minutes)
    assert session.total_duration() == minutes * 60
    reps = [s for s in session.segments if s.kind == "freeride"]
    assert len(reps) >= 3, "a degenerate sprint session is not a sprint session"
    assert session.estimated_tss > 0
    assert plan.hard_seconds(session) > 0


def test_a_criterium_plan_reports_a_non_zero_and_sane_hard_fraction():
    p = plan.generate_plan("Crit", MONDAY, 16, [0, 2, 4, 5], 8.0, 2,
                           model="polarized", phases=goals.CRITERIUM_ARC)
    sharpen = [w["week"] for w, name in zip(p["weekly"], p["phases"]["weeks"])
               if name == "sharpen"]
    assert sharpen, "the 16-week criterium arc has a sharpening block"
    types_ = {w["type"] for w in p["workouts"]}
    assert "sprint" in types_
    for wk in p["weekly"]:
        assert wk["hard_s"] > 0
        assert 0.0 < wk["hard_fraction"] < 0.5
    assert 0.0 < p["polarized_hard_fraction"] < 0.5
    # Every sprint session contributes real hard seconds, not zero.
    for w in p["workouts"]:
        if w["type"] == "sprint":
            assert w["hard_s"] > 0


# ------------------------------------------------------ inertness (headline)
def _comparable(p):
    """Everything about a plan that a rider or a stored row can observe."""
    return {
        "meta": {k: v for k, v in p.items()
                 if k not in ("workouts", "weekly", "phases")},
        "weekly": p["weekly"],
        "workouts": [
            {k: v for k, v in w.items() if k != "session"} for w in p["workouts"]
        ],
    }


def _configs(hours=(3.5, 6.0, 8.0, 12.0), max_days=8, hits=(1, 2, 3)):
    for hours_ in hours:
        for n_days in range(2, max_days):
            for hit in hits:
                for model in plan.MODELS:
                    yield hours_, list(range(n_days)), hit, model


@pytest.mark.parametrize("with_races", [False, True])
def test_a_plan_with_no_goal_is_byte_identical_to_main(pre_goals, with_races):
    races = _RACES if with_races else None
    checked = 0
    for hours, days, hit, model in _configs():
        if plan.validate_plan_inputs(12, days, hours, hit, None, model):
            continue
        args = ("P", MONDAY, 12, days, hours, hit)
        old = pre_goals.generate_plan(*args, model=model, races=races)
        new = plan.generate_plan(*args, model=model, races=races,
                                 phases=goals.arc_for(None))
        assert "phases" not in new
        assert _comparable(new) == _comparable(old), (
            f"{hours}h, {len(days)} days, {hit} hard, {model}"
        )
        checked += 1
    assert checked > 100  # the grid really was exercised


def test_a_plan_with_no_goal_exports_identical_zwo(pre_goals):
    """The .zwo string is what actually lands in the rider's Zwift folder."""
    args = ("P", MONDAY, 16, [0, 1, 3, 5, 6], 10.0, 2)
    old = pre_goals.generate_plan(*args, races=_RACES)
    new = plan.generate_plan(*args, races=_RACES, phases=goals.arc_for(None))
    assert [zwo.zwo_string(w["session"]) for w in new["workouts"]] == [
        zwo.zwo_string(w["session"]) for w in old["workouts"]
    ]


# ------------------------------------------------ the weekly-hours promise
@pytest.mark.parametrize("goal_key", GOAL_KEYS)
@pytest.mark.parametrize("with_races", [False, True])
def test_the_weekly_hours_cap_holds_for_every_goal(goal_key, with_races):
    """No week exceeds the hours the rider asked for. Any goal, any model,
    any length, races or not - a phase redistributes intensity inside the
    budget and may only ever reduce it."""
    races = _RACES if with_races else None
    arc = goals.GOALS[goal_key].arc
    # A sampled grid, not the full one from the inertness test: the cap is a
    # duration-agnostic property, so short/mid/long lengths over a spread of
    # hours/days/hits/models is enough. 4 x 60 = 240 plans per case.
    configs = list(_configs(hours=(3.5, 8.0, 12.0), max_days=7, hits=(1, 3)))
    checked = 0
    for weeks in (4, 12, 20, 32):
        for hours, days, hit, model in configs:
            if plan.validate_plan_inputs(weeks, days, hours, hit, None, model):
                continue
            p = plan.generate_plan("P", MONDAY, weeks, days, hours, hit,
                                   model=model, races=races, phases=arc)
            for wk in p["weekly"]:
                assert wk["total_s"] <= hours * 3600, (
                    goal_key, weeks, hours, len(days), hit, model, wk
                )
            checked += 1
    assert checked == 4 * 60


@pytest.mark.parametrize("goal_key", GOAL_KEYS)
def test_every_goal_generates_a_plan_at_every_length_it_offers(goal_key):
    """No length in the form's range may raise - including the short ones the
    length advice calls out as below the floor."""
    for weeks in range(1, 53):
        p = plan.generate_plan("P", MONDAY, weeks, [0, 2, 4, 5], 8.0, 2,
                               phases=goals.GOALS[goal_key].arc)
        assert len(p["weekly"]) == weeks
        assert p["workouts"]


# ------------------------------------------------------------- the recipe
def _seed(user_id, weeks=12, goal=None, name="P"):
    """Store a plan the way the route stores one."""
    recipe = reflow.build_recipe([0, 2, 4], 6.0, 1, model="polarized", goal=goal)
    generated = plan.generate_plan(
        name, MONDAY, weeks, recipe["days_of_week"], recipe["hours_per_week"],
        recipe["hit_days_per_week"], model=recipe["model"],
        phases=goals.arc_for(recipe.get("goal")),
    )
    plan_id = db.create_plan(user_id, name, generated["start_date"],
                             generated["weeks"], model=generated["model"],
                             recipe=recipe)
    for w in generated["workouts"]:
        db.add_plan_workout(
            plan_id, user_id, w["date"], w["name"], w["type"], w["duration_s"],
            w["tss"], zwo.zwo_string(w["session"]), variant=w.get("variant"),
            origin=reflow.GENERATED,
        )
    return plan_id


@pytest.mark.parametrize("goal_key", GOAL_KEYS)
def test_the_recipe_round_trips_the_goal(user_id, goal_key):
    plan_id = _seed(user_id, goal=goal_key)
    stored = db.get_plan(user_id, plan_id)
    assert stored["recipe"]["goal"] == goal_key
    assert stored["recipe"]["version"] == reflow.RECIPE_VERSION


def test_an_unrecognized_goal_is_stored_as_no_goal():
    recipe = reflow.build_recipe([0, 2], 5.0, 1, goal="not-a-goal")
    assert recipe["goal"] is None


def test_an_old_recipe_with_no_goal_still_reflows_unchanged(user_id):
    """Every plan created before goals existed carries a v1 recipe."""
    plan_id = _seed(user_id, goal=None)
    # Rewrite the stored recipe into the exact v1 shape: version 1, no goal key.
    old = dict(db.get_plan(user_id, plan_id)["recipe"])
    old.pop("goal")
    old["version"] = 1
    import json
    from wattracker.db import connect
    conn = connect()
    try:
        conn.execute("UPDATE plans SET recipe = ? WHERE id = ?",
                     (json.dumps(old), plan_id))
        conn.commit()
    finally:
        conn.close()

    before = db.plan_workouts_for_plan(user_id, plan_id, include_zwo=True)
    result = reflow.reflow_plan(user_id, plan_id, now=NOW)
    assert result["status"] == "ok"
    assert (result["updated"], result["inserted"], result["deleted"]) == (0, 0, 0)
    assert db.plan_workouts_for_plan(user_id, plan_id, include_zwo=True) == before


@pytest.mark.parametrize("goal_key", GOAL_KEYS)
def test_reflow_with_a_goal_is_idempotent(user_id, goal_key):
    """Reflow runs unattended nightly, so it must reach a fixed point."""
    plan_id = _seed(user_id, goal=goal_key)
    reflow.reflow_plan(user_id, plan_id, now=NOW)
    settled = db.plan_workouts_for_plan(user_id, plan_id, include_zwo=True)

    second = reflow.reflow_plan(user_id, plan_id, now=NOW)
    assert second["status"] == "ok"
    assert (second["updated"], second["inserted"], second["deleted"],
            second["failed"]) == (0, 0, 0, 0)
    assert db.plan_workouts_for_plan(user_id, plan_id, include_zwo=True) == settled


def test_a_goalless_plan_is_not_rewritten_by_a_reflow(user_id):
    """The reason inertness matters: the sweep reflows every stored plan."""
    plan_id = _seed(user_id, goal=None)
    before = db.plan_workouts_for_plan(user_id, plan_id, include_zwo=True)
    result = reflow.reflow_plan(user_id, plan_id, now=NOW)
    assert (result["updated"], result["inserted"], result["deleted"]) == (0, 0, 0)
    assert db.plan_workouts_for_plan(user_id, plan_id, include_zwo=True) == before


# ------------------------------------------------------- the reflow notice
def test_an_unattended_reflow_that_changes_nothing_says_nothing(user_id):
    plan_id = _seed(user_id, goal="ftp")
    reflow.reflow_plan(user_id, plan_id, now=NOW, notify=True)
    assert db.get_plan(user_id, plan_id)["reflow_notice"] is None


def test_an_unattended_reflow_that_changes_things_tells_the_rider(user_id):
    plan_id = _seed(user_id, goal="ftp")
    # Something outside the recipe changed: a race now bends the plan.
    db.add_race_date(user_id, "2026-08-10", priority="A", name="Nationals",
                     duration_min=120)
    result = reflow.reflow_plan(user_id, plan_id, now=NOW, notify=True)
    assert result["updated"] + result["inserted"] + result["deleted"] > 0

    notice = db.get_plan(user_id, plan_id)["reflow_notice"]
    assert notice and notice["changed"] == (
        result["updated"] + result["inserted"] + result["deleted"]
    )
    assert str(result["updated"]) in notice["message"]
    assert "overnight" in notice["message"]
    # A later no-op run must not erase the message from the run that changed
    # things - reflow is idempotent, the rider's notice is not.
    reflow.reflow_plan(user_id, plan_id, now=NOW, notify=True)
    assert db.get_plan(user_id, plan_id)["reflow_notice"] == notice


def test_a_rider_triggered_reflow_leaves_no_notice(user_id):
    """Editing a race is a change the rider already knows they made."""
    plan_id = _seed(user_id, goal="ftp")
    db.add_race_date(user_id, "2026-08-10", priority="A", duration_min=120)
    reflow.reflow_plan(user_id, plan_id, now=NOW)
    assert db.get_plan(user_id, plan_id)["reflow_notice"] is None


def test_the_notice_can_be_dismissed(client):
    _register(client)
    uid = db.get_user_by_username("rider")["id"]
    plan_id = _seed(uid, goal="ftp")
    db.set_plan_reflow_notice(uid, plan_id, {"changed": 3, "message": "3 changed"})

    assert "3 changed" in client.get("/calendar").text
    r = client.post(f"/plan/{plan_id}/reflow-notice/dismiss")
    assert r.status_code in (200, 303)
    assert db.get_plan(uid, plan_id)["reflow_notice"] is None
    assert "3 changed" not in client.get("/calendar").text


def test_a_notice_on_a_non_active_plan_can_be_seen_and_dismissed(client):
    """Regression: the calendar and the current-plan card only ever surface the
    ACTIVE plan's notice, so a notice the sweep left on any other plan used to
    be invisible - and therefore permanently undismissable."""
    _register(client)
    uid = db.get_user_by_username("rider")["id"]
    active_id = _seed(uid, goal="ftp", name="Active")
    other_id = _seed(uid, goal="ftp", name="Other")
    db.set_active_plan(uid, active_id)
    db.set_plan_reflow_notice(uid, other_id, {"changed": 3, "message": "3 changed"})

    assert "3 changed" not in client.get("/calendar").text  # not the active plan
    body = client.get(f"/plan?plan_id={other_id}").text
    assert "3 changed" in body
    assert f'action="/plan/{other_id}/reflow-notice/dismiss"' in body

    r = client.post(f"/plan/{other_id}/reflow-notice/dismiss")
    assert r.status_code in (200, 303)
    assert db.get_plan(uid, other_id)["reflow_notice"] is None
    assert "3 changed" not in client.get(f"/plan?plan_id={other_id}").text


def test_a_viewed_plans_notice_is_shown_once(client):
    """The current-plan card and the plan being viewed are the same plan here;
    the rider must not get the same alert twice."""
    _register(client)
    uid = db.get_user_by_username("rider")["id"]
    plan_id = _seed(uid, goal="ftp")
    db.set_active_plan(uid, plan_id)
    db.set_plan_reflow_notice(uid, plan_id, {"changed": 3, "message": "3 changed"})

    assert client.get(f"/plan?plan_id={plan_id}").text.count("3 changed") == 1
    # ...and still shown on the plan page when no plan is being viewed.
    assert "3 changed" in client.get("/plan").text


def test_dismissing_from_a_viewed_plan_stays_on_that_plan(client):
    """Dismissing used to bounce to the bare plan page, dropping the plan the
    rider was actually reading."""
    _register(client)
    uid = db.get_user_by_username("rider")["id"]
    active_id = _seed(uid, goal="ftp", name="Active")
    other_id = _seed(uid, goal="ftp", name="Other")
    db.set_active_plan(uid, active_id)
    db.set_plan_reflow_notice(uid, other_id, {"changed": 3, "message": "3 changed"})

    r = client.post(
        f"/plan/{other_id}/reflow-notice/dismiss",
        headers={"referer": f"http://testserver/plan?plan_id={other_id}"},
        follow_redirects=False,
    )
    assert r.headers["location"] == f"/plan?plan_id={other_id}"

    # A Referer naming a DIFFERENT plan cannot steer the redirect.
    db.set_plan_reflow_notice(uid, active_id, {"changed": 1, "message": "1 changed"})
    r = client.post(
        f"/plan/{active_id}/reflow-notice/dismiss",
        headers={"referer": f"http://testserver/plan?plan_id={other_id}"},
        follow_redirects=False,
    )
    assert r.headers["location"] == "/plan"


def test_a_notice_cannot_be_dismissed_on_someone_elses_plan(client):
    _register(client, "rider")
    _register(client, "other")
    other = db.get_user_by_username("other")["id"]
    plan_id = _seed(other, goal="ftp")
    db.set_plan_reflow_notice(other, plan_id, {"changed": 1, "message": "x"})

    client.post("/login", data={"username": "rider", "password": "password123"})
    assert client.post(f"/plan/{plan_id}/reflow-notice/dismiss").status_code == 404
    assert db.get_plan(other, plan_id)["reflow_notice"] is not None


# ------------------------------------------------- the advisory length
@pytest.mark.parametrize("goal_key", GOAL_KEYS)
def test_a_below_floor_choice_still_generates_a_plan(client, goal_key):
    """Advisory means advisory: the length is explained, never enforced."""
    _register(client)
    rec = duration.recommend_weeks(goal_key)
    weeks = max(1, rec.floor_weeks - 3)
    assert duration.classify_chosen_weeks(weeks, rec) == "short"
    r = client.post("/generate/plan", data={
        "name": "Short", "weeks": str(weeks), "hours_per_week": "8",
        "hit_days_per_week": "2", "start_date": "2026-08-03",
        "days": ["0", "2", "4", "5"], "model": "polarized", "goal": goal_key,
    })
    assert r.status_code == 200
    uid = db.get_user_by_username("rider")["id"]
    plans = db.list_plans(uid)
    assert len(plans) == 1 and plans[0]["recipe"]["goal"] == goal_key
    assert db.plan_workouts_for_plan(uid, plans[0]["id"])
    # ...and the rider is told what a short plan cost them - either which
    # phases did not fit, or that the arc was abandoned entirely.
    assert (f"{weeks} weeks is below this goal" in r.text
            or "too short to periodize" in r.text), r.text[-3000:]


def test_the_form_shows_each_goals_length_and_how_well_founded_it_is(client):
    _register(client)
    text = client.get("/plan").text
    for goal_key in GOAL_KEYS:
        rec = duration.recommend_weeks(goal_key)
        assert goals.GOALS[goal_key].label in text
        assert f"{rec.ideal_weeks} weeks" in text
    # The FTP figure is literature-informed and the other two are convention;
    # the page must say which is which rather than showing three bare numbers.
    assert "literature-informed" in text
    assert "coaching convention" in text


def test_the_basis_strength_reaches_the_generated_plan_response(client):
    _register(client)
    r = client.post("/generate/plan", data={
        "name": "Fondo", "weeks": "20", "hours_per_week": "8",
        "hit_days_per_week": "2", "start_date": "2026-08-03",
        "days": ["0", "2", "4", "5"], "model": "pyramidal", "goal": "long_ride",
    })
    assert "coaching convention" in r.text
    assert "durability" in r.text  # the arc's blocks are shown


def test_the_goal_picker_offers_no_goal_and_defaults_to_it(client):
    _register(client)
    text = client.get("/plan").text
    assert 'name="goal" value=""' in text
    r = client.post("/generate/plan", data={
        "name": "Flat", "weeks": "8", "hours_per_week": "8",
        "hit_days_per_week": "2", "start_date": "2026-08-03",
        "days": ["0", "2", "4", "5"],
    })
    assert r.status_code == 200
    uid = db.get_user_by_username("rider")["id"]
    assert db.list_plans(uid)[0]["recipe"]["goal"] is None


# --------------------------------------------------------- progress signals
def test_the_long_ride_panel_shows_nothing_rather_than_zeros(client):
    """Durability needs a hard 5-min effort late in a long ride, which the
    endurance rides this goal prescribes usually do not contain. No evidence
    must read as SILENCE - never as 0% retention."""
    _register(client)
    uid = db.get_user_by_username("rider")["id"]
    plan_id = _seed(uid, weeks=20, goal="long_ride")

    text = client.get(f"/plan?plan_id={plan_id}").text
    assert "Long ride" in text          # the goal itself is shown
    assert "retention" not in text      # ...but the absent measurement is not
    assert "0%" not in text
    assert "0.0%" not in text


def test_a_progress_signal_with_evidence_is_shown(client, monkeypatch):
    """The mirror image: when durability HAS evidence, the rider sees it."""
    from wattracker import server as servermod
    from wattracker.metrics.durability import DurabilityResult

    monkeypatch.setattr(
        servermod.durabilitymod, "compute_durability",
        lambda *a, **k: DurabilityResult(retention_ratio=0.93,
                                         fresh_5min_power=300.0,
                                         late_5min_power=279.0,
                                         qualifying_rides=4),
    )
    _register(client)
    uid = db.get_user_by_username("rider")["id"]
    plan_id = _seed(uid, weeks=20, goal="long_ride")
    text = client.get(f"/plan?plan_id={plan_id}").text
    assert "93.0% retention" in text
    assert "secondary" in text  # durability is explicitly the secondary signal


def test_each_goal_names_its_own_progress_signal():
    assert [s.key for s in goals.GOALS["ftp"].signals] == ["ftp_trend"]
    assert [s.key for s in goals.GOALS["criterium"].signals] == ["peak_power"]
    long_ride = goals.GOALS["long_ride"].signals
    # Decoupling is deliberately the PRIMARY one: durability is the better
    # construct but is usually absent (see goals.py).
    assert long_ride[0].key == "decoupling" and long_ride[0].role == "primary"
    assert long_ride[1].key == "durability" and long_ride[1].role == "secondary"


# ----------------------------------------------------------- the calendar
def test_the_calendar_labels_the_current_phase(client):
    _register(client)
    uid = db.get_user_by_username("rider")["id"]
    _seed(uid, weeks=16, goal="ftp")  # starts 2026-07-06
    text = client.get("/calendar?year=2026&month=7").text
    assert "cal-phase-tag" in text
    assert "base" in text


def test_the_calendar_labels_nothing_for_a_goalless_plan(client):
    _register(client)
    uid = db.get_user_by_username("rider")["id"]
    _seed(uid, weeks=16, goal=None)
    assert "cal-phase-tag" not in client.get("/calendar?year=2026&month=7").text
