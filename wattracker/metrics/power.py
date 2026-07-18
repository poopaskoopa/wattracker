"""Power-based metrics: Normalized Power, Intensity Factor, TSS, FTP estimate.

All functions operate on per-second power streams (one sample per second).
"""
from __future__ import annotations

import math
from typing import Iterable, Sequence

import numpy as np


# --- Detraining decay model for the FTP estimate ------------------------------
# The FTP estimate weights each past effort by how much fitness is expected to
# have decayed since it was ridden. Efforts inside a short grace window count in
# full; after that, the weight decays exponentially with an e-folding time of
# FTP_DECAY_TAU_DAYS. This replaces the old hard trailing window, which cliffed
# every pre-break effort to zero the moment the anchor moved past it and made
# the estimate collapse after a training layoff.
FTP_DECAY_GRACE_DAYS = 10   # no detraining penalty inside this many days
FTP_DECAY_TAU_DAYS = 240    # e-folding time of detraining after the grace period


def detraining_factor(days_since: float) -> float:
    """Fraction of a past effort's power still assumed available after a layoff.

    1.0 for efforts within FTP_DECAY_GRACE_DAYS, then a smooth exponential decay
    (tau = FTP_DECAY_TAU_DAYS) afterwards. Roughly 0.875 at 42 days, i.e. a
    ~12% loss after a six-week break, in line with detraining physiology.
    """
    if days_since <= FTP_DECAY_GRACE_DAYS:
        return 1.0
    return math.exp(-(days_since - FTP_DECAY_GRACE_DAYS) / FTP_DECAY_TAU_DAYS)


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


def _activity_power_streams(activities: Iterable) -> "list[tuple[object, Sequence[float]]]":
    """Extract (when, per-second power stream) pairs from a mixed iterable.

    Each item may be:
      - a raw power stream (list/sequence of numbers) -> ``when`` is None, or
      - an activity dict with a "streams" mapping and optional "start_time" ->
        ``when`` is the parsed naive datetime (or None if unparseable).
    """
    from ..timeutil import parse_naive

    out: "list[tuple[object, Sequence[float]]]" = []
    for item in activities:
        if isinstance(item, dict):
            power = (item.get("streams") or {}).get("power") or item.get("power")
            if not power:
                continue
            out.append((parse_naive(item.get("start_time")), power))
        else:
            out.append((None, item))
    return out


def estimate_ftp(
    activities: Iterable,
    override: float | None = None,
    window_days: "int | None" = None,
    now=None,
) -> float:
    """Estimate FTP as best (detraining-weighted) 20-min power * 0.95.

    Each activity contributes ``best_20min_power * detraining_factor(days since
    the effort)``; the estimate is 0.95 * the maximum of those weighted values.
    A recent hard effort therefore dominates, while older efforts fade smoothly
    rather than dropping off a cliff - so the estimate decays honestly through a
    training break instead of collapsing.

    Accepts either raw power streams or activity dicts (with "streams" /
    "start_time"). When ``now`` is None, no decay is applied and the result is
    simply best-20-min * 0.95 (used with raw streams). Undated items always get
    a decay factor of 1.0.

    ``window_days`` is deprecated and ignored (the smooth decay replaces the old
    hard trailing window); the kwarg is kept for call-site compatibility.
    A user override always wins when provided and positive.
    """
    if override is not None and override > 0:
        return float(override)

    import datetime as _dt

    best = 0.0
    for when, stream in _activity_power_streams(activities):
        b = best_20min_power(stream)
        if b <= 0:
            continue
        if now is not None and when is not None:
            days = (now - when).total_seconds() / 86400.0
            b *= detraining_factor(days)
        if b > best:
            best = b
    return best * 0.95
