"""Assemble a TrainingState from stored activities."""
from __future__ import annotations

import datetime as _dt
from typing import Dict, List, Optional, Tuple

import bisect

from .. import db
from ..timeutil import parse_naive, utc_now
from ..ingest.importer import current_ftp
from ..metrics.power import (
    detraining_factor,
    FTP_DECAY_GRACE_DAYS,
    FTP_DECAY_TAU_IDLE,
    FTP_DECAY_TAU_ACTIVE,
)
from ..metrics.curve import MMP_DURATIONS, fit_cp_wprime, mean_maximal_power
from ..metrics.load import compute_load, daily_tss_series
from . import activity_cache, zones
from .detect import evaluate
from .state import TrainingState


def _window_power(
    activities: List[dict], start_days_ago: int, end_days_ago: int
) -> List[List[float]]:
    now = utc_now()
    lo = now - _dt.timedelta(days=start_days_ago)
    hi = now - _dt.timedelta(days=end_days_ago)
    out: List[List[float]] = []
    for a in activities:
        when = parse_naive(a.get("start_time"))
        if when is None:
            continue
        if hi <= when < lo:
            power = (a.get("streams") or {}).get("power") or []
            if power:
                out.append(power)
    return out


def _safe_cp(mmp: Dict[int, float]) -> Tuple[Optional[float], Optional[float]]:
    """CP/W' for this MMP curve, or (None, None) if it cannot honestly be fitted.

    ``fit_cp_wprime`` raises when there is too little data inside the model's
    valid 2-20 minute domain, or when the fit lands somewhere physiologically
    impossible. Both mean "we do not know this rider's CP", which the rest of
    the app already renders as an absent profile rather than a wrong one.
    """
    try:
        return fit_cp_wprime(mmp)
    except ValueError:
        return None, None


def build_state(user_id: int) -> TrainingState:
    """Compute the current TrainingState for a user from the database."""
    db.init_db()
    ftp = current_ftp(user_id)

    # Load / CTL-ATL-TSB
    load = compute_load(daily_tss_series(db.daily_tss(user_id)))
    ctl = load[-1]["ctl"] if load else 0.0
    atl = load[-1]["atl"] if load else 0.0
    tsb = load[-1]["tsb"] if load else 0.0

    # Mean-maximal power over trailing 90 days and CP/W'
    streams_90 = db.recent_power_streams(user_id, days=90)
    mmp = mean_maximal_power(streams_90) if streams_90 else {}
    # No point-count pre-check here: the fit itself knows how many points it
    # needs and, crucially, which ones count (only those inside its valid
    # duration window - a rider with 17 sprint samples still has no CP).
    cp, wprime = _safe_cp(mmp)

    # Prior/recent 4-week windows for plateau detection. Only the trailing ~8
    # weeks are needed, so decompress just those activities (not all history).
    recent_activities = db.recent_full_activities(user_id, days=57)
    recent_streams = _window_power(recent_activities, 0, 28)
    prior_streams = _window_power(recent_activities, 28, 56)
    mmp_recent = mean_maximal_power(recent_streams) if recent_streams else {}
    mmp_prior = mean_maximal_power(prior_streams) if prior_streams else {}
    cp_prior, wprime_prior = _safe_cp(mmp_prior)

    # Aerobic decoupling from the most recent long steady effort (>45min).
    # Cached (activity-static): avoids inflating every stream per request.
    decoupling = activity_cache.get_digest(user_id).decoupling

    state = TrainingState(
        ftp=ftp,
        cp=cp,
        wprime=wprime,
        ctl=ctl,
        atl=atl,
        tsb=tsb,
        decoupling=decoupling,
        mmp=mmp if mmp else mmp_recent,
    )
    evaluate(
        state,
        cp_prior=cp_prior,
        wprime_prior=wprime_prior,
        load=load,
        mmp_prior=mmp_prior,
    )
    return state


_DAYS_PER_MONTH = 30.44

FTP_WINDOW_DAYS = 42
FTP_STEP_DAYS = 7


def _filter_by_months(
    series: List[dict], months: Optional[float], now: Optional[_dt.datetime] = None
) -> List[dict]:
    """Keep only series items whose ``date`` falls in the trailing N months."""
    if not months or float(months) <= 0:
        return series
    now = now or utc_now()
    cutoff = now - _dt.timedelta(days=int(round(float(months) * _DAYS_PER_MONTH)))
    out: List[dict] = []
    for item in series:
        d = parse_naive(item.get("date"))
        if d is not None and d >= cutoff:
            out.append(item)
    return out


def load_series(user_id: int, months: Optional[float] = None) -> List[dict]:
    """CTL/ATL/TSB daily series for charting, optionally trailing N months."""
    series = compute_load(daily_tss_series(db.daily_tss(user_id)))
    return _filter_by_months(series, months)


def ftp_recorded(user_id: int, months: Optional[float] = None) -> List[dict]:
    """Recorded (manual/monthly) FTP history rows, optionally trailing N months."""
    return _filter_by_months(db.ftp_history_list(user_id), months)


def ftp_rolling_series(
    user_id: int,
    months: Optional[float] = None,
    window_days: int = FTP_WINDOW_DAYS,
    step_days: int = FTP_STEP_DAYS,
    now: Optional[_dt.datetime] = None,
) -> dict:
    """Rolling estimated-FTP time series plus recorded FTP-history points.

    The estimate at each sampled date is ``0.95 * max(best20 *
    detraining_factor(...))`` over every effort ridden on or before that date,
    where the factor uses the gap-aware detraining model (decay accrues only
    during inactivity, measured against the activity calendar up to the sample
    date - trailing gap = sample minus the last ride before it). The series
    therefore declines through a training break and holds steady while the rider
    keeps training, without gaps or cliffs. Sampled every ``step_days`` from the
    first activity to the last (or ``now``).

    ``window_days`` is deprecated and ignored (the decay model replaces the old
    hard trailing window); kept for call-site compatibility.

    Returns {"estimated": [{date, ftp}], "recorded": [{date, ftp, source}]}.
    """
    # (when, best20) per effort + the full activity calendar (every dated ride,
    # even power-less ones, counts for the gaps) come from the cached digest, so
    # the ~850 stream BLOBs are inflated at most once per import, not per request.
    digest = activity_cache.get_digest(user_id)
    activity_days = digest.activity_days
    effort_days = digest.effort_days
    effort_b20 = digest.effort_b20
    effort_i = digest.effort_i
    prefix = digest.prefix

    recorded = _filter_by_months(db.ftp_history_list(user_id), months, now)
    if not effort_days:
        return {"estimated": [], "recorded": recorded}

    start = effort_days[0]
    last = effort_days[-1]
    end = now if (now is not None and now > last) else last

    step = _dt.timedelta(days=step_days)

    g = FTP_DECAY_GRACE_DAYS

    def _estimate_at(anchor: _dt.datetime) -> Optional[float]:
        # idle/active for each (effort, anchor) pair via the prefix-sum identity
        # (verified equivalent to metrics.power._idle_active_days): the interior
        # gaps between the effort and anchor are P[j]-P[i]; the two boundary
        # gaps are handled explicitly. O(1) per effort instead of rescanning the
        # calendar, so the whole series is O(samples x efforts).
        j = bisect.bisect_right(activity_days, anchor) - 1
        if j < 0:
            return None
        p_j = prefix[j]
        trail = (anchor - activity_days[j]).total_seconds() / 86400.0
        trail_idle = trail - g if trail > g else 0.0
        best = 0.0
        for k in range(len(effort_days)):
            when = effort_days[k]
            if when > anchor:
                break
            span = (anchor - when).total_seconds() / 86400.0
            if span <= 0:
                idle = 0.0
                active = 0.0
            else:
                i = effort_i[k]
                if i > j:
                    idle = span - g if span > g else 0.0
                else:
                    g1 = (activity_days[i] - when).total_seconds() / 86400.0
                    idle = (g1 - g if g1 > g else 0.0) + (p_j - prefix[i]) + trail_idle
                active = span - idle
                if active < 0.0:
                    active = 0.0
            weighted = effort_b20[k] * detraining_factor(idle, active)
            if weighted > best:
                best = weighted
        if best <= 0:
            return None
        return best * 0.95

    # Only samples inside the requested trailing window survive _filter_by_months,
    # so skip evaluating the (expensive) estimate for samples outside it. The
    # grid itself is still anchored to ``start`` (identical sample dates).
    if months and float(months) > 0:
        _now = now or utc_now()
        cutoff = _now - _dt.timedelta(days=int(round(float(months) * _DAYS_PER_MONTH)))
    else:
        cutoff = None

    est_points: List[dict] = []
    cur = start
    while cur <= end:
        if cutoff is None or _dt.datetime.combine(cur.date(), _dt.time()) >= cutoff:
            est = _estimate_at(cur)
            if est is not None:
                est_points.append(
                    {"date": cur.date().isoformat(), "ftp": round(est, 1)}
                )
        cur += step

    # Always include a final sample exactly at the end date.
    end_iso = end.date().isoformat()
    if not est_points or est_points[-1]["date"] != end_iso:
        est = _estimate_at(end)
        if est is not None:
            est_points.append({"date": end_iso, "ftp": round(est, 1)})

    return {
        "estimated": _filter_by_months(est_points, months, now),
        "recorded": recorded,
    }


DETAIL_MAX_POINTS = 1500


def _downsample(values: List, target: int) -> List:
    """Block-average a numeric stream down to ~target points (None-safe).

    Non-numeric / empty streams collapse to []. Averaging (rather than picking)
    keeps power/HR shapes faithful; altitude too. Returns rounded floats.
    """
    clean = [v for v in (values or []) if v is not None]
    if not clean:
        return []
    n = len(values)
    if n <= target:
        return [round(float(v), 1) if v is not None else None for v in values]
    step = n / float(target)
    out: List = []
    i = 0.0
    while int(i) < n:
        lo = int(i)
        hi = min(n, int(i + step))
        bucket = [v for v in values[lo:hi] if v is not None]
        out.append(round(sum(bucket) / len(bucket), 1) if bucket else None)
        i += step
    return out


def activity_detail(
    user_id: int, activity_id: int, max_points: int = DETAIL_MAX_POINTS
) -> Optional[dict]:
    """Per-ride detail + downsampled streams for the activity graphs.

    Streams are persisted per activity (see importer), so no FIT re-parse is
    needed. Each of power/heartrate/cadence/altitude is downsampled to
    ~max_points; the x axis is elapsed minutes. Missing streams come back as
    empty lists so the page renders whatever is available. Returns None when
    the activity does not belong to the user.
    """
    act = db.get_activity(user_id, activity_id)
    if not act:
        return None
    streams = act.get("streams") or {}
    # Zone duration is derived from the full-resolution aligned streams before
    # graph downsampling, so short peaks and exact elapsed time are preserved.
    zone_summary = zones.activity_zone_summary(user_id, act)
    n = max(
        (len(streams.get(k) or []) for k in
         ("power", "heartrate", "cadence", "altitude", "time")),
        default=0,
    )
    minutes = [round(i / 60.0, 3) for i in range(n)]
    series = {
        k: _downsample(streams.get(k) or [], max_points)
        for k in ("power", "heartrate", "cadence", "altitude")
    }
    t = _downsample(minutes, max_points)
    have = {k: any(v is not None for v in vals) for k, vals in series.items()}
    return {
        "id": act["id"],
        "filename": act.get("filename"),
        "start_time": act.get("start_time"),
        "duration_s": act.get("duration_s"),
        "distance_m": act.get("distance_m"),
        "avg_power": act.get("avg_power"),
        "avg_hr": act.get("avg_hr"),
        "np": act.get("np"),
        "if_": act.get("if_"),
        "tss": act.get("tss"),
        "rpe": act.get("rpe"),
        "t": t,
        "power": series["power"],
        "heartrate": series["heartrate"],
        "cadence": series["cadence"],
        "altitude": series["altitude"],
        "have": have,
        "points": len(t),
        "zones": zone_summary,
    }


def curve_points(user_id: int, state: Optional[TrainingState] = None) -> dict:
    """Power-duration data for dashboard MMP views plus its fixed CP/W' model."""
    if state is None:
        state = build_state(user_id)

    def _points(mmp: Dict[int, float]) -> List[dict]:
        return [
            {"t": int(t), "power": round(p, 1)} for t, p in sorted(mmp.items())
        ]

    def _power_stream(activity: dict) -> List[float]:
        streams = activity.get("streams")
        power = streams.get("power") if isinstance(streams, dict) else None
        return power if isinstance(power, list) else []

    # ``measured`` is deliberately retained as the trailing-90-day curve for
    # API compatibility and as the sole source for the CP/W' model.
    measured = _points(state.mmp)
    # Scan the history once: the same effective streams feed the all-time
    # aggregate and identify the newest ride with usable power.
    all_streams = []
    last_ride_mmp: Dict[int, float] = {}
    for activity in db.iter_full_activities_desc(user_id):
        power = _power_stream(activity)
        if not power:
            continue
        all_streams.append(power)
        if not last_ride_mmp:
            candidate = mean_maximal_power([power], MMP_DURATIONS)
            if candidate:
                last_ride_mmp = candidate
    all_time = _points(mean_maximal_power(all_streams, MMP_DURATIONS))
    last_ride = _points(last_ride_mmp)

    model = []
    if state.cp is not None and state.wprime is not None:
        for t in sorted(set(list(MMP_DURATIONS) + [int(x["t"]) for x in measured])):
            if t > 0:
                model.append({"t": t, "power": round(state.cp + state.wprime / t, 1)})
    return {
        "measured": measured,
        "all_time": all_time,
        "last_ride": last_ride,
        "model": model,
        "cp": state.cp,
        "wprime": state.wprime,
    }
