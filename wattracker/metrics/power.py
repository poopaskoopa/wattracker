"""Power-based metrics: Normalized Power, Intensity Factor, TSS, FTP estimate.

All functions operate on per-second power streams (one sample per second).
"""
from __future__ import annotations

import math
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
#     grace window). These decay very slowly - a mild staleness term that keeps a
#     year of easy-only riding from pegging FTP to one ancient hard effort.
# factor = exp(-idle_excess / TAU_IDLE  -  active_days / TAU_ACTIVE)
# This replaces the old trailing hard window (which cliffed pre-break efforts to
# zero) and the effort-age decay (which wrongly charged detraining for days the
# rider was actually training).
FTP_DECAY_GRACE_DAYS = 14      # inactivity gaps shorter than this cost nothing
FTP_DECAY_TAU_IDLE = 240       # e-folding time of detraining while off the bike
FTP_DECAY_TAU_ACTIVE = 1440    # slow staleness of an old effort while still training


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
    ``active_days`` (days spent training, or inside a grace window) decay slowly
    (tau = FTP_DECAY_TAU_ACTIVE). See ``_idle_active_days`` for how the split is
    derived from the activity calendar. A continuously-training rider barely
    decays; a long layoff decays substantially.
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
      - ``efforts`` is a list of ``(when, power_stream)`` for items that carry
        power (``when`` is the parsed naive datetime, or None for raw streams /
        undated dicts).
      - ``activity_days`` is the sorted list of every dated activity's timestamp
        - including power-less rides, which still count as time on the bike for
        the detraining-gap calendar.
    """
    from ..timeutil import parse_naive

    efforts: "list[tuple[object, Sequence[float]]]" = []
    activity_days: list = []
    for item in activities:
        if isinstance(item, dict):
            when = parse_naive(item.get("start_time"))
            if when is not None:
                activity_days.append(when)
            power = (item.get("streams") or {}).get("power") or item.get("power")
            if power:
                efforts.append((when, power))
        else:
            efforts.append((None, item))
    activity_days.sort()
    return efforts, activity_days


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

    efforts, activity_days = _split_activities(activities)

    best = 0.0
    for when, stream in efforts:
        b = best_20min_power(stream)
        if b <= 0:
            continue
        if now is not None and when is not None:
            idle, active = _idle_active_days(when, now, activity_days)
            b *= detraining_factor(idle, active)
        if b > best:
            best = b
    return best * 0.95
