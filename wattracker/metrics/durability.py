"""Power durability, a companion to rather than an extension of decoupling.

Decoupling measures Pw:HR drift during a steady effort.  Durability instead
measures whether five-minute power can be repeated after substantial work, so
it deliberately contains no heart-rate or drift calculation.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Mapping, Optional, Sequence

from .curve import best_rolling_power, mean_maximal_power

FIVE_MINUTES_SECONDS = 300
LATE_WORK_THRESHOLD_KJ_PER_KG = 15.0
# 1,125 kJ is the 15 kJ/kg threshold for a representative 75 kg rider.  It
# preserves useful, conservative behavior when body weight has not been saved.
LATE_WORK_FALLBACK_KJ = 1125.0
MIN_QUALIFYING_RIDES = 3


@dataclass(frozen=True)
class DurabilityResult:
    """Best-observed late-power retention, with optional evidence details."""

    retention_ratio: Optional[float] = None
    fresh_5min_power: Optional[float] = None
    late_5min_power: Optional[float] = None
    qualifying_rides: Optional[int] = None


def _finite_positive(value: object) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and number > 0.0 else None


def _clean_stream(raw: object) -> Optional[list[float]]:
    if raw is None or isinstance(raw, (str, bytes, bytearray, Mapping)):
        return None
    try:
        values = list(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    cleaned: list[float] = []
    for value in values:
        try:
            watts = float(value) if value is not None else 0.0
        except (TypeError, ValueError, OverflowError):
            watts = 0.0
        if not math.isfinite(watts) or watts < 0.0:
            watts = 0.0
        cleaned.append(watts)
    return cleaned


def _power_stream(item: object) -> Optional[list[float]]:
    if isinstance(item, Mapping):
        streams = item.get("streams")
        raw = streams.get("power") if isinstance(streams, Mapping) else None
        return _clean_stream(raw)
    return _clean_stream(item)


def _streams(activities: object) -> list[list[float]]:
    if activities is None or isinstance(
        activities, (str, bytes, bytearray, Mapping)
    ):
        return []
    try:
        items = iter(activities)  # type: ignore[arg-type]
    except TypeError:
        return []
    result: list[list[float]] = []
    try:
        for item in items:
            stream = _power_stream(item)
            if stream:
                result.append(stream)
    except Exception:
        # A malformed or failing input iterator is simply incomplete evidence.
        return result
    return result


def compute_durability(
    activities: Iterable[Sequence[float]] | Iterable[Mapping[str, object]] | object,
    weight_kg: object = None,
) -> DurabilityResult:
    """Return five-minute power retention after substantial cumulative work.

    The numerator is the best observed five-minute effort after the cumulative
    work threshold in any qualifying ride.  The denominator is fresh 300-second
    mean-maximal power across all supplied rides.  At least
    ``MIN_QUALIFYING_RIDES`` are required so a single unusually good or bad ride
    does not masquerade as a durable pattern.

    Invalid or thin evidence produces optional fields rather than exceptions.
    """
    try:
        streams = _streams(activities)
        if not streams:
            return DurabilityResult()

        mmp = mean_maximal_power(streams, durations=[FIVE_MINUTES_SECONDS])
        fresh = _finite_positive(mmp.get(FIVE_MINUTES_SECONDS))
        if fresh is None:
            return DurabilityResult()

        weight = _finite_positive(weight_kg)
        threshold_kj = (
            LATE_WORK_THRESHOLD_KJ_PER_KG * weight
            if weight is not None
            else LATE_WORK_FALLBACK_KJ
        )
        threshold_j = threshold_kj * 1000.0

        late_efforts: list[float] = []
        for stream in streams:
            cumulative_j = 0.0
            late_start: Optional[int] = None
            for index, watts in enumerate(stream):
                cumulative_j += watts
                if cumulative_j >= threshold_j:
                    late_start = index + 1
                    break
            if late_start is None or len(stream) - late_start < FIVE_MINUTES_SECONDS:
                continue
            late = best_rolling_power(stream[late_start:], FIVE_MINUTES_SECONDS)
            if late > 0.0:
                late_efforts.append(late)

        qualifying = len(late_efforts)
        if qualifying < MIN_QUALIFYING_RIDES:
            return DurabilityResult(
                fresh_5min_power=fresh,
                qualifying_rides=qualifying,
            )

        # Best-observed aggregation measures demonstrated capability and avoids
        # penalizing recovery rides that happen to cross the work threshold.
        late_best = max(late_efforts)
        return DurabilityResult(
            retention_ratio=late_best / fresh,
            fresh_5min_power=fresh,
            late_5min_power=late_best,
            qualifying_rides=qualifying,
        )
    except Exception:
        return DurabilityResult()


durability = compute_durability
