"""Record power profile computation and descriptive rider phenotype.

The duration grid and domain comparison are inspired by the Record Power
Profile literature (Pinot & Grappe, 2011, DOI 10.1055/s-0031-1279773) and the
power-profiling review at PMC8783871.  The phenotype labels below are
transparent heuristics for describing the *shape* of this rider's curve, not a
validated diagnostic classification or a measure of absolute fitness.
"""
from __future__ import annotations

import datetime as _dt
import math
from collections.abc import Mapping
from typing import Iterable, Optional

import numpy as np

from .. import db
from ..timeutil import utc_now


DURATIONS = (
    (1, "1 sec"),
    (15, "15 sec"),
    (30, "30 sec"),
    (60, "1 min"),
    (120, "2 min"),
    (300, "5 min"),
    (600, "10 min"),
    (1200, "20 min"),
    (2400, "40 min"),
    (3600, "60 min"),
)

METHODOLOGY_CAVEAT = (
    "Descriptive heuristic based on the shape of recorded best efforts; it is "
    "not a validated physiological diagnosis."
)


def _finite_nonnegative(value) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def rolling_maxima(power: Iterable, durations: Iterable[int] = ()) -> dict[int, float]:
    """Return exact rolling-mean maxima for a one-sample-per-second stream.

    Invalid and negative samples break a window rather than being silently
    turned into zero. A result is omitted unless its best mean is positive.
    """
    if (
        power is None
        or isinstance(power, (str, bytes, bytearray, Mapping))
    ):
        return {}
    try:
        iterator = iter(power)
    except TypeError:
        return {}

    wanted = tuple(durations) or tuple(seconds for seconds, _ in DURATIONS)
    values = [_finite_nonnegative(value) for value in iterator]
    if not values:
        return {}

    valid = np.fromiter((value is not None for value in values), dtype=bool)
    samples = np.fromiter(
        (value if value is not None else 0.0 for value in values), dtype=float
    )
    prefix = np.concatenate(([0.0], np.cumsum(samples, dtype=float)))
    invalid = np.concatenate(([0], np.cumsum(~valid, dtype=np.int64)))

    result: dict[int, float] = {}
    length = len(values)
    for duration in wanted:
        if not isinstance(duration, int) or duration <= 0 or duration > length:
            continue
        sums = prefix[duration:] - prefix[:-duration]
        complete = (invalid[duration:] - invalid[:-duration]) == 0
        best = float(np.max(sums[complete]) / duration) if np.any(complete) else 0.0
        if best > 0:
            result[duration] = best
    return result


def _weight(value) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _utc_naive(value: _dt.datetime) -> _dt.datetime:
    if value.tzinfo is not None:
        return value.astimezone(_dt.timezone.utc).replace(tzinfo=None)
    return value


def _is_recent(start_time, cutoff: _dt.datetime, now: _dt.datetime) -> bool:
    if not isinstance(start_time, str) or not start_time:
        return False
    try:
        parsed = _dt.datetime.fromisoformat(start_time.replace("Z", "+00:00"))
    except ValueError:
        return False
    parsed = _utc_naive(parsed)
    return parsed is not None and cutoff <= parsed <= now


def classify_phenotype(all_time: dict[int, float]) -> dict:
    """Describe curve shape using conservative, scale-invariant thresholds.

    At least one best in each of the short (<=30 s), punch (1--5 min), and
    sustained (>=20 min) domains is required. Ratios are anchored to the
    20-minute best.
    A sprinter needs 15--30-second power at least 2.15x sustained and 1.45x
    punch power; a puncheur needs 1--5-minute power at least 1.45x sustained without meeting
    the sprint rule; an endurance specialist needs >=40-minute retention of at
    least 88%, with neither short nor punch ratios elevated. Everything less
    distinctive is an all-rounder. These deliberately broad thresholds avoid
    asserting a specialty from small differences.
    """
    # Exclude one-second peaks: they are naturally far above 20-minute power
    # even on balanced curves and are unusually sensitive to sensor spikes.
    def usable(duration):
        value = all_time.get(duration)
        return (
            float(value)
            if isinstance(value, (int, float)) and math.isfinite(value) and value > 0
            else None
        )

    valid_records = {
        duration: value
        for duration, _label in DURATIONS
        if (value := usable(duration)) is not None
    }
    short = [value for d in (15, 30) if (value := usable(d)) is not None]
    punch = [value for d in (60, 120, 300) if (value := usable(d)) is not None]
    sustained = [
        value for d in (1200, 2400, 3600) if (value := usable(d)) is not None
    ]
    anchor = usable(1200)
    if (
        len(short) < 2
        or len(punch) < 2
        or anchor is None
        or len(valid_records) < 6
    ):
        return {
            "label": "Insufficient data",
            "rationale": (
                "Broader record coverage is needed: both 15- and 30-second "
                "bests, at least two 1–5-minute bests, a 20-minute best, and "
                "six durations overall."
            ),
            "caveat": METHODOLOGY_CAVEAT,
        }

    short_ratio = sum(short) / len(short) / anchor
    punch_ratio = sum(punch) / len(punch) / anchor
    long_values = [
        value for d in (2400, 3600) if (value := usable(d)) is not None
    ]
    retention = sum(long_values) / len(long_values) / anchor if long_values else None

    if short_ratio >= 2.15 and short_ratio >= punch_ratio * 1.45:
        label = "Sprinter"
        rationale = "Short-duration power stands well above both punch and sustained power."
    elif punch_ratio >= 1.45 and short_ratio < punch_ratio * 1.6:
        label = "Puncheur"
        rationale = "One-to-five-minute power is the clearest strength relative to sustained power."
    elif (
        retention is not None
        and retention >= 0.88
        and short_ratio < 1.85
        and punch_ratio < 1.4
    ):
        label = "Endurance specialist (climber/time-trialist)"
        rationale = "Long-duration power is retained strongly while shorter efforts are less dominant."
    else:
        label = "All-rounder"
        rationale = "The recorded curve has no single domain dominant enough for a specialty label."
    return {"label": label, "rationale": rationale, "caveat": METHODOLOGY_CAVEAT}


def compute(
    activities: Iterable[dict],
    weight_kg=None,
    now: Optional[_dt.datetime] = None,
) -> dict:
    """Compute all-time and trailing-60-day record power presentation data."""
    reference = _utc_naive(now or utc_now())
    cutoff = reference - _dt.timedelta(days=60)
    all_best: dict[int, float] = {}
    recent_best: dict[int, float] = {}
    all_counts = {duration: 0 for duration, _ in DURATIONS}
    recent_counts = {duration: 0 for duration, _ in DURATIONS}

    for activity in activities:
        streams = activity.get("streams") if isinstance(activity, dict) else None
        power = streams.get("power") if isinstance(streams, dict) else None
        maxima = rolling_maxima(power)
        start_time = activity.get("start_time") if isinstance(activity, dict) else None
        recent = _is_recent(start_time, cutoff, reference)
        for duration, value in maxima.items():
            all_counts[duration] += 1
            all_best[duration] = max(value, all_best.get(duration, 0.0))
            if recent:
                recent_counts[duration] += 1
                recent_best[duration] = max(value, recent_best.get(duration, 0.0))

    rider_weight = _weight(weight_kg)
    rows = []
    for duration, label in DURATIONS:
        all_value = all_best.get(duration)
        recent_value = recent_best.get(duration)
        all_watts = round(all_value) if all_value is not None else None
        recent_watts = round(recent_value) if recent_value is not None else None
        percent = (
            round(recent_value / all_value * 100)
            if all_value and recent_value is not None
            else None
        )
        rows.append({
            "duration": duration,
            "label": label,
            "all_time": all_watts,
            "recent_60d": recent_watts,
            "recent_percent": percent,
            "all_time_wkg": round(all_value / rider_weight, 2)
            if all_value is not None and rider_weight else None,
            "recent_60d_wkg": round(recent_value / rider_weight, 2)
            if recent_value is not None and rider_weight else None,
            "all_time_rides": all_counts[duration],
            "recent_60d_rides": recent_counts[duration],
            "available": all_value is not None,
            "recent_available": recent_value is not None,
        })

    chart = {
        "labels": [row["label"] for row in rows],
        "all_time": [100 if row["all_time"] is not None else None for row in rows],
        "recent_60d": [row["recent_percent"] for row in rows],
        "all_time_watts": [row["all_time"] for row in rows],
        "recent_60d_watts": [row["recent_60d"] for row in rows],
        "all_time_wkg": [row["all_time_wkg"] for row in rows],
        "recent_60d_wkg": [row["recent_60d_wkg"] for row in rows],
    }
    available_rows = sum(row["available"] for row in rows)
    recent_rows = sum(row["recent_available"] for row in rows)
    return {
        "available": bool(available_rows),
        "rows": rows,
        "chart": chart,
        "phenotype": classify_phenotype(all_best),
        "cutoff_date": cutoff.date().isoformat(),
        "cutoff_text": f"{cutoff.strftime('%b')} {cutoff.day}, {cutoff.year}",
        "as_of_date": reference.date().isoformat(),
        "coverage": {
            "all_time_durations": available_rows,
            "recent_60d_durations": recent_rows,
            "duration_count": len(DURATIONS),
            "all_time_rides": max(all_counts.values(), default=0),
            "recent_60d_rides": max(recent_counts.values(), default=0),
        },
    }


def for_user(user_id: int, now: Optional[_dt.datetime] = None) -> dict:
    """Build a profile from one user's nonduplicate, inflated activities."""
    settings = db.get_user_settings(user_id)
    return compute(
        db.full_activities(user_id),
        weight_kg=settings.get("weight_kg"),
        now=now,
    )
