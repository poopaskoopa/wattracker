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


# --- Domain of the 2-parameter Critical Power model --------------------------
# P(t) = CP + W'/t is only a description of reality over a narrow band of effort
# durations, and fitting it outside that band does not merely add noise - it
# moves both parameters to values that are physiologically impossible.
#
# Below ~2 minutes the hyperbola's asymptote is nowhere near reached: power is
# dominated by neuromuscular and anaerobic-glycolytic supply the model does not
# represent, and P(t) -> infinity as t -> 0. Because the residuals are in watts
# and a sprint sits 600-700 W above CP, those few points carry more squared
# error than the whole aerobic tail combined and drag CP up to meet them.
# Above ~20 minutes the other assumption fails: W' is supposed to be a fixed,
# fully-expendable reservoir, but efforts that long are limited by substrate,
# thermoregulation and pacing, so measured power falls BELOW the hyperbola and
# drags CP back down.
#
# The classical protocol (Monod & Scherrer 1965; Poole 1988; Jones & Vanhatalo
# 2017) therefore uses predicting trials of roughly 2-20 minutes, and that is
# the window used here. The lower bound is 120s rather than 180s because the
# sampling grid (MMP_DURATIONS) only offers 120/180/300/600/900/1200 inside the
# band at all: starting at 180s leaves five points, and dropping to four the
# moment a rider has no ride longer than 15 minutes. 120s is inside the
# published range, and keeping it buys a degree of freedom that matters far more
# to fit stability than the small extra anaerobic contribution it carries.
#
# Short points are DROPPED, not down-weighted. Down-weighting would be the right
# tool if sprints were the same relationship measured more noisily; they are
# not - they are a different physiological regime the model does not describe,
# so any non-zero weight still biases CP upward, and the weight itself would be
# an unjustifiable free parameter. Dropping is also what the literature the
# model comes from actually does.
CP_FIT_MIN_S = 120
CP_FIT_MAX_S = 1200
# Two points fit two parameters exactly: zero residual degrees of freedom, so a
# single noisy MMP sample lands entirely in CP and W' with nothing to reveal it.
# Three is the minimum that can be over-determined at all.
CP_FIT_MIN_POINTS = 3


def fit_cp_wprime(mmp: Dict[int, float]) -> Tuple[float, float]:
    """Fit P(t) = CP + W'/t by least-squares over the model's valid domain.

    Only MMP points inside [CP_FIT_MIN_S, CP_FIT_MAX_S] are used - see the
    constants above for why the 2-parameter CP model must not be fitted outside
    roughly 2-20 minutes. Requires at least CP_FIT_MIN_POINTS such points.

    Raises ValueError when there is not enough in-window data, or when the fit
    lands somewhere physiologically impossible (CP at or above the rider's own
    best power at the longest in-window duration, or a non-positive W'). CP is
    the asymptote of the curve, so it must sit strictly below every measured
    point on it; a fit that violates that is not a profile worth storing, and
    callers already handle the failure by leaving CP/W' unknown.

    Returns (CP watts, W' joules).
    """
    points: List[Tuple[float, float]] = sorted(
        (float(t), float(p))
        for t, p in mmp.items()
        if p > 0 and CP_FIT_MIN_S <= t <= CP_FIT_MAX_S
    )
    if len(points) < CP_FIT_MIN_POINTS:
        raise ValueError(
            f"CP/W' fit requires at least {CP_FIT_MIN_POINTS} (duration, power) "
            f"points between {CP_FIT_MIN_S}s and {CP_FIT_MAX_S}s; got "
            f"{len(points)}"
        )

    t = np.array([p[0] for p in points], dtype=float)
    y = np.array([p[1] for p in points], dtype=float)

    def residuals(params: np.ndarray) -> np.ndarray:
        cp, wprime = params
        return (cp + wprime / t) - y

    # Initial guess: CP ~ lowest observed power, W' from a mid point. Both
    # parameters are bounded non-negative - a negative CP or W' is meaningless
    # and only ever the optimiser's way of describing data the model cannot.
    cp0 = float(y.min())
    wprime0 = float((y.max() - cp0) * t.min()) if y.max() > cp0 else 20000.0
    sol = least_squares(
        residuals,
        x0=[cp0, max(wprime0, 1.0)],
        bounds=([0.0, 0.0], [np.inf, np.inf]),
    )
    cp, wprime = float(sol.x[0]), float(sol.x[1])

    longest_power = points[-1][1]
    if wprime <= 0.0 or cp >= longest_power:
        raise ValueError(
            f"CP/W' fit is not physiologically valid (CP={cp:.1f} W, "
            f"W'={wprime:.0f} J vs {points[-1][0]:.0f}s power "
            f"{longest_power:.1f} W)"
        )
    return cp, wprime


def power_duration_curve(cp: float, wprime: float, durations: Sequence[int]) -> List[Tuple[int, float]]:
    """Model-predicted power for each duration, given CP and W'."""
    return [(int(d), cp + wprime / float(d)) for d in durations if d > 0]
