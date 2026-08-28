"""Power-based metrics: Normalized Power, Intensity Factor, TSS, FTP estimate.

All functions operate on per-second power streams (one sample per second).
"""
from __future__ import annotations

import datetime
import math
from typing import Optional
from collections.abc import Mapping
from typing import Iterable, Sequence

import numpy as np


# --- Gap-aware detraining decay model for the FTP estimate --------------------
# The FTP estimate weights each past effort by how much fitness is expected to
# have decayed SINCE it was ridden - but detraining accrues only while the rider
# is OFF the bike, not merely because an effort is old. So the interval from an
# effort to the evaluation anchor is split, using the rider's activity calendar,
# into:
#   * idle-excess days: the portion of each inactivity gap beyond a short grace
#     window (short breaks cost nothing - research shows minimal loss under ~2
#     weeks). This includes the trailing gap from the last ride up to the anchor,
#     so the estimate keeps decaying while a rider stays away.
#   * active days: everything else (days the rider was training, or inside a
#     grace window). These are NOT charged detraining - a rider who never stops
#     riding has not detrained. They carry only a very slow EVIDENCE-STALENESS
#     discount (~5% per unbroken training year), because a 20-minute number from
#     three years ago is weaker evidence about today than last month's, and
#     without it one ancient hard effort would peg FTP forever for a rider who
#     only ever rides easy afterwards.
# factor = exp(-idle_excess / TAU_IDLE  -  active_days / TAU_ACTIVE)
# This replaces the old trailing hard window (which cliffed pre-break efforts to
# zero) and the effort-age decay (which wrongly charged detraining for days the
# rider was actually training).
FTP_DECAY_GRACE_DAYS = 14      # inactivity gaps shorter than this cost nothing
FTP_DECAY_TAU_IDLE = 240       # e-folding time of detraining while off the bike

# The active term is calibrated in the unit it is actually reasoned about: how
# much a year of UNBROKEN training discounts an effort ridden at its start.
#
# It was previously tau = 1440 days, which costs 1 - exp(-365/1440) = 22% a
# year. That is a detraining-sized number charged to a rider who never stopped
# training - exactly what the model above says must not happen, and on real data
# it was silently shaving several percent off riders who had done nothing wrong.
#
# It is not simply removed, because the case its docstring named is real: the
# recent-effort floor (below) only looks back FTP_FLOOR_WINDOW_DAYS, and it is a
# floor - it can raise the estimate but never lower it - so with no active term
# at all, a single hard effort from years ago would hold the estimate up
# forever, no matter how easy every ride since. 5%/yr keeps that from being
# permanent (a 3-year-old effort is discounted ~14%) while being far too slow to
# masquerade as detraining over the months that actually matter: a 6-month-old
# effort loses 2.5%, well inside the noise of the underlying 20-minute number.
FTP_DECAY_ACTIVE_ANNUAL_LOSS = 0.05
FTP_DECAY_TAU_ACTIVE = 365.0 / -math.log(1.0 - FTP_DECAY_ACTIVE_ANNUAL_LOSS)


def _idle_active_days(effort, anchor, activity_days) -> "tuple[float, float]":
    """Split the interval [effort, anchor] into (idle_excess_days, active_days).

    ``activity_days`` is the sorted list of the user's activity timestamps (any
    ride counts, even with no/zero power - time on the bike maintains fitness).
    Consecutive gaps between activity days in (effort, anchor], plus the trailing
    gap from the last such activity up to ``anchor``, each contribute
    ``max(0, gap_days - FTP_DECAY_GRACE_DAYS)`` to the idle excess. Active days
    are the remainder of the span.
    """
    span = (anchor - effort).total_seconds() / 86400.0
    if span <= 0:
        return 0.0, 0.0
    boundaries = [effort]
    for d in activity_days:
        if effort < d <= anchor:
            boundaries.append(d)
    boundaries.append(anchor)
    idle_excess = 0.0
    for a, b in zip(boundaries, boundaries[1:]):
        gap = (b - a).total_seconds() / 86400.0
        if gap > FTP_DECAY_GRACE_DAYS:
            idle_excess += gap - FTP_DECAY_GRACE_DAYS
    active = span - idle_excess
    if active < 0.0:
        active = 0.0
    return idle_excess, active


def detraining_factor(idle_excess_days: float, active_days: float) -> float:
    """Fraction of a past effort's power still assumed available now.

    Decay tracks INACTIVITY, not effort age: ``idle_excess_days`` (inactivity
    beyond the grace window) decays fast (tau = FTP_DECAY_TAU_IDLE), while
    ``active_days`` (days spent training, or inside a grace window) carry only
    the evidence-staleness discount of FTP_DECAY_ACTIVE_ANNUAL_LOSS per year
    (tau = FTP_DECAY_TAU_ACTIVE). See ``_idle_active_days`` for how the split is
    derived from the activity calendar. A continuously-training rider is
    essentially undecayed over a season; a long layoff decays substantially.
    """
    return math.exp(
        -idle_excess_days / FTP_DECAY_TAU_IDLE - active_days / FTP_DECAY_TAU_ACTIVE
    )


def _clean_power(power: Iterable[float]) -> np.ndarray:
    """Coerce a power stream to a float numpy array, treating None/NaN as 0."""
    arr = np.array([0.0 if p is None else float(p) for p in power], dtype=float)
    arr = np.nan_to_num(arr, nan=0.0)
    return arr


def rolling_mean(values: Sequence[float], window: int) -> np.ndarray:
    """Simple trailing rolling mean over `window` samples.

    Returns an array of the fully-populated windows (length N-window+1).
    """
    arr = _clean_power(values)
    if window <= 1:
        return arr
    if len(arr) < window:
        return np.array([], dtype=float)
    cumsum = np.cumsum(np.insert(arr, 0, 0.0))
    return (cumsum[window:] - cumsum[:-window]) / float(window)


def normalized_power(power: Sequence[float], window: int = 30) -> float:
    """Normalized Power.

    30-second rolling average of power, raise each to the 4th power, take the
    mean, then the 4th root. For streams shorter than the window, fall back to
    the simple mean.
    """
    arr = _clean_power(power)
    if arr.size == 0:
        return 0.0
    if arr.size < window:
        return float(arr.mean())
    roll = rolling_mean(arr, window)
    if roll.size == 0:
        return float(arr.mean())
    return float(np.power(np.mean(np.power(roll, 4)), 0.25))


# --- plausibility floor for any FTP used as a scoring basis -------------------
# TSS is quadratic in 1/FTP, so an FTP that is wrong by a factor of 300 makes a
# ride's TSS wrong by a factor of ~90,000. A failed estimate therefore does not
# degrade a ride's score, it destroys it (and every load metric derived from it
# - see issue #60, where a decayed estimate of 0.64 W produced a TSS of
# 16,136,334).
#
# The floor is set at the lowest wattage a human being could plausibly hold for
# an hour, not at the lowest FTP this app expects to see - it exists only to
# separate "an unusually weak rider" from "the estimator returned garbage", and
# a false reject is much more costly than a slightly permissive floor.
# Reference points: clinical cardiac-rehab ergometry protocols start at 20-25 W
# and progress in 25 W steps, so a rider mid-rehab can genuinely sit near 50 W;
# a deconditioned adult beginning indoor cycling is typically 100-150 W; a
# child on a trainer is 50-80 W. 50 W is at or below the bottom of every one of
# those populations, while the failure mode it guards against produced basis
# values of 0.64-49.9 W across 2,335 stored rows.
#
# The number is reasoned from that human range alone. It is NOT calibrated
# against any observed population of riders, and nothing here should be read as
# claiming it was: in the deployment where the bug was found, no FTP anywhere
# between 50 and 100 W exists at all (ftp_history min 141 W, manual overrides
# 200-250 W), so a 100 W floor would have rejected exactly as little real data
# as this one. The choice between them rests on which populations a rider could
# belong to, not on which ones happen to be in a database today - and on a
# false reject (a real rider's rides silently unscored) being far worse than a
# false accept (an absurd basis that is still absurd at 60 W, which is why
# contemporaneous scoring, not this floor, is the real fix - see below).
FTP_PLAUSIBLE_MIN_WATTS = 50.0

# --- bounds on an ASSERTED basis ---------------------------------------------
# The floor above filters our estimates; a rider's own statement is admitted
# below it. That is not a licence for any number at all. "Honour the rider" is
# an argument about the rider's KNOWLEDGE of their own body, and it stops
# exactly where the number stops describing a body: an FTP is a wattage a human
# holds for an hour on a bicycle, so a basis outside the human range is not an
# unusual assertion, it is a typo, a unit mix-up or a corrupt row - and because
# TSS is quadratic in 1/FTP, honouring it stores load figures in the millions.
#
# These bounds are on the SCORING BASIS and are enforced here regardless of what
# any input route accepts: this is the layer that has to hold, because a basis
# also arrives from migrations, imports and rescores that no form ever touched.
# Issue #64 has since given the input routes one shared policy
# (wattracker.ftp_input) which admits exactly this window and challenges the
# sub-plausible part of it - but that is a courtesy to the rider, not this
# rail's justification, and removing it must not weaken anything here.
#
# Lower: 20 W. Clinical cardiac-rehab and deconditioned-patient ergometry
# protocols begin at 20-25 W and step up by 25 W, so 20 W is the bottom of the
# lowest workload a supervised human is asked to sustain at all. A rider
# mid-rehab asserting 40 W - the case this whole provenance path exists to keep
# working - sits comfortably above it, as does any child or deconditioned
# beginner. Below 20 W the number no longer describes a person pedalling; the
# observed failure produced 0.64-3.7 W bases, an order of magnitude under it.
# Upper: 700 W. The best hour ever ridden by a human is ~440 W (UCI Hour Record
# aerodynamics aside, no verified hour power approaches 500 W), so 700 W is
# ~60% above the strongest cyclist alive and cannot reject a real rider, while
# still catching the fat-finger 2500 that would otherwise store a sixth of the
# true TSS forever.
#
# The window is intentionally wide: a false reject silently zeroes a real
# rider's training load, which is worse than a false accept of a merely absurd
# number. It exists to bound the damage, not to referee anyone's fitness.
FTP_ASSERTION_MIN_WATTS = 20.0
FTP_ASSERTION_MAX_WATTS = 700.0

# --- the no-data placeholder --------------------------------------------------
# What the app uses when it knows NOTHING about a rider: no rides to estimate
# from, no history, no assertion. It is a stated placeholder so the dashboard
# and the workout targets have some number to draw, and it is deliberately the
# ONLY such number in the app - the same literal used to sit inline in
# importer.current_ftp, in two of server.py's setup contexts and in the wizard
# template, so the displayed fallback, the stored history and the resolved FTP
# could drift apart (issue #55).
#
# It is NOT a measurement and must never be recorded as one. Writing it into
# ftp_history as source='estimated' makes an invented number indistinguishable
# from an analysed one for every later reader - the same class of defect as
# #44/#54/#60. Nothing writes it to ftp_history; it is resolved at read time by
# current_ftp when there is genuinely nothing to resolve, which means it also
# disappears by itself the moment a real ride or a rider assertion exists.
#
# 200 W is the conventional mid-range adult cyclist FTP and is only ever a
# placeholder; no claim is made that it describes any particular rider.
DEFAULT_FTP = 200.0

# What this floor does NOT fix: the estimator is anchored at wall-clock now, so
# the deeper defect is that a rider's *historical* rides are scored against a
# basis decayed to *today*. The floor bounds how wrong that can be, it does not
# make it right - a 300-day gap still yields a legitimate-looking 73 W estimate
# and stores IF 2.7 / TSS 750 for a normal hour, and 325 existing rows sit in
# the 50-100 W band for exactly that reason. Scoring each ride against the FTP
# effective on its own date is the actual repair and lives in #54/#59; it is
# deliberately not attempted here. evaluate_ftp likewise still persists a
# heavily decayed estimate: as an answer to "what could this rider hold today"
# it is correct and is what a returning rider's dashboard should show, so
# refusing to record it would break the estimator's legitimate use to paper
# over a scoring bug that has its own fix.


class AssertedFTP(float):
    """An FTP the rider stated, carrying that provenance with the number.

    The plausibility floor exists to reject a FAILED ESTIMATE, not to overrule
    the rider. But by the time a wattage reaches the scorer it is just a float,
    and the scorer cannot tell 0.64 W (the estimator decayed across a three-year
    gap) from 40 W (a rider mid-rehab who typed it into their settings). Getting
    that distinction wrong in either direction is a silent data defect: honour
    everything and TSS lands in the millions; filter everything and a rider who
    asserted 40 W accrues *zero* training load, with CTL/ATL/TSB reading as
    untrained.

    This class is a CONVENIENCE, not the answer. It marks the value the
    importer has just resolved so the rest of that one call chain does not have
    to re-derive provenance (and so a freshly typed assertion works before it
    has been written anywhere). The durable answer lives in the database and is
    resolved by :mod:`wattracker.ftp_provenance` when a caller supplies its
    database path to ``is_plausible_ftp``. Anything that reads a basis back out
    of SQLite gets a bare float and must pass that path explicitly.

    It is a float subclass, so it stores, serializes, rounds, compares and does
    arithmetic exactly like the number it wraps. Arithmetic on it yields a plain
    float, which is correct: a number *derived* from an assertion is not itself
    asserted.
    """

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return f"AssertedFTP({float(self)!r})"


def _finite(watts) -> "float | None":
    """``watts`` as a finite float, or None if it is not a usable number."""
    if watts is None or isinstance(watts, bool):
        return None
    try:
        value = float(watts)
    except (TypeError, ValueError, OverflowError):
        return None
    return value if math.isfinite(value) else None


def within_assertion_bounds(watts) -> bool:
    """Whether ``watts`` is inside the range a rider's FTP could physically be."""
    value = _finite(watts)
    if value is None:
        return False
    return FTP_ASSERTION_MIN_WATTS <= value <= FTP_ASSERTION_MAX_WATTS


def asserted_ftp(watts) -> "AssertedFTP | None":
    """Mark ``watts`` as a rider assertion, or None if it cannot be one.

    Rejects anything outside the human range (see ``FTP_ASSERTION_MIN_WATTS`` /
    ``FTP_ASSERTION_MAX_WATTS``): an assertion is honoured because it is the
    rider's knowledge of their own body, and 0.64 W is not a claim about a body.
    """
    value = _finite(watts)
    if value is None or not within_assertion_bounds(value):
        return None
    return AssertedFTP(value)


def is_asserted_ftp(value) -> bool:
    """Whether ``value`` carries rider-asserted provenance in this process."""
    return isinstance(value, AssertedFTP)


def is_plausible_ftp(watts, path: Optional[str] = None) -> bool:
    """Whether ``watts`` may be used as a scoring basis.

    The single admission test. False for None, non-numeric, non-finite, and for
    anything outside the human range entirely.

    Between ``FTP_ASSERTION_MIN_WATTS`` and ``FTP_PLAUSIBLE_MIN_WATTS`` a bare
    value is rejected unless ``path`` explicitly identifies the database whose
    provenance should be checked. The default path is deliberately pure: this
    predicate must never open the configured database as a side effect.
    """
    value = _finite(watts)
    if value is None or value > FTP_ASSERTION_MAX_WATTS:
        return False
    if is_asserted_ftp(watts):
        return value >= FTP_ASSERTION_MIN_WATTS
    if value >= FTP_PLAUSIBLE_MIN_WATTS:
        return True
    if value < FTP_ASSERTION_MIN_WATTS:
        return False
    if path is None:
        return False
    from ..ftp_provenance import is_asserted_watts

    return is_asserted_watts(value, path=path)


# --- the scoring chokepoint --------------------------------------------------
# Every IF and TSS that is WRITTEN to an activity row is computed by these two
# functions, so the admission test lives HERE rather than at each call site: a
# rail that every future caller has to remember to invoke is not a rail. In
# particular the offline rescore pass in #59 (`ftp_rescore.score_activity`)
# resolves its own FTP from ftp_history and never touches the importer, so this
# is the only place a guard can sit that it cannot bypass. An implausible basis
# yields 0.0 - the same "stored but never scored" state ``_build_record``
# leaves, identifiable as np > 0 with if_ == 0.
#
# It is NOT a single chokepoint on the READ side, and nothing here should be
# read as claiming so: `races.py` (:379, :638) and `analysis/zones.py`
# (:143, :178) divide by an FTP by hand when rendering, so a damaged row still
# displays IF ~317 on the races page. That is pre-existing and belongs with the
# historical repair (#62); this rail stops new damage being written.


def intensity_factor(
    np_value: float, ftp: float, path: Optional[str] = None
) -> float:
    """IF = NP / FTP, or 0.0 when ``ftp`` is not an admissible scoring basis."""
    if not is_plausible_ftp(ftp, path=path):
        return 0.0
    return float(np_value) / float(ftp)


def training_stress_score(
    duration_seconds: float, np_value: float, ftp: float,
    path: Optional[str] = None,
) -> float:
    """TSS = (duration_s * NP * IF) / (FTP * 3600) * 100.

    One hour at FTP yields exactly 100 TSS. Returns 0.0 when ``ftp`` is not an
    admissible scoring basis (see ``is_plausible_ftp``).
    """
    if duration_seconds <= 0 or not is_plausible_ftp(ftp, path=path):
        return 0.0
    intensity = intensity_factor(np_value, ftp, path=path)
    return (float(duration_seconds) * float(np_value) * intensity) / (
        float(ftp) * 3600.0
    ) * 100.0


def tss_from_stream(
    power: Sequence[float], ftp: float, path: Optional[str] = None
) -> float:
    """Convenience: compute TSS directly from a per-second power stream."""
    arr = _clean_power(power)
    if arr.size == 0 or not is_plausible_ftp(ftp, path=path):
        return 0.0
    npw = normalized_power(arr)
    return training_stress_score(arr.size, npw, ftp, path=path)


def best_20min_power(power: Sequence[float]) -> float:
    """Best 20-minute (1200s) rolling average power in a stream."""
    roll = rolling_mean(power, 1200)
    if roll.size == 0:
        return 0.0
    return float(roll.max())


# Zwift's rule, and the one this module has always used: a ramp test's FTP is
# 75% of the best one-minute step.
RAMP_TEST_FTP_FRACTION = 0.75

# The per-minute rise, in watts, that ``ramp_test_ftp_candidate`` will accept as
# a ramp. Named because a caller has to be able to ask whether a ramp it
# PRESCRIBED falls inside the band before reading anything into a 0.0: a slope
# expressed as a fraction of FTP leaves the band at the low end (a 120 W rider
# at 5%/min rises 6 W a minute), and that is the detector being out of range,
# not a disagreement about the result.
RAMP_DETECTOR_MIN_SLOPE_W = 8.0
RAMP_DETECTOR_MAX_SLOPE_W = 35.0


def best_rolling_power(power: Sequence[float], window_s: int,
                       start_s: int = 0, end_s: Optional[int] = None) -> float:
    """Best ``window_s`` rolling mean power within [start_s, end_s).

    Seconds are indices into a one-sample-per-second stream. A window shorter
    than ``window_s`` yields 0.0 rather than a mean over fewer seconds: a
    partial minute is not a minute.
    """
    arr = _clean_power(power)
    lo = max(0, int(start_s))
    hi = arr.size if end_s is None else min(arr.size, int(end_s))
    if hi - lo < int(window_s) or window_s <= 0:
        return 0.0
    roll = rolling_mean(arr[lo:hi], int(window_s))
    if roll.size == 0:
        return 0.0
    return float(roll.max())


def ramp_test_ftp_in_window(power: Sequence[float], start_s: int,
                            end_s: int) -> float:
    """FTP from a ramp whose position in the stream is already KNOWN.

    75% of the best minute of ACTUAL recorded power inside the window. Actual
    rather than prescribed, because a rider who fails 20 seconds into a step
    rode that step's target for 20 seconds and their own power for the other
    40; the target would credit them with a minute they did not complete.

    This is the primary measurement for a test the rider DECLARED by selecting
    it. ``ramp_test_ftp_candidate`` answers a different question - "was this a
    ramp, and where?" - for a file that arrived with no context, and on the
    declared path there is no such question to ask.
    """
    best = best_rolling_power(power, 60, start_s, end_s)
    return best * RAMP_TEST_FTP_FRACTION


def ramp_test_ftp_candidate(power: Sequence[float]) -> float:
    """Return an FTP candidate from a conservatively recognized ramp test.

    Zwift's ramp result is 75% of the highest one-minute step.  To avoid
    treating an ordinary sprint, interval, or long-ride hill as a ramp, this
    recognizes only streams no longer than 45 minutes (the conservative upper
    bound for a 10-20 minute ramp workout plus cooldown), containing five or
    more consecutive, non-overlapping 60-second blocks whose power is fairly
    steady, rises by 8-35 W each minute, and has a preceding minute at least
    20 W below the first block.  The 8-35 W slope range covers normal
    10-30 W/min ramps while the preceding low minute requires the short
    warm-up/ramp transition.

    "Fairly steady" means a standard deviation no greater than 12 W or 7% of
    the block's own power, whichever is larger.  A rider's steps get noisier as
    a ramp gets hard - cadence surges, out-of-saddle efforts, a struggling last
    minute - so a flat watt bound would demand the most composure exactly where
    real ramps have the least.  An unsteady block, or the unsteady cooldown
    that follows a ramp, ends the run at that point; it does not discard the
    rising steps already recognized.

    The run must also end in a collapse - some minute within the three that
    follow it at most half the final step - unless it runs to the end of the
    stream.  A ramp test ends in failure; a climb or a progressive ride tapers
    or carries on.  Without it the warm-up guard has little force, since in a
    20 W/min ramp every step is itself 20 W below the next and so qualifies as
    the "clearly lower" minute for the step after it.

    The block alignment is allowed to move by up to 59 seconds because stored
    streams need not begin on a step boundary.  A result of 0.0 means that no
    sufficiently distinctive ramp was found.
    """
    arr = _clean_power(power)
    block = 60
    if arr.size < 6 * block or arr.size > 45 * 60:
        return 0.0

    best_step = 0.0
    # A real stream may start part-way through the warm-up, so inspect every
    # possible start in the first 15 minutes.  Ramp warm-ups are short; this
    # bound also prevents a long ride from turning into a quadratic scan.
    for start in range(min(15 * 60, arr.size - 6 * block) + 1):
        means = []
        pos = start
        while pos + block <= arr.size:
            chunk = arr[pos:pos + block]
            mean = float(chunk.mean())
            # An unsteady block ends the run rather than voiding it: what came
            # before it was still a ramp.  The bound scales with the step's own
            # power because a rider holds 300 W less exactly, in watts, than
            # they hold 120 W; below 171 W the flat 12 W bound still governs.
            if float(chunk.std()) > max(12.0, 0.07 * mean):
                break
            if means and not (
                RAMP_DETECTOR_MIN_SLOPE_W
                <= mean - means[-1]
                <= RAMP_DETECTOR_MAX_SLOPE_W
            ):
                break
            means.append(mean)
            pos += block
        if len(means) < 5:
            continue
        # Require the detected run to begin after a clearly lower warm-up
        # minute.  This rejects a sequence of rising intervals in isolation.
        if start < block or float(arr[start - block:start].mean()) > means[0] - 20.0:
            continue
        # Require the run to end in a collapse.  That is what a ramp test
        # physically is: the rider cannot hold the next step and power falls
        # away.  A climb, a group ride or a progressive workout tapers, or
        # simply carries on, so demand that some minute in the three that
        # follow is at most half the final step.  This is the guard that the
        # warm-up minute cannot supply on its own, because in a ramp of 20 W a
        # minute every step is itself "clearly lower" than the next.  A run
        # reaching the end of the stream is exempt: a recording may stop at the
        # moment of failure, leaving no cooldown to inspect.
        tail = arr[pos:pos + 3 * block]
        if tail.size >= block and float(
            rolling_mean(tail, block).min()
        ) > 0.5 * means[-1]:
            continue
        best_step = max(best_step, max(means))
    return best_step * RAMP_TEST_FTP_FRACTION


def _split_activities(activities: Iterable):
    """Split a mixed activity iterable into efforts and the activity calendar.

    Returns ``(efforts, activity_days)`` where:
      - ``efforts`` is a list of ``(when, power_stream, rpe)`` for items that
        carry power (``when`` is the parsed naive datetime, or None for raw
        streams / undated dicts; ``rpe`` is the rider's 1-10 session rating when
        they recorded one, else None - see ``recent_effort_floor``).
      - ``activity_days`` is the sorted list of every dated activity's timestamp
        - including power-less rides, which still count as time on the bike for
        the detraining-gap calendar.
    """
    from ..timeutil import parse_naive

    efforts: "list[tuple[object, Sequence[float], object]]" = []
    activity_days: list = []

    def rpe_or_none(value):
        if value is None or isinstance(value, bool):
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def stream_or_none(value, *, persisted: bool = False):
        if value is None or isinstance(value, (str, bytes, bytearray, Mapping)):
            return None
        if persisted and not isinstance(value, (list, tuple)):
            return None
        try:
            iter(value)
        except TypeError:
            return None
        if isinstance(value, (list, tuple)) and not value:
            return None
        return value

    for item in activities:
        if isinstance(item, dict):
            when = parse_naive(item.get("start_time"))
            if when is not None:
                activity_days.append(when)
            streams = item.get("streams")
            power = streams.get("power") if isinstance(streams, Mapping) else None
            if power is None:
                power = item.get("power")
            power = stream_or_none(power, persisted=True)
            if power is not None:
                efforts.append((when, power, rpe_or_none(item.get("rpe"))))
        else:
            power = stream_or_none(item)
            if power is not None:
                efforts.append((None, power, None))
    activity_days.sort()
    return efforts, activity_days


# --- Recent-evidence floor ---------------------------------------------------
# The 20-minute test is the only effort the decayed estimate can see, and a
# rider who trains with structured ERG workouts never rides one: every interval
# is deliberately submaximal and 12-20 minutes long, so their best 20-minute
# number is diluted by the recoveries inside it and the estimate reads 15-20%
# low. These are the durations a long effort genuinely constrains FTP at, with
# the fraction of that mean power an FTP sits at (Coggan's 95%-of-20min
# extended either side: shorter efforts are further above FTP, a 60-minute
# effort IS FTP). Nothing shorter than 13 minutes is admitted - a 5-minute
# maximal effort says almost nothing about the hour, and letting one in would
# turn every VO2max session into an FTP bump.
FTP_FLOOR_DURATIONS = (
    (780, 0.92),    # 13 min
    (1200, 0.95),   # 20 min
    (1800, 0.98),   # 30 min
    (3600, 1.00),   # 60 min
)
FTP_FLOOR_WINDOW_DAYS = 42
# A submaximal effort under-reports what the rider could have done. RPE is the
# only evidence we have of how hard it actually was, so an effort the rider
# rated below 8/10 is credited a little more than its raw watts - 2.5% per RPE
# point below 8, capped at +10% so a single "easy" rating can never invent a
# fitness jump. No rating means no adjustment.
FTP_FLOOR_RPE_REFERENCE = 8
FTP_FLOOR_RPE_STEP = 0.025
FTP_FLOOR_RPE_MAX = 1.10


def _rpe_scale(rpe) -> float:
    """Submaximality credit for an effort the rider rated below 8/10."""
    if rpe is None or rpe >= FTP_FLOOR_RPE_REFERENCE:
        return 1.0
    scale = 1.0 + FTP_FLOOR_RPE_STEP * (FTP_FLOOR_RPE_REFERENCE - float(rpe))
    return min(FTP_FLOOR_RPE_MAX, scale)


def recent_effort_floor(
    activities: Iterable, now, window_days: int = FTP_FLOOR_WINDOW_DAYS
) -> float:
    """Highest FTP implied by any long effort ridden in the last ``window_days``.

    For every dated activity inside the window, the best rolling mean at each of
    ``FTP_FLOOR_DURATIONS`` is converted to its FTP equivalent and scaled by the
    rider's RPE (see ``_rpe_scale``); the maximum across every activity and
    duration is returned. Durations longer than the stream are skipped.

    Each contribution is then decayed by ``detraining_factor`` against the same
    activity calendar ``estimate_ftp`` uses. Without it the floor would cancel
    detraining inside its own window - the 20-minute mapping is numerically the
    same 0.95 the decayed estimator applies, so a rider who did one 20-minute
    max and then stopped riding for six weeks would keep the undecayed number
    for the whole window. A rider who kept training barely decays, which is
    exactly the case the floor exists to serve.

    This is a FLOOR, not an estimate: it only ever says "the rider demonstrably
    held this, so their FTP is at least this much". Returns 0.0 when ``now`` is
    None or nothing in the window qualifies.
    """
    if now is None:
        return 0.0
    cutoff = now - datetime.timedelta(days=int(window_days))
    efforts, activity_days = _split_activities(activities)
    best = 0.0
    for when, stream, rpe in efforts:
        if when is None or when < cutoff:
            continue
        idle, active = _idle_active_days(when, now, activity_days)
        scale = _rpe_scale(rpe) * detraining_factor(idle, active)
        for window, fraction in FTP_FLOOR_DURATIONS:
            roll = rolling_mean(stream, window)
            if roll.size == 0:
                continue
            implied = float(roll.max()) * fraction * scale
            if implied > best:
                best = implied
    return best


def estimate_ftp(
    activities: Iterable,
    override: float | None = None,
    window_days: "int | None" = None,
    now=None,
) -> float:
    """Estimate FTP from dated 20-minute efforts and recognized ramp tests.

    Each dated effort contributes the best of ``best_20min_power`` and the
    raw-equivalent of a recognized ramp candidate (highest one-minute step
    * 0.75 / 0.95), multiplied by ``detraining_factor(...)`` where the factor is
    derived from the rider's activity calendar between the effort and ``now``
    (see ``detraining_factor`` / ``_idle_active_days``). The final 0.95 factor
    is applied once, after selecting the best decayed raw-equivalent effort, so
    the established ``0.95 * best_decayed`` invariant remains intact while a
    ramp result still means exactly 75% of its highest step.
    Detraining accrues only during INACTIVITY, so an effort ridden while the
    rider kept training barely decays, while an effort followed by a long layoff
    decays substantially. A recent hard effort dominates and old efforts fade
    smoothly rather than cliffing to zero.

    That figure is then floored by ``recent_effort_floor``: a rider who only
    does structured ERG work never rides a maximal 20 minutes, so the decayed
    number alone reads well below what their recent long efforts already prove
    they can hold. The floor never lowers the estimate.

    Accepts either raw power streams or activity dicts (with "streams" /
    "start_time"). When ``now`` is None, no decay is applied and the result is
    simply best-20-min * 0.95 (used with raw streams). Undated items always get
    a decay factor of 1.0.

    ``window_days`` is deprecated and ignored (the decay model replaces the old
    hard trailing window); the kwarg is kept for call-site compatibility.
    A user override always wins when provided and positive.
    """
    if override is not None and override > 0:
        return float(override)

    # Materialized because the recent-evidence floor iterates the same input a
    # second time and callers may pass a generator.
    activities = list(activities)
    efforts, activity_days = _split_activities(activities)

    best_decayed = 0.0
    for when, stream, _rpe in efforts:
        b = best_20min_power(stream)
        ramp = ramp_test_ftp_candidate(stream)
        candidate = max(b, ramp / 0.95 if ramp > 0 else 0.0)
        if candidate <= 0:
            continue
        if now is not None and when is not None:
            idle, active = _idle_active_days(when, now, activity_days)
            candidate *= detraining_factor(idle, active)
        if candidate > best_decayed:
            best_decayed = candidate
    decayed = best_decayed * 0.95
    if now is None:
        return decayed
    return max(decayed, recent_effort_floor(activities, now))
