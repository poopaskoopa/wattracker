"""Multi-week polarized (~80/20) training-plan generator.

Distributes weekly riding hours across selected days, assigns high-intensity
sessions to HIT days and endurance/tempo to the rest, applies a gentle weekly
ramp with a recovery week every 4th week, and dates every workout. Reuses the
interval machinery in ``planner.build_workout`` - it does not reinvent the math.

An optional periodization arc (``phases``, see prescribe/phases.py) varies which
intensity a week's hard days carry and how much of the week goes to them. It
never varies the weekly hours upward: weekly volume is a hard promise to the
rider, so a phase may only redistribute inside that budget or reduce it.
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Dict, List, Optional, Sequence, Set

from .phases import Phase, PhasePlan, resolve_phases
from .planner import VARIANTS, Session, build_workout

if TYPE_CHECKING:  # typing only - generation stays a pure function
    from ..metrics.rider import RiderMetrics

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

# Session kinds a B race displaces: the race is that week's hard work, so an
# interval day either side of it becomes endurance. ``sprint`` is here for the
# same reason the others are - a criterium goal's sharpening block schedules
# them, and a maximal-effort day the evening before a race is exactly the day
# the race should take over.
HARD_KINDS = ("vo2max", "threshold", "sweet_spot", "sprint")

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


def race_priorities(races: Optional[Sequence[dict]]) -> List[dict]:
    """Each stored race with the priority the PLAN would use, plus any demotion.

    This is the resolution rule's only public front door for views: the
    calendar badge and the plan summary both come through here rather than
    re-deriving when an A race counts as an A race. It needs no plan, because
    the demotion depends on the race list alone.

    Which race took the taper is read off ``resolve_race_conflicts``' own
    output, in order, rather than re-derived from the dates. Re-deriving it as
    "the latest A race strictly before this one" was wrong for two A races on
    the SAME date - the resolver demotes the second, but no A race is earlier,
    so the reason came out as "your A race on None".
    """
    resolved, conflicts = resolve_race_conflicts(normalize_races(races))
    # One conflict per demoted race, appended in resolution order.
    demotions = iter(conflicts)
    out: List[dict] = []
    for r in resolved:
        conflict = next(demotions) if r.get("demoted") else None
        out.append({
            "id": r.get("id"),
            "date": r["date"].isoformat(),
            "name": r.get("name"),
            "priority": r["priority"],       # EFFECTIVE, post-resolution
            "duration_min": r.get("duration_min"),
            "demoted": bool(r.get("demoted")),
            # The A race that took the taper; only set on a demotion.
            "conflicts_with": conflict["conflicts_with"] if conflict else None,
            "separation_days": A_RACE_SEPARATION_DAYS,
        })
    return out


def describe_races(
    races: Optional[Sequence[dict]],
    name: str,
    start_date: _dt.date,
    weeks: int,
    days_of_week: Sequence[int],
    hours_per_week: float,
    hit_days_per_week: int,
    hard_days: Optional[Sequence[int]] = None,
    model: str = DEFAULT_MODEL,
    profile: Optional["RiderMetrics"] = None,
    phases: Optional[Sequence[Phase]] = None,
    stored: Optional[Dict[str, dict]] = None,
    today: Optional[str] = None,
) -> List[dict]:
    """What this plan actually DID about each race, established date by date.

    The unit here is a DATE, not a race. A race is never "applied" or "not
    applied": parts of it land and parts deliberately cannot, because reflow
    refuses to rewrite past and completed workouts. An earlier version judged
    the whole race by whether every date it touched matched, and so suppressed
    four real, stored effects because two dates in the past had (correctly)
    never been tapered.

    Every claim therefore needs two independent things to be true of one date:

    * EVIDENCE - the row this plan actually STORES differs from the raceless
      baseline in the relevant way (shorter, kind changed, row absent).
    * ATTRIBUTION - the with-races generation predicts an effect of THIS race
      on that date.

    Only the intersection is described, and it is read off the stored rows: a
    date is in ``shorter`` iff its STORED session is shorter than the baseline,
    race day is displaced iff the row is really absent, a day is eased or
    recovered iff its STORED kind really changed that way. Requiring
    attribution as well as evidence is also what makes this safe against
    baseline drift - the baseline is generated now while the rows were written
    earlier, possibly against a different measured profile, and a difference the
    race does not predict is never claimed.

    Dates the race predicts but the stored plan does not show come back split
    in two: ``left_alone`` (past or already completed - reflow never rewrites
    those, which is a feature and gets said plainly) and ``pending`` (the plan
    genuinely has not been recomputed for this race, e.g. it is not the active
    plan).

    Nothing here describes the SHAPE of a taper. Sentences like "shorter still
    from D" or "frequency and intensity hold" quantify over a set of days, and
    the generator guarantees no such thing: a session already at its feasible
    floor does not shorten, and another race can remove a ride from the middle
    of the fortnight. Every one of those claims was false for some plan. The
    taper therefore comes back as ``shorter`` - one entry per date that really
    is shorter, carrying the stored minutes and the baseline it came down
    from - and the caller lists the facts instead of characterising them.

    A race is described when it falls inside the plan's span OR when it
    predicts anything inside it - a race the week after the plan ends really
    does shorten the plan's final sessions, and saying nothing about that would
    be the same silence this whole description exists to end.
    """
    base = race_priorities(races)
    if not base:
        return []

    args = dict(days_of_week=days_of_week, hours_per_week=hours_per_week,
                hit_days_per_week=hit_days_per_week, hard_days=hard_days,
                model=model, profile=profile, phases=phases)
    with_races = generate_plan(name, start_date, weeks, races=races, **args)
    raceless = generate_plan(name, start_date, weeks, races=None, **args)
    w_by = {x["date"]: x for x in with_races["workouts"]}
    o_by = {x["date"]: x for x in raceless["workouts"]}
    rows = stored or {}

    monday0 = start_date - _dt.timedelta(days=start_date.weekday())
    last_day = monday0 + _dt.timedelta(days=7 * int(weeks) - 1)

    # Attribution boundaries. A shortened day belongs to the next A race after
    # it and a recovery day to the previous one, so neighbouring races cannot
    # claim each other's work: without this, a demoted race would report the
    # taper of the very A race that demoted it as its own.
    a_days = sorted(_dt.date.fromisoformat(r["date"]) for r in base
                    if r["priority"] == PRIORITY_A)

    out: List[dict] = []
    for item in base:
        item = dict(item)
        day = _dt.date.fromisoformat(item["date"])
        iso = item["date"]
        prev_a = max([d for d in a_days if d < day], default=None)
        next_a = min([d for d in a_days if d > day], default=None)
        predicted: Set[str] = set()

        # Race day: the raceless plan rides here and the race takes the day.
        # Predicted by the workout's absence, evidenced by the row's absence.
        if iso in o_by and iso not in w_by:
            predicted.add(iso)
        item["displaces_workout"] = iso in predicted and iso not in rows

        # Taper: days in the fortnight before an A race, back only as far as
        # the previous A race. A hard day already at its feasible floor does
        # not shorten and is therefore neither predicted nor claimed. A day the
        # generation turns into a recovery spin is excluded outright: it is the
        # PREVIOUS A race's post-race easy day, which that race reports itself,
        # and its shorter duration is not evidence of this race's taper.
        tapered: List[str] = []
        if item["priority"] == PRIORITY_A:
            for off in range(TAPER_FAR_DAYS, 0, -1):
                d = day - _dt.timedelta(days=off)
                if prev_a is not None and d <= prev_a:
                    continue
                k = d.isoformat()
                if k not in o_by or k not in w_by:
                    continue
                if w_by[k]["duration_s"] >= o_by[k]["duration_s"]:
                    continue
                if w_by[k]["type"] == "recovery" != o_by[k]["type"]:
                    continue
                predicted.add(k)
                row = rows.get(k)
                if row is not None and int(row["duration_s"]) < o_by[k]["duration_s"]:
                    tapered.append(k)
        # The taper is reported as the DATES that are actually shorter and by
        # how much, straight off the rows. Every sentence that described its
        # SHAPE instead - "taper from D", "shorter still from D", "frequency
        # and intensity hold" - quantified over a set of days and was false
        # for some plan: a floor-clamped session inside the "shorter still"
        # period, a ride removed by another race inside the fortnight. These
        # numbers each come from one row and can be checked against it.
        item["shorter"] = [
            {"date": d,
             "minutes": int(round(int(rows[d]["duration_s"]) / 60)),
             "was": int(round(int(o_by[d]["duration_s"]) / 60))}
            for d in tapered
        ]

        # Post-race recovery: days after an A race whose kind became a recovery
        # spin, up to the next A race (which owns everything past it).
        recovery: List[str] = []
        if item["priority"] == PRIORITY_A:
            for k in sorted(w_by):
                if k <= iso or (next_a is not None and k >= next_a.isoformat()):
                    continue
                if k not in o_by or w_by[k]["type"] != "recovery":
                    continue
                if o_by[k]["type"] == "recovery":
                    continue
                predicted.add(k)
                row = rows.get(k)
                if row is not None and row["type"] == "recovery":
                    recovery.append(k)
        item["recovery_dates"] = recovery

        # A B race's neighbours: a day either side whose kind changed from
        # interval work to endurance. Only a B race does this, so an A race
        # next to one cannot claim it.
        easy: List[str] = []
        if item["priority"] == PRIORITY_B:
            for k in ((day - _dt.timedelta(days=1)).isoformat(),
                      (day + _dt.timedelta(days=1)).isoformat()):
                if k not in o_by or k not in w_by:
                    continue
                if (w_by[k]["type"] != "endurance"
                        or o_by[k]["type"] not in HARD_KINDS):
                    continue
                predicted.add(k)
                row = rows.get(k)
                if row is not None and row["type"] == "endurance":
                    easy.append(k)
        item["easy_dates"] = easy

        claimed = set(tapered) | set(recovery) | set(easy)
        if item["displaces_workout"]:
            claimed.add(iso)
        item["affects"] = sorted(claimed)
        item["predicted"] = sorted(predicted)
        # A date the race wanted but the plan does not show. Past and completed
        # rows are never rewritten by design, so they are explained rather than
        # reported as something missing; the rest genuinely await a recompute.
        missing = predicted - claimed
        # Only a date with a stored row can have "stayed as it was" - a date
        # with no row has nothing that stayed.
        item["left_alone"] = sorted(
            d for d in missing
            if d in rows and ((today is not None and d <= today)
                              or rows[d].get("completed_activity_id") is not None)
        )
        item["pending"] = sorted(missing - set(item["left_alone"]))
        item["outside_plan"] = not (monday0 <= day <= last_day)
        # A race outside the span that the plan does nothing about, applied or
        # not, is not this plan's business at all.
        if item["outside_plan"] and not predicted:
            continue
        out.append(item)
    return out


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
    """Seconds of high-intensity work in a session.

    Two shapes count. An ``intervals`` segment contributes its 'on' time. A
    ``freeride`` segment contributes its whole duration when its
    ``load_fraction`` puts it at or above FTP: a maximal effort has no power
    TARGET to inspect (see ``planner._sprint`` - naming a number would turn "go
    as hard as you can" into "do not exceed this"), so the load-accounting
    fraction is the only honest marker of how hard it is.

    Counting freeride is what lets a sprint session report the hard time it
    actually contains. Without it a criterium plan's sharpening block - the one
    place in the product that schedules sprints - would score zero hard seconds
    and report a hard fraction of 0 for its hardest weeks.
    """
    total = 0
    for seg in session.segments:
        if seg.kind == "intervals" and seg.repeat:
            total += int(seg.repeat) * int(seg.on_duration or 0)
        elif seg.kind == "freeride" and (seg.load_fraction or 0.0) >= 1.0:
            total += int(seg.duration or 0)
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
    profile: Optional["RiderMetrics"] = None,
    phases: Optional[Sequence[Phase]] = None,
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

    ``profile`` is the rider's measured capacities, passed straight through to
    ``build_workout`` so prescriptions are built on what this rider can actually
    do. Like races it is an INPUT to generation and is deliberately never stored
    in the plan recipe: it is re-read on every reflow, so a rider's targets
    follow their measured capacity as it changes instead of being frozen at the
    moment the plan was created. ``profile=None`` reproduces the population
    constants exactly.

    ``phases`` is an optional periodization arc (see prescribe/phases.py). Each
    week the arc claims takes its ``hard_types`` and ``hard_volume_fraction``
    from the phase instead of the model, and the phase's ``volume_multiplier``
    (never above 1.0) composes with recovery weeks and race tapers by taking the
    DEEPER reduction rather than multiplying - stacking two reductions would
    over-cut and could push sessions under their feasible floors. An arc that
    cannot be periodized coherently in the weeks available is abandoned by the
    resolver, so the plan comes back unphased with ``phases.unphased_reason``
    set. ``phases=None`` leaves every existing code path untouched.
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

    # None unless a caller opted in; every phase-aware branch below is written
    # so that this being None reproduces the pre-phases plan exactly.
    phase_plan: Optional[PhasePlan] = (
        resolve_phases(weeks, list(phases)) if phases else None
    )

    workouts: List[dict] = []
    weekly: List[dict] = []
    hit_counter = 0
    easy_counter = 0
    # Per-kind occurrence counter: the i-th workout of a kind gets
    # VARIANTS[kind][i % len], so consecutive same-kind days always differ
    # (deterministically) while each keeps its training purpose.
    kind_counter: dict = {}

    for w in range(weeks):
        phase = phase_plan.phase_for(w) if phase_plan is not None else None
        # A week with no phase - either because no arc was given or because the
        # arc had no room for one here - keeps the model's own knobs.
        hard_types = list(phase.hard_types) if phase else cfg.hard_types
        hard_fraction = (phase.hard_volume_fraction if phase
                         else cfg.hard_volume_fraction)
        recovery_mult = week_multiplier(w + 1)
        phase_mult = phase.volume_multiplier if phase else 1.0
        # Deeper-wins, not product: a phase reduction and a recovery week are
        # two opinions about the same week, and multiplying them would cut
        # twice for one reason.
        week_mult = min(recovery_mult, phase_mult)

        weekly_minutes = float(hours_per_week) * 60.0 * week_mult
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
                weekly_minutes * hard_fraction / hit,
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

            # ---- Step 1: what a RACELESS plan would put here. -------------
            # Every rotation counter is driven from this and only this, so a
            # race can never perturb the sequence: the day a race touches is
            # the only day that changes, and the plan after it is untouched.
            if i in hit_pos:
                kind = hard_types[hit_counter % len(hard_types)]
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

            # ---- Step 2: consume this day's slot in the variant rotation. --
            # Unconditional: skipped race days and substituted days consume
            # their slot exactly as an untouched day would.
            names = VARIANTS.get(kind, ["classic"])
            variant = names[kind_counter.get(kind, 0) % len(names)]
            kind_counter[kind] = kind_counter.get(kind, 0) + 1

            if iso in effects.skip:
                # Race day: the race IS the session, so nothing is generated.
                continue

            # ---- Step 3: apply the race's substitution, off-rotation. ------
            # A substituted session is a one-off imposed by the race, not a
            # member of the rotation, so it is pinned to "classic" and takes
            # no variant slot of its own. (Its counter was already advanced
            # above, for the kind the rotation actually asked for.)
            if iso in effects.recovery:
                # Post-race: keep the day, drop it to a recovery spin.
                kind, hard_slot, variant = "recovery", False, "classic"
            elif iso in effects.easy_adjacent and kind in HARD_KINDS:
                # Either side of a B race: the race is the week's hard work, so
                # the neighbouring interval day becomes endurance at the same
                # duration. No taper, no extended recovery - it is a race, but
                # it is not the one the season is built around.
                kind, hard_slot, variant = "endurance", False, "classic"

            if phase is None:
                # No phase: the race taper applies exactly as it always has.
                mult = effects.taper.get(iso)
                if mult is not None:
                    dur = max(dur * mult, _feasible_min(kind, hard_slot))
            else:
                # With a phase, the day's total reduction is the DEEPER of what
                # the day already had (recovery week x race taper, unchanged
                # from the raceless code above) and the phase's own multiplier -
                # never their product. Two reductions stacked would over-cut and
                # could drive sessions under the floors _feasible_min guards.
                # ``dur`` already carries ``week_mult``, so only the difference
                # between that and the target is applied here.
                race_mult = effects.taper.get(iso, 1.0)
                target = min(recovery_mult * race_mult, phase_mult)
                if target < week_mult:
                    dur = max(dur * (target / week_mult),
                              _feasible_min(kind, hard_slot))

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
            session = build_workout(day["kind"], day["minutes"], day["variant"],
                                    profile=profile)
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
    out: Dict = {
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
    # The key only exists on a periodized plan: an unphased plan's dict must
    # stay exactly what it was before phases existed.
    if phase_plan is not None:
        out["phases"] = {
            "blocks": [{"name": phase_name, "weeks": count}
                       for phase_name, count in phase_plan.blocks],
            # Phases the arc could not fit, so a caller can say "this is a
            # build block, not full preparation".
            "omitted": list(phase_plan.omitted),
            "weeks": [p.name if p else None for p in phase_plan.weeks],
            # Set when the plan was too short to periodize coherently and the
            # arc was abandoned: the plan below is exactly an unphased one, and
            # this is the sentence explaining that to the rider.
            "unphased_reason": phase_plan.unphased_reason,
        }
    return out
