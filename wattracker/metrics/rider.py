"""Measured rider capacities: HRmax, anaerobic capacity, power ratios.

This is the *measurement* layer. It turns stored ride data into the handful of
per-rider numbers a training prescription should be built on - the rider's own
sprint and VO2 power expressed as multiples of FTP, W' per kg, and a detected
HRmax - instead of the fixed %FTP constants currently hardcoded in
``prescribe/planner.py``. Nothing here prescribes anything; a later step
consumes it.

Every field is independently optional. This is called from page renders, so no
function here raises: missing, empty or corrupt data yields ``None``.

Deliberately NOT here: rider phenotype classification (sprinter / pursuiter /
time-trialist / all-rounder). PR #23 adds ``classify_phenotype`` in
``analysis/power_profile.py`` and we consume that rather than grow a second
implementation.
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np

from .. import db
from ..analysis.state import TrainingState
from ..timeutil import parse_naive, utc_now

# --- HRmax detection ---------------------------------------------------------
# A raw max() over the heart-rate stream is the wrong statistic. Field HR is
# spike-prone: strap contact loss, a dry electrode at the start of a ride, and
# cross-talk from a nearby rider's ANT+ strap all produce isolated samples of
# 220-250 bpm on an otherwise 150 bpm ride. Taking max() means the reported
# HRmax is whichever artefact was largest - it can only ever ratchet upward, and
# a single bad second permanently poisons the number.
#
# Physiology gives the fix: true maximal heart rate is *sustainable* for tens of
# seconds (it is reached at the end of a hard effort and holds there), whereas
# an artefact lasts a sample or two. So per activity we take the maximum of a
# rolling mean rather than of the raw samples. 10s is long enough that a 1-5
# sample spike is diluted below the surrounding HR, and short enough that a
# genuine max-effort finish - which plateaus for far longer than 10s - is
# reported essentially undiminished.
HR_SMOOTH_WINDOW_S = 10

# Second layer: reject one bad *file*. A whole activity can be wrong (a strap
# reading double cadence-derived noise for 20 minutes will survive smoothing),
# so we do not take the outright highest per-activity value across activities
# either. Instead we take a non-interpolated 90th percentile of the per-activity
# maxima, which for any realistic activity count discards the single highest
# contributor and returns an actually-observed value from the next tier down.
# A genuine HRmax is hit on several hard rides, so it survives; a one-off
# artefact is dropped.
HR_PERCENTILE = 90.0

# Below this many contributing activities the percentile has nothing to reject
# with, so we report nothing rather than a confident wrong number.
HR_MIN_ACTIVITIES = 5

# Plausibility bounds on a per-activity sustained peak. Anything outside these
# is not a heart rate: a stream of zeros/dropouts falls below, sensor cross-talk
# and unit confusion fall above. Bounds are deliberately wide - the point is to
# exclude nonsense, not to second-guess an unusual athlete.
HR_MIN_BPM = 120.0
HR_MAX_BPM = 230.0

# HRmax declines by roughly a beat a year, so unlike power there is no reason to
# use a short trailing window; a year of rides gives plenty of contributors
# while staying current enough for an ageing athlete.
HR_LOOKBACK_DAYS = 365

HR_STREAM_KEY = "heartrate"  # fit_parser maps FIT `heart_rate` to this key


def _finite_floats(values: Iterable[Any]) -> np.ndarray:
    """Coerce a stream to a float array, DROPPING anything that is not a number.

    Streams from the wild contain ``None`` gaps and, after a partly-corrupt
    import, strings and other junk. Unlike power (where ``None`` genuinely means
    zero watts) an absent heart-rate sample is a dropout, not a heart rate of 0,
    so bad samples are removed rather than zero-filled - zero-filling would drag
    a rolling mean down and hide a real peak.
    """
    out: List[float] = []
    for v in values or []:
        if v is None or isinstance(v, bool):
            continue
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if np.isfinite(f):
            out.append(f)
    return np.array(out, dtype=float)


def sustained_hr_peak(
    stream: Sequence[Any], window_s: int = HR_SMOOTH_WINDOW_S
) -> Optional[float]:
    """Highest ``window_s`` rolling-mean heart rate in one activity's stream.

    Returns ``None`` when the stream is empty, all junk, or shorter than the
    smoothing window (too short to establish that a value was sustained).
    """
    arr = _finite_floats(stream)
    if arr.size == 0:
        return None
    if window_s <= 1:
        return float(arr.max())
    if arr.size < window_s:
        return None
    cumsum = np.cumsum(np.insert(arr, 0, 0.0))
    roll = (cumsum[window_s:] - cumsum[:-window_s]) / float(window_s)
    if roll.size == 0:
        return None
    return float(roll.max())


def detect_hr_max(
    streams: Iterable[Sequence[Any]],
    window_s: int = HR_SMOOTH_WINDOW_S,
    min_activities: int = HR_MIN_ACTIVITIES,
) -> Tuple[Optional[float], int]:
    """Detect HRmax from many activities' heart-rate streams.

    Returns ``(hr_max, n_contributing_activities)``. ``hr_max`` is ``None``
    when fewer than ``min_activities`` streams yield a plausible sustained peak.
    """
    peaks: List[float] = []
    for stream in streams:
        peak = sustained_hr_peak(stream, window_s)
        if peak is None:
            continue
        if HR_MIN_BPM <= peak <= HR_MAX_BPM:
            peaks.append(peak)
    n = len(peaks)
    if n < min_activities:
        return None, n
    # method="lower" keeps the result an actually-observed value and, unlike
    # linear interpolation, does not let the single highest (possibly bad)
    # activity pull the answer up.
    value = float(np.percentile(np.array(peaks, dtype=float), HR_PERCENTILE,
                                method="lower"))
    return value, n


def _positive(value: Any) -> Optional[float]:
    """Float value if it is a finite number > 0, else None."""
    if value is None or isinstance(value, bool):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if not np.isfinite(f) or f <= 0:
        return None
    return f


def wprime_per_kg(wprime_j: Optional[float], weight_kg: Optional[float]) -> Optional[float]:
    """Anaerobic capacity normalized by mass (J/kg). None if either is missing."""
    w = _positive(wprime_j)
    m = _positive(weight_kg)
    if w is None or m is None:
        return None
    return w / m


def cp_per_kg(cp_w: Optional[float], weight_kg: Optional[float]) -> Optional[float]:
    """Critical Power normalized by mass (W/kg). None if either is missing."""
    c = _positive(cp_w)
    m = _positive(weight_kg)
    if c is None or m is None:
        return None
    return c / m


def mmp_at(mmp: Optional[Dict[int, float]], seconds: int) -> Optional[float]:
    """Mean-maximal power at a duration, or None when not measured."""
    if not mmp:
        return None
    try:
        value = mmp.get(seconds)
    except AttributeError:
        return None
    return _positive(value)


def power_ratio(
    mmp: Optional[Dict[int, float]], seconds: int, ftp: Optional[float]
) -> Optional[float]:
    """``mmp[seconds] / ftp`` - the rider's measured multiple of FTP.

    None whenever either side is missing or non-positive, so callers can fall
    back to a population default rather than divide by zero.
    """
    peak = mmp_at(mmp, seconds)
    f = _positive(ftp)
    if peak is None or f is None:
        return None
    return peak / f


@dataclass(frozen=True)
class RiderMetrics:
    """Measured capacities for one rider. Every field is independently optional."""

    ftp: Optional[float] = None
    weight_kg: Optional[float] = None

    hr_max: Optional[float] = None
    hr_max_source: Optional[str] = None  # "manual" | "measured" | None
    n_hr_activities: int = 0

    cp: Optional[float] = None
    wprime: Optional[float] = None
    wprime_j_per_kg: Optional[float] = None
    cp_w_per_kg: Optional[float] = None

    peak_5s: Optional[float] = None
    peak_60s: Optional[float] = None
    peak_300s: Optional[float] = None
    sprint_ratio: Optional[float] = None
    vo2_ratio: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        def r(v: Optional[float], nd: int) -> Optional[float]:
            return round(v, nd) if v is not None else None

        return {
            "ftp": r(self.ftp, 1),
            "weight_kg": r(self.weight_kg, 1),
            "hr_max": r(self.hr_max, 0),
            "hr_max_source": self.hr_max_source,
            "n_hr_activities": self.n_hr_activities,
            "cp": r(self.cp, 1),
            "wprime": r(self.wprime, 1),
            "wprime_j_per_kg": r(self.wprime_j_per_kg, 1),
            "cp_w_per_kg": r(self.cp_w_per_kg, 2),
            "peak_5s": r(self.peak_5s, 1),
            "peak_60s": r(self.peak_60s, 1),
            "peak_300s": r(self.peak_300s, 1),
            "sprint_ratio": r(self.sprint_ratio, 3),
            "vo2_ratio": r(self.vo2_ratio, 3),
        }


def _hr_streams(
    user_id: int, now: Optional[_dt.datetime] = None
) -> List[Sequence[Any]]:
    """Heart-rate streams from the trailing ``HR_LOOKBACK_DAYS`` of activities.

    Uses the existing ``recent_full_activities`` accessor. Its cutoff is
    relative to *wall clock*, so when a caller supplies an earlier ``now`` we
    widen the query far enough to cover that anchor and re-filter here.
    """
    days = HR_LOOKBACK_DAYS
    lo: Optional[_dt.datetime] = None
    hi: Optional[_dt.datetime] = None
    if now is not None:
        back = (utc_now() - now).total_seconds() / 86400.0
        if back > 0:
            days += int(back) + 1
        lo = now - _dt.timedelta(days=HR_LOOKBACK_DAYS)
        hi = now
    try:
        activities = db.recent_full_activities(user_id, days=days)
    except Exception:
        return []
    out: List[Sequence[Any]] = []
    for act in activities:
        if lo is not None:
            when = parse_naive(act.get("start_time"))
            if when is None or when < lo or when > hi:
                continue
        stream = (act.get("streams") or {}).get(HR_STREAM_KEY)
        if stream:
            out.append(stream)
    return out


def for_user(
    user_id: int,
    state: Optional[TrainingState] = None,
    now: Optional[_dt.datetime] = None,
) -> RiderMetrics:
    """Assemble every measured capacity we have for a rider.

    ``state`` is a ``TrainingState``; it is built via ``analysis.pipeline`` when
    not supplied. Never raises - a brand-new user with no rides, no weight and
    no FTP gets an all-None ``RiderMetrics``.
    """
    if state is None:
        try:
            from ..analysis.pipeline import build_state

            state = build_state(user_id)
        except Exception:
            state = None

    try:
        settings = db.get_user_settings(user_id) or {}
    except Exception:
        settings = {}

    weight = _positive(settings.get("weight_kg"))
    ftp = _positive(getattr(state, "ftp", None))
    cp = _positive(getattr(state, "cp", None))
    wprime = _positive(getattr(state, "wprime", None))
    mmp = getattr(state, "mmp", None)
    if not isinstance(mmp, dict):
        mmp = {}

    # A manually entered HRmax always wins: the rider knows something a lab test
    # or a memorable max-effort day told them, and detection can only ever be a
    # lower bound on what they have actually hit.
    manual_hr = _positive(settings.get("hr_max"))
    n_hr = 0
    if manual_hr is not None:
        hr_max: Optional[float] = manual_hr
        hr_source: Optional[str] = "manual"
    else:
        try:
            detected, n_hr = detect_hr_max(_hr_streams(user_id, now))
        except Exception:
            detected, n_hr = None, 0
        hr_max = detected
        hr_source = "measured" if detected is not None else None

    return RiderMetrics(
        ftp=ftp,
        weight_kg=weight,
        hr_max=hr_max,
        hr_max_source=hr_source,
        n_hr_activities=n_hr,
        cp=cp,
        wprime=wprime,
        wprime_j_per_kg=wprime_per_kg(wprime, weight),
        cp_w_per_kg=cp_per_kg(cp, weight),
        peak_5s=mmp_at(mmp, 5),
        peak_60s=mmp_at(mmp, 60),
        peak_300s=mmp_at(mmp, 300),
        sprint_ratio=power_ratio(mmp, 5, ftp),
        vo2_ratio=power_ratio(mmp, 300, ftp),
    )
