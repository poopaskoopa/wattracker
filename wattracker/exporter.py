"""Keep a user's Zwift custom-workout folder in sync with their plan.

Exports EVERY not-yet-completed plan workout (past and future) whose date is
not inside an out-of-office (OOTO) range and is not a planned race day, and
removes the .zwo of any workout that is now skipped (or completed) so the
Zwift list stays clean. Used by the "Export all to Zwift" action and the daily
maintenance sweep.

Deciding *what* the Zwift folder should contain is pure database work and
happens here (``plan_export_manifest``); actually touching files is the
backend's job, because in a server/client install those files are on another
machine entirely.
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional

from . import db
from .backend import BackendUnavailable, ExportManifest, get_backend
from .prescribe import zwo

log = logging.getLogger(__name__)


def _all_plan_workouts(user_id: int) -> List[dict]:
    out: List[dict] = []
    for p in db.list_plans(user_id):
        out.extend(db.plan_workouts_for_plan(user_id, p["id"], include_zwo=True))
    return out


def plan_export_manifest(user_id: int) -> Optional[ExportManifest]:
    """The .zwo files a user's Zwift folder should hold, and which to prune.

    Pure: reads the database and returns intent, touching no files. ``None``
    means the user has no plan workouts at all, which the callers report as
    'empty' rather than writing an empty manifest.
    """
    settings = db.get_user_settings(user_id)
    workouts = _all_plan_workouts(user_id)
    if not workouts:
        return None

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
        if skip:
            # Completed rides / OOTO / race days should not linger in Zwift.
            to_remove.append(zwo.plan_filename(w["date"], w["name"]))
        else:
            to_write.append(
                {"date": w["date"], "name": w["name"], "zwo": w["zwo_or_segments"]}
            )

    return ExportManifest(
        # No "me" fallback. "me" is a valid bare folder name, so the resolver
        # takes its zwift_id branch and hands back <Workouts>/me whenever that
        # folder exists - which is every install upgraded from the version that
        # created it - and the export reports success into a folder Zwift never
        # reads (#44). An absent id must fall through to detection.
        zwift_id=settings.get("zwift_id"),
        override=settings.get("workouts_dir"),
        write=to_write,
        remove=to_remove,
    )


def sync_plan_exports(user_id: int) -> Dict:
    """Export all exportable plan workouts; prune skipped/completed ones.

    Returns {status, directory, exported, removed, reason}.
      - status 'ok'      : wrote/pruned files (directory set)
      - status 'choose'  : several Zwift player folders, user must pick
      - status 'missing' : no Zwift Workouts folder on this machine
      - status 'empty'   : nothing to export
    """
    backend = get_backend(user_id)
    manifest = plan_export_manifest(user_id)
    if manifest is None:
        settings = db.get_user_settings(user_id)
        try:
            target, reason = backend.resolve_export_dir(
                settings.get("zwift_id"), settings.get("workouts_dir")
            )
        except BackendUnavailable:
            return {"status": "offline", "directory": None, "exported": 0,
                    "removed": 0, "reason": "offline"}
        if not target:
            return {"status": reason, "directory": None, "exported": 0,
                    "removed": 0, "reason": reason}
        return {"status": "empty", "directory": target, "exported": 0,
                "removed": 0, "reason": None}

    return backend.apply_exports(manifest)


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
    manifest = ExportManifest(
        zwift_id=settings.get("zwift_id"),  # never "me" - see plan_export_manifest
        override=settings.get("workouts_dir"),
        remove=[
            zwo.plan_filename(w["date"], w["name"])
            for w in db.plan_workouts_for_plan(user_id, plan_id)
        ],
    )
    result = get_backend(user_id).apply_exports(manifest)
    # This call only ever prunes, so 'exported' is noise in its contract.
    result.pop("exported", None)
    return result
