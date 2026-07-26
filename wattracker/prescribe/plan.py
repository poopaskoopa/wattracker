"""Multi-week polarized (~80/20) training-plan generator.

Distributes weekly riding hours across selected days, assigns high-intensity
sessions to HIT days and endurance/tempo to the rest, applies a gentle weekly
ramp with a recovery week every 4th week, and dates every workout. Reuses the
interval machinery in ``planner.build_workout`` - it does not reinvent the math.
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence, Set

from .planner import VARIANTS, Session, build_workout

# Session-duration limits (minutes).
MIN_SESSION_MIN = 20
HIT_MIN_MIN = 50   # VO2max needs ~47min to fit its intervals; keep a margin
HIT_MAX_MIN = 90
SWEET_SPOT_MIN_MIN = 35

# Polarized target: ~20% of weekly volume steered toward the HIT sessions.
HARD_VOLUME_FRACTION = 0.18


@dataclass(frozen=True)
class PlanModel:
    """A training-plan philosophy: how hard days are capped, which workout
    types fill hard slots, and how much weekly volume goes to intensity."""

    label: str
    description: str
    # days_per_week -> maximum allowed hard days that week.
    max_hard: Callable[[int], int]
    # Cycled through, in order, to type each hard slot across the whole plan.
    hard_types: List[str]
    # Fraction of weekly minutes steered toward the hard sessions.
    hard_volume_fraction: float


MODELS: Dict[str, PlanModel] = {
    "polarized": PlanModel(
        label="Polarized (80/20)",
        description="80/20 (Seiler): most rides easy Z1-Z2, small dose of "
                    "high-intensity VO2max work.",
        max_hard=lambda d: 1 if d <= 3 else 2,
        hard_types=["vo2max", "threshold"],
        hard_volume_fraction=0.18,
    ),
    "sweet_spot": PlanModel(
        label="Sweet spot base",
        description="Sweet spot base (time-crunched): frequent 88-94% FTP "
                    "sessions for efficient CTL growth.",
        max_hard=lambda d: max(1, min(4, d - 1)),
        hard_types=["sweet_spot", "sweet_spot", "threshold"],
        hard_volume_fraction=0.35,
    ),
    "pyramidal": PlanModel(
        label="Pyramidal",
        description="Pyramidal (traditional): large aerobic base, moderate "
                    "tempo/threshold, small top of VO2max.",
        max_hard=lambda d: min(3, max(1, d - 2)),
        hard_types=["threshold", "threshold", "vo2max"],
        hard_volume_fraction=0.25,
    ),
}

DEFAULT_MODEL = "polarized"

RECOVERY_WEEK_EVERY = 4
RECOVERY_MULTIPLIER = 0.65

# ------------------------------------------------------------------- races
# A rider's planned races are an INPUT to generation, never stored in the plan
# recipe: they live in their own table and are read fresh every time the plan
# is recomputed (see prescribe/reflow.py). The plan bends around them; they
# never bend around the plan.

# Taper shape (Bosquet et al. 2007, meta-analysis of tapering): the effect
# comes from cutting session DURATION while holding frequency and intensity.
# So no training day is dropped and no hard day is softened - only minutes go.
TAPER_FAR_DAYS = 14      # taper starts this many days before an A race
TAPER_NEAR_DAYS = 7      # the final week cuts harder
TAPER_FAR_MULT = 0.75    # days -14..-8
TAPER_NEAR_MULT = 0.45   # days -7..-1

# Two A-races closer than this cannot both be tapered for; the later one is
# planned as a B race instead (see resolve_race_conflicts).
A_RACE_SEPARATION_DAYS = 21

# Post-race recovery: ceil(race hours) easy sessions, bounded.
RECOVERY_SESSIONS_MIN = 2
RECOVERY_SESSIONS_MAX = 5

HARD_KINDS = ("vo2max", "threshold", "sweet_spot")

PRIORITY_A = "A"
PRIORITY_B = "B"


def _race_date(value) -> _dt.date:
    if isinstance(value, _dt.datetime):
        return value.date()
    if isinstance(value, _dt.date):
        return value
    return _dt.date.fromisoformat(str(value))


def normalize_races(races: Optional[Sequence[dict]]) -> List[dict]:
    """Parse race rows into {date, priority, name, duration_min}, date-sorted.

    Unparseable dates are dropped rather than raising: a single bad row must
    not make a rider's whole plan un-regenerable.
    """
    out: List[dict] = []
    for r in races or []:
        try:
            d = _race_date(r.get("date"))
        except (ValueError, TypeError):
            continue
        priority = str(r.get("priority") or PRIORITY_B).strip().upper()
        if priority not in (PRIORITY_A, PRIORITY_B):
            priority = PRIORITY_B
        duration = r.get("duration_min")
        out.append({
            "id": r.get("id"),
            "date": d,
            "priority": priority,
            "name": r.get("name"),
            "duration_min": int(duration) if duration else None,
        })
    # id breaks date ties so the ordering (and therefore the plan) is stable.
    out.sort(key=lambda r: (r["date"], r["id"] if r["id"] is not None else 0))
    return out


def resolve_race_conflicts(races: List[dict]) -> tuple:
    """Demote A races that sit inside another A race's taper. -> (races, conflicts)

    Two A races closer than ``A_RACE_SEPARATION_DAYS`` cannot both be tapered
    for - the second taper would start before the first race happened. Resolve
    it deterministically in favour of the EARLIER race (it is the one already
    being trained for) and plan the later one as a B race. This is a
    planning-time decision only: the stored row keeps its priority, so moving
    or deleting the first race restores the second one's taper.
    """
    resolved: List[dict] = []
    conflicts: List[dict] = []
    last_a: Optional[dict] = None
    for r in races:
        if r["priority"] == PRIORITY_A:
            if last_a is not None and (
                (r["date"] - last_a["date"]).days < A_RACE_SEPARATION_DAYS
            ):
                r = {**r, "priority": PRIORITY_B, "demoted": True}
                conflicts.append({
                    "date": r["date"].isoformat(),
                    "name": r["name"],
                    "conflicts_with": last_a["date"].isoformat(),
                    "planned_as": PRIORITY_B,
                })
            else:
                last_a = r
        resolved.append(r)
    return resolved, conflicts


def recovery_sessions_for(duration_min: Optional[int]) -> int:
    """How many easy sessions follow a race of this length."""
    hours = -(-int(duration_min or 0) // 60)  # ceil
    return max(RECOVERY_SESSIONS_MIN, min(RECOVERY_SESSIONS_MAX, hours))


@dataclass(frozen=True)
class RaceEffects:
    """Per-date instructions the day loop applies (all keys are ISO dates)."""

    skip: Set[str]            # race day itself - the race IS the session
    taper: Dict[str, float]   # duration multiplier (A races only)
    recovery: Set[str]        # post-race easy days (A races only)
    easy_adjacent: Set[str]   # B-race neighbours: hard day -> endurance

    def window(self) -> Set[str]:
        """Every date a race has an opinion about.

        Reflow uses this to decide when a race is allowed to overwrite an
        adapt.py adjustment (see prescribe/reflow.py).
        """
        return set(self.skip) | set(self.taper) | self.recovery | self.easy_adjacent


def race_effects(races: List[dict], scheduled: Sequence[_dt.date]) -> RaceEffects:
    """Turn resolved races into per-date instructions for the given ride days."""
    skip = {r["date"].isoformat() for r in races}
    taper: Dict[str, float] = {}
    recovery: Set[str] = set()
    easy_adjacent: Set[str] = set()
    ride_days = sorted(d for d in scheduled if d.isoformat() not in skip)

    for r in races:
        day = r["date"]
        if r["priority"] == PRIORITY_A:
            for offset in range(1, TAPER_FAR_DAYS + 1):
                iso = (day - _dt.timedelta(days=offset)).isoformat()
                mult = (TAPER_NEAR_MULT if offset <= TAPER_NEAR_DAYS
                        else TAPER_FAR_MULT)
                # Overlapping tapers: the deeper cut wins. Tapers only ever
                # reduce, so taking the minimum keeps the volume invariant.
                taper[iso] = min(taper.get(iso, 1.0), mult)
            after = [d for d in ride_days if d > day]
            for d in after[:recovery_sessions_for(r["duration_min"])]:
                recovery.add(d.isoformat())
        else:
            easy_adjacent.add((day - _dt.timedelta(days=1)).isoformat())
            easy_adjacent.add((day + _dt.timedelta(days=1)).isoformat())
    return RaceEffects(skip, taper, recovery, easy_adjacent)


def _feasible_min(kind: str, hard_slot: bool) -> float:
    """Shortest duration ``build_workout`` can actually construct this session in.

    The taper multiplier is applied and THEN clamped here. A 0.45x cut on a
    70-min VO2max session asks for 31 minutes, which the interval builder
    cannot fit and would raise on. Clamping (rather than swapping to a shorter
    interval variant) is deliberate: keeping the intensity intact is the whole
    point of a taper, so the volume reduction comes from the easy days instead.
    """
    if hard_slot:
        return HIT_MIN_MIN
    if kind == "sweet_spot":
        return SWEET_SPOT_MIN_MIN
    return MIN_SESSION_MIN


def week_multiplier(week: int) -> float:
    """Volume multiplier for a 1-indexed week. Volume is flat (1.0) on normal
    weeks and never increases week to week; every 4th week is a reduced
    recovery week (0.65)."""
    if week % RECOVERY_WEEK_EVERY == 0:
        return RECOVERY_MULTIPLIER
    return 1.0


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


def _hours_label(hours: float) -> str:
    """'6 h' / '6.5 h' - no trailing '.0' in a user-facing message."""
    h = float(hours)
    return f"{h:g} h"


def _cap_message(model: str, cap: int, selected: int) -> str:
    plural = "s" if cap != 1 else ""
    return (
        f"A {model} plan allows at most {cap} hard day{plural}/week "
        f"— you selected {selected}. Reduce hard days or choose a "
        f"different plan model."
    )


def validate_plan_inputs(
    weeks: int,
    days_of_week: Sequence[int],
    hours_per_week: float,
    hit_days_per_week: int,
    hard_days: Optional[Sequence[int]] = None,
    model: str = DEFAULT_MODEL,
) -> Optional[str]:
    """Return an error message if inputs are invalid, else None."""
    if model not in MODELS:
        return f"Unknown plan model '{model}'."
    if weeks is None or int(weeks) < 1:
        return "Weeks must be at least 1."
    if not days_of_week:
        return "Select at least one day of the week to ride."
    if hours_per_week is None or float(hours_per_week) <= 0:
        return "Hours per week must be greater than 0."
    if hit_days_per_week is None or int(hit_days_per_week) < 0:
        return "High-intensity days cannot be negative."
    n_days = len(set(int(d) for d in days_of_week))
    if int(hit_days_per_week) > n_days:
        return "High-intensity days cannot exceed the number of selected ride days."
    cap = MODELS[model].max_hard(n_days)
    if int(hit_days_per_week) > cap:
        return _cap_message(model, cap, int(hit_days_per_week))
    # Feasibility: every session has a floor its interval builder cannot go
    # below, so a week has a minimum length regardless of how the hours are
    # distributed. If the floors do not fit the budget, no allocation exists
    # and the plan must be refused - the only alternatives are dropping ride
    # days or dropping hard days, and both would override one explicit user
    # choice to honour another. Name the knobs instead.
    hit = min(int(hit_days_per_week), n_days)
    floor_min = hit * HIT_MIN_MIN + (n_days - hit) * MIN_SESSION_MIN
    if floor_min > float(hours_per_week) * 60.0:
        return (
            f"{_hours_label(hours_per_week)}/week across {n_days} day"
            f"{'s' if n_days != 1 else ''} with {hit} hard day"
            f"{'s' if hit != 1 else ''} needs at least {floor_min} min/week "
            f"(a hard session needs {HIT_MIN_MIN} min and an easy one "
            f"{MIN_SESSION_MIN}). Ride fewer days, do fewer hard days, or "
            f"raise your weekly hours."
        )
    if hard_days:
        marked = set(int(d) for d in hard_days)
        if not marked.issubset(set(int(d) for d in days_of_week)):
            return "Hard days must be among the selected ride days."
        if len(marked) > int(hit_days_per_week):
            return "Days marked hard cannot exceed high-intensity days per week."
        if len(marked) > cap:
            return _cap_message(model, cap, len(marked))
    return None


def _fit_week_to_budget(week_days: List[dict], budget_min: float) -> None:
    """Trim a week's laid-out sessions back under ``budget_min``, in place.

    Weekly hours are a hard user promise: a plan may come in under the number
    the rider gave us, never over. The allocation above distributes fractional
    minutes, but ``build_workout`` can only construct whole minutes, so every
    session rounds independently - a week of five 97.5-minute days becomes five
    98-minute days and overshoots the budget by 2 minutes. (The error scales
    with the number of ride days, up to n/2 minutes.)

    Minutes come off the LONGEST easy session first, one at a time, so the cut
    lands where it is least felt and stays evenly spread. Hard sessions are a
    last resort: intensity is the part of a week worth protecting, and by then
    we are trimming single minutes anyway. Every session keeps its own
    feasible floor.

    Raises ValueError if the excess cannot be absorbed at all - that means the
    floors do not fit the budget, which ``validate_plan_inputs`` rejects up
    front, so it should be unreachable.
    """
    excess = sum(d["minutes"] for d in week_days) - int(budget_min)
    if excess <= 0:
        return
    # Easy days first, then hard ones; within each group, longest first.
    for hard_pass in (False, True):
        while excess > 0:
            candidates = [d for d in week_days
                          if d["hard_slot"] is hard_pass
                          and d["minutes"] > d["floor"]]
            if not candidates:
                break
            victim = max(candidates, key=lambda d: (d["minutes"], d["date"]))
            victim["minutes"] -= 1
            excess -= 1
    if excess > 0:
        raise ValueError(
            f"{int(budget_min)} min/week cannot hold this week's sessions "
            f"even at their minimum durations."
        )


def generate_plan(
    name: str,
    start_date: _dt.date,
    weeks: int,
    days_of_week: Sequence[int],
    hours_per_week: float,
    hit_days_per_week: int,
    hard_days: Optional[Sequence[int]] = None,
    model: str = DEFAULT_MODEL,
    races: Optional[Sequence[dict]] = None,
) -> Dict:
    """Generate a dated, multi-week plan for the chosen training model.

    days_of_week are weekday indices (Mon=0 .. Sun=6). ``hard_days`` optionally
    pins specific weekdays as the HIT days (must be a subset of days_of_week and
    at most hit_days_per_week long); unpinned HIT slots keep the even-spread
    auto-assignment. Returns a dict with the plan metadata, a list of dated
    workouts (each carrying its Session), and a per-week summary. Raises
    ValueError on invalid input.

    ``races`` are the rider's planned races ({date, priority, name,
    duration_min}); the plan bends around them - no workout on race day, a
    two-week duration taper before an A race, easy days after it, and a hard
    day either side of a B race softened to endurance. They only ever REDUCE
    volume, so a week never exceeds ``hours_per_week``. The resolved races and
    any A-race conflicts come back under the ``races`` key.
    """
    err = validate_plan_inputs(
        weeks, days_of_week, hours_per_week, hit_days_per_week, hard_days, model
    )
    if err:
        raise ValueError(err)

    cfg = MODELS[model]
    weeks = int(weeks)
    hit_per_week = int(hit_days_per_week)
    days = sorted(set(int(d) for d in days_of_week))
    n = len(days)
    hard_set = set(int(d) for d in (hard_days or []))
    marked_pos = {i for i, d in enumerate(days) if d in hard_set}

    # Anchor to the Monday of the start week so weeks are calendar Mon-Sun.
    monday0 = start_date - _dt.timedelta(days=start_date.weekday())

    # Races are resolved against the plan's full set of ride days up front:
    # post-race recovery needs to know which days are actually ridden, and
    # that is only knowable once every date is laid out.
    scheduled = [
        monday0 + _dt.timedelta(days=7 * w + weekday)
        for w in range(weeks) for weekday in days
    ]
    resolved_races, race_conflicts = resolve_race_conflicts(normalize_races(races))
    effects = race_effects(resolved_races, scheduled)

    workouts: List[dict] = []
    weekly: List[dict] = []
    hit_counter = 0
    easy_counter = 0
    # Per-kind occurrence counter: the i-th workout of a kind gets
    # VARIANTS[kind][i % len], so consecutive same-kind days always differ
    # (deterministically) while each keeps its training purpose.
    kind_counter: dict = {}

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
                weekly_minutes * cfg.hard_volume_fraction / hit,
                HIT_MIN_MIN, HIT_MAX_MIN,
            )
        else:
            hit_dur = 0.0

        easy_total = max(0.0, weekly_minutes - hit_dur * hit)
        easy_dur = max(MIN_SESSION_MIN, easy_total / easy_days) if easy_days else 0.0

        monday = monday0 + _dt.timedelta(days=7 * w)
        # The week is laid out first and only then built, so the rounding
        # reconciliation below can see the whole week at once.
        week_days: List[dict] = []

        for i, weekday in enumerate(days):
            date = monday + _dt.timedelta(days=weekday)
            iso = date.isoformat()
            if i in hit_pos:
                kind = cfg.hard_types[hit_counter % len(cfg.hard_types)]
                hit_counter += 1
                dur = hit_dur
                hard_slot = True
            else:
                # Mostly endurance, an occasional sweet-spot tempo day.
                if easy_counter % 3 == 2 and easy_dur >= SWEET_SPOT_MIN_MIN:
                    kind = "sweet_spot"
                else:
                    kind = "endurance"
                easy_counter += 1
                dur = easy_dur
                hard_slot = False

            if iso in effects.skip:
                # Race day: the race IS the session, so nothing is generated.
                # The rotation counters still advance as if a workout had been
                # placed here - otherwise removing one day would reshuffle the
                # type and variant of every session after it, and adding a race
                # would look like a change to the whole rest of the plan.
                kind_counter[kind] = kind_counter.get(kind, 0) + 1
                continue

            if iso in effects.recovery:
                # Post-race: keep the day, drop it to a recovery spin.
                kind, hard_slot = "recovery", False
            elif iso in effects.easy_adjacent and kind in HARD_KINDS:
                # Either side of a B race: the race is the week's hard work, so
                # the neighbouring interval day becomes endurance at the same
                # duration. No taper, no extended recovery - it is a race, but
                # it is not the one the season is built around.
                kind, hard_slot = "endurance", False

            mult = effects.taper.get(iso)
            if mult is not None:
                dur = max(dur * mult, _feasible_min(kind, hard_slot))

            names = VARIANTS.get(kind, ["classic"])
            variant = names[kind_counter.get(kind, 0) % len(names)]
            kind_counter[kind] = kind_counter.get(kind, 0) + 1
            week_days.append({
                "date": date.isoformat(),
                "kind": kind,
                "variant": variant,
                # build_workout rounds to whole minutes, so track the day in
                # minutes and let the reconciliation below work in that unit.
                "minutes": int(round(dur)),
                "floor": int(_feasible_min(kind, hard_slot)),
                "hard_slot": hard_slot,
            })

        _fit_week_to_budget(week_days, float(hours_per_week) * 60.0)

        week_total_s = 0
        week_hard_s = 0
        week_tss = 0.0
        for day in week_days:
            session = build_workout(day["kind"], day["minutes"], day["variant"])
            hs = hard_seconds(session)
            week_total_s += session.total_duration()
            week_hard_s += hs
            week_tss += session.estimated_tss
            workouts.append(
                {
                    "date": day["date"],
                    "name": session.name,
                    "type": day["kind"],
                    "variant": day["variant"],
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
        "model": model,
        "start_date": monday0.isoformat(),
        "weeks": weeks,
        "days_of_week": days,
        "hours_per_week": float(hours_per_week),
        "hit_days_per_week": hit_per_week,
        "hard_days": sorted(hard_set),
        "workouts": workouts,
        "weekly": weekly,
        "races": {
            "planned": [
                {"date": r["date"].isoformat(), "priority": r["priority"],
                 "name": r["name"], "duration_min": r["duration_min"]}
                for r in resolved_races
            ],
            "conflicts": race_conflicts,
            # Dates a race has an opinion about; reflow needs them to know when
            # a race may overwrite an adaptation.
            "window": sorted(effects.window()),
        },
        "polarized_hard_fraction": round(total_hard_s / total_s, 3) if total_s else 0.0,
    }
