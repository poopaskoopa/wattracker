"""Personalized heart-rate/power zones and full-resolution time accounting."""
from __future__ import annotations

import datetime as _dt
import math
import threading
from collections.abc import Mapping, Sequence
from collections import deque
from typing import Iterable, Optional

from .. import db
from ..ingest import importer
from ..metrics.power import best_20min_power
from ..timeutil import parse_naive, utc_now


POWER_ZONES = (
    ("Z1", "Active recovery", None, 0.56, "<56%"),
    ("Z2", "Endurance", 0.56, 0.76, "56–75%"),
    ("Z3", "Tempo", 0.76, 0.91, "76–90%"),
    ("Z4", "Threshold", 0.91, 1.06, "91–105%"),
    ("Z5", "VO₂ max", 1.06, 1.21, "106–120%"),
    ("Z6", "Anaerobic", 1.21, 1.51, "121–150%"),
    ("Z7", "Neuromuscular", 1.51, None, ">150%"),
)

HR_ZONES = (
    ("Z1", "Recovery", None, 0.60, "50–59%"),
    ("Z2", "Easy", 0.60, 0.70, "60–69%"),
    ("Z3", "Aerobic", 0.70, 0.80, "70–79%"),
    ("Z4", "Threshold", 0.80, 0.90, "80–89%"),
    ("Z5", "Maximum", 0.90, None, "90–100%+"),
)

_auto_hr_cache: dict[int, tuple[tuple, dict]] = {}
_cache_lock = threading.Lock()


def _as_utc_naive(value: Optional[_dt.datetime]) -> _dt.datetime:
    if value is None:
        return utc_now()
    if value.tzinfo is not None:
        return value.astimezone(_dt.timezone.utc).replace(tzinfo=None)
    return value


def _finite(value) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _format_duration(seconds: float) -> str:
    total = max(0, int(round(seconds)))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def _time_value(value):
    number = _finite(value)
    if number is not None:
        return number
    if not isinstance(value, str):
        return None
    try:
        parsed = _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed


def _delta(a, b) -> Optional[float]:
    left, right = _time_value(a), _time_value(b)
    if left is None or right is None or type(left) is not type(right):
        return None
    try:
        return float((right - left).total_seconds()) if isinstance(left, _dt.datetime) else right - left
    except (TypeError, ValueError, OverflowError):
        return None


def _safe_samples(value) -> Sequence:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return value
    return ()


def _sample_intervals(values: list, timestamps: Optional[list]) -> Iterable[tuple[object, float, bool]]:
    """Yield (sample, seconds, continuous) without inventing time for FIT gaps."""
    values = _safe_samples(values)
    timestamps = _safe_samples(timestamps)
    have_times = any(t is not None for t in timestamps)
    if not have_times:
        for value in values:
            yield value, 1.0, True
        return
    # Timestamped samples own the interval until the following timestamp. A
    # lone timestamp has no measurable interval and therefore gets no credit.
    n = max(len(values), len(timestamps))
    for i in range(max(0, n - 1)):
        value = values[i] if i < len(values) else None
        dt = _delta(
            timestamps[i] if i < len(timestamps) else None,
            timestamps[i + 1] if i + 1 < len(timestamps) else None,
        )
        if dt is None or dt <= 0:
            yield value, 0.0, False
        elif dt > 5.0:
            yield value, dt, False
        else:
            yield value, dt, True


def _zone_index(value: float, anchor: float, definitions) -> int:
    ratio = value / anchor
    for i, (_label, _name, _low, high, _pct) in enumerate(definitions):
        if high is None or ratio < high:
            return i
    return len(definitions) - 1


def zone_ranges(anchor: float, definitions) -> list[dict]:
    rows = []
    for label, name, low, high, pct in definitions:
        minimum = 0 if low is None else int(math.ceil(anchor * low))
        maximum = None if high is None else int(math.ceil(anchor * high) - 1)
        if maximum is None:
            display = f"≥{minimum}"
        elif low is None:
            display = f"≤{maximum}"
        else:
            display = f"{minimum}–{maximum}"
        rows.append({
            "label": label, "name": name, "pct": pct,
            "min": minimum, "max": maximum, "range": display,
        })
    return rows


def resolve_current_ftp(user_id: int) -> dict:
    """Return a real personalized Training FTP, never the 200 W fallback."""
    setting = _finite(db.get_user_settings(user_id).get("ftp"))
    if setting is not None and setting > 0:
        return {"available": True, "value": setting, "source": "Manual Training FTP setting"}
    latest = db.latest_ftp(user_id)
    if latest and _finite(latest.get("ftp_watts")) and float(latest["ftp_watts"]) > 0:
        source = latest.get("source") or "recorded"
        return {
            "available": True, "value": float(latest["ftp_watts"]),
            "source": f"Training FTP history ({source}, {latest['date']})",
        }

    # current_ftp is personalized only when a usable 20-minute FIT effort
    # exists. Guard it so its generic 200 W empty-data fallback cannot leak.
    usable = False
    for activity in db.full_activities(user_id):
        streams = activity.get("streams")
        if not isinstance(streams, Mapping):
            continue
        power = streams.get("power")
        if not isinstance(power, (list, tuple)) or not power:
            continue
        try:
            usable = best_20min_power(power) > 0
        except (TypeError, ValueError, OverflowError):
            continue
        if usable:
            break
    if usable:
        value = _finite(importer.current_ftp(user_id))
        if value is not None and value > 0:
            return {"available": True, "value": value, "source": "FIT power estimate"}
    return {"available": False, "value": None, "source": "No personalized FTP available"}


def resolve_activity_ftp(user_id: int, activity: dict) -> dict:
    start = str(activity.get("start_time") or "")
    date = start[:10]
    if date:
        value = _finite(db.ftp_as_of(user_id, date))
        if value is not None and value > 0:
            return {"available": True, "value": value, "source": f"Training FTP as of {date}"}
    np_value = _finite(activity.get("np"))
    intensity = _finite(activity.get("if_"))
    if np_value is not None and np_value > 0 and intensity is not None and intensity > 0:
        recovered = np_value / intensity
        if math.isfinite(recovered) and recovered > 0:
            return {
                "available": True, "value": recovered,
                "source": "Recovered from activity NP ÷ IF",
            }
    return resolve_current_ftp(user_id)


def _rolling_peak(values: list, timestamps: list) -> tuple[Optional[float], float]:
    """Highest continuous, duration-weighted 30-second HR average."""
    window = deque()
    window_s = window_sum = valid_s = 0.0
    best = None
    for raw, seconds, continuous in _sample_intervals(values, timestamps):
        hr = _finite(raw)
        valid = hr is not None and 30 <= hr <= 230 and seconds > 0 and continuous
        if not valid:
            window.clear()
            window_s = window_sum = 0.0
            continue
        valid_s += seconds
        window.append([hr, seconds])
        window_s += seconds
        window_sum += hr * seconds
        while window_s > 30.0 and window:
            excess = window_s - 30.0
            take = min(excess, window[0][1])
            window[0][1] -= take
            window_s -= take
            window_sum -= window[0][0] * take
            if window[0][1] <= 1e-9:
                window.popleft()
        if window_s >= 30.0 - 1e-9:
            average = window_sum / window_s
            best = average if best is None else max(best, average)
    return best, valid_s


def estimate_hr_max(activities: list[dict], now: Optional[_dt.datetime] = None) -> dict:
    now = _as_utc_naive(now)
    cutoff = now - _dt.timedelta(days=365)
    rides = []
    total_valid = 0.0
    for activity in activities:
        filename = str(activity.get("filename") or "")
        if not filename.lower().endswith(".fit"):
            continue
        when = parse_naive(activity.get("start_time"))
        if when is None or when < cutoff or when > now:
            continue
        streams = activity.get("streams")
        if not isinstance(streams, Mapping):
            streams = {}
        peak, valid_s = _rolling_peak(
            _safe_samples(streams.get("heartrate")),
            _safe_samples(streams.get("time")),
        )
        total_valid += valid_s
        if peak is not None and valid_s >= 600.0:
            rides.append({"date": when.date().isoformat(), "peak": peak, "valid_s": valid_s})
    rides.sort(key=lambda row: row["peak"], reverse=True)
    corroborating = None
    for i, first in enumerate(rides):
        for second in rides[i + 1:]:
            if abs(first["peak"] - second["peak"]) <= 5.0:
                corroborating = (first, second)
                break
        if corroborating:
            break
    sufficient = len(rides) >= 2 and total_valid >= 3600.0 and corroborating is not None
    value = int(round(max(r["peak"] for r in corroborating))) if sufficient else None
    return {
        "available": sufficient,
        "value": value,
        "source": "Estimated from corroborated 30-second FIT peaks" if sufficient else "Insufficient FIT heart-rate data",
        "confidence": "moderate" if sufficient else "insufficient",
        "rides": len(rides),
        "valid_seconds": round(total_valid, 1),
    }


def _activity_fingerprint(user_id: int) -> tuple:
    conn = db.connect()
    try:
        database = conn.execute("PRAGMA database_list").fetchone()["file"]
        row = conn.execute(
            "SELECT COUNT(*) AS c, COALESCE(MAX(id), 0) AS m, "
            "SUM(duplicate_of IS NOT NULL) AS d FROM activities WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        # Linking a duplicate removes a ride from full_activities without
        # changing the count or max id, so it belongs in the fingerprint.
        settings = db.get_user_settings(user_id)
        # History visibility is part of the evidence set. Timezone matters
        # because the cutoff is defined by the rider's local activity date.
        return (
            str(database), int(row["c"]), int(row["m"]), int(row["d"] or 0),
            settings.get("history_start_date"), settings.get("timezone"),
        )
    finally:
        conn.close()


def resolve_hr_max(
    user_id: int, now: Optional[_dt.datetime] = None
) -> dict:
    manual = _finite(db.get_user_settings(user_id).get("hr_max"))
    if manual is not None and 80 <= manual <= 230:
        return {
            "available": True, "value": int(manual), "source": "Manual HRmax",
            "confidence": "manual", "rides": None, "valid_seconds": None,
        }
    now = _as_utc_naive(now)
    # Eligibility changes as the trailing 365-day window advances even when no
    # activities are inserted. Include the evaluation day so cached evidence
    # cannot remain eligible indefinitely after it ages out.
    fingerprint = (*_activity_fingerprint(user_id), now.date().isoformat())
    with _cache_lock:
        cached = _auto_hr_cache.get(user_id)
    if cached and cached[0] == fingerprint:
        return dict(cached[1])
    estimate = estimate_hr_max(db.full_activities(user_id), now=now)
    with _cache_lock:
        _auto_hr_cache[user_id] = (fingerprint, dict(estimate))
    return estimate


def rider_profile(user_id: int) -> dict:
    power = resolve_current_ftp(user_id)
    hr = resolve_hr_max(user_id)
    power["zones"] = zone_ranges(power["value"], POWER_ZONES) if power["available"] else []
    hr["zones"] = zone_ranges(hr["value"], HR_ZONES) if hr["available"] else []
    return {"power": power, "heart_rate": hr}


def time_in_zones(values: list, timestamps: list, anchor: Optional[float], definitions, kind: str) -> dict:
    if not values:
        return {
            "available": False, "reason": f"No {kind} stream", "zones": [],
            "covered_s": 0.0, "missing_s": 0.0, "coverage_pct": 0.0,
        }
    if anchor is None or anchor <= 0:
        return {
            "available": False, "reason": f"No personalized {kind} zone anchor", "zones": [],
            "covered_s": 0.0, "missing_s": 0.0, "coverage_pct": 0.0,
        }
    seconds_by_zone = [0.0] * len(definitions)
    covered = missing = 0.0
    for raw, seconds, continuous in _sample_intervals(values, timestamps):
        if seconds <= 0:
            continue
        value = _finite(raw)
        valid = value is not None and continuous
        if kind == "power":
            valid = valid and value >= 0
        else:
            valid = valid and 30 <= value <= 230
        if not valid:
            missing += seconds
            continue
        covered += seconds
        seconds_by_zone[_zone_index(value, anchor, definitions)] += seconds
    total = covered + missing
    ranges = zone_ranges(anchor, definitions)
    rows = []
    for zone, seconds in zip(ranges, seconds_by_zone):
        rows.append({
            **zone,
            "seconds": round(seconds, 1),
            "duration": _format_duration(seconds),
            "percent": round(100.0 * seconds / covered, 1) if covered else 0.0,
        })
    return {
        "available": covered > 0,
        "reason": None if covered > 0 else f"No valid {kind} samples",
        "zones": rows,
        "covered_s": round(covered, 1),
        "missing_s": round(missing, 1),
        "coverage_pct": round(100.0 * covered / total, 1) if total else 0.0,
    }


def activity_zone_summary(user_id: int, activity: dict) -> dict:
    streams = activity.get("streams")
    if not isinstance(streams, Mapping):
        streams = {}
    timestamps = _safe_samples(streams.get("time"))
    ftp = resolve_activity_ftp(user_id, activity)
    hrmax = resolve_hr_max(user_id)
    power = time_in_zones(
        _safe_samples(streams.get("power")),
        timestamps,
        ftp.get("value"),
        POWER_ZONES,
        "power",
    )
    heart_rate = time_in_zones(
        _safe_samples(streams.get("heartrate")),
        timestamps,
        hrmax.get("value"),
        HR_ZONES,
        "heart-rate",
    )
    power["anchor"] = ftp.get("value")
    power["source"] = ftp.get("source")
    heart_rate["anchor"] = hrmax.get("value")
    heart_rate["source"] = hrmax.get("source")
    return {"power": power, "heart_rate": heart_rate}
