"""Tests for periodization phases: resolution, inertness, and composition.

The headline property is INERTNESS: ``generate_plan(..., phases=None)`` has to
be byte-identical to the generator as it stood before phases existed, because
the nightly maintenance sweep reflows every plan and would otherwise rewrite
every rider's workouts and .zwo exports the first time it ran. That is checked
against the real pre-phases source pulled out of git, not against expectations
written down here.
"""
import datetime as dt
import subprocess
import sys
import types
from pathlib import Path

import pytest

from wattracker import db
from wattracker.prescribe import phases as ph
from wattracker.prescribe import plan, reflow, zwo
from wattracker.prescribe.phases import (
    DEFAULT_ARC, MIN_PHASE_WEEKS, MIN_VIABLE_PHASES, Phase,
    minimum_viable_weeks, resolve_phases,
)

MIN_VIABLE = minimum_viable_weeks(DEFAULT_ARC)

MONDAY = dt.date(2026, 7, 6)  # a Monday
NOW = dt.datetime(2026, 7, 15, 9, 0)
REPO = Path(__file__).resolve().parents[1]

# The last commit before phases existed. Pinned deliberately: comparing against
# whatever ``main`` happens to be would make this test compare the phase code
# with itself the moment this branch merges.
PRE_PHASES_REV = "baafd82"


def _pre_phases_plan_module():
    """Import the pre-phases plan.py straight out of git history.

    It is executed as a member of ``wattracker.prescribe`` so its relative
    imports resolve against the real (unchanged) planner module.
    """
    try:
        src = subprocess.run(
            ["git", "show", f"{PRE_PHASES_REV}:wattracker/prescribe/plan.py"],
            cwd=REPO, capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as e:  # pragma: no cover
        pytest.skip(f"git unavailable: {e}")
    if src.returncode != 0:  # pragma: no cover - shallow clone
        pytest.skip(f"commit {PRE_PHASES_REV} not available in this checkout")
    name = "wattracker.prescribe._plan_pre_phases"
    mod = types.ModuleType(name)
    mod.__package__ = "wattracker.prescribe"
    # dataclasses resolves string annotations through sys.modules, so the
    # module has to be registered before its body runs.
    sys.modules[name] = mod
    try:
        exec(compile(src.stdout, f"<{PRE_PHASES_REV}:plan.py>", "exec"),
             mod.__dict__)
    except Exception:  # pragma: no cover - a broken import must not linger
        sys.modules.pop(name, None)
        raise
    return mod


@pytest.fixture(scope="module")
def pre_phases():
    return _pre_phases_plan_module()


# ------------------------------------------------------ inertness (headline)
_GRID_HOURS = [3.5, 6.0, 8.0, 12.0]
_GRID_RACES = [
    {"id": 1, "date": "2026-08-16", "priority": "A", "duration_min": 180},
    {"id": 2, "date": "2026-07-18", "priority": "B"},
    {"id": 3, "date": "2026-07-27", "priority": "B", "duration_min": 60},
]


def _configs():
    for hours in _GRID_HOURS:
        for n_days in range(2, 8):
            for hit in range(1, 4):
                for model in plan.MODELS:
                    yield hours, list(range(n_days)), hit, model


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


@pytest.mark.parametrize("with_races", [False, True])
def test_phases_none_is_byte_identical_to_the_pre_phases_generator(
    pre_phases, with_races
):
    races = _GRID_RACES if with_races else None
    checked = 0
    for hours, days, hit, model in _configs():
        if plan.validate_plan_inputs(8, days, hours, hit, None, model):
            continue
        args = ("P", MONDAY, 8, days, hours, hit)
        old = pre_phases.generate_plan(*args, model=model, races=races)
        new = plan.generate_plan(*args, model=model, races=races, phases=None)
        assert "phases" not in new  # an unphased plan's dict is unchanged too
        assert _comparable(new) == _comparable(old), (
            f"{hours}h, {len(days)} days, {hit} hard, {model}"
        )
        checked += 1
    assert checked > 100  # the grid really was exercised


@pytest.mark.parametrize("model", sorted(plan.MODELS))
def test_phases_none_produces_identical_zwo_for_whole_plans(pre_phases, model):
    """The .zwo string is what actually reaches the rider's Zwift folder."""
    for weeks, days, hours, hit, races in (
        (12, [0, 2, 4, 5], 8.0, 2, None),
        (16, [0, 1, 3, 5, 6], 10.0, 2, _GRID_RACES),
        (4, [1, 3], 5.0, 1, None),
    ):
        if plan.validate_plan_inputs(weeks, days, hours, hit, None, model):
            continue
        args = ("P", MONDAY, weeks, days, hours, hit)
        old = pre_phases.generate_plan(*args, model=model, races=races)
        new = plan.generate_plan(*args, model=model, races=races)
        assert [zwo.zwo_string(w["session"]) for w in new["workouts"]] == [
            zwo.zwo_string(w["session"]) for w in old["workouts"]
        ]


# ------------------------------------------------------- phase construction
def test_a_phase_that_would_increase_volume_is_rejected():
    with pytest.raises(ValueError, match="volume_multiplier"):
        Phase(name="ramp", share=0.5, hard_types=("vo2max",),
              hard_volume_fraction=0.2, volume_multiplier=1.05)


@pytest.mark.parametrize("kwargs", [
    {"volume_multiplier": 0.0},
    {"hard_types": ()},
    {"hard_volume_fraction": 0.0},
    {"hard_volume_fraction": 1.5},
    {"share": 0.0},
    {"min_weeks": 0},
    {"min_weeks": 4, "max_weeks": 3},
    {"name": ""},
])
def test_malformed_phases_are_rejected(kwargs):
    base = dict(name="p", share=0.5, hard_types=("vo2max",),
                hard_volume_fraction=0.2)
    with pytest.raises(ValueError):
        Phase(**{**base, **kwargs})


def test_a_volume_multiplier_of_exactly_one_is_allowed():
    assert Phase(name="p", share=0.5, hard_types=("vo2max",),
                 hard_volume_fraction=0.2, volume_multiplier=1.0)


# ---------------------------------------------------------- phase resolution
def _names(resolved):
    return [p.name if p else None for p in resolved.weeks]


def test_the_default_arc_splits_proportionally():
    r = resolve_phases(20, DEFAULT_ARC)
    assert r.blocks == (("base", 8), ("build", 6), ("peak", 4), ("taper", 2))
    assert r.omitted == ()
    assert len(r.weeks) == 20
    # 16 weeks: shares scale down, taper keeps its two weeks.
    r16 = resolve_phases(16, DEFAULT_ARC)
    assert sum(c for _, c in r16.blocks) == 16
    assert dict(r16.blocks)["taper"] == 2
    assert dict(r16.blocks)["base"] > dict(r16.blocks)["build"]


def test_every_week_is_assigned_and_blocks_are_contiguous():
    for weeks in range(1, 60):
        r = resolve_phases(weeks, DEFAULT_ARC)
        assert len(r.weeks) == weeks
        # weeks and blocks describe the same thing.
        expanded = [n for n, c in r.blocks for _ in range(c)]
        assert [n for n in _names(r) if n is not None] == expanded
        # Unperiodized weeks only ever sit at the front.
        assigned = _names(r)
        first_phase = next((i for i, n in enumerate(assigned) if n), len(assigned))
        assert all(n is not None for n in assigned[first_phase:])


def test_a_phase_that_cannot_meet_its_minimum_is_dropped_not_shrunk():
    for weeks in range(5, 40):
        r = resolve_phases(weeks, DEFAULT_ARC)
        for name, count in r.blocks:
            phase = next(p for p in DEFAULT_ARC if p.name == name)
            assert count >= phase.min_weeks, (weeks, r.blocks)


def test_the_taper_is_allocated_first_and_never_below_its_floor():
    taper = next(p for p in DEFAULT_ARC if p.name == "taper")
    for weeks in range(MIN_VIABLE, 60):
        r = resolve_phases(weeks, DEFAULT_ARC)
        assert _names(r)[-1] == "taper"          # always at the very end
        assert taper.min_weeks <= dict(r.blocks)["taper"] <= taper.max_weeks


def test_a_short_plan_drops_phases_from_the_front_and_reports_them():
    r8 = resolve_phases(8, DEFAULT_ARC)
    assert r8.omitted == ("base",)              # base goes first
    assert [n for n, _ in r8.blocks] == ["build", "peak", "taper"]
    assert r8.unphased_reason is None

    # 11 weeks is enough for the whole arc.
    assert resolve_phases(11, DEFAULT_ARC).omitted == ()


def test_omissions_are_reported_in_arc_order_not_drop_order():
    arc = (
        Phase(name="a", share=0.4, hard_types=("endurance",),
              hard_volume_fraction=0.15),
        Phase(name="b", share=0.3, hard_types=("threshold",),
              hard_volume_fraction=0.2),
        Phase(name="c", share=0.2, hard_types=("vo2max",),
              hard_volume_fraction=0.25),
        Phase(name="d", share=0.1, hard_types=("vo2max",),
              hard_volume_fraction=0.25, min_weeks=2, max_weeks=2,
              anchored_end=True),
    )
    r = resolve_phases(8, arc)          # only b, c, d fit
    assert r.omitted == ("a",)
    order = [p.name for p in arc]
    assert list(r.omitted) == [n for n in order if n in r.omitted]


def test_a_long_plan_repeats_mesocycles_rather_than_stretching_one():
    r = resolve_phases(40, DEFAULT_ARC)
    names = [n for n, _ in r.blocks]
    assert names.count("base") > 1 and names.count("build") > 1
    for name, count in r.blocks:
        phase = next(p for p in DEFAULT_ARC if p.name == name)
        # No block is stretched past its natural ceiling.
        assert count <= phase.max_weeks, r.blocks
    # Only repeatable phases repeat; peak and taper appear exactly once.
    assert names.count("peak") == 1 and names.count("taper") == 1


def test_repetition_never_stretches_any_block_past_its_ceiling():
    for weeks in range(20, 80):
        for name, count in resolve_phases(weeks, DEFAULT_ARC).blocks:
            phase = next(p for p in DEFAULT_ARC if p.name == name)
            assert count <= phase.max_weeks, (weeks, name, count)


# ------------------------------------------------- the shape of the result
# Counts alone do not say whether an arc is coherent TRAINING. These assert the
# progression itself: a repeated body must still climb, and a plan too short to
# hold a structure must not emit a fragment of one.

def _declared_order(arc):
    return {p.name: i for i, p in enumerate(arc)}


def test_a_repeated_body_never_enters_peak_out_of_a_base():
    """base -> peak skips the block that bridges them, and puts the arc's
    lowest-intensity work directly before its highest."""
    for weeks in range(MIN_VIABLE, 60):
        names = [n for n, _ in resolve_phases(weeks, DEFAULT_ARC).blocks]
        if "peak" not in names:
            continue
        before_peak = names[names.index("peak") - 1]
        assert before_peak == "build", (weeks, names)


def test_block_order_always_follows_the_arcs_declared_order():
    """Within a cycle the order is the arc's own; the only allowed step back
    is a wrap from the LAST repeatable phase to the FIRST one."""
    order = _declared_order(DEFAULT_ARC)
    repeatable = [p.name for p in DEFAULT_ARC if p.repeatable]
    for weeks in range(1, 60):
        names = [n for n, _ in resolve_phases(weeks, DEFAULT_ARC).blocks]
        for a, b in zip(names, names[1:]):
            if order[b] > order[a]:
                continue
            assert a == repeatable[-1] and b == repeatable[0], (weeks, names)


def test_every_resolved_arc_in_the_whole_range_is_a_sane_structure():
    """The sweep that would have caught both defects at once."""
    order = _declared_order(DEFAULT_ARC)
    by_name = {p.name: p for p in DEFAULT_ARC}
    repeatable = [p.name for p in DEFAULT_ARC if p.repeatable]
    for weeks in range(1, 53):
        r = resolve_phases(weeks, DEFAULT_ARC)
        assert len(r.weeks) == weeks
        if r.unphased_reason:
            # Abandoned wholesale - no fragment of an arc survives.
            assert r.blocks == () and all(p is None for p in r.weeks)
            assert weeks < MIN_VIABLE
            continue
        names = [n for n, _ in r.blocks]
        # 1. Every week belongs to a phase, and every block is a real
        #    mesocycle inside its own bounds.
        assert sum(c for _, c in r.blocks) == weeks
        assert all(p is not None for p in r.weeks)
        for name, count in r.blocks:
            assert by_name[name].min_weeks <= count <= by_name[name].max_weeks
        # 2. At least a viable structure, ending on the taper.
        assert len(set(names)) >= min(MIN_VIABLE_PHASES, len(DEFAULT_ARC))
        assert names[-1] == "taper"
        # 3. The progression climbs, wrapping only between whole cycles.
        for a, b in zip(names, names[1:]):
            assert order[b] > order[a] or (a == repeatable[-1]
                                           and b == repeatable[0]), (weeks, names)


def test_a_plan_too_short_for_a_structure_is_unphased_with_a_reason():
    for weeks in range(1, MIN_VIABLE):
        r = resolve_phases(weeks, DEFAULT_ARC)
        assert r.weeks == (None,) * weeks
        assert r.blocks == ()
        assert set(r.omitted) == {p.name for p in DEFAULT_ARC}
        assert r.unphased_reason
        assert str(MIN_VIABLE) in r.unphased_reason
    # ...and one week more is periodized.
    assert resolve_phases(MIN_VIABLE, DEFAULT_ARC).unphased_reason is None


def test_the_viability_floor_is_not_met_by_shrinking_phases():
    """The arc is abandoned, never rescued by lowering a phase's minimum."""
    for weeks in range(1, MIN_VIABLE):
        assert resolve_phases(weeks, DEFAULT_ARC).blocks == ()
    taper = next(p for p in DEFAULT_ARC if p.name == "taper")
    assert taper.min_weeks == 2 and MIN_PHASE_WEEKS == 3


def test_minimum_viable_weeks_matches_where_the_resolver_gives_up():
    assert minimum_viable_weeks(DEFAULT_ARC) == MIN_VIABLE
    assert resolve_phases(MIN_VIABLE - 1, DEFAULT_ARC).unphased_reason
    assert resolve_phases(MIN_VIABLE, DEFAULT_ARC).unphased_reason is None
    # An arc with fewer phases than the viability floor needs all of them.
    two = (
        Phase(name="build", share=0.7, hard_types=("threshold",),
              hard_volume_fraction=0.2),
        Phase(name="taper", share=0.3, hard_types=("vo2max",),
              hard_volume_fraction=0.25, min_weeks=2, max_weeks=2,
              volume_multiplier=0.6, anchored_end=True),
    )
    assert minimum_viable_weeks(two) == 5
    assert resolve_phases(4, two).unphased_reason
    assert resolve_phases(5, two).blocks == (("build", 3), ("taper", 2))


def test_a_phase_with_no_ceiling_absorbs_the_remainder():
    arc = (
        Phase(name="base", share=0.6, hard_types=("sweet_spot",),
              hard_volume_fraction=0.2),
        Phase(name="peak", share=0.4, hard_types=("vo2max",),
              hard_volume_fraction=0.3),
    )
    r = resolve_phases(30, arc)
    assert sum(c for _, c in r.blocks) == 30
    assert dict(r.blocks)["base"] > dict(r.blocks)["peak"]


def test_resolution_is_deterministic():
    for weeks in (3, 7, 12, 16, 23, 41):
        results = [resolve_phases(weeks, DEFAULT_ARC) for _ in range(5)]
        assert all(r == results[0] for r in results)


def test_no_phases_resolves_to_an_unperiodized_plan():
    r = resolve_phases(6, ())
    assert r.weeks == (None,) * 6 and r.blocks == () and r.omitted == ()
    assert r.phase_for(0) is None and r.phase_for(99) is None


def test_zero_weeks_is_refused():
    with pytest.raises(ValueError):
        resolve_phases(0, DEFAULT_ARC)


# ------------------------------------------------------------ generation
def _weeks_of(p):
    """{week_number: [workouts]} for a generated plan."""
    start = dt.date.fromisoformat(p["start_date"])
    out = {}
    for w in p["workouts"]:
        wk = (dt.date.fromisoformat(w["date"]) - start).days // 7
        out.setdefault(wk, []).append(w)
    return out


def test_phases_change_the_hard_types_week_to_week():
    p = plan.generate_plan("P", MONDAY, 16, [0, 2, 4, 5], 8.0, 2,
                           phases=DEFAULT_ARC)
    by_week = _weeks_of(p)
    assignment = p["phases"]["weeks"]
    base_weeks = [i for i, n in enumerate(assignment) if n == "base"]
    peak_weeks = [i for i, n in enumerate(assignment) if n == "peak"]
    base_types = {w["type"] for i in base_weeks for w in by_week[i]}
    peak_types = {w["type"] for i in peak_weeks for w in by_week[i]}
    # The base block never prescribes VO2max; the peak block leans on it.
    assert "vo2max" not in base_types
    assert "vo2max" in peak_types
    # A flat plan would have used exactly one rotation everywhere.
    flat = plan.generate_plan("P", MONDAY, 16, [0, 2, 4, 5], 8.0, 2)
    assert [w["type"] for w in p["workouts"]] != [w["type"] for w in flat["workouts"]]


def test_generation_with_phases_is_deterministic():
    def gen():
        p = plan.generate_plan("P", MONDAY, 16, [0, 2, 4, 5], 8.0, 2,
                               phases=DEFAULT_ARC, races=_GRID_RACES)
        return [(w["date"], w["name"], w["type"], w["variant"], w["duration_s"],
                 w["tss"], zwo.zwo_string(w["session"])) for w in p["workouts"]]

    assert gen() == gen() == gen()


def test_a_phased_plan_reports_its_blocks_and_omissions():
    p = plan.generate_plan("P", MONDAY, 8, [0, 2, 4], 6.0, 1, phases=DEFAULT_ARC)
    assert p["phases"]["omitted"] == ["base"]
    assert [b["name"] for b in p["phases"]["blocks"]] == ["build", "peak", "taper"]
    assert len(p["phases"]["weeks"]) == 8


@pytest.mark.parametrize("weeks", list(range(1, MIN_VIABLE)))
def test_a_plan_too_short_to_periodize_generates_exactly_as_an_unphased_one(
    pre_phases, weeks
):
    """The arc is abandoned, so the rider gets a normal plan - not a taper
    bolted to nothing - and the caller gets a sentence explaining why."""
    args = ("P", MONDAY, weeks, [0, 2, 4], 6.0, 1)
    p = plan.generate_plan(*args, phases=DEFAULT_ARC, races=_GRID_RACES)
    old = pre_phases.generate_plan(*args, races=_GRID_RACES)
    assert p["phases"]["unphased_reason"]
    assert p["phases"]["weeks"] == [None] * weeks
    assert _comparable(p) == _comparable(old)
    assert [zwo.zwo_string(w["session"]) for w in p["workouts"]] == [
        zwo.zwo_string(w["session"]) for w in old["workouts"]
    ]


def test_phase_driven_sessions_still_respect_the_feasibility_floors():
    hard = Phase(name="all_hard", share=1.0, hard_types=("vo2max", "threshold"),
                 hard_volume_fraction=0.95, volume_multiplier=0.5)
    for hours, days, hit, model in _configs():
        if plan.validate_plan_inputs(8, days, hours, hit, None, model):
            continue
        p = plan.generate_plan("P", MONDAY, 8, days, hours, hit, model=model,
                               phases=(hard,), races=_GRID_RACES)
        for w in p["workouts"]:
            minutes = w["duration_s"] / 60.0
            if w["type"] in plan.HARD_KINDS and w["type"] != "sweet_spot":
                assert minutes >= plan.MIN_SESSION_MIN
            assert minutes >= plan.MIN_SESSION_MIN
        for wk in p["weekly"]:
            assert wk["total_s"] <= hours * 3600


# ----------------------------------------------------------- composition
def _minutes_on(p, date):
    return next((w["duration_s"] / 60.0 for w in p["workouts"]
                 if w["date"] == date), None)


def test_a_phase_multiplier_and_a_race_taper_take_the_deeper_not_the_product():
    """0.6 phase x 0.45 race taper would be 0.27; the deeper cut is 0.45."""
    taper_phase = Phase(name="cut", share=1.0, hard_types=("endurance",),
                        hard_volume_fraction=0.2, volume_multiplier=0.6)
    days = [0, 1, 2, 3, 4]
    race = [{"id": 1, "date": "2026-07-27", "priority": "A", "duration_min": 120}]
    # 2026-07-22 is inside the near taper window (-5 days) of the race.
    inside = "2026-07-22"

    # No hard days: every session is a plain share of the week, so a day's
    # minutes read the applied multiplier directly instead of through the
    # HIT clamps.
    def gen(**kw):
        return plan.generate_plan("P", MONDAY, 4, days, 10.0, 0, **kw)

    full = gen()
    phased_only = gen(phases=(taper_phase,))
    raced_only = gen(races=race)
    both = gen(races=race, phases=(taper_phase,))

    base = _minutes_on(full, inside)
    assert _minutes_on(phased_only, inside) == pytest.approx(base * 0.6, abs=1.5)
    assert _minutes_on(raced_only, inside) == pytest.approx(base * 0.45, abs=1.5)
    # The deeper of the two, NOT 0.6 * 0.45 = 0.27.
    combined = _minutes_on(both, inside)
    assert combined == pytest.approx(base * 0.45, abs=1.5)
    assert combined > base * 0.27 * 1.2
    # And it is exactly what the race taper alone produced.
    assert combined == _minutes_on(raced_only, inside)


def test_a_phase_multiplier_and_a_recovery_week_take_the_deeper_not_the_product():
    """Week 4 is a recovery week (0.65); a 0.6 phase must not compound it."""
    cut = Phase(name="cut", share=1.0, hard_types=("threshold",),
                hard_volume_fraction=0.2, volume_multiplier=0.6)
    days = [0, 1, 2, 3, 4]
    p = plan.generate_plan("P", MONDAY, 4, days, 10.0, 0, phases=(cut,))
    flat = plan.generate_plan("P", MONDAY, 4, days, 10.0, 0)
    budget_s = 10.0 * 3600

    normal, recovery = p["weekly"][0]["total_s"], p["weekly"][3]["total_s"]
    assert normal == pytest.approx(budget_s * 0.6, rel=0.02)
    # min(0.65, 0.6) = 0.6, not 0.65 * 0.6 = 0.39.
    assert recovery == pytest.approx(budget_s * 0.6, rel=0.02)
    assert recovery > budget_s * 0.39 * 1.2
    # A shallower phase loses to the recovery week instead.
    shallow = Phase(name="shallow", share=1.0, hard_types=("threshold",),
                    hard_volume_fraction=0.2, volume_multiplier=0.9)
    q = plan.generate_plan("P", MONDAY, 4, days, 10.0, 0, phases=(shallow,))
    assert q["weekly"][3]["total_s"] == flat["weekly"][3]["total_s"]


def test_a_phase_volume_multiplier_only_ever_reduces():
    """A phase week is capped by its own multiplier, never above the budget.

    (It is NOT compared against the unphased plan week by week: a different
    hard_volume_fraction redistributes minutes between hard and easy days, so a
    phased week can sit closer to the budget than a flat week does while still
    never exceeding it. The promise is about the rider's requested hours.)
    """
    by_name = {p.name: p for p in DEFAULT_ARC}
    for hours, days, hit, model in _configs():
        if plan.validate_plan_inputs(12, days, hours, hit, None, model):
            continue
        phased = plan.generate_plan("P", MONDAY, 12, days, hours, hit,
                                    model=model, races=_GRID_RACES,
                                    phases=DEFAULT_ARC)
        assignment = phased["phases"]["weeks"]
        n_hard = min(hit, len(days))
        # A reduced week still cannot go below the interval builders' floors -
        # exactly as a recovery week cannot today.
        floor_s = 60 * (n_hard * plan.HIT_MIN_MIN
                        + (len(days) - n_hard) * plan.MIN_SESSION_MIN)
        for wk in phased["weekly"]:
            phase = by_name.get(assignment[wk["week"] - 1])
            cap = hours * 3600 * (phase.volume_multiplier if phase else 1.0)
            assert wk["total_s"] <= hours * 3600
            # Only the full budget is reconciled to the minute; against the
            # phase's own cap each day can round up by half a minute.
            assert wk["total_s"] <= max(cap, floor_s) + 60 * len(days)


# --------------------------------------------------------------- reflow
def _seed_phased_plan(user_id, weeks=12):
    """Store a plan generated WITH phases, the way the route stores one."""
    recipe = reflow.build_recipe([0, 2, 4], 6.0, 1)
    generated = plan.generate_plan(
        "Phased", MONDAY, weeks, recipe["days_of_week"],
        recipe["hours_per_week"], recipe["hit_days_per_week"],
        phases=DEFAULT_ARC,
    )
    plan_id = db.create_plan(user_id, "Phased", generated["start_date"],
                             generated["weeks"], model=generated["model"],
                             recipe=recipe)
    for w in generated["workouts"]:
        db.add_plan_workout(
            plan_id, user_id, w["date"], w["name"], w["type"], w["duration_s"],
            w["tss"], zwo.zwo_string(w["session"]), variant=w.get("variant"),
            origin=reflow.GENERATED,
        )
    return plan_id


def test_reflow_of_a_phased_plan_converges_and_is_idempotent(user_id):
    """Reflow runs unattended nightly, so it must reach a fixed point."""
    plan_id = _seed_phased_plan(user_id)
    reflow.reflow_plan(user_id, plan_id, now=NOW)
    settled = db.plan_workouts_for_plan(user_id, plan_id, include_zwo=True)

    second = reflow.reflow_plan(user_id, plan_id, now=NOW)
    assert second["status"] == "ok"
    assert (second["updated"], second["inserted"], second["deleted"],
            second["failed"]) == (0, 0, 0, 0)
    assert db.plan_workouts_for_plan(user_id, plan_id, include_zwo=True) == settled


def test_phases_are_not_yet_wired_into_any_caller():
    """Phases are opted into through the Goal registry in a later step.

    Until then nothing in the product may pass an arc to generate_plan - a
    default arc would rewrite every stored plan on the next nightly sweep.
    """
    hits = subprocess.run(
        ["git", "grep", "-l", "-e", "DEFAULT_ARC", "-e", "phases=",
         "--", "wattracker/"],
        cwd=REPO, capture_output=True, text=True,
    ).stdout.split()
    assert set(hits) <= {"wattracker/prescribe/phases.py",
                         "wattracker/prescribe/plan.py"}
    assert ph.DEFAULT_ARC  # the reference arc exists and is constructible
