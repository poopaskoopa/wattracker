"""Recompute stored activity metrics that depend on historical FTP."""
from __future__ import annotations

from typing import Optional, Sequence

from . import db
from .config import db_path
from .metrics.power import (
    intensity_factor,
    is_plausible_ftp,
    normalized_power,
    training_stress_score,
)
from .timeutil import parse_naive


def score_activity(row: dict, path: Optional[str] = None) -> Optional[dict]:
    """Rebase one row's FTP-dependent metrics, or None if it must be left alone.

    Used by the offline repair (:mod:`wattracker.ftp_backfill`). Power itself is
    never touched: a row with an active manual power correction keeps the
    corrected ``np``/``avg_power`` and only has IF and TSS rebased, and an
    uncorrected row has its stored NP re-derived from its own power stream so
    the repair does not propagate a stale summary.

    Returns None - meaning "skip, do not write" - when the row has no usable
    date, no usable duration, or when the FTP effective on that date is not an
    admissible scoring basis. Writing the 0.0 that the scoring rail would return
    in that last case would destroy a real stored value, which a repair tool
    must never do.
    """
    if parse_naive(row.get("start_time")) is None:
        return None
    try:
        ftp = float(row.get("ftp_watts"))
        duration_s = float(row.get("duration_s"))
    except (TypeError, ValueError, OverflowError):
        return None
    if duration_s < 0 or not is_plausible_ftp(ftp, path=path):
        return None
    try:
        np_value = float(row.get("np") or 0.0)
    except (TypeError, ValueError, OverflowError):
        np_value = 0.0
    if not row.get("has_correction"):
        streams = row.get("streams")
        power = streams.get("power") if isinstance(streams, dict) else None
        if isinstance(power, list) and power:
            np_value = normalized_power(power)
    return {
        "id": int(row["id"]),
        "if_": round(intensity_factor(np_value, ftp, path=path), 3),
        "tss": round(training_stress_score(duration_s, np_value, ftp, path=path), 1),
        "ftp_watts": ftp,
        "np": np_value,
        "has_correction": bool(row.get("has_correction")),
    }


def rescore_imported_activities(
    user_id: int, activity_ids: Sequence[int], path: Optional[str] = None
) -> int:
    """Refresh IF and TSS without retaining the whole import in memory."""
    path = path or db_path()
    updated = 0
    for rows in db.activities_for_ftp_rescore(user_id, activity_ids, path):
        updates = []
        for row in rows:
            ftp = row.get("ftp_watts")
            np_value = row.get("np")
            duration_s = row.get("duration_s")
            if ftp is None or np_value is None or duration_s is None:
                continue
            ftp_value = float(ftp)
            np_float = float(np_value)
            duration_float = float(duration_s)
            ifv = (
                intensity_factor(np_float, ftp_value, path=path)
                if ftp_value > 0 else 0.0
            )
            tss = (
                training_stress_score(
                    duration_float, np_float, ftp_value, path=path
                )
                if ftp_value > 0 else 0.0
            )
            updates.append({
                "id": row["id"],
                "if_": round(ifv, 3),
                "tss": round(tss, 1),
            })
        if updates:
            updated += db.update_activity_ftp_metrics(user_id, updates, path)
    return updated
