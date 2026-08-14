"""Pure proposals for adjusting generated workouts around an OOTO range."""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
from typing import Any, Iterable

VERSION = 1
HARD_TYPES = frozenset({"vo2max", "threshold", "sweet_spot", "sprint"})


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
    # A volume-preserving proposal must replace like-for-like stored volume.
    source_volume, target_volume = _volume(source), _volume(target)
    return source_volume is None or target_volume is None or source_volume == target_volume


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


def _option(kind: str, actions: list[dict], rationale: str) -> dict:
    return {"kind": kind, "actions": actions, "rationale": rationale}


def evaluate_ooto(
    plan, workouts, ooto_start, ooto_end, today, phase_by_date=None,
    race_dates=None, window_days=14,
) -> dict:
    """Return deterministic, stale-safe OOTO adjustment proposals.

    The function only reads its plain-dict inputs. Dates are serialized as ISO
    strings and every proposed mutation carries fingerprints of both rows.
    """
    start, end, now = _date(ooto_start), _date(ooto_end), _date(today)
    if end < start:
        raise ValueError("ooto_end must not precede ooto_start")
    races = _race_set(race_dates)
    ooto = {d.isoformat() for d in (start + _dt.timedelta(days=n)
             for n in range((end - start).days + 1))}
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

    def candidate(source: dict, target: dict) -> bool:
        target_day = _date(target["date"])
        if (target_day - end).days > int(window_days):
            return False
        if target_day.isoformat() in races or not _compatible(source, target):
            return False
        # A hard target must not be adjacent to an existing hard day.
        return not any(abs((target_day - _date(h["date"])).days) <= 1
                       for h in hard_elsewhere)

    reschedule_actions, rebalance_actions = [], []
    used = set()
    for source in keys:
        choices = [t for t in easy if id(t) not in used and candidate(source, t)]
        choices.sort(key=lambda t: (
            _phase(phase_by_date, _iso(t["date"])) != _phase(phase_by_date, _iso(source["date"])),
            (_date(t["date"]) - end).days, _iso(t["date"]), str(t.get("id", "")),
        ))
        if choices:
            target = choices[0]
            used.add(id(target))
            reschedule_actions.append(_action(source, target, "reschedule"))
            rebalance_actions.append(_action(source, target, "rebalance"))

    skip = _option("skip", [], "Leave the affected workouts unchanged.")
    reschedule = _option("reschedule", reschedule_actions,
                         "Move key workouts to compatible future easy slots while preserving stored slot volume.")
    rebalance = _option("rebalance", rebalance_actions,
                        "Carry key workouts in compatible future easy slots without adding sessions.")
    alternatives = [o for o in (reschedule, rebalance) if o["actions"]]
    recommended = "skip" if not alternatives else "reschedule"
    rationale = ("No future compatible slots are available; skipping is safest."
                 if not alternatives else "A deterministic compatible slot exists for each selected key workout.")
    return {
        "version": VERSION,
        "affected": affected_out,
        "options": [skip, reschedule, rebalance],
        "recommended_option": recommended,
        "rationale": rationale,
    }
