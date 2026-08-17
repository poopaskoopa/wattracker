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
from .metrics.power import (
    asserted_ftp,
    intensity_factor,
    is_plausible_ftp,
    normalized_power,
    training_stress_score,
    within_assertion_bounds,
)

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


def _number(value) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _never_scored(np_value, intensity) -> bool:
    """True for the "imported but deliberately not scored" marker.

    ``importer._build_record`` stores NP (a measurement) but leaves IF and TSS
    at 0 when no plausible FTP basis was available - so ``np > 0`` with
    ``if_ == 0`` means "this row has never been scored", which is precisely the
    state issue #62's repair pass looks for. A correction masks bad samples; it
    is not a decision to score, so it must leave that marker intact rather than
    back-filling a score from whatever FTP happens to be current today.
    """
    np_number = _number(np_value) or 0.0
    if_number = _number(intensity) or 0.0
    return np_number > 0 and if_number == 0.0


def _scoring_basis(basis, user_id: int) -> float:
    """Resolve a stored or back-solved basis into one the scorer will accept.

    This module deliberately re-scores a corrected ride against the SAME basis
    the row was originally scored against, so a correction changes only what the
    rider asked it to change. That design stands - but it cannot extend to
    propagating a basis the app has just declared impossible. A basis of 0.6378 W
    recovered from a legacy row is a failed estimate baked into the data (issue
    #60); passing it through unwrapped makes the scorers return 0.0, leaving the
    corrected row unscored rather than minting a brand-new IF of 313 today.

    Between those two cases sits a basis that is BELOW the estimate floor but
    inside the range a human FTP can take: a rider mid-rehab who stated 40 W and
    has rows legitimately scored at IF 5. Such a row was scored, and the only
    basis this app ever scores a row against below the floor is one the rider
    asserted, so the recovered number is honoured as the assertion it evidences.
    Note what this does NOT do: it does not consult today's FTP. A rider who has
    since recovered and updated 40 W to 250 W must not have their old row's
    valid score destroyed by a correction, and identity with the CURRENT
    assertion - to any tolerance - is both wrong here and exploitable (setting
    the current FTP to 0.64 would re-bless a corrupt 0.6378 legacy basis and
    re-score the ride at TSS 1,633,482). The test is on the number's physical
    range alone, which no setting can move.
    """
    number = _number(basis)
    if number is None or number <= 0 or is_plausible_ftp(number):
        return number if number is not None else 0.0
    if within_assertion_bounds(number):
        return asserted_ftp(number) or 0.0
    _log.warning(
        "not re-scoring correction for user %s against recovered basis %.3f W: "
        "outside the range an FTP can physically take", user_id, number,
    )
    return number


def _recovered_ftp(activity: dict, user_id: int) -> float:
    """The basis a row was scored against, for the ``ftp_basis`` audit column.

    Back-solved from the stored summary where the row was scored. A never-scored
    row has no basis to recover; the schema requires a positive number, so the
    rider's current FTP is recorded as "what a future rescore would use". It is
    NOT used to score - see ``_never_scored`` and ``apply``.
    """
    np_number = _number(activity.get("np"))
    intensity_number = _number(activity.get("if_"))
    if np_number and intensity_number and np_number > 0 and intensity_number > 0:
        recovered = _number(np_number / intensity_number)
        if recovered is not None and recovered > 0:
            return recovered
    try:
        current = _number(importer.current_ftp(user_id))
    except (TypeError, ValueError, OverflowError):
        # A stream so malformed the estimator cannot even coerce it; the audit
        # column still needs a number and nothing here is used to score.
        current = None
    return current if current and current > 0 else 200.0


def _summary(activity: dict, power: list, ftp: float) -> dict:
    cleaned = [
        number if (number := _power_value(value)) is not None else 0.0
        for value in power
    ]
    avg = sum(cleaned) / len(cleaned) if cleaned else 0.0
    np_value = normalized_power(cleaned) if cleaned else 0.0
    try:
        duration = float(activity.get("duration_s") or 0)
    except (TypeError, ValueError, OverflowError):
        duration = 0.0
    if not math.isfinite(duration) or duration < 0:
        duration = 0.0
    # No `if ftp > 0` guard: both scorers apply the plausibility admission test
    # themselves, so an implausible or absent basis yields 0.0 here - the same
    # never-scored state the importer leaves.
    intensity = intensity_factor(np_value, ftp)
    tss = training_stress_score(duration, np_value, ftp)
    return {
        "avg_power": avg,
        "np": round(np_value, 1),
        "if_": round(intensity, 3),
        "tss": round(tss, 1),
    }


def _refresh_user(user_id: int) -> None:
    activity_cache.invalidate(user_id)
    from .metrics import curve_store
    curve_store.ensure(user_id)
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
    # A row that was never scored stays never scored: masking samples changes
    # the measurement, not the decision to score. Otherwise re-score against the
    # row's own basis, admitted by _scoring_basis.
    basis = (
        0.0 if _never_scored(activity.get("np"), activity.get("if_"))
        else _scoring_basis(ftp_basis, user_id)
    )
    summary = _summary(activity, effective, basis)
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
        # Same two rules as apply(), read off the metrics this correction
        # captured: a partial undo must not score a row that apply() left
        # unscored, nor revive an implausible basis.
        basis = (
            0.0 if _never_scored(
                correction["original_np"], correction["original_if"]
            )
            else _scoring_basis(correction["ftp_basis"], user_id)
        )
        summary = _summary(activity, effective, basis)
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
