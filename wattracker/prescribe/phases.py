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
    """

    weeks: Tuple[Optional[Phase], ...]
    blocks: Tuple[Tuple[str, int], ...]
    omitted: Tuple[str, ...]

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


def _repeat_length(phase: Phase) -> int:
    """How long one repeated block of this phase is."""
    return phase.max_weeks if phase.max_weeks is not None else phase.min_weeks


def _allocate_body(
    body: Sequence[Phase], available: int
) -> Tuple[List[List], List[str]]:
    """Split ``available`` weeks across the non-anchored phases.

    Returns (blocks, omitted) where blocks is a list of ``[phase, weeks]``.
    Blocks always consume ``available`` exactly, unless every phase was
    dropped, in which case no block is produced and the caller leaves those
    weeks unperiodized.
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

    total_share = sum(p.share for p in survivors)
    raw = [available * p.share / total_share for p in survivors]
    counts = [
        max(p.min_weeks, int(math.floor(r))) if p.max_weeks is None
        else min(p.max_weeks, max(p.min_weeks, int(math.floor(r))))
        for p, r in zip(survivors, raw)
    ]

    # Snap to whole weeks: give back what the floors over-claimed, then hand
    # out what rounding left over. Ties break on the lowest index so the
    # assignment is deterministic - reflow recomputes plans nightly and any
    # instability would rewrite every workout every night.
    while sum(counts) > available:
        eligible = [i for i, p in enumerate(survivors) if counts[i] > p.min_weeks]
        if not eligible:
            break
        i = max(eligible, key=lambda i: (counts[i] - raw[i], -i))
        counts[i] -= 1
    while sum(counts) < available:
        eligible = [
            i for i, p in enumerate(survivors)
            if p.max_weeks is None or counts[i] < p.max_weeks
        ]
        if not eligible:
            break  # everything is at its natural ceiling - repeat instead
        i = max(eligible, key=lambda i: (raw[i] - counts[i], -i))
        counts[i] += 1

    blocks: List[List] = [[p, c] for p, c in zip(survivors, counts)]
    leftover = available - sum(counts)
    if leftover > 0:
        _repeat_into(blocks, survivors, leftover)
    return blocks, omitted


def _fold_into(block: List, leftover: int) -> bool:
    """Add ``leftover`` weeks to a block if that stays inside its ceiling."""
    ceiling = block[0].max_weeks
    if ceiling is None or block[1] + leftover <= ceiling:
        block[1] += leftover
        return True
    return False


def _repeat_into(
    blocks: List[List], survivors: Sequence[Phase], leftover: int
) -> None:
    """Absorb ``leftover`` weeks by REPEATING mesocycles, in place.

    A plan longer than the arc's natural length gets another base/build cycle
    rather than one enormous stretched phase: the training effect of a block
    comes from its length being about right, so 12 weeks of "build" is not a
    longer build, it is a plateau.

    Weeks that cannot be placed without pushing some block past its ceiling are
    left unplaced; ``resolve_phases`` puts them at the FRONT of the plan as
    unperiodized weeks, which behave exactly as a plan with no arc at all.
    """
    repeatable = [p for p in survivors if p.repeatable]
    if not repeatable:
        # Nothing may repeat, so the last block absorbs what it can.
        _fold_into(blocks[-1], leftover)
        return
    # Repeats are inserted after the last repeatable block so the sequence
    # reads base, build, base, build, ..., peak, taper.
    insert_at = max(i for i, b in enumerate(blocks) if b[0] in repeatable) + 1
    added: List[List] = []
    k = 0
    while leftover > 0:
        phase = repeatable[k % len(repeatable)]
        k += 1
        take = min(_repeat_length(phase), leftover)
        if take < phase.min_weeks:
            # Too little left for a block of its own: fold it into the
            # preceding block if that block has headroom, otherwise leave it
            # unperiodized rather than stretch a phase past its ceiling.
            _fold_into(added[-1] if added else blocks[insert_at - 1], leftover)
            break
        added.append([phase, take])
        leftover -= take
    blocks[insert_at:insert_at] = added


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
    * Too short: phases are dropped from the FRONT and reported in ``omitted``.
    * Too long: ``repeatable`` mesocycles repeat instead of stretching.

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
    return PhasePlan(tuple(assigned), tuple(blocks), tuple(omitted))


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
