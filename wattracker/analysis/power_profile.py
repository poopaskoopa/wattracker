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
from typing import Callable, Iterable, Optional

import numpy as np

from .. import db
from ..timeutil import parse_naive, to_user_timezone, utc_now


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

# Longest run of consecutive invalid samples that is filled in by linear
# interpolation between the surrounding valid samples. A one-off sensor
# dropout is not a break in the effort -- discarding every window that spans
# it would erase a rider's 60-minute best over a single bad second -- but a
# genuine pause really does interrupt the effort, so anything longer than this
# still invalidates the windows that contain it. Runs touching the start or
# end of the stream have no anchor on one side and are never bridged.
MAX_BRIDGED_GAP_S = 3

# Trailing windows, narrowest first, tried in turn when classifying the
# phenotype; ``None`` means all time. The narrowest window that carries enough
# coverage wins, so a three-year-old sprint no longer shapes a rider's current
# label. Every ratio comes from a single window -- domains are never mixed.
PHENOTYPE_WINDOWS = (90, 180, 365, None)
PRIMARY_PHENOTYPE_WINDOW_DAYS = 90


def _finite_nonnegative(value) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _bridge_short_gaps(values: list[Optional[float]]) -> None:
    """Linearly interpolate invalid runs of at most ``MAX_BRIDGED_GAP_S``.

    Mutates ``values`` in place. Only runs with a valid sample on both sides
    are filled; leading and trailing runs are left invalid.
    """
    length = len(values)
    index = 0
    while index < length:
        if values[index] is not None:
            index += 1
            continue
        start = index
        while index < length and values[index] is None:
            index += 1
        end = index  # exclusive
        gap = end - start
        if gap > MAX_BRIDGED_GAP_S or start == 0 or end == length:
            continue
        before = values[start - 1]
        after = values[end]
        step = (after - before) / (gap + 1)
        for offset in range(gap):
            values[start + offset] = before + step * (offset + 1)


def rolling_maxima(power: Iterable, durations: Iterable[int] = ()) -> dict[int, float]:
    """Return exact rolling-mean maxima for a one-sample-per-second stream.

    Invalid and negative samples break a window rather than being silently
    turned into zero, except that a run of at most ``MAX_BRIDGED_GAP_S``
    invalid samples with valid samples on both sides is linearly interpolated
    and then treated as valid. A result is omitted unless its best mean is
    positive.
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
    _bridge_short_gaps(values)

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


def _best_within(
    records: Iterable[tuple], cutoff: Optional[_dt.datetime], now: _dt.datetime
) -> dict[int, float]:
    """Fold per-activity maxima into one best-of dict for a trailing window.

    ``cutoff`` of ``None`` means all time and admits every activity, including
    the ones whose ``start_time`` could not be parsed. Any finite window keeps
    only activities that ``_is_recent`` accepts, so undated and future-dated
    rides contribute all-time only.
    """
    best: dict[int, float] = {}
    for start_time, maxima in records:
        if cutoff is not None and not _is_recent(start_time, cutoff, now):
            continue
        for duration, value in maxima.items():
            best[duration] = max(value, best.get(duration, 0.0))
    return best


def _insufficient(rationale: str, window_days: Optional[int] = None) -> dict:
    return {
        "label": "Insufficient data",
        "key": "insufficient_data",
        "rationale": rationale,
        "caveat": METHODOLOGY_CAVEAT,
        "indices": None,
        "window_days": window_days,
        # There is no classification here to be fresh about, and a consumer
        # that checks staleness before coverage must not read this as current.
        "stale": True,
    }


def classify_phenotype(
    all_time: dict[int, float], window_days: Optional[int] = None
) -> dict:
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

    ``window_days`` records which trailing window the supplied bests came
    from; ``None`` means all time. It is echoed back in the result alongside
    ``stale``, which is ``True`` unless the classification came from the
    primary ``PRIMARY_PHENOTYPE_WINDOW_DAYS``-day window. That definition is
    deliberately mechanical so a consumer -- the prescription engine in
    particular -- can set its own freshness bar instead of inferring one from
    the rationale prose.
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
            "six durations overall.",
            window_days,
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
            "20-minute best.",
            window_days,
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
        "window_days": window_days,
        "stale": window_days != PRIMARY_PHENOTYPE_WINDOW_DAYS,
    }


def classify_with_recency(
    records: Iterable[tuple],
    now: Optional[_dt.datetime] = None,
    windows: Iterable[Optional[int]] = PHENOTYPE_WINDOWS,
) -> dict:
    """Classify from the narrowest trailing window that has enough coverage.

    ``records`` is an iterable of ``(start_time, maxima)`` pairs, one per
    activity, where ``maxima`` is that activity's ``{duration: watts}`` dict.
    Windows are tried narrowest first; the first one whose bests clear
    ``classify_phenotype``'s eligibility gate wins, and every ratio then comes
    from that one window. When no window qualifies, the all-time
    insufficient-data result is returned.
    """
    reference = _utc_naive(now or utc_now())
    materialized = list(records)
    fallback = None
    for window_days in windows:
        cutoff = (
            reference - _dt.timedelta(days=window_days)
            if window_days is not None
            else None
        )
        result = classify_phenotype(
            _best_within(materialized, cutoff, reference), window_days
        )
        if result["key"] != "insufficient_data":
            return result
        fallback = result
    if fallback is not None and fallback["window_days"] is None:
        return fallback
    return classify_phenotype(
        _best_within(materialized, None, reference), None
    )


def _weight_for(
    weight_fn: Optional[Callable[[Optional[str]], Optional[float]]],
    start_time: Optional[str],
    fallback: Optional[float],
) -> Optional[float]:
    """The W/kg divisor for one record: the weight as of the ride itself when
    a resolver is available (and resolves something), else the flat scalar."""
    if weight_fn is not None and start_time is not None:
        value = _weight(weight_fn(start_time))
        if value is not None:
            return value
    return fallback


def compute(
    activities: Iterable[dict],
    weight_kg=None,
    now: Optional[_dt.datetime] = None,
    weight_fn: Optional[Callable[[Optional[str]], Optional[float]]] = None,
) -> dict:
    """Compute all-time and trailing-60-day record power presentation data.

    ``weight_fn`` resolves a ride's W/kg divisor from the ride's own UTC
    ``start_time`` (so each record is divided by the rider's weight at the
    time it was set). Without it, or when it resolves nothing, the flat
    ``weight_kg`` scalar is the divisor - exactly the pre-history behaviour.
    """
    reference = _utc_naive(now or utc_now())
    cutoff = reference - _dt.timedelta(days=60)
    # (value, start_time) pairs: the W/kg of a record belongs to the ride that
    # set it, which is only knowable while keeping the start_time with the max.
    all_best: dict[int, tuple[float, Optional[str]]] = {}
    recent_best: dict[int, tuple[float, Optional[str]]] = {}
    all_counts = {duration: 0 for duration, _ in DURATIONS}
    recent_counts = {duration: 0 for duration, _ in DURATIONS}
    records: list[tuple] = []

    for activity in activities:
        streams = activity.get("streams") if isinstance(activity, dict) else None
        power = streams.get("power") if isinstance(streams, dict) else None
        maxima = rolling_maxima(power)
        start_time = activity.get("start_time") if isinstance(activity, dict) else None
        records.append((start_time, maxima))
        recent = _is_recent(start_time, cutoff, reference)
        for duration, value in maxima.items():
            all_counts[duration] += 1
            current = all_best.get(duration)
            if current is None or value >= current[0]:
                all_best[duration] = (value, start_time)
            if recent:
                recent_counts[duration] += 1
                current = recent_best.get(duration)
                if current is None or value >= current[0]:
                    recent_best[duration] = (value, start_time)

    rider_weight = _weight(weight_kg)
    rows = []
    for duration, label in DURATIONS:
        all_entry = all_best.get(duration)
        recent_entry = recent_best.get(duration)
        all_value = all_entry[0] if all_entry is not None else None
        recent_value = recent_entry[0] if recent_entry is not None else None
        all_watts = round(all_value) if all_value is not None else None
        recent_watts = round(recent_value) if recent_value is not None else None
        percent = (
            round(recent_value / all_value * 100)
            if all_value and recent_value is not None
            else None
        )
        all_divisor = _weight_for(weight_fn, all_entry[1] if all_entry else None,
                                  rider_weight)
        recent_divisor = _weight_for(weight_fn,
                                     recent_entry[1] if recent_entry else None,
                                     rider_weight)
        rows.append({
            "duration": duration,
            "label": label,
            "all_time": all_watts,
            "recent_60d": recent_watts,
            "recent_percent": percent,
            "all_time_wkg": round(all_value / all_divisor, 2)
            if all_value is not None and all_divisor else None,
            "recent_60d_wkg": round(recent_value / recent_divisor, 2)
            if recent_value is not None and recent_divisor else None,
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
        "phenotype": classify_with_recency(records, now=reference),
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
    """Build a profile from one user's nonduplicate, inflated activities.

    W/kg is resolved per record: each ride's best is divided by the weight
    effective on the ride's local calendar date (manual log first, then the
    Zwift-derived rows, then the settings scalar - the order
    ``db.weight_as_of`` already enforces), not by a single "current" number.
    """
    settings = db.get_user_settings(user_id)
    tz = settings.get("timezone")
    by_date: dict[str, Optional[float]] = {}

    def weight_for_start_time(start_time: Optional[str]) -> Optional[float]:
        parsed = parse_naive(start_time)
        if parsed is None:
            return None
        local_date = to_user_timezone(parsed, tz).date().isoformat()
        if local_date not in by_date:
            by_date[local_date] = db.weight_as_of(user_id, local_date)
        return by_date[local_date]

    return compute(
        db.full_activities(user_id),
        weight_kg=settings.get("weight_kg"),
        now=now,
        weight_fn=weight_for_start_time,
    )
