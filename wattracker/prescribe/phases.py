"""Periodization phases: how a plan's INTENSITY is distributed over its weeks.

A plan generated without phases is structurally flat - every week gets the same
``hard_types`` rotation and the same ``hard_volume_fraction``, so a 16-week plan
is one week repeated sixteen times. A phase is a mesocycle that overrides those
two knobs for the weeks it owns, so a plan can spend its early weeks on
sub-threshold work and its late weeks on VO2max without changing how long the
rider trains.

WEEKLY HOURS CAN NEVER INCREASE. That is a hard user promise, enforced in
``plan.generate_plan`` and property-tested across the whole configuration grid.
Periodization here therefore means REDISTRIBUTING INTENSITY INSIDE A FIXED
WEEKLY BUDGET, never ramping volume: a phase may vary ``hard_types`` and
``hard_volume_fraction`` freely, and its ``volume_multiplier`` may only ever
REDUCE (a taper or a recovery block) - a phase that tries to increase volume is
rejected at construction.

That restriction is also the better-evidenced design. Ronnestad's
block-periodization work found superior VO2max/Wmax gains against traditional
organization with total volume AND total intensity matched between the groups:
the variable under test was the DISTRIBUTION of the hard work, not its amount.
So distribution is the only thing phases move.

Phases are INERT unless a caller passes them. ``generate_plan(..., phases=None)``
is byte-identical to a plan generated before this module existed - same names,
types, variants, durations, TSS and .zwo output. This matters more than usual
because reflow runs unattended on the daily maintenance sweep: a phase arc that
applied by default would silently rewrite every existing plan and every exported
Zwift file the first time the sweep ran after deploy. Goal-specific arcs are
opted into through the ``Goal`` registry; until that lands this module has no
callers by design.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

# A mesocycle shorter than this is not a phase - it is a rounding artefact. A
# block needs enough weeks for the adaptation it targets to accumulate, so a
# phase that cannot reach its floor is DROPPED rather than shrunk to a stub.
MIN_PHASE_WEEKS = 3

# Fewer phases than this is not a structure, it is a fragment. Dropping from
# the front degrades an arc gracefully right up to the point where what is left
# stops being coherent training: a plan short enough to hold only the taper
# would be a taper for an event that does not exist, and one holding only
# peak + taper would be weeks of top-end work with no base under it - the way
# people get hurt. Below this, the arc is abandoned wholesale and the plan runs
# UNPERIODIZED, which is a perfectly good plan. The floors are never lowered to
# make an arc fit; that would just move the incoherence inside the phases.
MIN_VIABLE_PHASES = 3


@dataclass(frozen=True)
class Phase:
    """One mesocycle of a periodized plan.

    ``share`` is the fraction of the plan's weeks this phase would like; the
    resolver scales the shares to the actual week count and snaps to whole
    weeks. ``hard_types`` and ``hard_volume_fraction`` override the plan
    model's for every week the phase owns.

    ``volume_multiplier`` scales the week's minutes and MUST be <= 1.0 (see the
    module docstring). It composes with race tapers and recovery weeks by
    taking the DEEPER reduction, never by multiplying.

    ``min_weeks`` defaults to ``MIN_PHASE_WEEKS``; a taper deliberately sets it
    lower, because a taper is not a mesocycle - two weeks is the shape the
    tapering literature actually describes. ``max_weeks`` is the phase's
    natural ceiling: a plan longer than the arc repeats ``repeatable`` blocks
    rather than stretching one phase past it. ``anchored_end`` phases are
    allocated FIRST, from the end of the plan backward.
    """

    name: str
    share: float
    hard_types: Tuple[str, ...]
    hard_volume_fraction: float
    volume_multiplier: float = 1.0
    min_weeks: int = MIN_PHASE_WEEKS
    max_weeks: Optional[int] = None
    repeatable: bool = False
    anchored_end: bool = False

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("a phase needs a name")
        if not (self.share > 0):
            raise ValueError(f"phase '{self.name}': share must be > 0")
        if not self.hard_types:
            raise ValueError(f"phase '{self.name}': hard_types cannot be empty")
        if not (0.0 < self.hard_volume_fraction <= 1.0):
            raise ValueError(
                f"phase '{self.name}': hard_volume_fraction must be in (0, 1]"
            )
        # The whole point of the design: phases redistribute intensity inside a
        # fixed weekly budget. Anything above 1.0 would hand a rider more hours
        # than they asked for, which no phase is allowed to do.
        if not (0.0 < self.volume_multiplier <= 1.0):
            raise ValueError(
                f"phase '{self.name}': volume_multiplier must be in (0, 1] - a "
                f"phase may only ever reduce weekly volume, never increase it "
                f"(got {self.volume_multiplier})"
            )
        if self.min_weeks < 1:
            raise ValueError(f"phase '{self.name}': min_weeks must be >= 1")
        if self.max_weeks is not None and self.max_weeks < self.min_weeks:
            raise ValueError(
                f"phase '{self.name}': max_weeks cannot be below min_weeks"
            )


@dataclass(frozen=True)
class PhasePlan:
    """A resolved week-by-week phase assignment.

    ``weeks[i]`` is the phase owning 0-indexed week ``i``, or None for a week
    no phase could be fitted to - such a week falls back to the plan model's
    own ``hard_types``/``hard_volume_fraction``, i.e. exactly today's flat
    behaviour. ``blocks`` is the same information as consecutive
    ``(phase_name, week_count)`` runs, and ``omitted`` names the phases that
    did not fit at all, so a caller can tell the rider "this is a build block,
    not full preparation".

    ``unphased_reason`` is set when the arc was abandoned entirely because the
    plan is too short to carry a coherent structure (see
    ``MIN_VIABLE_PHASES``). Every week is then None and the plan generates
    exactly as an unperiodized one - which is a real training plan, where a
    taper bolted to nothing is not. It is a message for the rider, e.g. "12
    weeks is too short for this arc; the plan runs unperiodized".
    """

    weeks: Tuple[Optional[Phase], ...]
    blocks: Tuple[Tuple[str, int], ...]
    omitted: Tuple[str, ...]
    unphased_reason: Optional[str] = None

    def phase_for(self, week_index: int) -> Optional[Phase]:
        """The phase owning a 0-indexed week (None outside the plan)."""
        if 0 <= week_index < len(self.weeks):
            return self.weeks[week_index]
        return None


def _natural_weeks(phase: Phase, weeks: int) -> int:
    """The phase's proportional length, clamped to its own floor and ceiling."""
    want = int(math.floor(phase.share * weeks + 0.5))
    want = max(want, phase.min_weeks)
    if phase.max_weeks is not None:
        want = min(want, phase.max_weeks)
    return want


def _proportional(sequence: Sequence[Phase], available: int) -> List[int]:
    """Whole-week lengths for an ordered block sequence, share-proportional.

    Every block lands inside its own ``[min_weeks, max_weeks]``. The result
    sums to ``available`` unless every block is already at its ceiling, in
    which case it sums to less and the caller decides what to do with the rest.
    """
    total_share = sum(p.share for p in sequence)
    raw = [available * p.share / total_share for p in sequence]
    counts = [
        max(p.min_weeks, int(math.floor(r))) if p.max_weeks is None
        else min(p.max_weeks, max(p.min_weeks, int(math.floor(r))))
        for p, r in zip(sequence, raw)
    ]

    # Snap to whole weeks: give back what the floors over-claimed, then hand
    # out what rounding left over. Ties break on the lowest index so the
    # assignment is deterministic - reflow recomputes plans nightly and any
    # instability would rewrite every workout every night.
    while sum(counts) > available:
        eligible = [i for i, p in enumerate(sequence) if counts[i] > p.min_weeks]
        if not eligible:
            break
        i = max(eligible, key=lambda i: (counts[i] - raw[i], -i))
        counts[i] -= 1
    while sum(counts) < available:
        eligible = [
            i for i, p in enumerate(sequence)
            if p.max_weeks is None or counts[i] < p.max_weeks
        ]
        if not eligible:
            break  # everything is at its ceiling - the caller adds a cycle
        i = max(eligible, key=lambda i: (raw[i] - counts[i], -i))
        counts[i] += 1
    return counts


def _allocate_body(
    body: Sequence[Phase], available: int
) -> Tuple[List[List], List[str]]:
    """Split ``available`` weeks across the non-anchored phases.

    Returns (blocks, omitted) where blocks is a list of ``[phase, weeks]``.

    A plan longer than the arc's natural length repeats whole MESOCYCLE
    CYCLES - base, build, base, build, ... - rather than stretching one phase:
    the training effect of a block comes from its length being about right, so
    12 weeks of "build" is not a longer build, it is a plateau. Repeating whole
    cycles in the arc's declared order is also what keeps the progression
    intact: the run of repeats always ENDS on the highest-intensity repeatable
    phase, so the plan enters its peak block from a build and never straight
    out of a base.
    """
    survivors = list(body)
    omitted: List[str] = []
    # Too short: drop phases from the FRONT (base goes first) rather than
    # crushing every phase proportionally into sub-mesocycle stubs. A build
    # block with no base is a coherent, if incomplete, plan; four one-week
    # "phases" are not.
    while survivors and sum(p.min_weeks for p in survivors) > available:
        omitted.append(survivors.pop(0).name)
    if not survivors:
        return [], omitted

    sequence = list(survivors)
    counts = _proportional(sequence, available)
    repeatable = [p for p in survivors if p.repeatable]
    # Add whole cycles while the weeks do not fit inside the blocks' ceilings.
    # Extra cycles are PREPENDED so the arc's own order still closes the body:
    # (base, build) x n, then the full declared sequence.
    cycles = 1
    while repeatable and sum(counts) < available:
        candidate = list(repeatable) * cycles + list(survivors)
        if sum(p.min_weeks for p in candidate) > available:
            break  # another cycle cannot reach its own floors
        candidate_counts = _proportional(candidate, available)
        if sum(candidate_counts) <= sum(counts):
            break  # no improvement; leave the rest unperiodized
        sequence, counts = candidate, candidate_counts
        cycles += 1

    return [[p, c] for p, c in zip(sequence, counts)], omitted


def resolve_phases(weeks: int, phases: Sequence[Phase]) -> PhasePlan:
    """Map a plan's week count to a per-week phase assignment. Pure.

    Allocation order, and why:

    * ``anchored_end`` phases (the taper) are allocated FIRST, from the end
      backward, and are never compressed below their own floor. The taper is
      the highest-confidence element in the whole design (Bosquet et al. 2007
      is a meta-analysis, not a single trial), so it is the one thing that does
      not get squeezed when the plan is short.
    * The remaining weeks are split proportionally by ``share`` and snapped to
      whole weeks, with every phase held at or above its ``min_weeks`` floor.
    * Too short: phases are dropped from the FRONT and reported in ``omitted``,
      until fewer than ``MIN_VIABLE_PHASES`` are left - at which point the arc
      is abandoned and the plan runs UNPERIODIZED with a stated reason, because
      the fragment that survives (a bare taper, or peak with no base under it)
      is worse training than no periodization at all.
    * Too long: whole ``repeatable`` mesocycle CYCLES repeat, in the arc's
      declared order, instead of any phase stretching.

    Deterministic: identical inputs always produce an identical assignment.
    """
    weeks = int(weeks)
    if weeks < 1:
        raise ValueError("weeks must be at least 1")
    if not phases:
        return PhasePlan((None,) * weeks, (), ())

    order = {p.name: i for i, p in enumerate(phases)}
    tail = [p for p in phases if p.anchored_end]
    body = [p for p in phases if not p.anchored_end]

    omitted: List[str] = []
    remaining = weeks
    tail_blocks: List[List] = []
    for phase in reversed(tail):
        if remaining <= 0:
            omitted.append(phase.name)
            continue
        # Never below the floor - unless the whole plan is shorter than it, in
        # which case the plan IS the taper.
        take = max(_natural_weeks(phase, weeks), phase.min_weeks)
        take = min(take, remaining)
        tail_blocks.insert(0, [phase, take])
        remaining -= take

    body_blocks, body_omitted = _allocate_body(body, remaining)
    omitted.extend(body_omitted)
    # Report omissions in the arc's own order, not the order they were dropped.
    omitted.sort(key=lambda name: order.get(name, 0))

    assigned: List[Optional[Phase]] = []
    blocks: List[Tuple[str, int]] = []
    # Weeks no phase could claim sit at the FRONT and stay unperiodized, so
    # they behave exactly as a plan with no phases at all.
    unclaimed = remaining - sum(b[1] for b in body_blocks)
    if unclaimed > 0:
        assigned.extend([None] * unclaimed)
    for phase, count in list(body_blocks) + tail_blocks:
        assigned.extend([phase] * count)
        if blocks and blocks[-1][0] == phase.name:
            blocks[-1] = (phase.name, blocks[-1][1] + count)
        else:
            blocks.append((phase.name, count))

    if len(assigned) != weeks:  # pragma: no cover - allocation is exact
        raise AssertionError(
            f"phase allocation produced {len(assigned)} weeks, expected {weeks}"
        )

    # The viability gate. It is checked on the RESULT rather than on the week
    # count, so it covers both a plan too short to hold the structure and one
    # where the phases' own floors happen not to fit.
    distinct = {name for name, _ in blocks}
    floors_met = all(
        count >= next(p for p in phases if p.name == name).min_weeks
        for name, count in blocks
    )
    if len(distinct) < min(MIN_VIABLE_PHASES, len(phases)) or not floors_met:
        return unphased(weeks, phases)

    return PhasePlan(tuple(assigned), tuple(blocks), tuple(omitted))


def minimum_viable_weeks(phases: Sequence[Phase]) -> int:
    """Shortest plan this arc can periodize coherently.

    The phases that survive a short plan are the LAST ones declared (dropping
    is from the front) plus every anchored-end phase, each at its floor.
    """
    if not phases:
        return 0
    tail = [p for p in phases if p.anchored_end]
    body = [p for p in phases if not p.anchored_end]
    need = max(0, min(MIN_VIABLE_PHASES, len(phases)) - len(tail))
    kept = body[len(body) - need:] if need else []
    return sum(p.min_weeks for p in tail + kept)


def unphased(weeks: int, phases: Sequence[Phase]) -> PhasePlan:
    """The arc gave up: every week runs on the plan model, with a reason."""
    floor = minimum_viable_weeks(phases)
    reason = (
        f"{weeks} week{'s' if weeks != 1 else ''} is too short to periodize "
        f"this arc ({', '.join(p.name for p in phases)}): it needs at least "
        f"{floor} weeks to hold {min(MIN_VIABLE_PHASES, len(phases))} phases "
        f"at their minimum lengths. The plan runs unperiodized."
    )
    return PhasePlan((None,) * weeks, (), tuple(p.name for p in phases), reason)


# ------------------------------------------------------------- reference arc
# A single worked example so the machinery above is exercised and testable.
# It is deliberately NOT attached to any plan model or route: step 6 wires
# goal-specific arcs through the Goal registry, and until then nothing in the
# product passes phases to generate_plan. Volume is flat (1.0) everywhere
# except the taper, which only ever reduces.
DEFAULT_ARC: Tuple[Phase, ...] = (
    Phase(
        name="base",
        share=0.40,
        # Sub-threshold work with a single threshold touch: the aerobic block.
        hard_types=("sweet_spot", "sweet_spot", "threshold"),
        hard_volume_fraction=0.15,
        min_weeks=MIN_PHASE_WEEKS,
        max_weeks=8,
        repeatable=True,
    ),
    Phase(
        name="build",
        share=0.30,
        hard_types=("threshold", "sweet_spot", "vo2max"),
        hard_volume_fraction=0.22,
        min_weeks=MIN_PHASE_WEEKS,
        max_weeks=6,
        repeatable=True,
    ),
    Phase(
        name="peak",
        share=0.20,
        hard_types=("vo2max", "threshold"),
        hard_volume_fraction=0.28,
        min_weeks=MIN_PHASE_WEEKS,
        max_weeks=4,
    ),
    Phase(
        name="taper",
        share=0.10,
        # Bosquet et al. 2007: the effect comes from cutting DURATION while
        # frequency and intensity hold, so the hard rotation is unchanged from
        # the peak block and only volume moves.
        hard_types=("vo2max", "threshold"),
        hard_volume_fraction=0.28,
        volume_multiplier=0.60,
        min_weeks=2,
        max_weeks=2,
        anchored_end=True,
    ),
)
