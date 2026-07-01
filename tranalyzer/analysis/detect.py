"""Plateau and overreach detection, plus a readiness score."""
from __future__ import annotations

from typing import Dict, List, Optional

from .state import TrainingState


def cp_plateau(
    cp_now: Optional[float],
    cp_prior: Optional[float],
    wprime_now: Optional[float],
    wprime_prior: Optional[float],
) -> bool:
    """Plateau if |ΔCP| < 5W AND |ΔW'| < 1000J (4wk vs prior 4wk)."""
    if None in (cp_now, cp_prior, wprime_now, wprime_prior):
        return False
    return abs(cp_now - cp_prior) < 5.0 and abs(wprime_now - wprime_prior) < 1000.0


def ctl_slope_per_week(load: List[Dict], weeks: int = 3) -> Optional[float]:
    """CTL slope in points/week over the trailing `weeks` weeks (min 3wk of data)."""
    if not load:
        return None
    days = weeks * 7
    window = load[-days:]
    if len(window) < days:
        return None
    start = float(window[0]["ctl"])
    end = float(window[-1]["ctl"])
    span_weeks = (len(window) - 1) / 7.0
    if span_weeks <= 0:
        return None
    return (end - start) / span_weeks


def ctl_stagnant(load: List[Dict]) -> bool:
    """True when CTL slope < 1 pt/wk over 3+ weeks."""
    slope = ctl_slope_per_week(load, weeks=3)
    if slope is None:
        return False
    return slope < 1.0


def mmp_not_improved(mmp_now: Dict[int, float], mmp_prior: Dict[int, float]) -> bool:
    """True when neither 5-min (300s) nor 20-min (1200s) MMP improved."""
    checked = False
    improved = False
    for d in (300, 1200):
        now = mmp_now.get(d)
        prior = mmp_prior.get(d)
        if now is not None and prior is not None:
            checked = True
            if now > prior:
                improved = True
    return checked and not improved


def tsb_sustained_below(load: List[Dict], threshold: float, days: int) -> bool:
    """True if the trailing `days` all have TSB < threshold."""
    if len(load) < days:
        return False
    window = load[-days:]
    return all(float(d["tsb"]) < threshold for d in window)


def detect_plateau(
    cp_now=None,
    cp_prior=None,
    wprime_now=None,
    wprime_prior=None,
    load: Optional[List[Dict]] = None,
    mmp_now: Optional[Dict[int, float]] = None,
    mmp_prior: Optional[Dict[int, float]] = None,
) -> List[str]:
    """Return a list of plateau reason strings (empty if none)."""
    reasons: List[str] = []
    if cp_plateau(cp_now, cp_prior, wprime_now, wprime_prior):
        reasons.append("CP and W' unchanged over the last 4 weeks")
    if load and ctl_stagnant(load):
        reasons.append("CTL slope below 1 pt/wk over 3+ weeks")
    if mmp_now and mmp_prior and mmp_not_improved(mmp_now, mmp_prior):
        reasons.append("5- and 20-min power not improved in 4 weeks")
    return reasons


def detect_overreach(
    load: Optional[List[Dict]] = None,
    tsb: Optional[float] = None,
    decoupling: Optional[float] = None,
) -> List[str]:
    """Return a list of overreach reason strings (empty if none)."""
    reasons: List[str] = []
    if load and tsb_sustained_below(load, -25.0, 7):
        reasons.append("TSB below -25 sustained for 7+ days")
    if decoupling is not None and tsb is not None:
        if decoupling > 8.0 and tsb < -15.0:
            reasons.append("High aerobic decoupling (>8%) with negative TSB")
    return reasons


def readiness_score(
    tsb: Optional[float],
    decoupling: Optional[float],
    plateau: bool,
    overreach: bool,
) -> float:
    """Readiness 0-100. Fresh & adapting -> high; fatigued/overreached -> low."""
    score = 100.0
    if tsb is not None:
        # Negative TSB (fatigue) reduces readiness; strongly positive is fine.
        if tsb < 0:
            score += tsb * 1.5  # e.g. TSB -20 -> -30
        else:
            score += min(tsb, 10) * 0.0  # freshness doesn't inflate above 100
    if decoupling is not None and decoupling > 5.0:
        score -= (decoupling - 5.0) * 2.0
    if overreach:
        score -= 25.0
    if plateau:
        score -= 10.0
    return max(0.0, min(100.0, score))


def evaluate(
    state: TrainingState,
    cp_prior: Optional[float] = None,
    wprime_prior: Optional[float] = None,
    load: Optional[List[Dict]] = None,
    mmp_prior: Optional[Dict[int, float]] = None,
) -> TrainingState:
    """Populate plateau/overreach/readiness/alerts on a TrainingState in place."""
    plateau_reasons = detect_plateau(
        cp_now=state.cp,
        cp_prior=cp_prior,
        wprime_now=state.wprime,
        wprime_prior=wprime_prior,
        load=load,
        mmp_now=state.mmp,
        mmp_prior=mmp_prior,
    )
    overreach_reasons = detect_overreach(
        load=load, tsb=state.tsb, decoupling=state.decoupling
    )
    state.plateau = bool(plateau_reasons)
    state.overreach = bool(overreach_reasons)
    state.readiness = readiness_score(
        state.tsb, state.decoupling, state.plateau, state.overreach
    )
    alerts: List[str] = []
    for r in overreach_reasons:
        alerts.append("Overreach: " + r)
    for r in plateau_reasons:
        alerts.append("Plateau: " + r)
    state.alerts = alerts
    return state
