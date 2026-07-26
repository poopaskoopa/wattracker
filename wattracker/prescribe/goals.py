"""Training goals: what a plan's intensity is distributed TOWARD.

A ``PlanModel`` (prescribe/plan.py) answers *how* intensity is distributed -
Seiler's 80/20 against a sweet-spot base - and a ``Goal`` answers *what it is
distributed toward*: raising FTP, sharpening for criteriums, or finishing a
long day out. The two are deliberately ORTHOGONAL. Folding the goal into the
model registry would mean writing out every model x goal pair and maintaining
nine hard-day rotations that mostly differ in one phase; instead a goal carries
a periodization arc (a tuple of ``Phase``) and NOMINATES a default model that
the rider can override.

A goal is stored in ``plans.recipe`` rather than read fresh on every reflow, and
that is the one place a goal differs from a race or the rider's profile. Races
move and measured capacities drift, so both are re-read every night; the goal is
a deliberate choice the rider made when they created the plan, and re-deriving
it nightly could silently repoint a plan at a different arc. Existing plans have
no goal in their recipe at all: they resolve to ``arc=None`` and generate exactly
as they did before this module existed.

Each goal also names its PROGRESS SIGNAL - the measurement that says whether the
goal is being met - so the plan view can show the rider the number that actually
matters for the thing they are training for, rather than FTP for everybody.
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from typing import Dict, Optional, Sequence, Tuple

from .phases import MIN_PHASE_WEEKS, Phase, PhasePlan, resolve_phases

# ------------------------------------------------------------------- arcs
# Shares are the phase's requested fraction of the plan; the resolver snaps them
# to whole weeks, holds every phase at its floor, drops from the front when the
# plan is short and repeats whole (base, build) cycles when it is long. Every
# arc therefore has to be sensible across the whole 8-52 week range the form
# allows, not just at its ideal length - see tests/test_goals.py, which sweeps it.
#
# ``volume_multiplier`` is 1.0 everywhere but the taper: a phase may only ever
# REDUCE weekly volume (see prescribe/phases.py), and weekly hours are a promise.

FTP_ARC: Tuple[Phase, ...] = (
    Phase(
        name="base",
        share=0.40,
        # Sub-threshold work is the highest fitness-per-fatigue way to raise the
        # power a rider can hold for an hour; threshold appears once per cycle so
        # the adaptation is anchored at the duration being trained.
        hard_types=("sweet_spot", "sweet_spot", "threshold"),
        hard_volume_fraction=0.30,
        max_weeks=8,
        repeatable=True,
    ),
    Phase(
        name="build",
        share=0.30,
        hard_types=("threshold", "threshold", "vo2max"),
        hard_volume_fraction=0.28,
        max_weeks=6,
        repeatable=True,
    ),
    Phase(
        name="peak",
        share=0.20,
        # VO2max only: raising the ceiling is what lifts a threshold that has
        # stopped responding to more sub-threshold volume.
        hard_types=("vo2max",),
        hard_volume_fraction=0.30,
        max_weeks=4,
    ),
    Phase(
        name="taper",
        share=0.10,
        # Bosquet et al. 2007: the effect comes from cutting DURATION while
        # frequency and intensity hold, so the rotation is unchanged from the
        # block before it and only volume moves.
        hard_types=("vo2max", "threshold"),
        hard_volume_fraction=0.30,
        volume_multiplier=0.60,
        min_weeks=2,
        max_weeks=2,
        anchored_end=True,
    ),
)

CRITERIUM_ARC: Tuple[Phase, ...] = (
    Phase(
        name="base",
        share=0.35,
        # Endurance-heavy with a threshold touch: a criterium is decided by
        # repeated maximal efforts, and the thing that lets a rider repeat them
        # is aerobic support, not more top-end work in January.
        hard_types=("endurance", "threshold", "endurance"),
        hard_volume_fraction=0.18,
        max_weeks=8,
        repeatable=True,
    ),
    Phase(
        name="build",
        share=0.30,
        hard_types=("threshold", "vo2max"),
        hard_volume_fraction=0.22,
        max_weeks=6,
        repeatable=True,
    ),
    Phase(
        name="sharpen",
        share=0.22,
        # The only phase in the product that schedules sprints. A sprint session
        # is built from untargeted freeride efforts, which is why
        # ``plan.hard_seconds`` had to learn to count them before this arc could
        # exist - otherwise a sharpen week would report zero hard time.
        hard_types=("sprint", "vo2max"),
        hard_volume_fraction=0.25,
        max_weeks=4,
    ),
    Phase(
        name="taper",
        share=0.10,
        hard_types=("sprint", "vo2max"),
        hard_volume_fraction=0.25,
        volume_multiplier=0.60,
        min_weeks=2,
        max_weeks=2,
        anchored_end=True,
    ),
)

LONG_RIDE_ARC: Tuple[Phase, ...] = (
    Phase(
        name="base",
        share=0.40,
        hard_types=("endurance", "tempo", "endurance"),
        hard_volume_fraction=0.18,
        max_weeks=8,
        repeatable=True,
    ),
    Phase(
        name="build",
        share=0.28,
        hard_types=("tempo", "sweet_spot"),
        hard_volume_fraction=0.24,
        max_weeks=6,
        repeatable=True,
    ),
    Phase(
        name="durability",
        share=0.22,
        # Sweet spot with the long ride emphasised. The generator has no
        # long-ride concept - it splits the non-hard budget evenly across the
        # remaining days - so the only honest lever for "emphasise the long
        # ride" is to keep the hard fraction LOW, which leaves more of the
        # week's minutes on the endurance days. It is an approximation, and
        # naming a phase after it is not a claim that a single weekly long ride
        # is being scheduled.
        hard_types=("sweet_spot", "tempo"),
        hard_volume_fraction=0.16,
        max_weeks=5,
    ),
    Phase(
        name="taper",
        share=0.10,
        hard_types=("sweet_spot", "tempo"),
        hard_volume_fraction=0.16,
        volume_multiplier=0.60,
        min_weeks=2,
        max_weeks=2,
        anchored_end=True,
    ),
)


# --------------------------------------------------------- progress signals
# What the rider should watch to know whether the goal is being met. The keys
# are consumed by the plan view (server.py builds the panel from them); nothing
# here computes a measurement, it only names which one is relevant.

@dataclass(frozen=True)
class ProgressSignal:
    """One measurement that says whether a goal is being met.

    ``key`` selects the computation in the view layer; ``role`` is "primary" or
    "secondary". A secondary signal is shown only when there is evidence for it
    and is otherwise omitted entirely - never rendered as a zero.
    """

    key: str
    label: str
    description: str
    role: str = "primary"


@dataclass(frozen=True)
class Goal:
    """A training goal: an arc, a suggested model, and how progress is read."""

    key: str
    label: str
    description: str
    arc: Tuple[Phase, ...]
    # The goal SUGGESTS a model; the rider may pick any of them. Keeping the two
    # orthogonal is the whole design (see the module docstring).
    default_model: str
    signals: Tuple[ProgressSignal, ...]

    def __post_init__(self) -> None:
        if not self.arc:
            raise ValueError(f"goal '{self.key}': arc cannot be empty")
        if not self.signals:
            raise ValueError(f"goal '{self.key}': needs a progress signal")


GOALS: Dict[str, Goal] = {
    # Keys match prescribe/duration.py's baseline keys so a goal's length
    # recommendation is a straight lookup rather than a second mapping.
    "ftp": Goal(
        key="ftp",
        label="Raise FTP",
        description=(
            "Lift the power you can hold for an hour: a sub-threshold base, a "
            "threshold/VO2max build, a short VO2max peak and a taper."
        ),
        arc=FTP_ARC,
        default_model="sweet_spot",
        signals=(
            ProgressSignal(
                key="ftp_trend",
                label="FTP trend",
                description=(
                    "Your rolling FTP estimate over the plan. This is the goal's "
                    "own measurement, so it is the number to watch."
                ),
            ),
        ),
    ),
    "criterium": Goal(
        key="criterium",
        label="Criterium / short race",
        description=(
            "Sharpen for repeated maximal efforts: an endurance-heavy base, a "
            "threshold/VO2max build, a sprint-and-VO2max sharpening block and a "
            "taper."
        ),
        arc=CRITERIUM_ARC,
        default_model="polarized",
        signals=(
            ProgressSignal(
                key="peak_power",
                label="Peak 5s and 1min power",
                description=(
                    "A criterium is decided by short maximal efforts, so your "
                    "measured 5-second and 1-minute peaks are the goal's "
                    "signal - not FTP."
                ),
            ),
        ),
    ),
    "long_ride": Goal(
        key="long_ride",
        label="Long ride / gran fondo",
        description=(
            "Build fatigue resistance for a long day: an endurance/tempo base, a "
            "tempo/sweet-spot build, a durability block and a taper."
        ),
        arc=LONG_RIDE_ARC,
        default_model="pyramidal",
        signals=(
            # Decoupling is PRIMARY even though durability is the better
            # construct. Durability needs a hard 5-minute effort late in a long
            # ride, and steady endurance rides - the ones this goal actually
            # prescribes - rarely contain one, so the measurement is absent far
            # more often than it is present. Decoupling needs no maximal effort
            # at all, is computed on every long steady ride we already have, and
            # is the classic field marker for exactly this quality. So the
            # rider's headline number is the one that will actually be there,
            # and durability is shown underneath it when the evidence exists.
            ProgressSignal(
                key="decoupling",
                label="Aerobic decoupling",
                description=(
                    "Heart-rate drift against power on a long steady ride. "
                    "Lower is better; it needs no maximal effort, so it is "
                    "available from the endurance rides this plan prescribes."
                ),
            ),
            ProgressSignal(
                key="durability",
                label="Durability (late 5-min power retention)",
                description=(
                    "How much of your fresh 5-minute power you keep after "
                    "substantial work. A better measure of fatigue resistance, "
                    "but it needs a hard 5-minute effort late in a long ride, "
                    "so it is often absent."
                ),
                role="secondary",
            ),
        ),
    ),
}

DEFAULT_GOAL: Optional[str] = None  # no goal is the default: plans stay flat


def normalize_key(goal_key: object) -> Optional[str]:
    """The registry key for a caller-supplied goal, or None for 'no goal'.

    An unknown or malformed key is treated as NO GOAL rather than raising: a
    goal is an enhancement to a plan, and a recipe carrying a key we no longer
    recognize must still reflow into the flat plan it would have been.
    """
    if not isinstance(goal_key, str):
        return None
    key = goal_key.strip().lower()
    # A str subclass can return anything at all from strip()/lower().
    if type(key) is not str or key not in GOALS:
        return None
    return key


def get(goal_key: object) -> Optional[Goal]:
    """The ``Goal`` for a key, or None when there is no (recognized) goal."""
    key = normalize_key(goal_key)
    return GOALS[key] if key else None


def arc_for(goal_key: object) -> Optional[Tuple[Phase, ...]]:
    """The periodization arc to pass to ``generate_plan(phases=...)``.

    None means "no arc", which keeps ``generate_plan`` on the path it took
    before goals existed - byte-identical output, which every stored plan
    depends on across the nightly reflow.
    """
    goal = get(goal_key)
    return goal.arc if goal else None


def default_model_for(goal_key: object) -> Optional[str]:
    """The model a goal suggests, or None. Only ever a default - never forced."""
    goal = get(goal_key)
    return goal.default_model if goal else None


def resolve(goal_key: object, weeks: int) -> Optional[PhasePlan]:
    """Resolve a goal's arc against a week count, or None if there is no goal."""
    arc = arc_for(goal_key)
    return resolve_phases(weeks, arc) if arc else None


def phase_by_date(
    start_date: object, weeks: int, goal_key: object
) -> Dict[str, str]:
    """{ISO date: phase name} for every day the plan's arc covers.

    Used by the calendar so a day cell can say which block of the plan it sits
    in. Weeks the arc did not claim - and an arc that was abandoned as
    unviable - simply contribute no entries, so the caller renders nothing
    rather than a blank phase.
    """
    resolved = resolve(goal_key, weeks) if weeks and int(weeks) > 0 else None
    if resolved is None:
        return {}
    try:
        start = (
            start_date if isinstance(start_date, _dt.date)
            else _dt.date.fromisoformat(str(start_date))
        )
    except (ValueError, TypeError):
        return {}
    # The generator anchors week 0 to the Monday of the start week.
    monday0 = start - _dt.timedelta(days=start.weekday())
    out: Dict[str, str] = {}
    for index, phase in enumerate(resolved.weeks):
        if phase is None:
            continue
        for offset in range(7):
            day = monday0 + _dt.timedelta(days=7 * index + offset)
            out[day.isoformat()] = phase.name
    return out


def block_summary(goal_key: object, weeks: int) -> Optional[dict]:
    """The arc's blocks/omissions for display, or None when there is no goal.

    Mirrors the ``phases`` key ``generate_plan`` emits, so the plan form can
    show a rider what an arc WOULD look like at a chosen length before any plan
    exists.
    """
    resolved = resolve(goal_key, weeks)
    if resolved is None:
        return None
    return {
        "blocks": [{"name": name, "weeks": count}
                   for name, count in resolved.blocks],
        "omitted": list(resolved.omitted),
        "weeks": [p.name if p else None for p in resolved.weeks],
        "unphased_reason": resolved.unphased_reason,
    }


def all_goals() -> Sequence[Goal]:
    """Every goal, in registry order - the order the picker shows them in."""
    return tuple(GOALS.values())


__all__ = [
    "CRITERIUM_ARC",
    "DEFAULT_GOAL",
    "FTP_ARC",
    "GOALS",
    "LONG_RIDE_ARC",
    "MIN_PHASE_WEEKS",
    "Goal",
    "ProgressSignal",
    "all_goals",
    "arc_for",
    "block_summary",
    "default_model_for",
    "get",
    "normalize_key",
    "phase_by_date",
    "resolve",
]
