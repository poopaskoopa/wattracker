"""Rule-based workout planner producing structured Sessions.

Powers are stored as fractions of FTP (0.90 == 90% FTP). Durations are whole
seconds. `plan_workout(state, duration_min)` is a pure function.

Every builder takes an optional ``profile`` - the rider's MEASURED capacities
(``metrics.rider.RiderMetrics``) - so a prescription can be built on what this
rider can actually do rather than on a population constant. ``profile=None``
reproduces the population constants exactly, which is what keeps ad-hoc
previews, legacy plan rows and every existing caller identical.

Only the targets that a fixed %FTP genuinely cannot express are profile-derived:
neuromuscular sprint power and VO2max. Threshold, sweet spot, tempo, endurance
and recovery are already rider-specific, because FTP itself is the rider's own
measured 20-minute-derived number and those targets are expressed against it.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, List, Optional

if TYPE_CHECKING:  # import for typing only - this module stays dependency-free
    from ..metrics.rider import RiderMetrics

MIN_DURATION_MIN = 30
MAX_DURATION_MIN = 480

# --------------------------------------------------------------- rider targets
# Population fallbacks. Each is used verbatim when the rider has not measured
# the corresponding capacity, so an unmeasured rider gets exactly the
# prescription this planner has always produced.

# Neuromuscular (Coggan/Allen Level 7) power is listed as "N/A" as a %FTP - it
# is deliberately not prescribable that way. This figure is therefore NOT a
# target: sprints are prescribed as maximal free efforts (see ``_sprint``) and
# nothing is sent to the trainer. It exists only so a 12s all-out effort
# contributes a plausible amount of load to the TSS estimate, and as the
# nominal figure published to the "Just Ride" picker.
SPRINT_LOAD_RATIO_DEFAULT = 3.00

# VO2max work power as a multiple of FTP when 5-minute power is unmeasured.
VO2_RATIO_DEFAULT = 1.12

# A rider's 5-minute mean-maximal power is a single MAXIMAL effort by
# definition, so it cannot be the target for five repeats of it. Physiology
# gives the discount: repeated 4-minute intervals are sustainable at roughly
# 90-95% of 5-minute maximal power, so we take the midpoint of that band.
VO2_REPEATABLE_FRACTION = 0.92

# Bounds on the derived VO2max target. A single corrupt MMP point (a power
# spike, a mis-scaled file, an FTP that has not caught up with a step change in
# fitness) must not be able to prescribe an absurd session. The band is the
# Coggan Level 5 range widened slightly at the top for riders with a genuinely
# large aerobic reserve over their FTP.
VO2_RATIO_MIN = 1.06
VO2_RATIO_MAX = 1.30

# --- Quantization: why derived targets are deliberately coarse ---------------
# A measured ratio is not a stable number. It comes from ``mmp``, which
# ``analysis.pipeline`` recomputes over a ROLLING 90-day window, so it moves
# every single day as rides enter and 90-day-old rides leave - by fractions of
# a watt, with no change in the rider at all.
#
# On its own that is harmless. Combined with an unattended nightly reflow it is
# not: reflow diffs on tss and the stored .zwo, so any un-quantized derived
# value makes every plan and every exported .zwo file get rewritten every
# night, forever, driven purely by measurement noise. Measured before this was
# added, a vo2_ratio move of 0.001 - under a watt at a 250 W FTP - was enough
# to produce a different .zwo.
#
# So derived targets are snapped to a step coarse enough that ordinary noise
# cannot cross it, and fine enough to be a meaningful prescription: 0.01 of FTP
# for VO2max (~2.5 W at 250 W, well inside the precision of any interval a
# rider can actually hold) and 0.05 for the sprint load figure, which is not a
# target at all and only feeds a TSS estimate.
#
# Residual, accepted: a rider whose ratio sits exactly on a step edge can still
# flip between two adjacent values. That is bounded, rare and self-limiting -
# unlike continuous drift - so it is not worth the state that hysteresis would
# need.
VO2_QUANTUM = 0.01
SPRINT_LOAD_QUANTUM = 0.05

# Deliberately NOT consumed here: rider phenotype (sprinter / pursuiter /
# time-trialist / all-rounder) from ``analysis.power_profile.classify_phenotype``.
# Phenotype changes WHICH sessions a rider should be given, not what a given
# session's target is, so it belongs with the goal/plan-shape work rather than
# half-wired into individual builders.


def _measured(profile: Optional["RiderMetrics"], attr: str) -> Optional[float]:
    """A positive, finite measured value off the profile, else None.

    Duck-typed on purpose: callers pass a ``RiderMetrics``, but any object with
    the attribute works, and a profile whose every field is None behaves exactly
    like no profile at all.
    """
    if profile is None:
        return None
    value = getattr(profile, attr, None)
    if value is None or isinstance(value, bool):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f) or f <= 0:
        return None
    return f


def _quantize(value: float, step: float) -> float:
    """Snap ``value`` to the nearest multiple of ``step`` (see VO2_QUANTUM).

    The trailing round() removes the binary-float residue that would otherwise
    leave 4.36 -> 4.3500000000000005 and defeat the whole point.
    """
    return round(round(value / step) * step, 4)


def sprint_load_ratio(profile: Optional["RiderMetrics"] = None) -> float:
    """Multiple of FTP used to ACCOUNT for a 12s sprint's training load.

    This is a load-accounting figure, never a target: no sprint power is sent
    to the trainer or written into the .zwo (see ``_sprint``). The rider's own
    measured 5s peak is the honest number when we have it - a rider who sprints
    at 4.35x FTP does far more work in 12s than the 3.00x population stand-in
    credits them with, and their TSS should say so.

    Quantized to ``SPRINT_LOAD_QUANTUM`` so a rolling-window wobble in the
    measured peak cannot churn every plan nightly (see the constant).
    """
    measured = _measured(profile, "sprint_ratio")
    if measured is None:
        return SPRINT_LOAD_RATIO_DEFAULT
    return _quantize(measured, SPRINT_LOAD_QUANTUM)


def vo2_target(profile: Optional["RiderMetrics"] = None) -> Optional[float]:
    """Repeatable VO2max target as a multiple of FTP, or None if unmeasured.

    Returns None (rather than the default) so callers can keep their original
    wording when nothing was measured; ``vo2_target(None)`` therefore reproduces
    the previous prescription byte-for-byte.

    Quantized to ``VO2_QUANTUM``: the prescription tracks the rider's capacity,
    not the third decimal place of a rolling-window estimate.
    """
    ratio = _measured(profile, "vo2_ratio")
    if ratio is None:
        return None
    target = max(VO2_RATIO_MIN,
                 min(VO2_RATIO_MAX, ratio * VO2_REPEATABLE_FRACTION))
    return _quantize(target, VO2_QUANTUM)


def vo2_power(base: float, profile: Optional["RiderMetrics"] = None) -> float:
    """A VO2max variant's target, moved to this rider's aerobic ceiling.

    Each VO2max variant prescribes a different %FTP because its intervals are a
    different length (30/30s sit above 4-minute efforts, 5-minute efforts
    below), and those relationships are a property of the session, not of the
    rider. So a measured rider shifts the whole family by one factor - the ratio
    between their repeatable VO2 target and the population 112% - rather than
    every variant collapsing onto the same number. Each result is clamped to the
    same sane band, so a corrupt MMP point cannot escape through a variant.

    Returns ``base`` unchanged (same float, no rounding) when nothing is
    measured, which is what keeps an unmeasured rider's .zwo byte-identical.

    The scaled result is quantized on the same grid as ``vo2_target``, so every
    variant lands on a whole percent of FTP and a noisy measurement cannot move
    one variant while leaving another still (both bounds are multiples of the
    step, so quantizing after clamping cannot escape the band).
    """
    derived = vo2_target(profile)
    if derived is None:
        return base
    scaled = base * (derived / VO2_RATIO_DEFAULT)
    clamped = max(VO2_RATIO_MIN, min(VO2_RATIO_MAX, scaled))
    return _quantize(clamped, VO2_QUANTUM)


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
    # Load-accounting power for a segment that has no target (``freeride``).
    # It is NEVER rendered into the .zwo and never becomes a trainer target -
    # it exists so an unstructured maximal effort contributes its real share of
    # the TSS estimate instead of counting as zero watts.
    load_fraction: Optional[float] = None

    def avg_fraction(self) -> float:
        """Average power as a fraction of FTP over the whole segment.

        For a ``freeride`` segment this is the load-accounting estimate, not a
        prescription: there is no target to average.
        """
        if self.kind == "freeride":
            return self.load_fraction or self.power or 0.0
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
            "load_fraction": self.load_fraction,
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
    # Cap the trailing cooldown at 10min and reclaim the rest as a Zone 2 base
    # after the warmup. Every builder funnels its remainder into the cooldown
    # here, so applying the fix in _finish covers all kinds and variants at the
    # source (plan-generated and ad-hoc alike). No-op when already within cap.
    absorb_long_cooldown(session)
    return session


def _easy_endurance(total_s: int,
                    profile: Optional["RiderMetrics"] = None) -> Session:
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


def _vo2max(total_s: int,
            profile: Optional["RiderMetrics"] = None) -> Session:
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
    # Short rides (30-44min): the loops above stop at 5 reps / 300s warmup, which
    # does not fit. Trim further - reps down to 3, then warmup, then recoveries -
    # using the exact-overflow test so 45min+ output is untouched.
    while warmup + work > total_s and reps > 3:
        reps -= 1
        work = reps * (on + off)
    while warmup + work > total_s and warmup > 180:
        warmup -= 60
    while warmup + work > total_s and off > 120:
        off -= 30
        work = reps * (on + off)
    # The rider's own repeatable VO2 power when we have measured 5-minute
    # power, the population 112% when we do not. Wording follows the number:
    # with nothing measured the original "110-115% FTP" band is kept verbatim,
    # so an unmeasured rider's session is unchanged down to the .zwo text.
    on_power = vo2_power(VO2_RATIO_DEFAULT, profile)
    band = ("110-115% FTP" if on_power == VO2_RATIO_DEFAULT
            else f"{on_power * 100:.0f}% FTP")
    s = Session(
        name="VO2max Intervals",
        description=f"{reps} x 4min at {band} to break through a plateau.",
        workout_type="vo2max",
    )
    s.segments.append(
        Segment(kind="warmup", duration=warmup, power_low=0.50, power_high=0.85,
                text="Progressive warmup with a couple of openers.")
    )
    s.segments.append(
        Segment(kind="intervals", duration=work, repeat=reps,
                on_duration=on, off_duration=off,
                on_power=on_power, off_power=0.50,
                text=f"4min hard, 4min easy. Hold {band}.")
    )
    return _finish(s, total_s)


def _sweet_spot(total_s: int,
                profile: Optional["RiderMetrics"] = None) -> Session:
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


def _threshold(total_s: int,
               profile: Optional["RiderMetrics"] = None) -> Session:
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


def _z2_endurance(total_s: int,
                  profile: Optional["RiderMetrics"] = None) -> Session:
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


ENDURANCE_FILLER_POWER = 0.68
MAX_COOLDOWN_S = 600


def absorb_long_cooldown(session: Session, max_cooldown_s: int = MAX_COOLDOWN_S,
                         power: float = ENDURANCE_FILLER_POWER) -> int:
    """Trim an over-long trailing cooldown into a Zone 2 block before the work.

    Interval builders let `_finish` dump every unallocated second into the
    cooldown, so a long ride ends up mostly "cool down easy". This post-processing
    pass caps the trailing cooldown at `max_cooldown_s` and re-inserts the
    reclaimed time as a Zone 2 steadystate block positioned between the warmup
    and the work. Total duration is preserved exactly and TSS is recomputed.

    Mutates `session` in place and returns the number of seconds moved (0 when
    the cooldown is already within the cap, i.e. a no-op).
    """
    if not session.segments:
        return 0
    tail = session.segments[-1]
    if tail.kind != "cooldown" or tail.duration <= max_cooldown_s:
        return 0
    spare = tail.duration - max_cooldown_s
    tail.duration = max_cooldown_s
    at = 1 if session.segments[0].kind == "warmup" else 0
    session.segments.insert(
        at,
        Segment(kind="steadystate", duration=spare, power=power,
                text="Steady Zone 2 endurance - settle in and fuel."),
    )
    session.compute_tss()
    session.description += f" Ridden on a {spare // 60}min Zone 2 base."
    return spare


def _tempo(total_s: int,
           profile: Optional["RiderMetrics"] = None) -> Session:
    """Tempo intervals at 76-90% FTP (Coggan Level 3) on a Zone 2 base.

    Up to 5 x 15min @80% as the duration allows; any time beyond a 10min
    cooldown is ridden as Zone 2 endurance before the tempo blocks.
    """
    warmup = 600
    on = 900  # 15 min
    off = 300
    reps = max(2, min(5, (total_s - warmup - 120) // (on + off)))
    work = reps * (on + off)
    while warmup + work + 120 > total_s and reps > 2:
        reps -= 1
        work = reps * (on + off)
    while warmup + work + 120 > total_s and on > 480:
        on -= 60
        work = reps * (on + off)
    if warmup + work + 120 > total_s and warmup > 300:
        warmup = 300
    while warmup + work + 120 > total_s and off > 180:
        off -= 60
        work = reps * (on + off)
    while warmup + work + 120 > total_s and on > 300:
        on -= 60
        work = reps * (on + off)
    while warmup + work > total_s and reps > 1:
        reps -= 1
        work = reps * (on + off)
    s = Session(
        name="Tempo Intervals",
        description=f"{reps} x {on // 60}min at 80% FTP (tempo).",
        workout_type="tempo",
    )
    s.segments.append(
        Segment(kind="warmup", duration=warmup, power_low=0.50, power_high=0.75,
                text="Warm up into tempo pace.")
    )
    s.segments.append(
        Segment(kind="intervals", duration=work, repeat=reps,
                on_duration=on, off_duration=off,
                on_power=0.80, off_power=0.55,
                text="Tempo: steady at 76-90% FTP, breathing controlled.")
    )
    return _finish(s, total_s)


def _sprint(total_s: int,
            profile: Optional["RiderMetrics"] = None) -> Session:
    """Neuromuscular sprints (Coggan Level 7): 12s maximal, full recovery.

    Prescribed with NO power target - each effort is a free-ride block.
    """
    warmup = 600
    on = 12
    off = 168  # ~3 min easy between efforts
    reps = max(3, min(12, (total_s - warmup - 120) // (on + off)))
    work = reps * (on + off)
    while warmup + work + 120 > total_s and reps > 3:
        reps -= 1
        work = reps * (on + off)
    while warmup + work + 120 > total_s and warmup > 300:
        warmup -= 60
    while warmup + work > total_s and reps > 1:
        reps -= 1
        work = reps * (on + off)
    s = Session(
        name="Sprint / Neuromuscular",
        description=f"{reps} x 12s all-out sprints with full recovery, on an "
                    "aerobic base.",
        workout_type="sprint",
    )
    s.segments.append(
        Segment(kind="warmup", duration=warmup, power_low=0.50, power_high=0.80,
                text="Progressive warmup with two brief openers.")
    )
    # A sprint is prescribed as a MAXIMAL EFFORT WITH NO POWER TARGET. Coggan/
    # Allen list Level 7 as "N/A" as a %FTP for a reason, and ERG mode makes it
    # actively wrong to name a number: ERG clamps the rider to the target and
    # cannot track a 12s effort anyway, so a nominal figure turns "go as hard as
    # you can" into "do not exceed this". Each rep is therefore a free-ride
    # block (Zwift's FreeRide - the rider drives the trainer) followed by its
    # own recovery block, rather than one interval carrying an on_power.
    #
    # `load_fraction` is the separate, non-prescriptive concept: TSS still needs
    # a number for those 12 seconds, and the rider's own measured 5s ratio is
    # the honest one. It is never rendered into the .zwo.
    load = sprint_load_ratio(profile)
    for _ in range(reps):
        s.segments.append(
            Segment(kind="freeride", duration=on, load_fraction=load,
                    text="12s all out from a rolling start - no target, "
                         "just go as hard as you can.")
        )
        s.segments.append(
            Segment(kind="steadystate", duration=off, power=0.55,
                    text="Spin easy for 3min - full recovery before the next one.")
        )
    return _finish(s, total_s)


# ---------------------------------------------------------------------------
# Variant builders. Each preserves its type's training purpose (comparable
# IF/TSS/time-in-zone at equal duration) while producing a distinct session
# name and structure so day-to-day plan workouts feel different. The `classic`
# variant of every kind is the original builder above, reproduced byte-for-byte
# when variant is None/"classic" so legacy plan rows rebuild identically.
# ---------------------------------------------------------------------------


def _vo2max_short_short(total_s: int,
                        profile: Optional["RiderMetrics"] = None) -> Session:
    """VO2max 30/30s: sets of 10 x 30s @118% / 30s easy."""
    warmup = 600
    on, off, per_set = 30, 30, 10
    set_len = per_set * (on + off)  # 600s
    set_rest = 180
    sets = 4

    def used(n: int) -> int:
        return warmup + n * set_len + max(0, n - 1) * set_rest

    while sets > 2 and used(sets) + 120 > total_s:
        sets -= 1
    while used(sets) + 120 > total_s and warmup > 300:
        warmup -= 60
    on_power = vo2_power(1.18, profile)
    s = Session(
        name="VO2max 30/30s",
        description=(f"{sets} sets of 10 x 30s at {on_power * 100:.0f}% FTP / "
                     "30s easy."),
        workout_type="vo2max",
    )
    s.segments.append(
        Segment(kind="warmup", duration=warmup, power_low=0.50, power_high=0.85,
                text="Progressive warmup with a couple of openers.")
    )
    for k in range(sets):
        s.segments.append(
            Segment(kind="intervals", duration=set_len, repeat=per_set,
                    on_duration=on, off_duration=off,
                    on_power=on_power, off_power=0.55,
                    text="30s hard / 30s easy - punchy VO2 efforts.")
        )
        if k < sets - 1:
            s.segments.append(
                Segment(kind="steadystate", duration=set_rest, power=0.50,
                        text="Easy spin between sets.")
            )
    return _finish(s, total_s)


def _vo2max_long_intervals(total_s: int,
                           profile: Optional["RiderMetrics"] = None) -> Session:
    """VO2max long intervals: 4 x 5min @108% FTP."""
    warmup = 600
    on, off = 300, 240
    reps = 4
    work = reps * (on + off)
    while reps > 3 and warmup + work + 120 > total_s:
        reps -= 1
        work = reps * (on + off)
    while warmup + work + 120 > total_s and warmup > 300:
        warmup -= 60
    # Short rides: trim further (exact-overflow test keeps 45min+ untouched).
    while warmup + work > total_s and reps > 2:
        reps -= 1
        work = reps * (on + off)
    while warmup + work > total_s and warmup > 180:
        warmup -= 60
    on_power = vo2_power(1.08, profile)
    pct = f"{on_power * 100:.0f}%"
    s = Session(
        name="VO2max Long Intervals",
        description=f"{reps} x 5min at {pct} FTP - sustained VO2 efforts.",
        workout_type="vo2max",
    )
    s.segments.append(
        Segment(kind="warmup", duration=warmup, power_low=0.50, power_high=0.85,
                text="Progressive warmup with a couple of openers.")
    )
    s.segments.append(
        Segment(kind="intervals", duration=work, repeat=reps,
                on_duration=on, off_duration=off,
                on_power=on_power, off_power=0.52,
                text=f"5min hard, 4min easy. Hold ~{pct} FTP.")
    )
    return _finish(s, total_s)


def _vo2max_descending(total_s: int,
                       profile: Optional["RiderMetrics"] = None) -> Session:
    """VO2max descending ladder: 5-4-3-2min with equal recoveries."""
    warmup = 600
    rungs = [(dur, vo2_power(base, profile)) for dur, base in
             ((300, 1.10), (240, 1.12), (180, 1.13), (120, 1.14))]
    # The advertised band describes the ladder's shape, so it is taken from the
    # full ladder and does not narrow when a short ride drops the last rungs.
    powers = [p for _, p in rungs]

    def used(rs) -> int:
        # each work rung followed by equal-length recovery except the last
        return warmup + sum(d for d, _ in rs) + sum(d for d, _ in rs[:-1])

    while len(rungs) > 2 and used(rungs) + 120 > total_s:
        rungs = rungs[:-1]
    while used(rungs) + 120 > total_s and warmup > 300:
        warmup -= 60
    lo_pct = min(powers) * 100
    hi_pct = max(powers) * 100
    s = Session(
        name="VO2max Descending Ladder",
        description=(f"5-4-3-2min VO2 efforts at {lo_pct:.0f}-{hi_pct:.0f}% FTP, "
                     "equal recoveries."),
        workout_type="vo2max",
    )
    s.segments.append(
        Segment(kind="warmup", duration=warmup, power_low=0.50, power_high=0.85,
                text="Progressive warmup with a couple of openers.")
    )
    for i, (dur, pw) in enumerate(rungs):
        s.segments.append(
            Segment(kind="steadystate", duration=dur, power=pw,
                    text=f"{dur // 60}min hard at {int(pw * 100)}% FTP.")
        )
        if i < len(rungs) - 1:
            s.segments.append(
                Segment(kind="steadystate", duration=dur, power=0.52,
                        text="Equal recovery - spin easy.")
            )
    return _finish(s, total_s)


def _threshold_two_by_twenty(total_s: int,
                             profile: Optional["RiderMetrics"] = None) -> Session:
    """Threshold long intervals: 2 long sustained blocks at 91% FTP.

    Interval length scales with duration (keeping ~43% of time as work, like
    the classic builder) so IF/TSS stay comparable across durations.
    """
    warmup = 600
    reps, off = 2, 300
    # ~43% of ride time as work power, split across 2 reps, 60s-quantized.
    on = int(round(0.43 * total_s / reps / 60.0)) * 60
    on = max(600, min(1200, on))
    work = reps * (on + off)
    while warmup + work + 120 > total_s and on > 300:
        on -= 60
        work = reps * (on + off)
    while warmup + work + 60 > total_s and warmup > 180:
        warmup -= 60
    s = Session(
        name="Threshold Long Intervals",
        description=f"2 x {on // 60}min at 91% FTP - long sustained blocks.",
        workout_type="threshold",
    )
    s.segments.append(
        Segment(kind="warmup", duration=warmup, power_low=0.50, power_high=0.85,
                text="Warm up to threshold effort.")
    )
    s.segments.append(
        Segment(kind="intervals", duration=work, repeat=reps,
                on_duration=on, off_duration=off,
                on_power=0.91, off_power=0.55,
                text="Long threshold block - steady at 91% FTP.")
    )
    return _finish(s, total_s)


def _threshold_over_unders(total_s: int,
                           profile: Optional["RiderMetrics"] = None) -> Session:
    """Threshold over-unders: blocks alternating 2min@95% / 1min@105%."""
    warmup = 600
    under, over = 120, 60
    cycles = 3  # per block -> 9min block
    block = cycles * (under + over)
    rest = 240
    blocks = 3
    work = blocks * block

    def used(nb: int) -> int:
        return warmup + nb * block + max(0, nb - 1) * rest

    while blocks > 2 and used(blocks) + 120 > total_s:
        blocks -= 1
    while used(blocks) + 120 > total_s and warmup > 300:
        warmup -= 60
    s = Session(
        name="Threshold Over-Unders",
        description=f"{blocks} blocks alternating 2min@91% / 1min@101% FTP.",
        workout_type="threshold",
    )
    s.segments.append(
        Segment(kind="warmup", duration=warmup, power_low=0.50, power_high=0.85,
                text="Warm up to threshold effort.")
    )
    for k in range(blocks):
        s.segments.append(
            Segment(kind="intervals", duration=block, repeat=cycles,
                    on_duration=under, off_duration=over,
                    on_power=0.91, off_power=1.01,
                    text="Over-under: 2min just under, 1min just over threshold.")
        )
        if k < blocks - 1:
            s.segments.append(
                Segment(kind="steadystate", duration=rest, power=0.55,
                        text="Easy spin between blocks.")
            )
    return _finish(s, total_s)


def _sweet_spot_long_blocks(total_s: int,
                            profile: Optional["RiderMetrics"] = None) -> Session:
    """Sweet spot long blocks: 2 long blocks at 88% FTP (scales with duration)."""
    warmup = 600
    reps, off = 2, 300
    on = int(round(0.40 * total_s / reps / 60.0)) * 60
    on = max(600, min(1320, on))
    work = reps * (on + off)
    while warmup + work + 120 > total_s and on > 300:
        on -= 60
        work = reps * (on + off)
    while warmup + work + 60 > total_s and warmup > 180:
        warmup -= 60
    s = Session(
        name="Sweet Spot Long Blocks",
        description=f"2 x {on // 60}min at 88% FTP - long sweet-spot blocks.",
        workout_type="sweet_spot",
    )
    s.segments.append(
        Segment(kind="warmup", duration=warmup, power_low=0.50, power_high=0.80,
                text="Warm up progressively.")
    )
    s.segments.append(
        Segment(kind="intervals", duration=work, repeat=reps,
                on_duration=on, off_duration=off,
                on_power=0.88, off_power=0.55,
                text="Sweet spot: long, steady and controlled at ~88% FTP.")
    )
    return _finish(s, total_s)


def _sweet_spot_with_surges(total_s: int,
                            profile: Optional["RiderMetrics"] = None) -> Session:
    """Sweet spot with surges: 3 x 12min @89% with a 10s@110% surge every 3min."""
    warmup = 600
    surges = 4          # per block
    on_seg, surge = 170, 10
    block = surges * (on_seg + surge)  # 720s = 12min
    off = 300
    reps = 2
    work = reps * block + (reps - 1) * off

    def used(nb: int) -> int:
        return warmup + nb * block + max(0, nb - 1) * off

    while reps > 2 and used(reps) + 120 > total_s:
        reps -= 1
    while used(reps) + 120 > total_s and warmup > 300:
        warmup -= 60
    # Short rides: a single block still fits (exact-overflow test keeps 45min+
    # output untouched).
    while used(reps) > total_s and reps > 1:
        reps -= 1
    while used(reps) > total_s and warmup > 180:
        warmup -= 60
    s = Session(
        name="Sweet Spot with Surges",
        description=f"{reps} x 12min at 89% FTP with a 10s surge every 3min.",
        workout_type="sweet_spot",
    )
    s.segments.append(
        Segment(kind="warmup", duration=warmup, power_low=0.50, power_high=0.80,
                text="Warm up progressively.")
    )
    for k in range(reps):
        s.segments.append(
            Segment(kind="intervals", duration=block, repeat=surges,
                    on_duration=on_seg, off_duration=surge,
                    on_power=0.89, off_power=1.10,
                    text="Sweet spot at ~89% with a short 110% surge every 3min.")
        )
        if k < reps - 1:
            s.segments.append(
                Segment(kind="steadystate", duration=off, power=0.55,
                        text="Easy spin between blocks.")
            )
    return _finish(s, total_s)


def _endurance_negative_split(total_s: int,
                              profile: Optional["RiderMetrics"] = None) -> Session:
    """Endurance negative split: first half @64%, second half @72% FTP."""
    warmup = 600
    s = Session(
        name="Endurance Negative Split",
        description="Aerobic ride building from 64% to 72% FTP.",
        workout_type="endurance",
    )
    s.segments.append(
        Segment(kind="warmup", duration=warmup, power_low=0.45, power_high=0.62,
                text="Ease into aerobic pace.")
    )
    body = total_s - warmup - 300
    if body < 0:
        body = total_s - warmup
    first = body // 2
    second = body - first
    s.segments.append(
        Segment(kind="steadystate", duration=first, power=0.64,
                text="First half - relaxed Zone 2.")
    )
    s.segments.append(
        Segment(kind="steadystate", duration=second, power=0.72,
                text="Second half - lift to upper Zone 2.")
    )
    return _finish(s, total_s, cooldown_low=0.45, cooldown_high=0.55)


def _endurance_tempo_finish(total_s: int,
                            profile: Optional["RiderMetrics"] = None) -> Session:
    """Endurance with a tempo finish: Zone 2 then a final ~13% at 80% FTP."""
    warmup = 600
    s = Session(
        name="Endurance Tempo Finish",
        description="Zone 2 aerobic ride with a tempo push to finish.",
        workout_type="endurance",
    )
    s.segments.append(
        Segment(kind="warmup", duration=warmup, power_low=0.45, power_high=0.65,
                text="Ease into aerobic pace.")
    )
    body = total_s - warmup - 300
    if body < 0:
        body = total_s - warmup
    tempo = int(round(total_s * 0.13))
    tempo = max(0, min(tempo, body))
    base = body - tempo
    s.segments.append(
        Segment(kind="steadystate", duration=base, power=0.67,
                text="Steady Zone 2 - fuel and hydrate.")
    )
    if tempo:
        s.segments.append(
            Segment(kind="steadystate", duration=tempo, power=0.80,
                    text="Tempo finish - lift to ~80% FTP.")
        )
    return _finish(s, total_s, cooldown_low=0.45, cooldown_high=0.55)


def _endurance_cadence_play(total_s: int,
                            profile: Optional["RiderMetrics"] = None) -> Session:
    """Endurance with high-cadence blocks: Zone 2 base, 4-6 x 2min high-cadence."""
    warmup = 600
    on, off = 120, 180
    reps = 6
    work = reps * (on + off)
    while reps > 4 and warmup + work + 300 > total_s:
        reps -= 1
        work = reps * (on + off)
    while reps > 2 and warmup + work + 180 > total_s:
        reps -= 1
        work = reps * (on + off)
    while warmup + work + 120 > total_s and warmup > 180:
        warmup -= 60
    s = Session(
        name="Endurance Cadence Play",
        description=f"Zone 2 with {reps} x 2min high-cadence (100+ rpm) blocks.",
        workout_type="endurance",
    )
    s.segments.append(
        Segment(kind="warmup", duration=warmup, power_low=0.45, power_high=0.65,
                text="Ease into aerobic pace.")
    )
    pre = total_s - warmup - work - 300
    if pre < 0:
        pre = 0
    if pre:
        s.segments.append(
            Segment(kind="steadystate", duration=pre, power=0.68,
                    text="Steady Zone 2 endurance.")
        )
    s.segments.append(
        Segment(kind="intervals", duration=work, repeat=reps,
                on_duration=on, off_duration=off,
                on_power=0.72, off_power=0.66,
                text="2min high cadence (100+ rpm) at easy power, then settle.")
    )
    return _finish(s, total_s, cooldown_low=0.45, cooldown_high=0.55)


# variant name -> builder, per kind. "classic" is the original builder.
_VARIANT_BUILDERS = {
    "vo2max": {
        "classic": _vo2max,
        "short_short": _vo2max_short_short,
        "long_intervals": _vo2max_long_intervals,
        "descending": _vo2max_descending,
    },
    "threshold": {
        "classic": _threshold,
        "two_by_twenty": _threshold_two_by_twenty,
        "over_unders": _threshold_over_unders,
    },
    "sweet_spot": {
        "classic": _sweet_spot,
        "long_blocks": _sweet_spot_long_blocks,
        "with_surges": _sweet_spot_with_surges,
    },
    "endurance": {
        "classic": _z2_endurance,
        "negative_split": _endurance_negative_split,
        "tempo_finish": _endurance_tempo_finish,
        "cadence_play": _endurance_cadence_play,
    },
    "tempo": {
        "classic": _tempo,
    },
    "sprint": {
        "classic": _sprint,
    },
    "recovery": {
        "classic": _easy_endurance,
    },
}

# ---------------------------------------------------------------------------
# Published power-target metadata for the "Just Ride" picker.
#
# Zone boundaries follow the Coggan/Allen power-training levels (L1-L7) used by
# wattracker.analysis.zones.POWER_ZONES:
#   Allen & Coggan, "Training and Racing with a Power Meter" (training levels).
# `low`/`high` are fractions of FTP for the primary work effort of the session
# (not the whole-ride average). `high` is None for the open-ended top level.
# ---------------------------------------------------------------------------
WORKOUT_TYPE_INFO: List[dict] = [
    {
        "key": "endurance",
        "label": "Endurance",
        "zone": "Zone 2",
        "low": 0.56,
        "high": 0.75,
        "work": 0.70,
        "focus": "Builds aerobic base, fat oxidation and capillary density.",
        "structure": "Easy warmup, then a long steady Zone 2 block and a cooldown, "
                     "all ridden on that same Zone 2 base.",
    },
    {
        "key": "tempo",
        "label": "Tempo",
        "zone": "Zone 3",
        "low": 0.76,
        "high": 0.90,
        "work": 0.80,
        "focus": "Raises aerobic durability and muscular endurance below threshold.",
        "structure": "Warmup, then sustained tempo blocks at ~80% FTP with easy "
                     "recoveries, ridden on a Zone 2 base when the ride is long.",
    },
    {
        "key": "sweet_spot",
        "label": "Sweet Spot",
        "zone": "Zone 3-4 (sweet spot)",
        "low": 0.88,
        "high": 0.94,
        "work": 0.90,
        "focus": "Best fitness-per-fatigue trade-off for lifting FTP.",
        "structure": "Warmup, then sweet-spot blocks at ~90% FTP with easy "
                     "recoveries between them, ridden on a Zone 2 base when the "
                     "ride is long.",
    },
    {
        "key": "threshold",
        "label": "Threshold",
        "zone": "Zone 4",
        "low": 0.91,
        "high": 1.05,
        "work": 0.93,
        "focus": "Pushes lactate threshold and sustainable one-hour power.",
        "structure": "Warmup, then sustained threshold blocks at 91-95% FTP with "
                     "easy recoveries between them, ridden on a Zone 2 base when "
                     "the ride is long.",
    },
    {
        "key": "vo2max",
        "label": "VO2max",
        "zone": "Zone 5",
        "low": 1.06,
        "high": 1.20,
        # The published figure is the population default; a rider with measured
        # 5-minute power gets their own target (see ``vo2_target``).
        "work": VO2_RATIO_DEFAULT,
        "focus": "Develops maximal oxygen uptake and top-end aerobic power.",
        "structure": "Warmup, then hard VO2 intervals at 110-115% FTP with equal "
                     "easy recoveries, ridden on a Zone 2 base when the ride is "
                     "long.",
    },
    {
        "key": "sprint",
        "label": "Sprint / Neuromuscular",
        "zone": "Zone 7",
        "low": 1.50,
        "high": None,
        # Nominal only - the session itself prescribes no sprint target (see
        # ``_sprint``). Shared with the load-accounting constant so the picker
        # and the TSS estimate can never quote two different figures.
        "work": SPRINT_LOAD_RATIO_DEFAULT,
        "focus": "Trains neuromuscular power, recruitment and peak sprint watts.",
        "structure": "Warmup, then short all-out sprints with ~3min full recovery "
                     "between each, on an easy aerobic base.",
    },
    {
        "key": "recovery",
        "label": "Recovery",
        "zone": "Zone 1-2",
        "low": 0.45,
        "high": 0.65,
        "work": 0.65,
        "focus": "Easy aerobic recovery: promotes blood flow at minimal training load.",
        "structure": "Very easy warmup, a comfortable steady block at ~65% FTP "
                     "and an easy cooldown - nothing above low Zone 2, ridden on "
                     "a Zone 2 base when the ride is long.",
    },
]

WORKOUT_TYPE_KEYS = [info["key"] for info in WORKOUT_TYPE_INFO]

# Just Ride durations: 30 minutes to 4 hours in 15-minute increments.
JUST_RIDE_DURATIONS = list(range(30, 241, 15))


def workout_type_info(key: str) -> Optional[dict]:
    """Return the published metadata dict for a workout kind, or None."""
    for info in WORKOUT_TYPE_INFO:
        if info["key"] == key:
            return dict(info)
    return None

# Public: ordered variant names per kind (classic first). Used by the plan
# generator to rotate variants across same-kind days.
VARIANTS = {kind: list(builders.keys()) for kind, builders in _VARIANT_BUILDERS.items()}


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


def build_workout(kind: str, duration_min: float,
                  variant: Optional[str] = None,
                  profile: Optional["RiderMetrics"] = None) -> Session:
    """Build a Session of the given kind/duration (minutes), optional variant.

    ``variant`` None or "classic" reproduces the original output byte-for-byte;
    an unknown variant falls back to classic. Raises ValueError for unknown kind.

    ``profile`` is the rider's measured capacities. It only ever refines a
    target that a population constant cannot express (see the module docstring);
    ``profile=None``, or a profile whose relevant fields are unmeasured,
    produces exactly the same session as before profiles existed.
    """
    if kind not in _VARIANT_BUILDERS:
        raise ValueError(f"unknown workout kind: {kind}")
    builders = _VARIANT_BUILDERS[kind]
    builder = builders.get(variant or "classic", builders["classic"])
    total_s = int(round(float(duration_min))) * 60
    session = builder(total_s, profile)
    session.compute_tss()
    return session
