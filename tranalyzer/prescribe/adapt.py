"""Adaptive plan adjustment driven by plateau/overreach detection.

Adjusts only FUTURE, not-completed plan workouts inside a rolling window
(next ``ADAPT_WINDOW_DAYS`` days). Every workout can be adapted AT MOST ONCE
in its lifetime (the ``adapted`` column records the kind), which makes the
whole process idempotent and hard-caps cumulative drift: re-running detection
never stacks adjustments.

Policies (all %FTP-based - sessions are rebuilt through ``build_workout`` so
the calendar detail, ERG targets and .zwo export all see the same content):

- overreach  -> upcoming workouts become easy recovery rides at 75% of the
                planned duration (min 20 min): cut both intensity and volume.
- plateau    -> upcoming HARD days get a novel stimulus: the interval type is
                swapped (vo2max <-> threshold, sweet_spot -> vo2max) at the
                same duration. Same polarized volume, different signal - the
                classic response to a training plateau.
- progress   -> (no overreach, no plateau) no adaptation. Planned volume is
                held flat and must never increase week to week, so a healthy
                "keep going" signal leaves upcoming workouts untouched.
"""
from __future__ import annotations

import datetime as _dt
import logging
import os
from typing import Dict, List, Optional

from .. import db, paths
from . import zwo
from .planner import build_workout

log = logging.getLogger(__name__)

ADAPT_WINDOW_DAYS = 7
RECOVERY_DURATION_FACTOR = 0.75
MIN_DURATION_MIN = 20

OVERREACH = "overreach"
PLATEAU = "plateau"
PROGRESS = "progress"

HARD_TYPES = ("vo2max", "threshold", "sweet_spot")
_STIMULUS_SWAP = {"vo2max": "threshold", "threshold": "vo2max",
                  "sweet_spot": "vo2max"}


def detection_status(state) -> str:
    """Map a TrainingState to the banner/adaptation status (overreach wins)."""
    if getattr(state, "overreach", False):
        return OVERREACH
    if getattr(state, "plateau", False):
        return PLATEAU
    return PROGRESS


def _plan_change(status: str, wtype: str, duration_min: float):
    """New (type, duration_min, kind) for a workout, or None to leave it."""
    if status == OVERREACH:
        new_min = max(MIN_DURATION_MIN, round(duration_min * RECOVERY_DURATION_FACTOR))
        return "recovery", new_min, "recovery"
    if status == PLATEAU:
        if wtype not in _STIMULUS_SWAP:
            return None  # easy days keep their role during a plateau
        return _STIMULUS_SWAP[wtype], max(MIN_DURATION_MIN, round(duration_min)), "stimulus"
    # progress: no change - volume is held flat and must never increase.
    return None


def _reexport(uid: int, date: str, old_name: str, new_name: str, zwo_str: str) -> None:
    """Keep the Zwift folder in sync for an adapted workout (best effort)."""
    settings = db.get_user_settings(uid)
    target, _reason = paths.resolve_export_dir(
        settings.get("zwift_id"), settings.get("workouts_dir")
    )
    if not target or not os.path.isdir(target):
        return
    try:
        old_path = os.path.join(target, zwo.plan_filename(date, old_name))
        if old_name != new_name and os.path.exists(old_path):
            os.unlink(old_path)
        zwo.write_plan_to_zwift(
            [{"date": date, "name": new_name, "zwo": zwo_str}],
            settings.get("zwift_id") or "me",
            workouts_override=target,
        )
    except OSError as e:
        log.warning("re-export of adapted workout failed: %s", e)


def apply_adaptations(user_id: int, state, now: Optional[_dt.datetime] = None) -> Dict:
    """Run detection-driven plan adaptation for a user. Idempotent.

    Returns a summary for the dashboard banner:
      {status, adjusted (this run), upcoming (kind -> count of future adapted
       workouts), window_days}
    """
    now = now or _dt.datetime.now()
    today = now.date().isoformat()
    horizon = (now.date() + _dt.timedelta(days=ADAPT_WINDOW_DAYS)).isoformat()
    status = detection_status(state)

    adjusted = 0
    for w in db.adaptable_plan_workouts(user_id, today, horizon):
        change = _plan_change(status, w["type"], w["duration_s"] / 60.0)
        if change is None:
            continue
        new_type, new_min, kind = change
        try:
            session = build_workout(new_type, new_min)
        except ValueError:
            continue
        zwo_str = zwo.zwo_string(session)
        ok = db.update_plan_workout_content(
            user_id, w["id"], session.name, new_type,
            session.total_duration(), session.estimated_tss, zwo_str,
            kind, now.isoformat(timespec="seconds"),
        )
        if ok:
            adjusted += 1
            _reexport(user_id, w["date"], w["name"], session.name, zwo_str)

    return {
        "status": status,
        "adjusted": adjusted,
        "upcoming": db.upcoming_adapted_counts(user_id, today),
        "window_days": ADAPT_WINDOW_DAYS,
    }


# Banner copy per status; the route augments it with adaptation counts.
BANNER = {
    OVERREACH: {
        "level": "danger",
        "headline": "Overreach detected",
        "detail": "Fatigue is outrunning fitness - upcoming workouts are eased "
                  "so you can absorb the training.",
    },
    PLATEAU: {
        "level": "warn",
        "headline": "Plateau detected",
        "detail": "Fitness has stalled - upcoming hard days get a different "
                  "stimulus to break through.",
    },
    PROGRESS: {
        "level": "ok",
        "headline": "Progressing well",
        "detail": "No plateau or overreach detected - training continues at "
                  "your planned volume.",
    },
}


def banner_for(state, summary: Dict) -> Dict:
    """Build the dashboard status-banner payload."""
    status = summary.get("status") or detection_status(state)
    banner = dict(BANNER[status])
    banner["status"] = status
    alerts = list(getattr(state, "alerts", None) or [])
    if alerts:
        banner["detail"] = alerts[0]
        banner["alerts"] = alerts
    else:
        banner["alerts"] = []

    kind_word = {"recovery": "eased for recovery",
                 "stimulus": "switched to a new stimulus",
                 "overload": "nudged up for overload"}
    parts: List[str] = []
    for kind, n in sorted((summary.get("upcoming") or {}).items()):
        if n:
            parts.append(f"{n} upcoming workout{'s' if n != 1 else ''} "
                         f"{kind_word.get(kind, kind)}")
    banner["adaptation"] = "; ".join(parts) if parts else None
    banner["adjusted_now"] = summary.get("adjusted", 0)
    return banner
