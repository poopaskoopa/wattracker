"""Power-based metrics: Normalized Power, Intensity Factor, TSS, FTP estimate.

All functions operate on per-second power streams (one sample per second).
"""
from __future__ import annotations

import datetime
import math
from collections.abc import Mapping
from typing import Iterable, Sequence

import numpy as np


# --- Gap-aware detraining decay model for the FTP estimate --------------------
# The FTP estimate weights each past effort by how much fitness is expected to
# have decayed SINCE it was ridden - but detraining accrues only while the rider
# is OFF the bike, not merely because an effort is old. So the interval from an
# effort to the evaluation anchor is split, using the rider's activity calendar,
# into:
#   * idle-excess days: the portion of each inactivity gap beyond a short grace
#     window (short breaks cost nothing - research shows minimal loss under ~2
#     weeks). This includes the trailing gap from the last ride up to the anchor,
#     so the estimate keeps decaying while a rider stays away.
#   * active days: everything else (days the rider was training, or inside a
#     grace window). These are NOT charged detraining - a rider who never stops
#     riding has not detrained. They carry only a very slow EVIDENCE-STALENESS
#     discount (~5% per unbroken training year), because a 20-minute number from
#     three years ago is weaker evidence about today than last month's, and
#     without it one ancient hard effort would peg FTP forever for a rider who
#     only ever rides easy afterwards.
# factor = exp(-idle_excess / TAU_IDLE  -  active_days / TAU_ACTIVE)
# This replaces the old trailing hard window (which cliffed pre-break efforts to
# zero) and the effort-age decay (which wrongly charged detraining for days the
# rider was actually training).
FTP_DECAY_GRACE_DAYS = 14      # inactivity gaps shorter than this cost nothing
FTP_DECAY_TAU_IDLE = 240       # e-folding time of detraining while off the bike

# The active term is calibrated in the unit it is actually reasoned about: how
# much a year of UNBROKEN training discounts an effort ridden at its start.
#
# It was previously tau = 1440 days, which costs 1 - exp(-365/1440) = 22% a
# year. That is a detraining-sized number charged to a rider who never stopped
# training - exactly what the model above says must not happen, and on real data
# it was silently shaving several percent off riders who had done nothing wrong.
#
# It is not simply removed, because the case its docstring named is real: the
# recent-effort floor (below) only looks back FTP_FLOOR_WINDOW_DAYS, and it is a
# floor - it can raise the estimate but never lower it - so with no active term
# at all, a single hard effort from years ago would hold the estimate up
# forever, no matter how easy every ride since. 5%/yr keeps that from being
# permanent (a 3-year-old effort is discounted ~14%) while being far too slow to
# masquerade as detraining over the months that actually matter: a 6-month-old
# effort loses 2.5%, well inside the noise of the underlying 20-minute number.
FTP_DECAY_ACTIVE_ANNUAL_LOSS = 0.05
FTP_DECAY_TAU_ACTIVE = 365.0 / -math.log(1.0 - FTP_DECAY_ACTIVE_ANNUAL_LOSS)


def _idle_active_days(effort, anchor, activity_days) -> "tuple[float, float]":
    """Split the interval [effort, anchor] into (idle_excess_days, active_days).

    ``activity_days`` is the sorted list of the user's activity timestamps (any
    ride counts, even with no/zero power - time on the bike maintains fitness).
    Consecutive gaps between activity days in (effort, anchor], plus the trailing
    gap from the last such activity up to ``anchor``, each contribute
    ``max(0, gap_days - FTP_DECAY_GRACE_DAYS)`` to the idle excess. Active days
    are the remainder of the span.
    """
    span = (anchor - effort).total_seconds() / 86400.0
    if span <= 0:
        return 0.0, 0.0
    boundaries = [effort]
    for d in activity_days:
        if effort < d <= anchor:
            boundaries.append(d)
    boundaries.append(anchor)
    idle_excess = 0.0
    for a, b in zip(boundaries, boundaries[1:]):
        gap = (b - a).total_seconds() / 86400.0
        if gap > FTP_DECAY_GRACE_DAYS:
            idle_excess += gap - FTP_DECAY_GRACE_DAYS
    active = span - idle_excess
    if active < 0.0:
        active = 0.0
    return idle_excess, active


def detraining_factor(idle_excess_days: float, active_days: float) -> float:
    """Fraction of a past effort's power still assumed available now.

    Decay tracks INACTIVITY, not effort age: ``idle_excess_days`` (inactivity
    beyond the grace window) decays fast (tau = FTP_DECAY_TAU_IDLE), while
    ``active_days`` (days spent training, or inside a grace window) carry only
    the evidence-staleness discount of FTP_DECAY_ACTIVE_ANNUAL_LOSS per year
    (tau = FTP_DECAY_TAU_ACTIVE). See ``_idle_active_days`` for how the split is
    derived from the activity calendar. A continuously-training rider is
    essentially undecayed over a season; a long layoff decays substantially.
    """
    return math.exp(
        -idle_excess_days / FTP_DECAY_TAU_IDLE - active_days / FTP_DECAY_TAU_ACTIVE
    )


def _clean_power(power: Iterable[float]) -> np.ndarray:
    """Coerce a power stream to a float numpy array, treating None/NaN as 0."""
    arr = np.array([0.0 if p is None else float(p) for p in power], dtype=float)
    arr = np.nan_to_num(arr, nan=0.0)
    return arr


def rolling_mean(values: Sequence[float], window: int) -> np.ndarray:
    """Simple trailing rolling mean over `window` samples.

    Returns an array of the fully-populated windows (length N-window+1).
    """
    arr = _clean_power(values)
    if window <= 1:
        return arr
    if len(arr) < window:
        return np.array([], dtype=float)
    cumsum = np.cumsum(np.insert(arr, 0, 0.0))
    return (cumsum[window:] - cumsum[:-window]) / float(window)


def normalized_power(power: Sequence[float], window: int = 30) -> float:
    """Normalized Power.

    30-second rolling average of power, raise each to the 4th power, take the
    mean, then the 4th root. For streams shorter than the window, fall back to
    the simple mean.
    """
    arr = _clean_power(power)
    if arr.size == 0:
        return 0.0
    if arr.size < window:
        return float(arr.mean())
    roll = rolling_mean(arr, window)
    if roll.size == 0:
        return float(arr.mean())
    return float(np.power(np.mean(np.power(roll, 4)), 0.25))


def intensity_factor(np_value: float, ftp: float) -> float:
    """IF = NP / FTP."""
    if ftp <= 0:
        return 0.0
    return float(np_value) / float(ftp)


def training_stress_score(
    duration_seconds: float, np_value: float, ftp: float
) -> float:
    """TSS = (duration_s * NP * IF) / (FTP * 3600) * 100.

    One hour at FTP yields exactly 100 TSS.
    """
    if ftp <= 0 or duration_seconds <= 0:
        return 0.0
    intensity = intensity_factor(np_value, ftp)
    return (float(duration_seconds) * float(np_value) * intensity) / (
        float(ftp) * 3600.0
    ) * 100.0


def tss_from_stream(power: Sequence[float], ftp: float) -> float:
    """Convenience: compute TSS directly from a per-second power stream."""
    arr = _clean_power(power)
    if arr.size == 0 or ftp <= 0:
        return 0.0
    npw = normalized_power(arr)
    return training_stress_score(arr.size, npw, ftp)


def best_20min_power(power: Sequence[float]) -> float:
    """Best 20-minute (1200s) rolling average power in a stream."""
    roll = rolling_mean(power, 1200)
    if roll.size == 0:
        return 0.0
    return float(roll.max())


def _split_activities(activities: Iterable):
    """Split a mixed activity iterable into efforts and the activity calendar.

    Returns ``(efforts, activity_days)`` where:
      - ``efforts`` is a list of ``(when, power_stream, rpe)`` for items that
        carry power (``when`` is the parsed naive datetime, or None for raw
        streams / undated dicts; ``rpe`` is the rider's 1-10 session rating when
        they recorded one, else None - see ``recent_effort_floor``).
      - ``activity_days`` is the sorted list of every dated activity's timestamp
        - including power-less rides, which still count as time on the bike for
        the detraining-gap calendar.
    """
    from ..timeutil import parse_naive

    efforts: "list[tuple[object, Sequence[float], object]]" = []
    activity_days: list = []

    def rpe_or_none(value):
        if value is None or isinstance(value, bool):
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def stream_or_none(value, *, persisted: bool = False):
        if value is None or isinstance(value, (str, bytes, bytearray, Mapping)):
            return None
        if persisted and not isinstance(value, (list, tuple)):
            return None
        try:
            iter(value)
        except TypeError:
            return None
        if isinstance(value, (list, tuple)) and not value:
            return None
        return value

    for item in activities:
        if isinstance(item, dict):
            when = parse_naive(item.get("start_time"))
            if when is not None:
                activity_days.append(when)
            streams = item.get("streams")
            power = streams.get("power") if isinstance(streams, Mapping) else None
            if power is None:
                power = item.get("power")
            power = stream_or_none(power, persisted=True)
            if power is not None:
                efforts.append((when, power, rpe_or_none(item.get("rpe"))))
        else:
            power = stream_or_none(item)
            if power is not None:
                efforts.append((None, power, None))
    activity_days.sort()
    return efforts, activity_days


# --- Recent-evidence floor ---------------------------------------------------
# The 20-minute test is the only effort the decayed estimate can see, and a
# rider who trains with structured ERG workouts never rides one: every interval
# is deliberately submaximal and 12-20 minutes long, so their best 20-minute
# number is diluted by the recoveries inside it and the estimate reads 15-20%
# low. These are the durations a long effort genuinely constrains FTP at, with
# the fraction of that mean power an FTP sits at (Coggan's 95%-of-20min
# extended either side: shorter efforts are further above FTP, a 60-minute
# effort IS FTP). Nothing shorter than 13 minutes is admitted - a 5-minute
# maximal effort says almost nothing about the hour, and letting one in would
# turn every VO2max session into an FTP bump.
FTP_FLOOR_DURATIONS = (
    (780, 0.92),    # 13 min
    (1200, 0.95),   # 20 min
    (1800, 0.98),   # 30 min
    (3600, 1.00),   # 60 min
)
FTP_FLOOR_WINDOW_DAYS = 42
# A submaximal effort under-reports what the rider could have done. RPE is the
# only evidence we have of how hard it actually was, so an effort the rider
# rated below 8/10 is credited a little more than its raw watts - 2.5% per RPE
# point below 8, capped at +10% so a single "easy" rating can never invent a
# fitness jump. No rating means no adjustment.
FTP_FLOOR_RPE_REFERENCE = 8
FTP_FLOOR_RPE_STEP = 0.025
FTP_FLOOR_RPE_MAX = 1.10


def _rpe_scale(rpe) -> float:
    """Submaximality credit for an effort the rider rated below 8/10."""
    if rpe is None or rpe >= FTP_FLOOR_RPE_REFERENCE:
        return 1.0
    scale = 1.0 + FTP_FLOOR_RPE_STEP * (FTP_FLOOR_RPE_REFERENCE - float(rpe))
    return min(FTP_FLOOR_RPE_MAX, scale)


def recent_effort_floor(
    activities: Iterable, now, window_days: int = FTP_FLOOR_WINDOW_DAYS
) -> float:
    """Highest FTP implied by any long effort ridden in the last ``window_days``.

    For every dated activity inside the window, the best rolling mean at each of
    ``FTP_FLOOR_DURATIONS`` is converted to its FTP equivalent and scaled by the
    rider's RPE (see ``_rpe_scale``); the maximum across every activity and
    duration is returned. Durations longer than the stream are skipped.

    Each contribution is then decayed by ``detraining_factor`` against the same
    activity calendar ``estimate_ftp`` uses. Without it the floor would cancel
    detraining inside its own window - the 20-minute mapping is numerically the
    same 0.95 the decayed estimator applies, so a rider who did one 20-minute
    max and then stopped riding for six weeks would keep the undecayed number
    for the whole window. A rider who kept training barely decays, which is
    exactly the case the floor exists to serve.

    This is a FLOOR, not an estimate: it only ever says "the rider demonstrably
    held this, so their FTP is at least this much". Returns 0.0 when ``now`` is
    None or nothing in the window qualifies.
    """
    if now is None:
        return 0.0
    cutoff = now - datetime.timedelta(days=int(window_days))
    efforts, activity_days = _split_activities(activities)
    best = 0.0
    for when, stream, rpe in efforts:
        if when is None or when < cutoff:
            continue
        idle, active = _idle_active_days(when, now, activity_days)
        scale = _rpe_scale(rpe) * detraining_factor(idle, active)
        for window, fraction in FTP_FLOOR_DURATIONS:
            roll = rolling_mean(stream, window)
            if roll.size == 0:
                continue
            implied = float(roll.max()) * fraction * scale
            if implied > best:
                best = implied
    return best


def estimate_ftp(
    activities: Iterable,
    override: float | None = None,
    window_days: "int | None" = None,
    now=None,
) -> float:
    """Estimate FTP as best (detraining-weighted) 20-min power * 0.95.

    Each dated effort contributes ``best_20min_power * detraining_factor(...)``
    where the factor is derived from the rider's activity calendar between the
    effort and ``now`` (see ``detraining_factor`` / ``_idle_active_days``):
    detraining accrues only during INACTIVITY, so an effort ridden while the
    rider kept training barely decays, while an effort followed by a long layoff
    decays substantially. The estimate is 0.95 * the maximum weighted value, so a
    recent hard effort dominates and old efforts fade smoothly rather than
    cliffing to zero.

    That figure is then floored by ``recent_effort_floor``: a rider who only
    does structured ERG work never rides a maximal 20 minutes, so the decayed
    number alone reads well below what their recent long efforts already prove
    they can hold. The floor never lowers the estimate.

    Accepts either raw power streams or activity dicts (with "streams" /
    "start_time"). When ``now`` is None, no decay is applied and the result is
    simply best-20-min * 0.95 (used with raw streams). Undated items always get
    a decay factor of 1.0.

    ``window_days`` is deprecated and ignored (the decay model replaces the old
    hard trailing window); the kwarg is kept for call-site compatibility.
    A user override always wins when provided and positive.
    """
    if override is not None and override > 0:
        return float(override)

    # Materialized because the recent-evidence floor iterates the same input a
    # second time and callers may pass a generator.
    activities = list(activities)
    efforts, activity_days = _split_activities(activities)

    best = 0.0
    for when, stream, _rpe in efforts:
        b = best_20min_power(stream)
        if b <= 0:
            continue
        if now is not None and when is not None:
            idle, active = _idle_active_days(when, now, activity_days)
            b *= detraining_factor(idle, active)
        if b > best:
            best = b
    decayed = best * 0.95
    if now is None:
        return decayed
    return max(decayed, recent_effort_floor(activities, now))
