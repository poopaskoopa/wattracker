"""Pure proposals for adjusting generated workouts around an OOTO range.

The two non-trivial options are deliberately different SHAPES of answer, not
two labels for the same edit:

- ``reschedule`` MOVES a canceled key workout onto a future easy day. It keeps
  its own prescribed duration, so the easy session it lands on is lost and the
  plan's total volume drops by that session's length. The rider is told how
  much.
- ``rebalance`` moves NOTHING. The calendar shape is untouched: no session is
  relocated, inserted or destroyed. Instead the surviving easy sessions absorb
  part of the missed intensity by stepping up one dose level AT THE SAME
  DURATION, so weekly minutes cannot move and the lost load is only partly
  recovered.

Both are volume-safe by construction. Weekly minutes must never exceed
``hours_per_week * 60``: reschedule only ever swaps a session for one that is
at least as long (see ``_compatible``), which can only shrink a week, and
rebalance never touches ``duration_s`` at all.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
from typing import Any, Iterable

from . import zwo as _zwo
from .planner import build_workout

VERSION = 2
HARD_TYPES = frozenset({"vo2max", "threshold", "sweet_spot", "sprint"})

# How many surviving sessions are asked to absorb ONE canceled key workout.
REBALANCE_SPREAD = 2

# The one dose step rebalance is allowed to take: same duration, one rung up,
# and the result is never a HARD type. A canceled threshold must not silently
# turn an endurance ride into a second threshold session - partial recovery of
# the lost load is the honest outcome, so an easy Zone-2 ride becomes a tempo
# ride of exactly the same length and nothing else is ever stepped up.
DOSE_STEP = {"endurance": "tempo"}


def _date(value: Any) -> _dt.date:
    if isinstance(value, _dt.datetime):
        return value.date()
    if isinstance(value, _dt.date):
        return value
    return _dt.date.fromisoformat(str(value)[:10])


def _iso(value: Any) -> str:
    return _date(value).isoformat()


def _race_set(race_dates: Iterable[Any] | None) -> set[str]:
    result = set()
    for race in race_dates or ():
        value = race.get("date") if isinstance(race, dict) else race
        try:
            result.add(_iso(value))
        except (TypeError, ValueError):
            continue
    return result


def _rows(workouts: Any, plan: dict | None) -> list[dict]:
    if isinstance(workouts, dict):
        workouts = workouts.get("workouts", ())
    if workouts is None and isinstance(plan, dict):
        workouts = plan.get("workouts", ())
    return [dict(row) for row in (workouts or ()) if isinstance(row, dict)]


_FINGERPRINT_KEYS = (
    "id", "plan_id", "date", "name", "type", "duration_s", "tss",
    "zwo_or_segments", "completed_activity_id", "completed_date", "adapted",
    "adapted_at", "rpe", "variant", "compliance", "effective_ftp",
    "feedback_applied", "feedback_batch_id", "origin", "export_ftp",
    "adjustment_id", "adjustment_state", "adjustment_source_id",
)


def _fingerprint(row: dict) -> str:
    normalized = {key: row.get(key) for key in _FINGERPRINT_KEYS}
    normalized["feedback_applied"] = int(bool(normalized["feedback_applied"]))
    payload = json.dumps(normalized, sort_keys=True, separators=(",", ":"),
                         default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _eligible(row: dict, today: _dt.date, excluded: set[str]) -> bool:
    try:
        day = _date(row.get("date"))
    except (TypeError, ValueError):
        return False
    return (
        day > today
        and row.get("origin") == "generated"
        and row.get("completed_activity_id") is None
        and row.get("adapted") is None
        and row.get("adjustment_state") is None
        and day.isoformat() not in excluded
    )


def _hard(row: dict) -> bool:
    kind = str(row.get("type") or row.get("kind") or "").lower()
    return kind in HARD_TYPES or bool(row.get("hard"))


def _phase(phase_by_date: Any, day: str) -> Any:
    if isinstance(phase_by_date, dict):
        value = phase_by_date.get(day)
        return value.get("key") if isinstance(value, dict) else value
    return None


def _volume(row: dict) -> Any:
    return row.get("duration_s", row.get("duration_min", row.get("tss")))


def _compatible(source: dict, target: dict) -> bool:
    """May ``source`` take over ``target``'s day at its OWN duration?

    The moved session keeps the prescription it was designed with - it is never
    rescaled to fit the slot - so the target week's minutes change by
    ``source - target``. Requiring the source to be no longer than the session
    it replaces makes that delta zero or negative, which is what keeps the
    standing "weekly minutes never exceed hours_per_week * 60" invariant true
    on this path. It is deliberately NOT an equality test: real plans pair
    3000 s key sessions with 7800 s / 4020 s easy ones and equality dropped
    almost every proposal on the floor.
    """
    source_volume, target_volume = _volume(source), _volume(target)
    if source_volume is None or target_volume is None:
        return True
    try:
        return float(source_volume) <= float(target_volume)
    except (TypeError, ValueError):
        return False


def _action(source: dict, target: dict, mode: str) -> dict:
    return {
        "mode": mode,
        "source_id": source.get("id"),
        "source_date": _iso(source["date"]),
        "target_id": target.get("id"),
        "target_date": _iso(target["date"]),
        "expected_source_fingerprint": _fingerprint(source),
        "expected_target_fingerprint": _fingerprint(target),
    }


def _duration_s(row: dict) -> int | None:
    try:
        value = int(row["duration_s"])
    except (KeyError, TypeError, ValueError):
        return None
    return value if value > 0 else None


def _dose_step(row: dict, profile: Any) -> dict | None:
    """The one-rung dose increase for an easy row, or None if there isn't one.

    Same type-family step, same duration, classic variant (adaptation resets
    the variant the same way). Anything that would move ``duration_s`` by even
    a second is refused rather than rounded: rebalance's whole promise is that
    weekly minutes do not change.
    """
    kind = str(row.get("type") or row.get("kind") or "").lower()
    new_type = DOSE_STEP.get(kind)
    if new_type is None or new_type in HARD_TYPES:
        return None
    duration_s = _duration_s(row)
    if duration_s is None or duration_s % 60:
        return None
    try:
        session = build_workout(new_type, duration_s / 60.0, "classic",
                                profile=profile)
    except (ValueError, TypeError):
        return None
    if session.total_duration() != duration_s:
        return None
    try:
        if float(session.estimated_tss) <= float(row.get("tss") or 0.0):
            return None  # not actually a higher dose; leave the row alone
    except (TypeError, ValueError):
        return None
    return {
        "new_type": new_type,
        "new_variant": "classic",
        "new_name": session.name,
        "new_tss": float(session.estimated_tss),
        "new_zwo": _zwo.zwo_string(session),
        "duration_s": duration_s,
    }


def _dose_action(source: dict, target: dict, step: dict) -> dict:
    action = _action(source, target, "rebalance")
    action.update(step)
    return action


def _key_out(row: dict) -> dict:
    return {"id": row.get("id"), "date": _iso(row["date"]),
            "type": row.get("type", row.get("kind"))}


def _option(kind: str, actions: list[dict], rationale: str, *,
            affected_keys: int, unresolved: list[dict],
            volume_delta_s: int = 0) -> dict:
    """One rider-facing choice, with the numbers needed to describe it.

    ``affected_keys`` counts the key workouts the OOTO range cancels;
    ``resolved_keys`` counts the ones this option does something about. They
    are reported separately on purpose - ``len(actions)`` is a count of
    proposed edits, never a count of affected workouts, and rebalance emits
    several edits per canceled workout.
    """
    return {
        "kind": kind,
        "actions": actions,
        "rationale": rationale,
        "affected_keys": affected_keys,
        "resolved_keys": affected_keys - len(unresolved),
        "unresolved": [_key_out(r) for r in unresolved],
        "volume_delta_s": int(volume_delta_s),
    }


def evaluate_ooto(
    plan, workouts, ooto_start, ooto_end, today, phase_by_date=None,
    race_dates=None, window_days=14, profile=None,
) -> dict:
    """Return deterministic, stale-safe OOTO adjustment proposals.

    The function only reads its plain-dict inputs. Dates are serialized as ISO
    strings and every proposed mutation carries fingerprints of both rows.
    """
    start, end, now = _date(ooto_start), _date(ooto_end), _date(today)
    if end < start:
        raise ValueError("ooto_end must not precede ooto_start")
    races = _race_set(race_dates)
    rows = _rows(workouts, plan)
    # OOTO rows are intentionally retained here: they are the affected rows
    # the caller needs to review. Race dates, by contrast, are never owned by
    # this evaluator.
    usable = [r for r in rows if _eligible(r, now, races)]
    affected = sorted(
        (r for r in usable if start <= _date(r["date"]) <= end),
        key=lambda r: (_iso(r["date"]), str(r.get("id", ""))),
    )
    affected_out = [{
        "id": r.get("id"), "date": _iso(r["date"]),
        "type": r.get("type", r.get("kind")),
        "key": _hard(r), "fingerprint": _fingerprint(r),
    } for r in affected]
    keys = [r for r in affected if _hard(r)]

    easy = [r for r in usable if not _hard(r) and _date(r["date"]) > end]
    easy.sort(key=lambda r: (_date(r["date"]), str(r.get("id", ""))))
    hard_elsewhere = [r for r in usable if _hard(r) and r not in keys]

    # Dates that already hold a hard session. Targets chosen during the loop
    # are added to it as they are chosen: without that, two key workouts
    # cancelled by one range could be placed on consecutive days, which is
    # exactly what the adjacency rule exists to prevent.
    hard_days = {_iso(h["date"]) for h in hard_elsewhere}

    def in_window(target: dict) -> bool:
        target_day = _date(target["date"])
        return ((target_day - end).days <= int(window_days)
                and target_day.isoformat() not in races)

    def candidate(source: dict, target: dict) -> bool:
        if not in_window(target) or not _compatible(source, target):
            return False
        target_day = _date(target["date"])
        # A hard target must not be adjacent to a day that already holds hard
        # work - including one this same proposal has just claimed.
        return not any(abs((target_day - _dt.date.fromisoformat(d)).days) <= 1
                       for d in hard_days)

    def nearest(source: dict):
        def key(t: dict):
            return (
                _phase(phase_by_date, _iso(t["date"]))
                != _phase(phase_by_date, _iso(source["date"])),
                (_date(t["date"]) - end).days, _iso(t["date"]),
                str(t.get("id", "")),
            )
        return key

    # ---- reschedule: move the key workout, lose the easy session it lands on
    reschedule_actions: list[dict] = []
    moved_delta_s = 0
    used: set[int] = set()
    unmoved: list[dict] = []
    for source in keys:
        choices = [t for t in easy if id(t) not in used and candidate(source, t)]
        choices.sort(key=nearest(source))
        if not choices:
            unmoved.append(source)
            continue
        target = choices[0]
        used.add(id(target))
        hard_days.add(_iso(target["date"]))
        reschedule_actions.append(_action(source, target, "reschedule"))
        source_s, target_s = _duration_s(source), _duration_s(target)
        if source_s is not None and target_s is not None:
            moved_delta_s += source_s - target_s

    # ---- rebalance: move nothing, step up the dose of surviving easy days
    rebalance_actions: list[dict] = []
    boosted: set[int] = set()
    unrebalanced: list[dict] = []
    for source in keys:
        taken = 0
        for target in sorted(easy, key=nearest(source)):
            if taken >= REBALANCE_SPREAD:
                break
            if id(target) in boosted or not in_window(target):
                continue
            step = _dose_step(target, profile)
            if step is None:
                continue
            boosted.add(id(target))
            rebalance_actions.append(_dose_action(source, target, step))
            taken += 1
        if not taken:
            unrebalanced.append(source)

    skip = _option(
        "skip", [],
        "Leave the plan exactly as it is. The workouts inside your OOTO dates "
        "are simply skipped; nothing moves and nothing is re-dosed.",
        affected_keys=len(keys), unresolved=list(keys),
    )
    reschedule = _option(
        "reschedule", reschedule_actions,
        "Move each canceled key workout onto a future easy day. It keeps its "
        "own prescribed duration, so the easy session it lands on is lost and "
        "your planned volume drops by the difference.",
        affected_keys=len(keys), unresolved=unmoved,
        volume_delta_s=moved_delta_s,
    )
    rebalance = _option(
        "rebalance", rebalance_actions,
        "Move nothing. Your calendar keeps its shape and no session is lost - "
        "the surviving easy days step up one dose level at their existing "
        "durations, so weekly minutes are unchanged and the missed load is "
        "only partly recovered.",
        affected_keys=len(keys), unresolved=unrebalanced,
    )
    alternatives = [o for o in (reschedule, rebalance) if o["actions"]]
    recommended = "skip" if not alternatives else alternatives[0]["kind"]
    rationale = ("No future compatible slots are available; skipping is safest."
                 if not alternatives else "A deterministic compatible slot exists for each selected key workout.")
    return {
        "version": VERSION,
        "affected": affected_out,
        "options": [skip, reschedule, rebalance],
        "recommended_option": recommended,
        "rationale": rationale,
    }
