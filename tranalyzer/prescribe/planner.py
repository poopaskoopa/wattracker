"""Rule-based workout planner producing structured Sessions.

Powers are stored as fractions of FTP (0.90 == 90% FTP). Durations are whole
seconds. `plan_workout(state, duration_min)` is a pure function.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

MIN_DURATION_MIN = 30
MAX_DURATION_MIN = 480


@dataclass
class Segment:
    """One block of a workout.

    kind:
      - "warmup"      : ramp from power_low -> power_high
      - "cooldown"    : ramp from power_low -> power_high (usually descending)
      - "steadystate" : constant `power`
      - "intervals"   : `repeat` x (on_duration @ on_power / off_duration @ off_power)
      - "freeride"    : unstructured
    """

    kind: str
    duration: int  # total seconds for the segment
    power: Optional[float] = None
    power_low: Optional[float] = None
    power_high: Optional[float] = None
    repeat: Optional[int] = None
    on_duration: Optional[int] = None
    off_duration: Optional[int] = None
    on_power: Optional[float] = None
    off_power: Optional[float] = None
    text: Optional[str] = None

    def avg_fraction(self) -> float:
        """Average power as a fraction of FTP over the whole segment."""
        if self.kind == "intervals" and self.repeat:
            on = (self.on_duration or 0) * (self.on_power or 0.0)
            off = (self.off_duration or 0) * (self.off_power or 0.0)
            span = (self.on_duration or 0) + (self.off_duration or 0)
            return (on + off) / span if span else 0.0
        if self.kind in ("warmup", "cooldown"):
            lo = self.power_low or 0.0
            hi = self.power_high or 0.0
            return (lo + hi) / 2.0
        return self.power or 0.0

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "duration": self.duration,
            "power": self.power,
            "power_low": self.power_low,
            "power_high": self.power_high,
            "repeat": self.repeat,
            "on_duration": self.on_duration,
            "off_duration": self.off_duration,
            "on_power": self.on_power,
            "off_power": self.off_power,
            "text": self.text,
        }


@dataclass
class Session:
    """An ordered set of segments plus metadata."""

    name: str
    description: str
    workout_type: str
    segments: List[Segment] = field(default_factory=list)
    estimated_tss: float = 0.0

    def total_duration(self) -> int:
        return sum(s.duration for s in self.segments)

    def compute_tss(self) -> float:
        """Estimate TSS by treating each segment as steady at its avg fraction."""
        acc = 0.0
        for s in self.segments:
            frac = s.avg_fraction()
            acc += s.duration * frac * frac
        self.estimated_tss = round(acc / 3600.0 * 100.0, 1)
        return self.estimated_tss

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "workout_type": self.workout_type,
            "estimated_tss": self.estimated_tss,
            "total_duration": self.total_duration(),
            "segments": [s.to_dict() for s in self.segments],
        }


def _finish(session: Session, total_s: int, cooldown_low: float = 0.50,
            cooldown_high: float = 0.55) -> Session:
    """Append a cooldown that absorbs the remainder so segments sum to total_s."""
    used = sum(s.duration for s in session.segments)
    remainder = total_s - used
    if remainder < 0:
        raise ValueError("workout blocks exceed requested duration")
    if remainder > 0:
        session.segments.append(
            Segment(
                kind="cooldown",
                duration=remainder,
                power_low=cooldown_high,
                power_high=cooldown_low,
                text="Cool down easy.",
            )
        )
    session.compute_tss()
    return session


def _easy_endurance(total_s: int) -> Session:
    """Z1-2 easy endurance ride (recovery/overreach prescription)."""
    warmup = min(300, total_s // 6 or 60)
    s = Session(
        name="Easy Endurance",
        description="Zone 1-2 recovery endurance. Keep it comfortable.",
        workout_type="recovery",
    )
    s.segments.append(
        Segment(kind="warmup", duration=warmup, power_low=0.45, power_high=0.60,
                text="Ease in.")
    )
    body = total_s - warmup - min(300, total_s // 6 or 60)
    if body < 0:
        body = total_s - warmup
    s.segments.append(
        Segment(kind="steadystate", duration=body, power=0.65,
                text="Steady Zone 2 - conversational pace.")
    )
    return _finish(s, total_s, cooldown_low=0.45, cooldown_high=0.55)


def _vo2max(total_s: int) -> Session:
    """VO2max: 5-6 x 4min @110-115% FTP with equal recoveries."""
    warmup = 600
    on = 240  # 4 min
    off = 240
    reps = 6
    work = reps * (on + off)
    # Reduce reps until warmup + work + a small cooldown fits.
    while reps > 5 and warmup + work + 180 > total_s:
        reps -= 1
        work = reps * (on + off)
    while warmup + work + 120 > total_s and warmup > 300:
        warmup -= 60
    s = Session(
        name="VO2max Intervals",
        description=f"{reps} x 4min at 110-115% FTP to break through a plateau.",
        workout_type="vo2max",
    )
    s.segments.append(
        Segment(kind="warmup", duration=warmup, power_low=0.50, power_high=0.85,
                text="Progressive warmup with a couple of openers.")
    )
    s.segments.append(
        Segment(kind="intervals", duration=work, repeat=reps,
                on_duration=on, off_duration=off,
                on_power=1.12, off_power=0.50,
                text="4min hard, 4min easy. Hold 110-115% FTP.")
    )
    return _finish(s, total_s)


def _sweet_spot(total_s: int) -> Session:
    """Sweet spot intervals at 88-94% FTP."""
    warmup = 600
    on = 720  # 12 min
    off = 300
    reps = 3
    work = reps * (on + off)
    while warmup + work + 120 > total_s and reps > 2:
        reps -= 1
        work = reps * (on + off)
    while warmup + work + 120 > total_s and on > 300:
        on -= 60
        work = reps * (on + off)
    s = Session(
        name="Sweet Spot Intervals",
        description=f"{reps} x {on // 60}min at 90% FTP (sweet spot).",
        workout_type="sweet_spot",
    )
    s.segments.append(
        Segment(kind="warmup", duration=warmup, power_low=0.50, power_high=0.80,
                text="Warm up progressively.")
    )
    s.segments.append(
        Segment(kind="intervals", duration=work, repeat=reps,
                on_duration=on, off_duration=off,
                on_power=0.90, off_power=0.55,
                text="Sweet spot: steady and controlled at ~90% FTP.")
    )
    return _finish(s, total_s)


def _threshold(total_s: int) -> Session:
    """Threshold intervals at 91-95% FTP (3 x 12-15min)."""
    warmup = 600
    on = 780  # 13 min
    off = 300
    reps = 3
    work = reps * (on + off)
    while warmup + work + 120 > total_s and reps > 2:
        reps -= 1
        work = reps * (on + off)
    while warmup + work + 120 > total_s and on > 300:
        on -= 60
        work = reps * (on + off)
    if warmup + work + 120 > total_s and warmup > 300:
        warmup = 300
    s = Session(
        name="Threshold Intervals",
        description=f"{reps} x {on // 60}min at 91-95% FTP.",
        workout_type="threshold",
    )
    s.segments.append(
        Segment(kind="warmup", duration=warmup, power_low=0.50, power_high=0.85,
                text="Warm up to threshold effort.")
    )
    s.segments.append(
        Segment(kind="intervals", duration=work, repeat=reps,
                on_duration=on, off_duration=off,
                on_power=0.93, off_power=0.55,
                text="Threshold: sustained at 91-95% FTP.")
    )
    return _finish(s, total_s)


def _z2_endurance(total_s: int) -> Session:
    """Long Zone 2 aerobic endurance."""
    warmup = 600
    s = Session(
        name="Zone 2 Endurance",
        description="Long aerobic endurance ride in Zone 2.",
        workout_type="endurance",
    )
    s.segments.append(
        Segment(kind="warmup", duration=warmup, power_low=0.45, power_high=0.65,
                text="Ease into aerobic pace.")
    )
    body = total_s - warmup - 300
    s.segments.append(
        Segment(kind="steadystate", duration=body, power=0.70,
                text="Steady Zone 2 endurance - fuel and hydrate.")
    )
    return _finish(s, total_s, cooldown_low=0.45, cooldown_high=0.55)


def plan_workout(state, duration_min: int) -> Session:
    """Prescribe a workout for the given training state and duration (minutes).

    Raises ValueError if duration_min is outside [30, 480].
    """
    if duration_min < MIN_DURATION_MIN or duration_min > MAX_DURATION_MIN:
        raise ValueError(
            f"duration_min must be between {MIN_DURATION_MIN} and "
            f"{MAX_DURATION_MIN}; got {duration_min}"
        )

    total_s = int(duration_min) * 60

    overreach = getattr(state, "overreach", False)
    plateau = getattr(state, "plateau", False)
    tsb = getattr(state, "tsb", 0.0) or 0.0

    if overreach:
        return _easy_endurance(total_s)
    if plateau:
        return _vo2max(total_s)

    # Neutral / adapting: branch on TSB and duration.
    fresh = tsb > -5.0
    if duration_min > 105:
        return _z2_endurance(total_s)
    if 45 <= duration_min <= 105:
        if fresh:
            return _sweet_spot(total_s)
        return _threshold(total_s)
    # short (30-44): threshold if fresh, else easy endurance
    if fresh:
        return _threshold(total_s)
    return _easy_endurance(total_s)


# Public dispatch by workout kind, for the multi-week plan generator. Reuses the
# exact interval builders above so plan sessions match single-workout sessions.
WORKOUT_BUILDERS = {
    "vo2max": _vo2max,
    "threshold": _threshold,
    "sweet_spot": _sweet_spot,
    "endurance": _z2_endurance,
    "recovery": _easy_endurance,
}


def build_workout(kind: str, duration_min: float) -> Session:
    """Build a Session of the given kind at the given duration (minutes).

    Raises ValueError for an unknown kind.
    """
    if kind not in WORKOUT_BUILDERS:
        raise ValueError(f"unknown workout kind: {kind}")
    total_s = int(round(float(duration_min))) * 60
    session = WORKOUT_BUILDERS[kind](total_s)
    session.compute_tss()
    return session
