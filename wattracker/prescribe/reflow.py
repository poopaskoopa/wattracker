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

Races are the one INPUT that is deliberately not in the recipe. They live in
their own table and are read fresh on every reflow, because they change
independently of the plan they bend (added, moved and deleted repeatedly);
baking them into the recipe would create a second source of truth for them.

On adapted rows: a row carrying ``adapted`` is SKIPPED (counted in
``skipped_locked``) unless it falls inside a race window - a taper, a
post-race recovery day or a race day itself - in which case the race wins and
the row is claimed like any other, clearing ``adapted``. An adaptation is a
considered response to the rider's current state and should not be thrown away
by an unrelated reflow; but a rider doing VO2max intervals three days before
their A race is strictly worse than losing one adaptation. adapt.py's own
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


def _eligible(row: dict, today: str, race_window: Optional[set] = None) -> bool:
    """Is this stored row one the recipe owns and may rewrite?

    An adapted row is off limits unless a race has an opinion about its date -
    inside a taper or a post-race recovery window the race outranks the
    adaptation (see the module docstring).
    """
    if row.get("adapted") is not None and row["date"] not in (race_window or ()):
        return False
    return (
        row["date"] > today
        and row.get("completed_activity_id") is None
        and row.get("origin") == GENERATED
    )


# tss is a float that has been through SQLite REAL and back; compare it with a
# tolerance rather than exactly.
_TSS_EPSILON = 0.05


def _differs(stored: dict, fresh: dict, fresh_zwo: str) -> bool:
    """Does the stored row disagree with the freshly computed workout?

    This covers everything the row stores, not just its labels: a change to
    ``build_workout`` that leaves name/type/variant/duration alone but moves
    the segments or the TSS still has to be detected, otherwise plans keep a
    stale prescription forever while reflow reports zero updates.
    """
    if (
        stored["name"] != fresh["name"]
        or stored["type"] != fresh["type"]
        or stored.get("variant") != fresh.get("variant")
        or int(stored["duration_s"]) != int(fresh["duration_s"])
    ):
        return True
    if abs(float(stored["tss"]) - float(fresh["tss"])) > _TSS_EPSILON:
        return True
    return stored.get("zwo_or_segments") != fresh_zwo


def _by_date(rows: List[dict]) -> tuple:
    """Index stored rows by date. Returns (index, conflicted_dates).

    The generator emits at most one workout per date (weekdays are unique
    within a week), so date is a safe key. A date holding several rows means
    something outside the generator wrote there - report it as a conflict and
    leave every row on that date alone rather than guessing which one is ours.

    KNOWN LIMITATION: the index is per-plan, so reflowing plan A can insert a
    workout on a date where a DIFFERENT plan already has one. The active-plan
    concept largely mitigates this in practice; a real fix needs a product
    decision about what several plans on one date should mean.
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

    Returns {status, updated, inserted, deleted, skipped_locked, raced_lost,
    failed, conflicts, races, race_conflicts} on success, or
    {status: 'not_reflowable', reason} having changed nothing.

    ``races`` echoes the races the recomputation planned around (with their
    EFFECTIVE priority) and ``race_conflicts`` reports A races demoted to B
    because they sat inside an earlier A race's taper, so the UI can warn.

    ``skipped_locked`` counts rows that WOULD have changed but were already
    past-dated, completed or not generator-owned when they were read. It is
    stable across runs (a locked row stays locked and keeps being counted).

    ``raced_lost`` counts rows that looked eligible at read time but were
    refused by the database guard at write time - they were completed, or
    midnight rolled over, in between. The two are kept apart deliberately:
    ``skipped_locked`` is "we knew it was locked", ``raced_lost`` is "it became
    locked underneath us".

    ``failed`` counts rows whose write raised. Each arm commits on its own
    connection, so there is no enclosing transaction to roll back; instead a
    failing row is logged, counted and stepped over so the remaining rows still
    get their diff applied and the caller still gets its counts. That is safe
    because reflow is self-healing: re-running after a partial write converges
    on exactly the state a clean generation produces.

    Reflowing an unmodified recipe reports zero across the board.
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
    # Read races fresh: they are an input to generation but are deliberately
    # NOT part of the recipe (see the module docstring).
    races = db.list_race_dates(user_id)
    try:
        generated = generate_plan(
            plan["name"], start, int(plan["weeks"]),
            days_of_week=args["days_of_week"] or [],
            hours_per_week=args["hours_per_week"],
            hit_days_per_week=args["hit_days_per_week"],
            hard_days=args["hard_days"] or None,
            model=args["model"] or "polarized",
            races=races,
        )
    except (ValueError, TypeError) as e:
        log.warning("plan %s has an unusable recipe: %s", plan_id, e)
        return _not_reflowable("invalid_recipe")

    now = now or utc_now()
    today = now.date().isoformat()
    race_info = generated.get("races") or {}
    race_window = set(race_info.get("window") or ())
    fresh_by_date = {w["date"]: w for w in generated["workouts"]}
    stored_by_date, conflicted = _by_date(
        db.plan_workouts_for_plan(user_id, plan_id, include_zwo=True)
    )

    counts = {"updated": 0, "inserted": 0, "deleted": 0, "skipped_locked": 0,
              "raced_lost": 0, "failed": 0, "conflicts": len(conflicted)}

    for date in sorted(set(fresh_by_date) | set(stored_by_date)):
        if date in conflicted:
            continue  # already counted; leave every row on that date alone
        fresh = fresh_by_date.get(date)
        stored = stored_by_date.get(date)
        try:
            _apply_one(user_id, plan_id, date, today, fresh, stored, counts,
                       race_window)
        except Exception:  # noqa: BLE001 - one bad row must not sink the plan
            counts["failed"] += 1
            log.warning(
                "reflow of plan %s failed on %s (workout %s)", plan_id, date,
                (stored or {}).get("id"), exc_info=True,
            )

    return {"status": "ok", "races": race_info.get("planned") or [],
            "race_conflicts": race_info.get("conflicts") or [], **counts}


def _apply_one(user_id: int, plan_id: int, date: str, today: str,
               fresh: Optional[dict], stored: Optional[dict],
               counts: Dict, race_window: Optional[set] = None) -> None:
    """Apply one date's diff, mutating `counts`. Raises on a write failure."""
    if fresh is not None and stored is None:
        # New training day. Past dates are never back-filled: a workout
        # that never existed cannot retroactively have been ridden.
        if date <= today:
            counts["skipped_locked"] += 1
            return
        zwo_str = zwo.zwo_string(fresh["session"])
        db.add_plan_workout(
            plan_id, user_id, date, fresh["name"], fresh["type"],
            fresh["duration_s"], fresh["tss"], zwo_str,
            variant=fresh.get("variant"), origin=GENERATED,
        )
        counts["inserted"] += 1
        reexport_workout(user_id, date, fresh["name"], fresh["name"], zwo_str)
        return

    if fresh is None and stored is not None:
        # The recipe no longer wants a workout here.
        if not _eligible(stored, today, race_window):
            counts["skipped_locked"] += 1
            return
        if db.delete_generated_plan_workout(user_id, stored["id"], today):
            counts["deleted"] += 1
            reexport_workout(user_id, date, stored["name"], None)
        else:
            counts["raced_lost"] += 1
        return

    if fresh is None or stored is None:
        return
    zwo_str = zwo.zwo_string(fresh["session"])
    if not _differs(stored, fresh, zwo_str):
        return
    if not _eligible(stored, today, race_window):
        counts["skipped_locked"] += 1
        return
    ok = db.replace_plan_workout_content(
        user_id, stored["id"], fresh["name"], fresh["type"],
        fresh["duration_s"], fresh["tss"], zwo_str, today,
        variant=fresh.get("variant"),
    )
    if ok:
        counts["updated"] += 1
        reexport_workout(user_id, date, stored["name"], fresh["name"], zwo_str)
    else:
        counts["raced_lost"] += 1
