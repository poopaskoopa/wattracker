"""Mean-maximal power curve and Critical Power / W' model fit."""
from __future__ import annotations

from typing import Dict, Iterable, List, Sequence, Tuple

import numpy as np
from scipy.optimize import least_squares

from .power import rolling_mean

# Durations (seconds) at which we sample the mean-maximal power curve. A dense
# standard grid so the dashboard curve has a measured dot at every axis tick;
# includes 1s and 40min (2400s) for the race power-per-period table.
MMP_DURATIONS: Tuple[int, ...] = (
    1, 5, 10, 15, 30, 60, 120, 180, 300, 600, 900, 1200, 1800, 2400, 2700,
    3600, 5400,
)


def best_rolling_power(power: Sequence[float], window: int) -> float:
    """Best rolling-average power over `window` seconds within a single stream."""
    if window <= 1:
        arr = np.array([float(p or 0.0) for p in power], dtype=float)
        return float(arr.max()) if arr.size else 0.0
    roll = rolling_mean(power, window)
    return float(roll.max()) if roll.size else 0.0


def mean_maximal_power(
    activities_power: Iterable[Sequence[float]],
    durations: Sequence[int] = MMP_DURATIONS,
) -> Dict[int, float]:
    """Best rolling-average power for each duration across activities.

    Callers should pass only activities within the trailing 90-day window.
    Returns a mapping duration_seconds -> best power (watts). Durations with no
    qualifying data (stream shorter than the window) are omitted.
    """
    result: Dict[int, float] = {}
    streams = [list(s) for s in activities_power]
    for d in durations:
        best = 0.0
        found = False
        for stream in streams:
            if len(stream) >= d or d <= 1:
                b = best_rolling_power(stream, d)
                if b > 0:
                    found = True
                best = max(best, b)
        if found:
            result[d] = best
    return result


def fit_cp_wprime(mmp: Dict[int, float]) -> Tuple[float, float]:
    """Fit P(t) = CP + W'/t via least-squares over available (t, MMP) points.

    Requires at least 2 points. Returns (CP watts, W' joules).
    """
    points: List[Tuple[float, float]] = [
        (float(t), float(p)) for t, p in mmp.items() if t > 0 and p > 0
    ]
    if len(points) < 2:
        raise ValueError("CP/W' fit requires at least 2 (duration, power) points")

    t = np.array([p[0] for p in points], dtype=float)
    y = np.array([p[1] for p in points], dtype=float)

    def residuals(params: np.ndarray) -> np.ndarray:
        cp, wprime = params
        return (cp + wprime / t) - y

    # Initial guess: CP ~ lowest observed power, W' from a mid point.
    cp0 = float(y.min())
    wprime0 = float((y.max() - cp0) * t.min()) if y.max() > cp0 else 20000.0
    sol = least_squares(residuals, x0=[cp0, max(wprime0, 1.0)])
    cp, wprime = float(sol.x[0]), float(sol.x[1])
    return cp, wprime


def power_duration_curve(cp: float, wprime: float, durations: Sequence[int]) -> List[Tuple[int, float]]:
    """Model-predicted power for each duration, given CP and W'."""
    return [(int(d), cp + wprime / float(d)) for d in durations if d > 0]
