"""Aerobic decoupling and efficiency factor for steady Z2 efforts."""
from __future__ import annotations

from typing import Optional, Sequence

import numpy as np

from .power import normalized_power

MIN_STEADY_SECONDS = 45 * 60  # efforts must exceed 45 minutes


def _mean(vals: Sequence[float]) -> float:
    arr = np.array([0.0 if v is None else float(v) for v in vals], dtype=float)
    arr = np.nan_to_num(arr, nan=0.0)
    return float(arr.mean()) if arr.size else 0.0


def aerobic_decoupling(
    power: Sequence[float],
    heartrate: Sequence[float],
    min_seconds: int = MIN_STEADY_SECONDS,
) -> Optional[float]:
    """Aerobic decoupling % on a steady effort longer than `min_seconds`.

    ((HR2/P2) - (HR1/P1)) / (HR1/P1) * 100, where 1 = first half, 2 = second
    half of the effort. Returns None if the effort is too short or ratios are
    undefined (zero power).
    """
    n = min(len(power), len(heartrate))
    if n <= min_seconds:
        return None
    half = n // 2
    p1 = _mean(power[:half])
    p2 = _mean(power[half:n])
    hr1 = _mean(heartrate[:half])
    hr2 = _mean(heartrate[half:n])
    if p1 <= 0 or p2 <= 0:
        return None
    ratio1 = hr1 / p1
    ratio2 = hr2 / p2
    if ratio1 == 0:
        return None
    return ((ratio2 - ratio1) / ratio1) * 100.0


def efficiency_factor(
    power: Sequence[float], heartrate: Sequence[float]
) -> Optional[float]:
    """Efficiency Factor: EF = NP / mean HR."""
    mean_hr = _mean(heartrate)
    if mean_hr <= 0:
        return None
    return normalized_power(power) / mean_hr
