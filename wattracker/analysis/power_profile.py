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

# Reference ratios for a textbook balanced trained rider, taken from the
# Coggan-style power-profile curve expressed as multiples of FTP:
#   1s 6.4, 15s 2.9, 30s 2.2, 1min 2.0, 2min 1.60, 5min 1.27,
#   10min 1.15, 20min 1.05, 40min 1.00, 60min 0.97
# Phenotype ratios are divided by these so that an index of 1.0 means "the
# same shape as that balanced rider" regardless of absolute fitness.
REFERENCE_SHORT_RATIO = 2.40   # mean(15s, 30s) / 20min for a balanced trained rider
REFERENCE_PUNCH_RATIO = 1.55   # mean(1, 2, 5min) / 20min
REFERENCE_RETENTION = 0.94     # mean(40, 60min) / 20min


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


def _insufficient(rationale: str) -> dict:
    return {
        "label": "Insufficient data",
        "key": "insufficient_data",
        "rationale": rationale,
        "caveat": METHODOLOGY_CAVEAT,
        "indices": None,
    }


def classify_phenotype(all_time: dict[int, float]) -> dict:
    """Describe curve shape with indices relative to a balanced reference rider.

    Requires both the 15- and 30-second bests, at least two 1--5-minute bests,
    a 20-minute best used as the anchor, and six valid durations overall.
    Three ratios are anchored to the 20-minute best -- short (mean of 15 and
    30 s), punch (mean of 1, 2 and 5 min), and retention (mean of 40 and
    60 min, ``None`` when neither exists) -- and each is divided by the
    corresponding ``REFERENCE_*`` constant, so an index of 1.0 matches the
    balanced reference curve and the result is scale-invariant.

    A sprinter's short index is at least 1.10 and at least 1.12x the punch
    index; a puncheur is the mirror image; an endurance specialist retains at
    least 0.95 of reference retention with a short index no higher than 0.90
    and a punch index no higher than 0.95. Anything less distinctive is an
    all-rounder.
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
    anchor = usable(1200)
    if (
        len(short) < 2
        or len(punch) < 2
        or anchor is None
        or len(valid_records) < 6
    ):
        return _insufficient(
            "Broader record coverage is needed: both 15- and 30-second "
            "bests, at least two 1–5-minute bests, a 20-minute best, and "
            "six durations overall."
        )

    short_ratio = sum(short) / len(short) / anchor
    punch_ratio = sum(punch) / len(punch) / anchor
    long_values = [
        value for d in (2400, 3600) if (value := usable(d)) is not None
    ]
    retention = sum(long_values) / len(long_values) / anchor if long_values else None

    short_index = short_ratio / REFERENCE_SHORT_RATIO
    punch_index = punch_ratio / REFERENCE_PUNCH_RATIO
    retention_index = (
        retention / REFERENCE_RETENTION if retention is not None else None
    )
    # A denormal 20-minute anchor overflows the ratios to infinity, which would
    # then be handed to the prescription consumer and to any JSON encoder. Such
    # a record is not a real effort, so decline to classify rather than emit it.
    if not all(
        math.isfinite(index)
        for index in (short_index, punch_index, retention_index)
        if index is not None
    ):
        return _insufficient(
            "The recorded bests are not on a plausible scale relative to the "
            "20-minute best."
        )

    if short_index >= 1.10 and short_index >= punch_index * 1.12:
        label = "Sprinter"
        key = "sprinter"
        rationale = (
            "Short-duration power stands well above the balanced reference "
            "shape, and above punch power."
        )
    elif punch_index >= 1.10 and punch_index >= short_index * 1.12:
        label = "Puncheur"
        key = "puncheur"
        rationale = (
            "One-to-five-minute power is the clearest strength relative to "
            "the balanced reference shape."
        )
    elif (
        retention_index is not None
        and retention_index >= 0.95
        and short_index <= 0.90
        and punch_index <= 0.95
    ):
        label = "Endurance specialist (climber/time-trialist)"
        key = "endurance"
        rationale = (
            "Long-duration power is retained strongly while shorter efforts "
            "sit below the balanced reference shape."
        )
    else:
        label = "All-rounder"
        key = "all_rounder"
        rationale = "The recorded curve has no single domain dominant enough for a specialty label."
    return {
        "label": label,
        "key": key,
        "rationale": rationale,
        "caveat": METHODOLOGY_CAVEAT,
        "indices": {
            "short": round(short_index, 3),
            "punch": round(punch_index, 3),
            "retention": (
                round(retention_index, 3) if retention_index is not None else None
            ),
        },
    }


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
