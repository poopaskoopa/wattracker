"""Assemble a TrainingState from stored activities."""
from __future__ import annotations

import datetime as _dt
from typing import Dict, List, Optional, Tuple

from .. import db
from ..timeutil import parse_naive
from ..ingest.importer import current_ftp
from ..metrics.power import best_20min_power, detraining_factor
from ..metrics.curve import MMP_DURATIONS, fit_cp_wprime, mean_maximal_power
from ..metrics.decoupling import aerobic_decoupling
from ..metrics.load import compute_load, daily_tss_series
from .detect import evaluate
from .state import TrainingState


def _window_power(
    activities: List[dict], start_days_ago: int, end_days_ago: int
) -> List[List[float]]:
    now = _dt.datetime.now()
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
    try:
        return fit_cp_wprime(mmp)
    except ValueError:
        return None, None


def build_state(user_id: int) -> TrainingState:
    """Compute the current TrainingState for a user from the database."""
    db.init_db()
    ftp = current_ftp(user_id)

    activities = db.full_activities(user_id)

    # Load / CTL-ATL-TSB
    load = compute_load(daily_tss_series(db.daily_tss(user_id)))
    ctl = load[-1]["ctl"] if load else 0.0
    atl = load[-1]["atl"] if load else 0.0
    tsb = load[-1]["tsb"] if load else 0.0

    # Mean-maximal power over trailing 90 days and CP/W'
    streams_90 = db.recent_power_streams(user_id, days=90)
    mmp = mean_maximal_power(streams_90) if streams_90 else {}
    cp, wprime = _safe_cp(mmp) if len(mmp) >= 2 else (None, None)

    # Prior/recent 4-week windows for plateau detection
    recent_streams = _window_power(activities, 0, 28)
    prior_streams = _window_power(activities, 28, 56)
    mmp_recent = mean_maximal_power(recent_streams) if recent_streams else {}
    mmp_prior = mean_maximal_power(prior_streams) if prior_streams else {}
    cp_prior, wprime_prior = (
        _safe_cp(mmp_prior) if len(mmp_prior) >= 2 else (None, None)
    )

    # Aerobic decoupling from the most recent long steady effort (>45min)
    decoupling: Optional[float] = None
    for a in reversed(activities):
        streams = a.get("streams") or {}
        power = streams.get("power") or []
        hr = streams.get("heartrate") or []
        d = aerobic_decoupling(power, hr)
        if d is not None:
            decoupling = d
            break

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
    now = now or _dt.datetime.now()
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
    detraining_factor(sample - effort date))`` over every activity ridden on or
    before that date. Older efforts fade smoothly via the detraining decay, so
    the series shows honest decline through a training break instead of gaps or
    cliffs. Sampled every ``step_days`` from the first activity to the last (or
    ``now``).

    ``window_days`` is deprecated and ignored (the smooth decay replaces the old
    hard trailing window); kept for call-site compatibility.

    Returns {"estimated": [{date, ftp}], "recorded": [{date, ftp, source}]}.
    """
    activities = db.full_activities(user_id)
    # Precompute (when, best20) once per activity, then reuse across all samples.
    dated: List[Tuple[_dt.datetime, float]] = []
    for a in activities:
        when = parse_naive(a.get("start_time"))
        power = (a.get("streams") or {}).get("power") or []
        if when is None or not power:
            continue
        b20 = best_20min_power(power)
        if b20 > 0:
            dated.append((when, b20))

    recorded = _filter_by_months(db.ftp_history_list(user_id), months, now)
    if not dated:
        return {"estimated": [], "recorded": recorded}

    start = min(d for d, _ in dated)
    last = max(d for d, _ in dated)
    end = now if (now is not None and now > last) else last

    step = _dt.timedelta(days=step_days)

    def _estimate_at(dt: _dt.datetime) -> Optional[float]:
        best = 0.0
        for when, b20 in dated:
            if when > dt:
                continue
            days = (dt - when).total_seconds() / 86400.0
            weighted = b20 * detraining_factor(days)
            if weighted > best:
                best = weighted
        if best <= 0:
            return None
        return best * 0.95

    est_points: List[dict] = []
    cur = start
    while cur <= end:
        est = _estimate_at(cur)
        if est is not None:
            est_points.append({"date": cur.date().isoformat(), "ftp": round(est, 1)})
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
        "t": t,
        "power": series["power"],
        "heartrate": series["heartrate"],
        "cadence": series["cadence"],
        "altitude": series["altitude"],
        "have": have,
        "points": len(t),
    }


def curve_points(user_id: int, state: Optional[TrainingState] = None) -> dict:
    """Power-duration data: measured MMP points + modeled CP/W' curve."""
    if state is None:
        state = build_state(user_id)
    measured = [
        {"t": int(t), "power": round(p, 1)} for t, p in sorted(state.mmp.items())
    ]
    model = []
    if state.cp is not None and state.wprime is not None:
        for t in sorted(set(list(MMP_DURATIONS) + [int(x["t"]) for x in measured])):
            if t > 0:
                model.append({"t": t, "power": round(state.cp + state.wprime / t, 1)})
    return {"measured": measured, "model": model, "cp": state.cp, "wprime": state.wprime}
