"""Keep a user's Zwift custom-workout folder in sync with their plan.

Exports EVERY not-yet-completed plan workout (past and future) whose date is
not inside an out-of-office (OOTO) range and is not a planned race day, and
removes the .zwo of any workout that is now skipped (or completed) so the
Zwift list stays clean. Used by the "Export all to Zwift" action and the daily
maintenance sweep.
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
      - status 'blocked' : a folder was configured/found and refused as unsafe
      - status 'empty'   : nothing to export

    It never raises paths.ExportTargetUnavailable: a refusal comes back as a
    status, because every caller of this function treats the return value as
    the whole answer.
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
        skip = (
            bool(w.get("completed_activity_id"))
            or db.ooto_covers(user_id, w["date"])
            # On a race day the race IS the session; reflow normally removes
            # the plan row, but a past-dated or otherwise locked row can
            # survive it, and it must still not be exported.
            or db.race_on(user_id, w["date"]) is not None
        )
        fname = zwo.plan_filename(w["date"], w["name"])
        if skip:
            # Completed rides / OOTO / race days should not linger in Zwift.
            to_remove.append(fname)
        else:
            to_write.append(
                {"date": w["date"], "name": w["name"], "zwo": w["zwo_or_segments"]}
            )

    exported = 0
    if to_write:
        try:
            # Pass the STORED setting, not the ``target`` resolved above.
            # workouts_override is the untrusted user value; feeding a resolved
            # directory back into it re-labels a folder the app DISCOVERED as
            # one the user SUBMITTED, and the writer then judges it by the
            # stricter submitted-path rule. That is how a rider whose Zwift
            # player folder is a junction to another drive got a directory here
            # and a refusal one call later. Both calls run the same resolver on
            # the same inputs, so result["directory"] is ``target``.
            result = zwo.write_plan_to_zwift(
                to_write,
                settings.get("zwift_id"),
                workouts_override=settings.get("workouts_dir"),
            )
        except paths.ExportTargetUnavailable as e:
            # resolve_export_dir() just handed us this directory, so the writer
            # should not be able to refuse it - but this function's contract is
            # a status dict, and callers (the "Export all" route, the OOTO and
            # race handlers, the nightly sweep) treat it as one. It is also
            # deliberately NOT an OSError, so the per-file `except OSError`
            # below would not have caught it. Report it in the resolver's own
            # vocabulary and prune nothing: a target the writer just rejected
            # is not one to start unlinking files in.
            log.warning("plan export refused (%s): %s", e.reason, e.detail or e)
            return {"status": e.reason, "directory": None, "exported": 0,
                    "removed": 0, "reason": e.reason}
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
