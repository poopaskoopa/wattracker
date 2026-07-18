"""Keep a user's Zwift custom-workout folder in sync with their plan.

Exports EVERY not-yet-completed plan workout (past and future) whose date is
not inside an out-of-office (OOTO) range, and removes the .zwo of any workout
that is now OOTO-skipped (or completed) so the Zwift list stays clean. Used by
the "Export all to Zwift" action and the daily maintenance sweep.
"""
from __future__ import annotations

import logging
import os
from typing import Dict, List, Optional

from . import db, paths
from .prescribe import zwo

log = logging.getLogger(__name__)


def _all_plan_workouts(user_id: int) -> List[dict]:
    out: List[dict] = []
    for p in db.list_plans(user_id):
        out.extend(db.plan_workouts_for_plan(user_id, p["id"], include_zwo=True))
    return out


def sync_plan_exports(user_id: int) -> Dict:
    """Export all exportable plan workouts; prune skipped/completed ones.

    Returns {status, directory, exported, removed, reason}.
      - status 'ok'      : wrote/pruned files (directory set)
      - status 'choose'  : several Zwift player folders, user must pick
      - status 'missing' : no Zwift Workouts folder on this machine
      - status 'empty'   : nothing to export
    """
    settings = db.get_user_settings(user_id)
    target, reason = paths.resolve_export_dir(
        settings.get("zwift_id"), settings.get("workouts_dir")
    )
    if not target:
        return {"status": reason, "directory": None, "exported": 0, "removed": 0,
                "reason": reason}

    workouts = _all_plan_workouts(user_id)
    if not workouts:
        return {"status": "empty", "directory": target, "exported": 0,
                "removed": 0, "reason": None}

    to_write: List[dict] = []
    to_remove: List[str] = []
    for w in workouts:
        skip = bool(w.get("completed_activity_id")) or db.ooto_covers(
            user_id, w["date"]
        )
        fname = zwo.plan_filename(w["date"], w["name"])
        if skip:
            # Completed rides / OOTO days should not linger in Zwift.
            to_remove.append(fname)
        else:
            to_write.append(
                {"date": w["date"], "name": w["name"], "zwo": w["zwo_or_segments"]}
            )

    exported = 0
    if to_write:
        result = zwo.write_plan_to_zwift(
            to_write, settings.get("zwift_id") or "me", workouts_override=target
        )
        exported = result["count"]

    removed = 0
    for fname in to_remove:
        p = os.path.join(target, fname)
        try:
            if os.path.exists(p):
                os.unlink(p)
                removed += 1
        except OSError as e:
            log.warning("could not remove skipped export %s: %s", p, e)

    return {"status": "ok", "directory": target, "exported": exported,
            "removed": removed, "reason": reason}


def remove_plan_exports(user_id: int, plan_id: int) -> Dict:
    """Remove a plan's generated .zwo files from the Zwift custom-workout folder.

    MUST run BEFORE the plan's DB rows are deleted - filenames are derived from
    the workout rows via zwo.plan_filename(date, name). Reuses sync_plan_exports'
    export-dir resolution and per-file OSError handling.

    Returns {status, directory, removed, reason}.
      - status 'ok'      : resolved a folder and pruned files (may be 0)
      - status 'choose'  : several Zwift player folders, none removed
      - status 'missing' : no Zwift Workouts folder on this machine
    """
    settings = db.get_user_settings(user_id)
    target, reason = paths.resolve_export_dir(
        settings.get("zwift_id"), settings.get("workouts_dir")
    )
    if not target:
        return {"status": reason, "directory": None, "removed": 0,
                "reason": reason}

    workouts = db.plan_workouts_for_plan(user_id, plan_id)
    removed = 0
    for w in workouts:
        fname = zwo.plan_filename(w["date"], w["name"])
        p = os.path.join(target, fname)
        try:
            if os.path.exists(p):
                os.unlink(p)
                removed += 1
        except OSError as e:
            log.warning("could not remove plan export %s: %s", p, e)

    return {"status": "ok", "directory": target, "removed": removed,
            "reason": reason}
