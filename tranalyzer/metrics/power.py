"""Power-based metrics: Normalized Power, Intensity Factor, TSS, FTP estimate.

All functions operate on per-second power streams (one sample per second).
"""
from __future__ import annotations

from typing import Iterable, Sequence

import numpy as np


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


def _activity_power_streams(
    activities: Iterable,
    window_days: "int | None" = None,
    now=None,
) -> "list[Sequence[float]]":
    """Extract per-second power streams from a mixed iterable of activities.

    Each item may be:
      - a raw power stream (list/sequence of numbers), or
      - an activity dict with a "streams" mapping and optional "start_time".

    When `window_days` is given and an activity dict carries a parseable
    "start_time", only activities within the trailing window (relative to `now`)
    are kept. Raw-stream items are always kept (no date to filter on).
    """
    import datetime as _dt

    if now is None:
        now = _dt.datetime.now()
    cutoff = now - _dt.timedelta(days=window_days) if window_days else None

    out: "list[Sequence[float]]" = []
    for item in activities:
        if isinstance(item, dict):
            power = (item.get("streams") or {}).get("power") or item.get("power")
            if not power:
                continue
            if cutoff is not None:
                from ..timeutil import parse_naive

                when = parse_naive(item.get("start_time"))
                if when is not None and when < cutoff:
                    continue
            out.append(power)
        else:
            out.append(item)
    return out


def estimate_ftp(
    activities: Iterable,
    override: float | None = None,
    window_days: "int | None" = None,
    now=None,
) -> float:
    """Estimate FTP as best 20-min rolling avg power * 0.95.

    Uses the best 20-minute effort across the provided activities. Accepts
    either raw power streams or activity dicts (with "streams"/"start_time");
    when `window_days` is set, dated activities outside the trailing window are
    excluded. A user override always wins when provided and positive.
    """
    if override is not None and override > 0:
        return float(override)
    best = 0.0
    for stream in _activity_power_streams(activities, window_days=window_days, now=now):
        b = best_20min_power(stream)
        if b > best:
            best = b
    return best * 0.95
