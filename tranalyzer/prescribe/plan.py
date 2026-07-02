"""Multi-week polarized (~80/20) training-plan generator.

Distributes weekly riding hours across selected days, assigns high-intensity
sessions to HIT days and endurance/tempo to the rest, applies a gentle weekly
ramp with a recovery week every 4th week, and dates every workout. Reuses the
interval machinery in ``planner.build_workout`` - it does not reinvent the math.
"""
from __future__ import annotations

import datetime as _dt
from typing import Dict, List, Optional, Sequence, Set

from .planner import Session, build_workout

# Session-duration limits (minutes).
MIN_SESSION_MIN = 20
HIT_MIN_MIN = 50   # VO2max needs ~47min to fit its intervals; keep a margin
HIT_MAX_MIN = 90
SWEET_SPOT_MIN_MIN = 35

# Polarized target: ~20% of weekly volume steered toward the HIT sessions.
HARD_VOLUME_FRACTION = 0.18

RECOVERY_WEEK_EVERY = 4
RECOVERY_MULTIPLIER = 0.65
RAMP_PER_WEEK = 0.06  # ~6% gentle progression on non-recovery weeks


def week_multiplier(week: int) -> float:
    """Volume multiplier for a 1-indexed week (recovery every 4th week)."""
    if week % RECOVERY_WEEK_EVERY == 0:
        return RECOVERY_MULTIPLIER
    return round(1.0 + RAMP_PER_WEEK * (week - 1), 4)


def _hit_positions(n: int, hit: int) -> Set[int]:
    """Choose `hit` evenly-spread day indices out of `n` selected days."""
    if hit <= 0 or n <= 0:
        return set()
    if hit >= n:
        return set(range(n))
    return {min(n - 1, int(round((i + 0.5) * n / hit))) for i in range(hit)}


def _resolve_hit_positions(n: int, hit: int, marked: Set[int]) -> Set[int]:
    """HIT day indices honoring explicit user marks.

    Marked positions are always HIT. If fewer are marked than `hit`, the
    remaining slots are filled from the unmarked days using the existing
    even-spread auto-assignment (then left-to-right as a last resort), so a
    week always gets exactly `hit` hard days.
    """
    positions = set(i for i in marked if 0 <= i < n)
    remaining = hit - len(positions)
    if remaining <= 0:
        return positions
    for cand in sorted(_hit_positions(n, hit)):
        if remaining <= 0:
            break
        if cand not in positions:
            positions.add(cand)
            remaining -= 1
    for i in range(n):
        if remaining <= 0:
            break
        if i not in positions:
            positions.add(i)
            remaining -= 1
    return positions


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def hard_seconds(session: Session) -> int:
    """Seconds of high-intensity interval 'on' time in a session."""
    total = 0
    for seg in session.segments:
        if seg.kind == "intervals" and seg.repeat:
            total += int(seg.repeat) * int(seg.on_duration or 0)
    return total


def validate_plan_inputs(
    weeks: int,
    days_of_week: Sequence[int],
    hours_per_week: float,
    hit_days_per_week: int,
    hard_days: Optional[Sequence[int]] = None,
) -> Optional[str]:
    """Return an error message if inputs are invalid, else None."""
    if weeks is None or int(weeks) < 1:
        return "Weeks must be at least 1."
    if not days_of_week:
        return "Select at least one day of the week to ride."
    if hours_per_week is None or float(hours_per_week) <= 0:
        return "Hours per week must be greater than 0."
    if hit_days_per_week is None or int(hit_days_per_week) < 0:
        return "High-intensity days cannot be negative."
    if int(hit_days_per_week) > len(days_of_week):
        return "High-intensity days cannot exceed the number of selected ride days."
    if hard_days:
        marked = set(int(d) for d in hard_days)
        if not marked.issubset(set(int(d) for d in days_of_week)):
            return "Hard days must be among the selected ride days."
        if len(marked) > int(hit_days_per_week):
            return "Days marked hard cannot exceed high-intensity days per week."
    return None


def generate_plan(
    name: str,
    start_date: _dt.date,
    weeks: int,
    days_of_week: Sequence[int],
    hours_per_week: float,
    hit_days_per_week: int,
    hard_days: Optional[Sequence[int]] = None,
) -> Dict:
    """Generate a dated, polarized multi-week plan.

    days_of_week are weekday indices (Mon=0 .. Sun=6). ``hard_days`` optionally
    pins specific weekdays as the HIT days (must be a subset of days_of_week and
    at most hit_days_per_week long); unpinned HIT slots keep the even-spread
    auto-assignment. Returns a dict with the plan metadata, a list of dated
    workouts (each carrying its Session), and a per-week summary. Raises
    ValueError on invalid input.
    """
    err = validate_plan_inputs(
        weeks, days_of_week, hours_per_week, hit_days_per_week, hard_days
    )
    if err:
        raise ValueError(err)

    weeks = int(weeks)
    hit_per_week = int(hit_days_per_week)
    days = sorted(set(int(d) for d in days_of_week))
    n = len(days)
    hard_set = set(int(d) for d in (hard_days or []))
    marked_pos = {i for i, d in enumerate(days) if d in hard_set}

    # Anchor to the Monday of the start week so weeks are calendar Mon-Sun.
    monday0 = start_date - _dt.timedelta(days=start_date.weekday())

    workouts: List[dict] = []
    weekly: List[dict] = []
    hit_counter = 0
    easy_counter = 0

    for w in range(weeks):
        weekly_minutes = float(hours_per_week) * 60.0 * week_multiplier(w + 1)
        hit = min(hit_per_week, n)
        hit_pos = (
            _resolve_hit_positions(n, hit, marked_pos)
            if marked_pos else _hit_positions(n, hit)
        )
        easy_days = n - hit

        # Steer ~20% of weekly volume to the HIT sessions, split across HIT
        # days, feasibility-clamped so the interval builders always fit.
        if hit > 0:
            hit_dur = _clamp(
                weekly_minutes * HARD_VOLUME_FRACTION / hit, HIT_MIN_MIN, HIT_MAX_MIN
            )
        else:
            hit_dur = 0.0

        easy_total = max(0.0, weekly_minutes - hit_dur * hit)
        easy_dur = max(MIN_SESSION_MIN, easy_total / easy_days) if easy_days else 0.0

        monday = monday0 + _dt.timedelta(days=7 * w)
        week_total_s = 0
        week_hard_s = 0
        week_tss = 0.0

        for i, weekday in enumerate(days):
            date = monday + _dt.timedelta(days=weekday)
            if i in hit_pos:
                kind = "vo2max" if hit_counter % 2 == 0 else "threshold"
                hit_counter += 1
                dur = hit_dur
            else:
                # Mostly endurance, an occasional sweet-spot tempo day.
                if easy_counter % 3 == 2 and easy_dur >= SWEET_SPOT_MIN_MIN:
                    kind = "sweet_spot"
                else:
                    kind = "endurance"
                easy_counter += 1
                dur = easy_dur

            session = build_workout(kind, dur)
            hs = hard_seconds(session)
            week_total_s += session.total_duration()
            week_hard_s += hs
            week_tss += session.estimated_tss
            workouts.append(
                {
                    "date": date.isoformat(),
                    "name": session.name,
                    "type": kind,
                    "duration_s": session.total_duration(),
                    "tss": session.estimated_tss,
                    "hard_s": hs,
                    "session": session,
                }
            )

        weekly.append(
            {
                "week": w + 1,
                "recovery": (w + 1) % RECOVERY_WEEK_EVERY == 0,
                "total_s": week_total_s,
                "hard_s": week_hard_s,
                "total_tss": round(week_tss, 1),
                "hard_fraction": round(week_hard_s / week_total_s, 3) if week_total_s else 0.0,
            }
        )

    total_s = sum(x["total_s"] for x in weekly)
    total_hard_s = sum(x["hard_s"] for x in weekly)
    return {
        "name": name,
        "start_date": monday0.isoformat(),
        "weeks": weeks,
        "days_of_week": days,
        "hours_per_week": float(hours_per_week),
        "hit_days_per_week": hit_per_week,
        "hard_days": sorted(hard_set),
        "workouts": workouts,
        "weekly": weekly,
        "polarized_hard_fraction": round(total_hard_s / total_s, 3) if total_s else 0.0,
    }
