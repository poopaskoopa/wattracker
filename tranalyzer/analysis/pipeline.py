"""Assemble a TrainingState from stored activities."""
from __future__ import annotations

import datetime as _dt
from typing import Dict, List, Optional, Tuple

from .. import db
from ..ingest.importer import current_ftp
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
        st = a.get("start_time")
        if not st:
            continue
        try:
            when = _dt.datetime.fromisoformat(st)
        except (ValueError, TypeError):
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


def load_series(user_id: int) -> List[dict]:
    """CTL/ATL/TSB daily series for charting."""
    return compute_load(daily_tss_series(db.daily_tss(user_id)))


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
