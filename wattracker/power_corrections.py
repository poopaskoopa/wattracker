"""Find and reversibly mask anomalous full-resolution power samples."""
from __future__ import annotations

import datetime as _dt
import logging
import math
from typing import List, Optional, Sequence

from . import db
from .analysis import activity_cache
from .ingest import importer
from .metrics import profile_store
from .metrics.power import intensity_factor, normalized_power, training_stress_score

_log = logging.getLogger(__name__)
MAX_CANDIDATES = 200
NEIGHBOR_SAMPLES = 5
MAX_TARGET_PREVIEW_SAMPLES = 5
MAX_SAFE_POWER_WATTS = 1_000_000.0


class CorrectionError(ValueError):
    pass


def _stream_dict(activity: dict, key: str = "streams") -> dict:
    streams = activity.get(key)
    return streams if isinstance(streams, dict) else {}


def _power_value(value) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if (
        not math.isfinite(number)
        or number < 0
        or number > MAX_SAFE_POWER_WATTS
    ):
        return None
    return number


def _timestamp(activity: dict, index: int) -> str:
    times = _stream_dict(activity).get("time")
    if not isinstance(times, (list, tuple)):
        times = []
    if index < len(times) and times[index] is not None:
        return str(times[index])
    start = activity.get("start_time")
    if start:
        try:
            when = _dt.datetime.fromisoformat(str(start).replace("Z", "+00:00"))
            return (when + _dt.timedelta(seconds=index)).isoformat()
        except ValueError:
            pass
    return f"+{index}s"


def _preview_indices(length: int, start: int, end: int) -> List[int]:
    before = range(max(0, start - NEIGHBOR_SAMPLES), start)
    if end - start + 1 <= MAX_TARGET_PREVIEW_SAMPLES:
        target = range(start, end + 1)
    else:
        target = (start, start + 1, start + 2, end - 1, end)
    after = range(end + 1, min(length, end + 1 + NEIGHBOR_SAMPLES))
    return list(before) + list(target) + list(after)


def _sample_rows(activity: dict, indices: Sequence[int], target: tuple) -> List[dict]:
    power = _stream_dict(activity).get("power") or []
    start, end = target
    return [
        {
            "index": i,
            "timestamp": _timestamp(activity, i),
            "value": power[i],
            "targeted": start <= i <= end,
        }
        for i in indices
    ]


def find_anomalies(user_id: int, threshold: float) -> List[dict]:
    """Consecutive, unmasked full-resolution samples at or above ``threshold``."""
    try:
        threshold = float(threshold)
    except (TypeError, ValueError) as exc:
        raise CorrectionError("Threshold must be a number.") from exc
    if not math.isfinite(threshold) or not 1 <= threshold <= 10000:
        raise CorrectionError("Threshold must be between 1 and 10,000 W.")

    candidates: List[dict] = []
    for activity in db.iter_full_activities_desc(user_id):
        power = _stream_dict(activity).get("power")
        if not isinstance(power, list):
            continue
        i = 0
        while i < len(power):
            value = _power_value(power[i])
            matches = value is not None and value >= threshold
            if not matches:
                i += 1
                continue
            start = i
            i += 1
            while i < len(power):
                value = _power_value(power[i])
                if value is None or value < threshold:
                    break
                i += 1
            end = i - 1
            rows = _sample_rows(
                activity, _preview_indices(len(power), start, end), (start, end)
            )
            candidates.append(
                {
                    "activity_id": activity["id"],
                    "filename": activity.get("filename"),
                    "start_time": activity.get("start_time"),
                    "start_index": start,
                    "end_index": end,
                    "sample_count": end - start + 1,
                    "applicable": (
                        end - start + 1 <= db.POWER_CORRECTION_MAX_SAMPLES
                    ),
                    "matches": [r for r in rows if r["targeted"]],
                    "preview_truncated": (
                        end - start + 1 > MAX_TARGET_PREVIEW_SAMPLES
                    ),
                    "neighbors": rows,
                    "after": [
                        {**r, "value": None} if r["targeted"] else dict(r)
                        for r in rows
                    ],
                }
            )
            if len(candidates) >= MAX_CANDIDATES:
                return candidates
    return candidates


def _masked_power(raw: Sequence, ranges: Sequence[tuple]) -> list:
    power = list(raw)
    for start, end in ranges:
        power[start:end + 1] = [None] * (end - start + 1)
    return power


def _recovered_ftp(activity: dict, user_id: int) -> float:
    np_value = activity.get("np")
    intensity = activity.get("if_")
    try:
        np_number = float(np_value)
        intensity_number = float(intensity)
        recovered = np_number / intensity_number
        if (
            np_number > 0
            and intensity_number > 0
            and math.isfinite(recovered)
        ):
            return recovered
    except (TypeError, ValueError, OverflowError, ZeroDivisionError):
        pass
    try:
        current = float(importer.current_ftp(user_id))
    except (TypeError, ValueError, OverflowError):
        return 200.0
    return current if math.isfinite(current) and current > 0 else 200.0


def _summary(activity: dict, power: list, ftp: float) -> dict:
    cleaned = [
        number if (number := _power_value(value)) is not None else 0.0
        for value in power
    ]
    avg = sum(cleaned) / len(cleaned) if cleaned else 0.0
    np_value = normalized_power(cleaned) if cleaned else 0.0
    intensity = intensity_factor(np_value, ftp) if ftp > 0 else 0.0
    try:
        duration = float(activity.get("duration_s") or 0)
    except (TypeError, ValueError, OverflowError):
        duration = 0.0
    if not math.isfinite(duration) or duration < 0:
        duration = 0.0
    tss = training_stress_score(
        duration, np_value, ftp
    ) if ftp > 0 else 0.0
    return {
        "avg_power": avg,
        "np": round(np_value, 1),
        "if_": round(intensity, 3),
        "tss": round(tss, 1),
    }


def _refresh_user(user_id: int) -> None:
    activity_cache.invalidate(user_id)
    try:
        importer.evaluate_ftp(user_id)
    except Exception:
        _log.warning("FTP refresh after power correction failed for user %s",
                     user_id, exc_info=True)
    try:
        profile_store.refresh(user_id)
    except Exception:
        _log.warning("profile refresh after power correction failed for user %s",
                     user_id, exc_info=True)


def apply(
    user_id: int,
    activity_id: int,
    start_index: int,
    end_index: int,
    reason: Optional[str] = None,
) -> int:
    activity = db.power_correction_activity(user_id, activity_id)
    if activity is None:
        raise CorrectionError("Activity not found.")
    raw = _stream_dict(activity, "raw_streams").get("power")
    if not isinstance(raw, list):
        raise CorrectionError("Activity has no power stream.")
    if (
        isinstance(start_index, bool)
        or isinstance(end_index, bool)
        or not isinstance(start_index, int)
        or not isinstance(end_index, int)
        or start_index < 0
        or end_index < start_index
        or end_index >= len(raw)
        or end_index - start_index + 1 > db.POWER_CORRECTION_MAX_SAMPLES
    ):
        raise CorrectionError(
            f"Range must be valid, non-overlapping, and no longer than "
            f"{db.POWER_CORRECTION_MAX_SAMPLES} samples."
        )
    existing_ranges = list(activity["corrections"])
    ranges = existing_ranges + [(start_index, end_index)]
    try:
        effective = _masked_power(raw, ranges)
    except (IndexError, TypeError):
        raise CorrectionError("Invalid sample range.")
    ftp_basis = activity.get("correction_ftp_basis")
    if ftp_basis is None:
        ftp_basis = _recovered_ftp(activity, user_id)
    summary = _summary(activity, effective, ftp_basis)
    correction_id = db.apply_power_correction(
        user_id,
        activity_id,
        start_index,
        end_index,
        ftp_basis,
        reason,
        summary,
        expected_ranges=existing_ranges,
    )
    if correction_id is None:
        raise CorrectionError(
            f"Range must be valid, non-overlapping, and no longer than "
            f"{db.POWER_CORRECTION_MAX_SAMPLES} samples."
        )
    _refresh_user(user_id)
    return correction_id


def undo(user_id: int, correction_id: int) -> None:
    active = {
        int(row["id"]): row
        for row in db.list_power_corrections(user_id, active_only=True)
    }
    correction = active.get(correction_id)
    if correction is None:
        raise CorrectionError("Active correction not found.")
    activity_id = int(correction["activity_id"])
    activity = db.power_correction_activity(user_id, activity_id)
    if activity is None:
        raise CorrectionError("Activity not found.")
    target = (int(correction["start_index"]), int(correction["end_index"]))
    ranges = list(activity["corrections"])
    try:
        ranges.remove(target)
    except ValueError as exc:
        raise CorrectionError("Active correction not found.") from exc
    raw = _stream_dict(activity, "raw_streams").get("power")
    if not isinstance(raw, list):
        raise CorrectionError("Activity has no power stream.")
    if ranges:
        effective = _masked_power(raw, ranges)
        ftp_basis = float(correction["ftp_basis"])
        summary = _summary(activity, effective, ftp_basis)
    else:
        summary = {
            "avg_power": correction["original_avg_power"],
            "np": correction["original_np"],
            "if_": correction["original_if"],
            "tss": correction["original_tss"],
        }
    if not db.undo_power_correction(
        user_id,
        correction_id,
        summary,
        expected_ranges=activity["corrections"],
    ):
        raise CorrectionError("Active correction not found.")
    _refresh_user(user_id)
