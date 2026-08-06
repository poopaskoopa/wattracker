"""Recompute stored activity metrics that depend on historical FTP."""
from __future__ import annotations

from typing import Optional, Sequence

from . import db
from .metrics.power import intensity_factor, training_stress_score


def rescore_imported_activities(
    user_id: int, activity_ids: Sequence[int], path: Optional[str] = None
) -> int:
    """Refresh IF and TSS without retaining the whole import in memory."""
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
            ifv = intensity_factor(np_float, ftp_value) if ftp_value > 0 else 0.0
            tss = (
                training_stress_score(duration_float, np_float, ftp_value)
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
