"""Recompute a stored plan from the recipe it was generated from.

A plan is persisted twice over: as its OUTPUT (the ``plan_workouts`` rows) and
as its INPUT (the ``plans.recipe`` JSON - the arguments ``generate_plan`` was
called with). Because ``generate_plan`` is a pure function of those arguments,
reflow can throw the old prescription away and recompute the whole plan, then
diff the result against the stored rows. Nothing is ever incrementally
patched, so a plan can be recomputed an unbounded number of times without
accumulating drift - which is what upcoming race handling needs, since races
get added, moved and deleted repeatedly.

The regeneration is always WHOLE-plan, never partial: the variant rotation in
``generate_plan`` walks counters across the entire plan, so recomputing a
subset would desync every workout after it.

Rows the recipe does not own are never touched: a row is eligible only if it
is future-dated, not completed, and carries ``origin = 'generated'``. Plans
with no recipe (everything created before the recipe column existed) are
refused outright - guessing a recipe would silently rewrite a plan into
something the user never asked for.

On clearing ``adapted``: when reflow claims a row it resets that row's
one-shot adapt.py adjustment budget. That is deliberate. An A-race taper has
to be able to overwrite a week adapt.py already locked; the alternative leaves
a rider doing VO2max intervals three days before their A-race. adapt.py's own
once-only guard (``db.update_plan_workout_content``) is untouched.
"""
from __future__ import annotations

import datetime as _dt
import logging
from typing import Dict, List, Optional

from .. import db
from ..timeutil import utc_now
from . import zwo
from .adapt import reexport_workout
from .plan import generate_plan

log = logging.getLogger(__name__)

RECIPE_VERSION = 1

# The generator arguments the recipe carries. name/start_date/weeks are columns
# on `plans` and are deliberately NOT duplicated here.
_RECIPE_KEYS = ("days_of_week", "hours_per_week", "hit_days_per_week",
                "hard_days", "model")

GENERATED = "generated"


def build_recipe(
    days_of_week, hours_per_week: float, hit_days_per_week: int,
    hard_days=None, model: str = "polarized",
) -> Dict:
    """The recipe dict to persist alongside a freshly generated plan."""
    return {
        "version": RECIPE_VERSION,
        "days_of_week": sorted({int(d) for d in days_of_week}),
        "hours_per_week": float(hours_per_week),
        "hit_days_per_week": int(hit_days_per_week),
        "hard_days": sorted({int(d) for d in (hard_days or [])}),
        "model": model,
    }


def _not_reflowable(reason: str) -> Dict:
    return {"status": "not_reflowable", "reason": reason}


def _eligible(row: dict, today: str) -> bool:
    """Is this stored row one the recipe owns and may rewrite?"""
    return (
        row["date"] > today
        and row.get("completed_activity_id") is None
        and row.get("origin") == GENERATED
    )


def _differs(stored: dict, fresh: dict) -> bool:
    return (
        stored["name"] != fresh["name"]
        or stored["type"] != fresh["type"]
        or stored.get("variant") != fresh.get("variant")
        or int(stored["duration_s"]) != int(fresh["duration_s"])
    )


def _by_date(rows: List[dict]) -> tuple:
    """Index stored rows by date. Returns (index, conflicted_dates).

    The generator emits at most one workout per date (weekdays are unique
    within a week), so date is a safe key. A date holding several rows means
    something outside the generator wrote there - report it as a conflict and
    leave every row on that date alone rather than guessing which one is ours.
    """
    index: Dict[str, dict] = {}
    conflicts = set()
    for r in rows:
        if r["date"] in index:
            conflicts.add(r["date"])
        index[r["date"]] = r
    for d in conflicts:
        index.pop(d, None)
    return index, conflicts


def reflow_plan(
    user_id: int, plan_id: int, now: Optional[_dt.datetime] = None
) -> Dict:
    """Recompute `plan_id` from its recipe and apply the diff. Idempotent.

    Returns {status, updated, inserted, deleted, skipped_locked, conflicts} on
    success, or {status: 'not_reflowable', reason} having changed nothing.

    ``skipped_locked`` counts rows that WOULD have changed but are past-dated,
    completed or not generator-owned. It is stable across runs (a locked row
    stays locked and keeps being counted); reflowing an unmodified recipe
    reports zero across the board.
    """
    plan = db.get_plan(user_id, plan_id)
    if plan is None:
        return _not_reflowable("missing")
    recipe = plan.get("recipe")
    if not recipe:
        return _not_reflowable("legacy")
    if recipe.get("version") != RECIPE_VERSION:
        return _not_reflowable("unsupported_version")

    try:
        start = _dt.date.fromisoformat(plan["start_date"])
    except (ValueError, TypeError):
        return _not_reflowable("bad_start_date")

    args = {k: recipe.get(k) for k in _RECIPE_KEYS}
    try:
        generated = generate_plan(
            plan["name"], start, int(plan["weeks"]),
            days_of_week=args["days_of_week"] or [],
            hours_per_week=args["hours_per_week"],
            hit_days_per_week=args["hit_days_per_week"],
            hard_days=args["hard_days"] or None,
            model=args["model"] or "polarized",
        )
    except (ValueError, TypeError) as e:
        log.warning("plan %s has an unusable recipe: %s", plan_id, e)
        return _not_reflowable("invalid_recipe")

    now = now or utc_now()
    today = now.date().isoformat()
    fresh_by_date = {w["date"]: w for w in generated["workouts"]}
    stored_by_date, conflicted = _by_date(
        db.plan_workouts_for_plan(user_id, plan_id, include_zwo=True)
    )

    counts = {"updated": 0, "inserted": 0, "deleted": 0,
              "skipped_locked": 0, "conflicts": len(conflicted)}

    for date in sorted(set(fresh_by_date) | set(stored_by_date)):
        if date in conflicted:
            continue  # already counted; leave every row on that date alone
        fresh = fresh_by_date.get(date)
        stored = stored_by_date.get(date)

        if fresh is not None and stored is None:
            # New training day. Past dates are never back-filled: a workout
            # that never existed cannot retroactively have been ridden.
            if date <= today:
                counts["skipped_locked"] += 1
                continue
            zwo_str = zwo.zwo_string(fresh["session"])
            db.add_plan_workout(
                plan_id, user_id, date, fresh["name"], fresh["type"],
                fresh["duration_s"], fresh["tss"], zwo_str,
                variant=fresh.get("variant"), origin=GENERATED,
            )
            counts["inserted"] += 1
            reexport_workout(user_id, date, fresh["name"], fresh["name"], zwo_str)
            continue

        if fresh is None and stored is not None:
            # The recipe no longer wants a workout here.
            if not _eligible(stored, today):
                counts["skipped_locked"] += 1
                continue
            if db.delete_generated_plan_workout(user_id, stored["id"], today):
                counts["deleted"] += 1
                reexport_workout(user_id, date, stored["name"], None)
            continue

        if fresh is None or stored is None or not _differs(stored, fresh):
            continue
        if not _eligible(stored, today):
            counts["skipped_locked"] += 1
            continue
        zwo_str = zwo.zwo_string(fresh["session"])
        ok = db.replace_plan_workout_content(
            user_id, stored["id"], fresh["name"], fresh["type"],
            fresh["duration_s"], fresh["tss"], zwo_str, today,
            variant=fresh.get("variant"),
        )
        if ok:
            counts["updated"] += 1
            reexport_workout(user_id, date, stored["name"], fresh["name"], zwo_str)

    return {"status": "ok", **counts}
