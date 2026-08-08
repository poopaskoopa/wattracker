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

Post-race suppression: a race spikes ATL and craters TSB, so detection reports
``overreach`` for days afterwards and would gut the following week. That
fatigue is the EXPECTED cost of racing, not a training error, so adaptation is
suppressed for ``POST_RACE_QUIET_DAYS`` after any race (either priority) and
the plan's own post-race recovery days are left to do their job. Detection
itself stays pure - it still reports what the numbers say; only the response
is held back.

Race WINDOWS are skipped for the same reason, looking forward instead of back:
a workout inside a taper, on a race day, or on a post-race recovery day has
already had its load deliberately reduced by the generator, so easing it again
for overreach double-counts the reduction and wrecks the taper the rider is
training for. It also breaks a loop that is otherwise invisible: reflow claims
adapted rows inside a race window and clears ``adapted`` (races outrank an
adaptation there), which makes the row adaptable again, so adapt and reflow
rewrote the same rows and re-exported the same Zwift files every single night
with no net change. The window comes from ``plan.race_effects`` - the same
function the generator uses - so the two can never disagree about which dates
a race owns.
"""
from __future__ import annotations

import datetime as _dt
import logging
from typing import Dict, List, Optional

from .. import db
from ..backend import ExportManifest, get_backend
from ..ingest.importer import current_ftp
from ..metrics import profile_store
from ..timeutil import utc_now
from . import zwo
from .plan import normalize_races, race_effects, resolve_race_conflicts
from .planner import build_workout

log = logging.getLogger(__name__)

ADAPT_WINDOW_DAYS = 7
RECOVERY_DURATION_FACTOR = 0.75
MIN_DURATION_MIN = 20

POST_RACE_QUIET_DAYS = 10

# How far either side of the adaptation horizon to look for races when working
# out which upcoming days a race already owns. An A race tapers for 14 days
# before it and its recovery days trail it by up to ~2 weeks of ride days, so
# this covers every race that could have an opinion about a date inside the
# 7-day adaptation window with room to spare.
RACE_CONTEXT_DAYS = 45

OVERREACH = "overreach"
PLATEAU = "plateau"
PROGRESS = "progress"
POST_RACE = "post_race"

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


def reexport_workout(
    uid: int, date: str, old_name: str, new_name: Optional[str],
    zwo_str: Optional[str] = None,
) -> None:
    """Keep the Zwift folder in sync for one rewritten workout (best effort).

    ``new_name=None`` means the workout is gone: remove the old .zwo and write
    nothing. Shared with prescribe/reflow.py so both rewrite paths prune and
    re-write files the same way.

    Never raises: an unwritable, unresolvable or refused Zwift folder is logged
    and returned from. The caller is rewriting a plan; keeping the DB and the
    Zwift folder in step is a side effect of that, not a reason to fail it.
    """
    settings = db.get_user_settings(uid)
    write = (
        [{"date": date, "name": new_name, "zwo": zwo_str}]
        if new_name is not None and zwo_str is not None
        else []
    )
    manifest = ExportManifest(
        # No "me" fallback: it resolves to a stale <Workouts>/me folder Zwift
        # never reads. See exporter.plan_export_manifest and paths.workouts_dir.
        zwift_id=settings.get("zwift_id"),
        override=settings.get("workouts_dir"),
        write=write,
        # Only a rename orphans a file; re-writing under the same name just
        # overwrites it.
        remove=(
            [zwo.plan_filename(date, old_name)] if old_name != new_name else []
        ),
        # Best effort: never conjure a Zwift folder that isn't there.
        require_existing=True,
    )
    try:
        get_backend(uid).apply_exports(manifest)
    except OSError as e:
        log.warning("re-export of adapted workout failed: %s", e)


def race_window(user_id: int, plan_id: int, now: _dt.datetime) -> set:
    """ISO dates in ``plan_id`` that a race already has an opinion about.

    Delegates every date decision to ``plan.race_effects``, the function the
    generator itself uses, so "inside a race window" means exactly the same
    thing here, in the generator and in reflow.

    Scoped to ONE plan, because the generator's is: post-race recovery lands on
    the first N ride days after the race *of the plan being generated*. Feeding
    it a second, overlapping plan's rows would hand this function ride days the
    generator never saw and produce a window the two disagree about.

    Never raises: a rider with no races, no plan or unparseable rows simply
    owns no dates.
    """
    try:
        lo = (now.date() - _dt.timedelta(days=RACE_CONTEXT_DAYS)).isoformat()
        hi = (now.date() + _dt.timedelta(days=RACE_CONTEXT_DAYS)).isoformat()
        races = db.races_in_range(user_id, lo, hi)
        if not races:
            return set()
        # Post-race recovery lands on the rider's actual ride days, so the
        # window can only be computed against the days they ride.
        scheduled = [
            _dt.date.fromisoformat(d)
            for d in db.plan_workout_dates(user_id, plan_id, lo, hi)
        ]
        resolved, _conflicts = resolve_race_conflicts(normalize_races(races))
        return race_effects(resolved, scheduled).window()
    except Exception:  # noqa: BLE001 - never block adaptation on race context
        log.warning("race window lookup failed for user %s", user_id,
                    exc_info=True)
        return set()


def apply_adaptations(user_id: int, state, now: Optional[_dt.datetime] = None) -> Dict:
    """Run detection-driven plan adaptation for a user. Idempotent.

    Returns a summary for the dashboard banner, with the SAME keys on every
    path: {status, adjusted (this run), skipped_raced (left alone because a
    race already owns the day), upcoming (kind -> count of future adapted
    workouts), window_days}.
    """
    now = now or utc_now()
    today = now.date().isoformat()
    horizon = (now.date() + _dt.timedelta(days=ADAPT_WINDOW_DAYS)).isoformat()

    # Post-race quiet period. A race spikes ATL and drops TSB, so detection
    # says "overreach" and the policy above would rewrite the whole next week
    # as recovery rides - on top of the recovery days the plan already put
    # there for the race. Expected fatigue is not overreaching: hold the
    # response back and let the planned recovery run.
    since = (now.date() - _dt.timedelta(days=POST_RACE_QUIET_DAYS)).isoformat()
    recent_races = db.races_in_range(user_id, since, today)
    if recent_races:
        return {
            "status": POST_RACE,
            "adjusted": 0,
            # Same keys on every path: a summary with two shapes is a trap for
            # every consumer (the banner, the sweep totals, the tests).
            "skipped_raced": 0,
            "upcoming": db.upcoming_adapted_counts(user_id, today),
            "window_days": ADAPT_WINDOW_DAYS,
            "post_race_until": (
                _dt.date.fromisoformat(max(r["date"] for r in recent_races))
                + _dt.timedelta(days=POST_RACE_QUIET_DAYS)
            ).isoformat(),
        }

    status = detection_status(state)
    adjusted = 0
    skipped_raced = 0
    # Race windows are per-plan, so they are resolved per-plan and memoized
    # for the handful of plans this window can touch.
    windows: Dict[int, set] = {}
    # Rebuilt sessions must match the exported .zwo, so adaptation uses the
    # same rider profile the generator and reflow do.
    profile = profile_store.for_user(user_id)
    # Restamped with the rewritten content: export_ftp must keep describing the
    # FTP the stored fractions were written for, or the completion matcher ends
    # up checking fitted wattage against an FTP the prescription never used.
    export_ftp = current_ftp(user_id)
    for w in db.adaptable_plan_workouts(user_id, today, horizon):
        change = _plan_change(status, w["type"], w["duration_s"] / 60.0)
        if change is None:
            continue  # adaptation had no opinion about this day either way
        plan_id = w["plan_id"]
        if plan_id not in windows:
            windows[plan_id] = race_window(user_id, plan_id, now)
        if w["date"] in windows[plan_id]:
            # A taper, a race day or a post-race recovery day: the generator
            # has already cut this day's load on purpose (see the module
            # docstring). Easing it again double-counts, and reflow would put
            # it straight back tomorrow night.
            #
            # Counted only AFTER _plan_change, so the number means "workouts a
            # race stopped us changing", not "workouts inside a race window".
            # A 14-day taper otherwise had the banner claiming a dozen
            # workouts were left as planned when most were never candidates.
            skipped_raced += 1
            continue
        new_type, new_min, kind = change
        # Adaptation resets the session to the new kind's classic variant.
        new_variant = "classic"
        try:
            session = build_workout(new_type, new_min, new_variant,
                                    profile=profile)
        except ValueError:
            continue
        zwo_str = zwo.zwo_string(session)
        ok = db.update_plan_workout_content(
            user_id, w["id"], session.name, new_type,
            session.total_duration(), session.estimated_tss, zwo_str,
            kind, now.isoformat(timespec="seconds"), variant=new_variant,
            export_ftp=export_ftp,
        )
        if ok:
            adjusted += 1
            reexport_workout(user_id, w["date"], w["name"], session.name, zwo_str)

    return {
        "status": status,
        "adjusted": adjusted,
        "skipped_raced": skipped_raced,
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
    POST_RACE: {
        "level": "ok",
        "headline": "Recovering from a race",
        "detail": "Post-race fatigue is expected, so automatic plan "
                  "adjustments are paused while you recover.",
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
    # Say when a race is the reason nothing was eased. Silently doing nothing
    # reads as "the adaptation is broken"; the rider is mid-taper and the plan
    # has already cut those days on purpose.
    skipped = summary.get("skipped_raced", 0)
    banner["race_skipped"] = skipped
    if skipped:
        plural = skipped != 1
        banner["race_note"] = (
            f"{skipped} upcoming workout{'s' if plural else ''} left as "
            f"planned - {'they sit' if plural else 'it sits'} inside a race "
            "taper or recovery window, which already reduces the load."
        )
    return banner
