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
from typing import TYPE_CHECKING, List, Optional, Tuple

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

# Bounds on the measured sprint ratio, for the same reason VO2max has them: a
# 5s peak is exactly the statistic a power-meter dropout or spike corrupts, and
# an unbounded ratio propagates straight into the TSS estimate as its SQUARE
# (a spiked 15x FTP turned a 60-min sprint session into 931 TSS). Trained 5s
# peaks run from about 2x FTP for a pure endurance rider to about 6x for a
# track sprinter (Coggan/Allen power-profile tables, converted from W/kg), so
# anything outside that is a measurement artefact rather than an athlete.
SPRINT_RATIO_MIN = 2.0
SPRINT_RATIO_MAX = 6.0

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

    Clamped to [SPRINT_RATIO_MIN, SPRINT_RATIO_MAX] and quantized to
    ``SPRINT_LOAD_QUANTUM``: bounded so a spiked 5s sample cannot inflate the
    session's load without limit (the figure is squared to make TSS), coarse so
    a rolling-window wobble in the measured peak cannot churn every plan
    nightly (see the constants). Zero, negative, NaN and infinite inputs are
    already rejected upstream by ``_measured`` and fall back to the default.
    """
    measured = _measured(profile, "sprint_ratio")
    if measured is None:
        return SPRINT_LOAD_RATIO_DEFAULT
    clamped = max(SPRINT_RATIO_MIN, min(SPRINT_RATIO_MAX, measured))
    return _quantize(clamped, SPRINT_LOAD_QUANTUM)


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


def _interval_block(reps: int, on_duration: int, off_duration: int,
                    on_power: float, off_power: float,
                    text: Optional[str] = None) -> List[Segment]:
    """``reps`` work efforts with recoveries BETWEEN them and none after the last.

    A plain ``intervals`` segment always emits a trailing recovery, so every
    interval session used to end "5min at 55% FTP" immediately followed by the
    cooldown ``_finish`` appends - two easy blocks back to back, the first of
    them recovering into the second. The final rep is therefore emitted as its
    own ``steadystate`` segment and the last recovery is simply not prescribed;
    the seconds it used to occupy are returned to the cooldown (and from there
    to a Zone 2 base by ``absorb_long_cooldown``) rather than lost.

    Total work time is ``reps * on + (reps - 1) * off``. With ``reps <= 1``
    there is nothing to repeat, so only the single effort is emitted.
    """
    segments: List[Segment] = []
    if reps > 1:
        segments.append(
            Segment(kind="intervals",
                    duration=(reps - 1) * (on_duration + off_duration),
                    repeat=reps - 1,
                    on_duration=on_duration, off_duration=off_duration,
                    on_power=on_power, off_power=off_power,
                    text=text)
        )
    segments.append(
        Segment(kind="steadystate", duration=on_duration, power=on_power,
                text=text)
    )
    return segments


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
    s.segments.extend(
        _interval_block(reps, on, off, on_power, 0.50,
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
    s.segments.extend(
        _interval_block(reps, on, off, 0.90, 0.55,
                        text="Sweet spot: steady and controlled at ~90% FTP.")
    )
    return _finish(s, total_s)


def _threshold(total_s: int,
               profile: Optional["RiderMetrics"] = None) -> Session:
    """Threshold intervals at 91-95% FTP (3 x 12-13min).

    A 60-minute threshold session is 35-45 minutes of time in zone (2x20, 3x15,
    3x12, 4x10 are the standard shapes). The fitting order below is what
    delivers that: ``on`` is shortened FIRST, down to a 10-minute floor, and
    reps are only dropped when even the shortest useful interval will not fit.
    Dropping reps first - the previous behaviour - collapsed an hour to 2x13min
    = 26 minutes in zone, a sweet-spot dose wearing a threshold label.
    """
    warmup = 600
    on_cap = 780    # 13 min
    on = on_cap
    off = 240       # 1 work : 3 rest; longer recoveries let VO2 decay
    reps = 3
    cooldown_min = 300

    def used(on_value: Optional[int] = None) -> int:
        # No recovery after the final rep (see ``_interval_block``).
        length = on if on_value is None else on_value
        return warmup + reps * length + (reps - 1) * off

    while used() + cooldown_min > total_s and on > 600:
        on -= 60
    if used() + cooldown_min > total_s:
        reps = 2
        while used() + cooldown_min > total_s and on > 300:
            on -= 60
    if used() + cooldown_min > total_s and warmup > 300:
        warmup = 300
    # The shrink loops only ever go down, so whatever short ``on`` forced the
    # drop to 2 reps was kept even though two reps leave room for far more: a
    # 45min session settled on 2x10min (20min in zone) when 2x13min fits inside
    # the same warmup and cooldown. Now that the rep count is final, grow ``on``
    # back toward its cap for as long as the session still fits.
    while on < on_cap and used(on + 60) + cooldown_min <= total_s:
        on += 60
    work_min = (reps * on) // 60
    s = Session(
        name="Threshold Intervals",
        description=(f"{reps} x {on // 60}min at 91-95% FTP - "
                     f"{work_min}min at threshold."),
        workout_type="threshold",
    )
    s.segments.append(
        Segment(kind="warmup", duration=warmup, power_low=0.50, power_high=0.85,
                text="Warm up to threshold effort.")
    )
    s.segments.extend(
        _interval_block(reps, on, off, 0.93, 0.55,
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


def _tempo_progression(total_s: int,
                       profile: Optional["RiderMetrics"] = None) -> Session:
    """Tempo progression: rising blocks that finish at the top of Zone 3.

    Both the block length and the rep count scale with the ride, so the tempo
    dose tracks classic ``_tempo`` (which grows to 5 x 15min = 75 minutes in
    zone). The fixed ``4 x 8min`` this used to prescribe gave EVERY session of
    60 minutes or more the same 32 minutes in zone: a two-hour "Tempo
    Progression" was 32min of Zone 3 on a 52min Zone 2 base, an endurance dose
    wearing a tempo label - the same defect already fixed in ``_threshold``.

    Blocks stay shorter than classic's 15min and the ramp from 78% to 86% FTP
    across them is kept: that rise is the variant's identity, not the dose.
    """
    warmup = min(600, max(300, total_s // 6))
    # The dose to hit is whatever classic tempo prescribes for this ride, so
    # the two shapes stay comparable at every duration instead of only at the
    # 60min the comparability test used to check. Classic emits its tempo work
    # as `intervals` segments and its Zone 2 base as steadystate, so the work
    # time is exactly the repeated on-durations.
    try:
        target = sum((seg.repeat or 0) * (seg.on_duration or 0)
                     for seg in _tempo(total_s, profile).segments
                     if seg.kind == "intervals")
    except ValueError:
        target = 0  # classic does not fit either; take the longest that does.

    def fit(warmup: int):
        """Shape closest to `target` seconds in zone that fits, or None.

        Blocks run 5-11min and 2-8 of them - always shorter than classic's
        15min, because the point of the variant is the rise across the blocks.
        Recoveries are 2-4min; a shorter one is only taken when it buys a
        closer match to the dose. Ties then go to the shape with more reps,
        which keeps the steps small and the top block earned.
        """
        budget = total_s - warmup - 120
        best = None
        for off in (240, 180, 120):
            for reps in range(2, 9):
                for on in range(300, 661, 60):
                    if reps * (on + off) <= budget:
                        cand = ((-abs(reps * on - target) if target
                                 else reps * on), off, reps, on)
                        if best is None or cand[:3] > best[:3]:
                            best = cand
        return None if best is None else best[1:]

    shape = fit(warmup)
    while shape is None and warmup > 180:
        warmup -= 60
        shape = fit(warmup)
    if shape is None:
        # Too short for even 2 x 5min: split what is left into two blocks.
        off, reps = 120, 2
        on = max(60, ((total_s - warmup - 120) // reps - off) // 60 * 60)
    else:
        off, reps, on = shape
    s = Session(
        name="Tempo Progression",
        description=(f"{reps} x {on // 60}min tempo progression from 78% to "
                     "86% FTP."),
        workout_type="tempo",
    )
    s.segments.append(Segment(kind="warmup", duration=warmup,
                              power_low=0.50, power_high=0.75,
                              text="Warm up into tempo pace."))
    for i in range(reps):
        power = 0.78 + 0.08 * i / max(1, reps - 1)
        text = f"Tempo block at {power * 100:.0f}% FTP."
        if i == reps - 1:
            # The top block is the one the whole ramp builds to, and it used to
            # be followed by a 2-4min recovery that ran straight into the
            # cooldown - the "two easy blocks back to back" `_interval_block`
            # exists to prevent. Emitting it through the helper drops that last
            # recovery; the seconds return to the Zone 2 base via `_finish`.
            s.segments.extend(_interval_block(1, on, off, power, 0.55, text=text))
        else:
            s.segments.append(Segment(kind="intervals", duration=on + off,
                                      repeat=1, on_duration=on, off_duration=off,
                                      on_power=power, off_power=0.55, text=text))
    return _finish(s, total_s)


def _recovery_progression(total_s: int,
                          profile: Optional["RiderMetrics"] = None) -> Session:
    """Recovery with a gentle power progression, never above low Zone 2."""
    warmup = min(300, max(180, total_s // 6))
    cooldown = min(300, max(180, total_s // 6))
    body = max(0, total_s - warmup - cooldown)
    first = body // 2
    s = Session(
        name="Recovery Progression",
        description="Easy recovery ride progressing gently from 61% to 65% FTP.",
        workout_type="recovery",
    )
    s.segments.append(Segment(kind="warmup", duration=warmup,
                              power_low=0.45, power_high=0.55,
                              text="Ease in gently."))
    if first:
        s.segments.append(Segment(kind="steadystate", duration=first,
                                  power=0.61, text="Very easy recovery pace."))
    if body - first:
        s.segments.append(Segment(kind="steadystate", duration=body - first,
                                  power=0.65, text="Comfortable low Zone 2 pace."))
    return _finish(s, total_s, cooldown_low=0.45, cooldown_high=0.55)


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


def _sprint_recovery_waves(total_s: int,
                           profile: Optional["RiderMetrics"] = None) -> Session:
    """Neuromuscular sprints with alternating two- and four-minute recoveries.

    The average recovery and total sprint time match the classic prescription;
    only the spacing changes. Both recovery lengths are long enough to keep
    each effort a quality maximal sprint rather than turning it into a
    fatigue-tolerance interval.
    """
    warmup = 600
    on = 12
    recoveries = (120, 216)
    reps = max(3, min(12, (total_s - warmup - 120) // 180))

    def work_seconds(n: int) -> int:
        return n * on + sum(recoveries[i % len(recoveries)] for i in range(n))

    while warmup + work_seconds(reps) + 120 > total_s and reps > 3:
        reps -= 1
    while warmup + work_seconds(reps) > total_s and reps > 1:
        reps -= 1
    s = Session(
        name="Sprint Recovery Waves",
        description=(f"{reps} x 12s all-out sprints with alternating 2min and "
                     "3min 36s recovery, on an aerobic base."),
        workout_type="sprint",
    )
    s.segments.append(
        Segment(kind="warmup", duration=warmup, power_low=0.50, power_high=0.80,
                text="Progressive warmup with two brief openers.")
    )
    load = sprint_load_ratio(profile)
    for i in range(reps):
        s.segments.append(
            Segment(kind="freeride", duration=on, load_fraction=load,
                    text="12s all out from a rolling start - no target, "
                         "just go as hard as you can.")
        )
        s.segments.append(
            Segment(kind="steadystate", duration=recoveries[i % len(recoveries)],
                    power=0.55,
                    text="Spin easy before the next quality sprint.")
        )
    return _finish(s, total_s)


# ---------------------------------------------------------------------------
# Variant builders. Each preserves its type's training purpose (comparable
# IF/TSS/time-in-zone at equal duration) while producing a distinct session
# name and structure so day-to-day plan workouts feel different. The `classic`
# variant of every kind is the original builder above, reproduced byte-for-byte
# when variant is None/"classic" so legacy plan rows rebuild identically.
#
# "Comparable time-in-zone" is enforced, not hoped for: every variant whose
# shape is fitted to the ride reads its target dose off the classic builder for
# the same `total_s` via `_classic_dose` and searches its own shape space for
# the closest match. Hard-coded rep counts are what broke six variants (a fixed
# 3 x 9min of over-unders bought no more threshold time on a four-hour ride than
# on an hour; a fixed 5-4-3-2 ladder delivered 58% of a VO2max dose), so a
# variant must not carry a literal shape that ignores the ride length.
# ---------------------------------------------------------------------------


def _dose_in_band(session: Session, kind: str) -> int:
    """Seconds `session` prescribes inside `kind`'s published power band.

    The band is read from ``WORKOUT_TYPE_INFO`` rather than a literal, so the
    dose a builder aims at is the same number the picker advertises to the
    rider. A `high` of None (the open-ended sprint level) means no ceiling.
    """
    info = workout_type_info(kind)
    if info is None:
        return 0
    low = info["low"]
    high = float("inf") if info["high"] is None else info["high"]
    tol = 1e-9

    def inside(power: Optional[float]) -> bool:
        return power is not None and low - tol <= power <= high + tol

    total = 0
    for s in session.segments:
        if s.kind == "intervals" and s.repeat:
            if inside(s.on_power):
                total += s.repeat * (s.on_duration or 0)
            if inside(s.off_power):
                total += s.repeat * (s.off_duration or 0)
        elif s.kind == "steadystate" and inside(s.power):
            total += s.duration
        elif s.kind == "freeride" and inside(s.load_fraction):
            total += s.duration
    return total


def _classic_dose(kind: str, total_s: int,
                  profile: Optional["RiderMetrics"] = None) -> int:
    """The in-band dose classic prescribes for this ride length.

    Returns 0 when classic itself does not fit the ride, which the callers read
    as "no target - take the largest shape that fits".
    """
    try:
        classic = _VARIANT_BUILDERS[kind]["classic"](total_s, profile)
    except (KeyError, ValueError):
        return 0
    return _dose_in_band(classic, kind)


def _vo2max_short_short(total_s: int,
                        profile: Optional["RiderMetrics"] = None) -> Session:
    """VO2max 30/30s: sets of 30s @118% / 30s easy, dosed like classic.

    Both the set count and the reps per set are fitted, because a fixed
    ``4 sets of 10`` could not fit an hour (it settled for 3 sets = 15min) and
    could not grow past 20min on any longer ride, against classic's 24. The
    30/30 alternation is the variant's identity and is untouched; only how many
    of them are prescribed follows the ride.
    """
    warmup_max = 600
    on, off = 30, 30
    set_rest = 180
    target = _classic_dose("vo2max", total_s, profile)

    def used(sets: int, per_set: int, wu: int) -> int:
        # No recovery after the final rep of a set (see ``_interval_block``).
        return (wu + sets * (per_set * (on + off) - off)
                + max(0, sets - 1) * set_rest)

    best = None
    for wu in range(warmup_max, 179, -60):
        for sets in range(2, 5):
            for per_set in range(6, 16):
                if used(sets, per_set, wu) + 120 > total_s:
                    continue
                work = sets * per_set * on
                # Closest dose first; then the longest warmup that still
                # delivers it; then the most sets, which keeps each set short
                # enough to stay a quality 30/30 block.
                key = (-abs(work - target) if target else work, wu, sets)
                if best is None or key > best[0]:
                    best = (key, wu, sets, per_set)
    if best is None:  # ride too short for even 2 x 6 reps
        warmup, sets, per_set = 180, 2, 6
    else:
        _, warmup, sets, per_set = best
    on_power = vo2_power(1.18, profile)
    s = Session(
        name="VO2max 30/30s",
        description=(f"{sets} sets of {per_set} x 30s at "
                     f"{on_power * 100:.0f}% FTP / 30s easy."),
        workout_type="vo2max",
    )
    s.segments.append(
        Segment(kind="warmup", duration=warmup, power_low=0.50, power_high=0.85,
                text="Progressive warmup with a couple of openers.")
    )
    for k in range(sets):
        s.segments.extend(
            _interval_block(per_set, on, off, on_power, 0.55,
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
    """VO2max long intervals: 5min efforts @108% FTP, dosed like classic.

    The rep count comes from classic's dose for the same ride rather than a
    fixed 4: classic grows from 5 x 4min to 6 x 4min at 75min and beyond, and
    a variant stuck at 4 x 5min delivered 83% of it from there on.
    """
    warmup = 600
    on, off = 300, 240
    target = _classic_dose("vo2max", total_s, profile)
    reps = max(2, min(8, int(round(target / on)))) if target else 4
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
    s.segments.extend(
        _interval_block(reps, on, off, on_power, 0.52,
                        text=f"5min hard, 4min easy. Hold ~{pct} FTP.")
    )
    return _finish(s, total_s)


def _vo2max_descending(total_s: int,
                       profile: Optional["RiderMetrics"] = None) -> Session:
    """VO2max descending ladder with equal recoveries, dosed like classic.

    The ladder is generated rather than hard-coded. The fixed 5-4-3-2min rungs
    this used to prescribe are 14 minutes of VO2 work at every ride length -
    58% of classic's 24min, the largest under-dose in the set, and a rider
    asking for VO2max work got barely half the stimulus.

    What makes the variant a ladder is preserved exactly: rungs step down 60s
    at a time and the power steps up as they shorten, with a recovery equal to
    the rung just ridden after every rung but the last. Only the top rung and
    the number of rungs are fitted, so a longer ride buys a longer ladder
    (6-5-4-3-2min for classic's 20min dose, 7-6-5-4-3min for its 24min).
    """
    warmup_max = 600
    step = 60
    rung_floor, rung_ceiling = 120, 420   # 2-7min: still a VO2max effort
    target = _classic_dose("vo2max", total_s, profile)

    def ladder(top: int, n: int) -> List[int]:
        return [top - i * step for i in range(n)]

    def used(durs: List[int], wu: int) -> int:
        # each work rung followed by an equal-length recovery except the last
        return wu + sum(durs) + sum(durs[:-1])

    best = None
    for wu in range(warmup_max, 179, -60):
        for n in range(3, 7):
            for top in range(rung_ceiling, rung_floor - 1, -step):
                durs = ladder(top, n)
                if durs[-1] < rung_floor or used(durs, wu) + 120 > total_s:
                    continue
                work = sum(durs)
                # Closest dose first, then the longest warmup that delivers it,
                # then the longest ladder (more rungs = more of the descent).
                key = (-abs(work - target) if target else work, wu, n)
                if best is None or key > best[0]:
                    best = (key, wu, top, n)
    if best is None:  # ride too short for even a 3-rung ladder
        warmup, top, n = 180, rung_floor + step, 2
    else:
        _, warmup, top, n = best
    durations = ladder(top, n)
    # Power rises as the rungs shorten - the point of a descending ladder.
    rungs = [(d, vo2_power(1.10 + 0.04 * i / max(1, n - 1), profile))
             for i, d in enumerate(durations)]
    powers = [p for _, p in rungs]
    lo_pct = min(powers) * 100
    hi_pct = max(powers) * 100
    shape = "-".join(str(d // 60) for d in durations)
    s = Session(
        name="VO2max Descending Ladder",
        description=(f"{shape}min VO2 efforts at {lo_pct:.0f}-{hi_pct:.0f}% FTP, "
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

    Interval length scales with duration so IF/TSS stay comparable across
    durations. The work fraction is 62% of ride time, not the 43% this used to
    take: at 43% an hour bought 2x13min = 26min in zone, well under the 35-45min
    a threshold session is supposed to deliver. At 62% an hour is 2x19min.
    """
    warmup = 600
    reps, off = 2, 240
    # ~62% of ride time as work power, split across 2 reps, 60s-quantized.
    on = int(round(0.62 * total_s / reps / 60.0)) * 60
    on = max(600, min(1200, on))
    work = reps * on + (reps - 1) * off
    while warmup + work + 120 > total_s and on > 300:
        on -= 60
        work = reps * on + (reps - 1) * off
    while warmup + work + 60 > total_s and warmup > 180:
        warmup -= 60
    s = Session(
        name="Threshold Long Intervals",
        description=(f"2 x {on // 60}min at 91% FTP - long sustained blocks, "
                     f"{(reps * on) // 60}min at threshold."),
        workout_type="threshold",
    )
    s.segments.append(
        Segment(kind="warmup", duration=warmup, power_low=0.50, power_high=0.85,
                text="Warm up to threshold effort.")
    )
    s.segments.extend(
        _interval_block(reps, on, off, 0.91, 0.55,
                        text="Long threshold block - steady at 91% FTP.")
    )
    return _finish(s, total_s)


def _threshold_over_unders(total_s: int,
                           profile: Optional["RiderMetrics"] = None) -> Session:
    """Threshold over-unders, dosed like classic.

    Both the block length and the block count are fitted. The fixed
    ``3 blocks of 3 cycles`` this used to prescribe is 27 minutes of threshold
    work on a one-hour ride and still 27 on a four-hour one - the saturation
    defect already fixed in ``_tempo_progression``, against a classic that
    delivers 36-39min.

    The alternation is the variant and is untouched: 2min just under threshold,
    1min just over, repeated. Both halves sit inside the published threshold
    band, so a cycle counts as three minutes in zone either way.
    """
    warmup_max = 600
    under, over = 120, 60
    cycle = under + over
    rest = 240
    target = _classic_dose("threshold", total_s, profile)

    def used(blocks: int, cycles: int, wu: int) -> int:
        return wu + blocks * cycles * cycle + max(0, blocks - 1) * rest

    best = None
    for wu in range(warmup_max, 179, -60):
        for blocks in range(2, 6):
            for cycles in range(3, 9):
                if used(blocks, cycles, wu) + 120 > total_s:
                    continue
                work = blocks * cycles * cycle
                # Closest dose first, then the longest warmup that delivers it,
                # then the block length nearest the canonical 12min over-under
                # block - a 21min block is a different session.
                key = (-abs(work - target) if target else work,
                       wu, -abs(cycles - 4))
                if best is None or key > best[0]:
                    best = (key, wu, blocks, cycles)
    if best is None:  # ride too short for 2 x 3 cycles
        warmup, blocks, cycles = 180, 2, 3
    else:
        _, warmup, blocks, cycles = best
    block = cycles * cycle
    s = Session(
        name="Threshold Over-Unders",
        description=(f"{blocks} x {block // 60}min blocks alternating "
                     "2min@91% / 1min@101% FTP."),
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
    """Sweet spot long blocks: 2 long blocks at 88% FTP, dosed like classic.

    Block length is set from classic's dose for the same ride, not from a flat
    40% of ride time. That fraction over-shot on long rides - a two-hour ride
    hit the 22min cap on both blocks for 44min of sweet spot against classic's
    36, the only variant in the set prescribing MORE than its classic. A rider
    picking a variant for variety must not silently get a harder session.

    Two long blocks is the variant; the ride only decides how long they are.
    """
    warmup = 600
    reps, off = 2, 300
    target = _classic_dose("sweet_spot", total_s, profile)
    on = int(round((target / reps if target else 0.40 * total_s / reps) / 60.0)) * 60
    on = max(300, min(1500, on))
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
    s.segments.extend(
        _interval_block(reps, on, off, 0.88, 0.55,
                        text="Sweet spot: long, steady and controlled at ~88% FTP.")
    )
    return _finish(s, total_s)


def _sweet_spot_with_surges(total_s: int,
                            profile: Optional["RiderMetrics"] = None) -> Session:
    """Sweet spot with controlled surges inside the declared band.

    The block count follows classic's dose for the same ride. It was pinned at
    2 x 12min = 24min for every ride length, which is classic's dose at 60min
    but only 67% of the 36min classic prescribes from 75min upwards.

    The surge is the variant and is fixed: a 10s lift every 3min, still inside
    the published sweet-spot band so it sharpens the block without turning it
    into a threshold session.
    """
    warmup = 600
    surges = 4          # per block
    on_seg, surge = 170, 10
    block = surges * (on_seg + surge)  # 720s = 12min
    off = 300
    target = _classic_dose("sweet_spot", total_s, profile)
    reps = max(1, min(5, int(round(target / block)))) if target else 2

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
                    on_power=0.89, off_power=0.94,
                    text="Sweet spot at ~89% with a short 94% surge every 3min.")
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
    """Endurance with an upper-Zone-2 finish."""
    warmup = 600
    s = Session(
        name="Endurance Upper-Zone-2 Finish",
        description="Zone 2 aerobic ride with a gentle upper-Zone-2 finish.",
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
            Segment(kind="steadystate", duration=tempo, power=0.74,
                    text="Upper Zone 2 finish - lift gently to ~74% FTP.")
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


# ---------------------------------------------------------------- ramp test
# The declared FTP test. Everything about its shape is a MEASUREMENT PROTOCOL
# rather than a training dose, which is why it is a named exception to several
# invariants every other kind holds (see ``MEASUREMENT_TYPES`` below).
#
# Slope. The owner asked for 10 W/min rather than Zwift's 20, so that aerobic
# limitation bites before the final minute instead of the rider's anaerobic
# reserve carrying one more step. A fixed watt slope only produces that
# intent at the FTP it was chosen against: both ends of a ramp scale with
# fitness, so at 10 W/min a 380 W rider ramps for ~41 minutes and the test
# measures endurance instead. The slope is therefore a FRACTION OF FTP per
# minute, which holds the ramp near ~20 minutes at any fitness. At the
# owner's 209 W FTP, 5%/min is 10.5 W/min - the number they asked for.
#
# Starting power is anchored to the rider's current FTP, which is the very
# number the test exists to correct. That self-reference is standard (Zwift
# does the same) and was explicitly accepted rather than designed around.
RAMP_TEST_KEY = "ramp_test"
RAMP_TEST_NAME = "Ramp Test"
RAMP_TEST_STEP_S = 60
RAMP_TEST_SLOPE_FRACTION = 0.05   # of FTP, added at every step
RAMP_TEST_START_FRACTION = 0.50   # first step
# 20 steps ends at 1.45 x FTP. A rider still turning the pedals at 145% of
# their recorded FTP does not have a stale FTP, they have a wrong one, and no
# number of further steps fixes the anchor the ramp started from.
RAMP_TEST_STEPS = 20
# The detector needs five consecutive steps to recognize a ramp at all, and
# below five steps there is no ramp to measure either.
RAMP_TEST_MIN_STEPS = 5
RAMP_TEST_WARMUP_S = 300
RAMP_TEST_COOLDOWN_S = 240
RAMP_TEST_WARMUP_LOW = 0.25
RAMP_TEST_WARMUP_HIGH = 0.35

# Kinds that measure the rider instead of training them. Their shape is fixed
# by the protocol, so three invariants every training session holds are simply
# not claims these make: that the session fills the duration the rider picked,
# that its load is comparable to other sessions of the same length, and that
# its efforts stay inside one published %FTP band. Tests exempt these kinds by
# name rather than by a literal, so adding a second protocol lands in one place.
MEASUREMENT_TYPES = frozenset({RAMP_TEST_KEY})


def ramp_test_steps(total_s: int) -> int:
    """How many one-minute steps fit in ``total_s``, within the protocol."""
    room = (int(total_s) - RAMP_TEST_WARMUP_S - RAMP_TEST_COOLDOWN_S)
    fits = room // RAMP_TEST_STEP_S
    return max(RAMP_TEST_MIN_STEPS, min(RAMP_TEST_STEPS, int(fits)))


def _ramp_test(total_s: int,
               profile: Optional["RiderMetrics"] = None) -> Session:
    """The FTP ramp test: 1-minute steps rising 5% of FTP until failure.

    ``total_s`` BOUNDS the session rather than setting it. A ramp test's
    length is decided by when the rider fails, not by a duration they picked
    off a menu, so the protocol is emitted at its own length and the requested
    duration only ever truncates it. The full protocol is 29 minutes, which
    fits inside the shortest duration the picker offers, so in practice it
    never truncates at all.

    The steps are discrete ``steadystate`` segments, deliberately NOT a
    ``ramp``/``warmup`` segment: those are linearly interpolated by
    ``ble.runner._flatten`` into a smooth rise, and a smooth rise has no
    one-minute step to take 75% of.
    """
    steps = ramp_test_steps(total_s)
    top = RAMP_TEST_START_FRACTION + (steps - 1) * RAMP_TEST_SLOPE_FRACTION
    s = Session(
        name=RAMP_TEST_NAME,
        description=(
            f"FTP test. Easy warm-up, then {steps} one-minute steps starting "
            f"at {round(RAMP_TEST_START_FRACTION * 100)}% of your current FTP "
            f"and rising {round(RAMP_TEST_SLOPE_FRACTION * 100)}% of FTP every "
            f"minute to {round(top * 100)}%. Hold each step for as long as you "
            "can; the test ends when you cannot hold the next one, and that is "
            "the result, not an abandoned ride. Your new FTP is 75% of your "
            "best minute."
        ),
        workout_type=RAMP_TEST_KEY,
    )
    s.segments.append(
        Segment(kind="warmup", duration=RAMP_TEST_WARMUP_S,
                power_low=RAMP_TEST_WARMUP_LOW,
                power_high=RAMP_TEST_WARMUP_HIGH,
                text="Easy spin. Stay well under the first step.")
    )
    for i in range(steps):
        power = RAMP_TEST_START_FRACTION + i * RAMP_TEST_SLOPE_FRACTION
        s.segments.append(
            Segment(kind="steadystate", duration=RAMP_TEST_STEP_S,
                    power=round(power, 4),
                    text=f"Step {i + 1} of {steps}.")
        )
    s.segments.append(
        Segment(kind="cooldown", duration=RAMP_TEST_COOLDOWN_S,
                power_low=RAMP_TEST_WARMUP_HIGH,
                power_high=RAMP_TEST_WARMUP_LOW,
                text="Spin it out.")
    )
    s.compute_tss()
    return s


def ramp_test_window(session: Session) -> Optional[Tuple[int, int]]:
    """(start, end) seconds of the stepped ramp inside a ramp-test session.

    This is what makes the result STRUCTURAL: the rider declared the test by
    selecting it, so the ramp's position is known and nothing has to infer it
    from the shape of the recorded stream. Returns None for every other kind.

    The bounds are workout seconds, which are also indices into the recorded
    sample stream: the controller advances its clock and appends exactly one
    sample per second of positive power, so the two run together.
    """
    if getattr(session, "workout_type", None) != RAMP_TEST_KEY:
        return None
    t = 0
    start = end = None
    for seg in session.segments:
        if seg.kind == "steadystate" and seg.duration == RAMP_TEST_STEP_S:
            if start is None:
                start = t
            end = t + seg.duration
        t += seg.duration
    if start is None or end is None or end <= start:
        return None
    return (start, end)


def ramp_test_window_for_samples(sample_count: int) -> Tuple[int, int]:
    """The ramp window for a recorded ramp test of ``sample_count`` seconds.

    Used where the Session that was ridden is no longer in hand (the accept
    route re-derives the result from the stored activity rather than trusting
    a number posted back to it). The warm-up is a protocol constant, and the
    end is clamped to the recording: a ramp test ends AT the failure, so the
    stream stops inside the window and the clamp is what the session's own
    window would have given anyway.
    """
    count = max(0, int(sample_count))
    end = min(count, RAMP_TEST_WARMUP_S + RAMP_TEST_STEPS * RAMP_TEST_STEP_S)
    return (RAMP_TEST_WARMUP_S, end)


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
        "progression": _tempo_progression,
    },
    "sprint": {
        "classic": _sprint,
        "recovery_waves": _sprint_recovery_waves,
    },
    "recovery": {
        "classic": _easy_endurance,
        "progression": _recovery_progression,
    },
    RAMP_TEST_KEY: {
        "classic": _ramp_test,
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
        # None, not a number: this session prescribes no power target at all
        # (see ``_sprint``), so the picker must advertise the effort rather
        # than a wattage. Publishing the load-accounting constant here put
        # ">150% FTP - 375 W" next to a workout whose own segment rows read
        # "Max effort - no target". `low` is kept as a floor the builder is
        # checked against, not as something shown for this type.
        "work": None,
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
    {
        "key": RAMP_TEST_KEY,
        "label": RAMP_TEST_NAME,
        "zone": "Test",
        # A ramp test has no band. Its whole purpose is to walk PAST FTP until
        # the rider fails, so there is no ceiling to publish - `high` is None
        # for exactly the reason the sprint level's is, an open-ended top. And
        # there is no single work target either, so `work` is None: filling it
        # with a number would advertise a wattage this session never asks the
        # rider to hold. `low` stays a real number because it is a real claim -
        # the first step - and it is the floor the builder is checked against.
        "low": RAMP_TEST_START_FRACTION,
        "high": None,
        "work": None,
        # `work is None` otherwise renders as "maximal effort - no target",
        # which is what a sprint is and not what this is: a ramp test has a
        # target every single minute, it just never stops raising it. This is
        # the escape hatch the band fields cannot express.
        "target_note": (
            f"{round(RAMP_TEST_START_FRACTION * 100)}% FTP rising "
            f"{round(RAMP_TEST_SLOPE_FRACTION * 100)}% FTP per minute "
            "until you fail"
        ),
        "focus": "Measures your FTP. The result can replace the number every "
                 "other workout is prescribed from.",
        "structure": "Easy warm-up, then one-minute steps that keep rising "
                     "until you cannot hold the next one. Stopping is the "
                     "result, not an abandoned ride: your FTP is 75% of your "
                     "best minute.",
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


def variant_names(kind: str) -> List[str]:
    """Return the Just Ride variant names for a known workout kind."""
    return list(_VARIANT_BUILDERS.get(kind, {}).keys())


def validate_variant(kind: str, variant: Optional[str]) -> str:
    """Validate an API variant, retaining classic for an omitted value."""
    selected = "classic" if variant is None or str(variant).strip() == "" else str(variant).strip()
    if selected not in _VARIANT_BUILDERS.get(kind, {}):
        raise ValueError(f"unknown variant: {variant or '(missing)'}")
    return selected

# Public: ordered variant names per kind (classic first). Used by the plan
# generator to rotate variants across same-kind days.
VARIANTS = {kind: list(builders.keys()) for kind, builders in _VARIANT_BUILDERS.items()}


def plan_workout(state, duration_min: int,
                 profile: Optional["RiderMetrics"] = None) -> Session:
    """Prescribe a workout for the given training state and duration (minutes).

    ``profile`` is the rider's measured capacities, passed to whichever builder
    the state selects - the one-off "generate a workout" path must prescribe
    against the same rider as their plan does. Raises ValueError if
    duration_min is outside [30, 480].
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
        return _easy_endurance(total_s, profile)
    if plateau:
        return _vo2max(total_s, profile)

    # Neutral / adapting: branch on TSB and duration.
    fresh = tsb > -5.0
    if duration_min > 105:
        return _z2_endurance(total_s, profile)
    if 45 <= duration_min <= 105:
        if fresh:
            return _sweet_spot(total_s, profile)
        return _threshold(total_s, profile)
    # short (30-44): threshold if fresh, else easy endurance
    if fresh:
        return _threshold(total_s, profile)
    return _easy_endurance(total_s, profile)


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
