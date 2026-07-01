"""Training load model: CTL / ATL / TSB from a daily TSS series."""
from __future__ import annotations

import datetime as _dt
from typing import Dict, List, Tuple

CTL_TAU = 42.0
ATL_TAU = 7.0


def daily_tss_series(
    dated_tss: Dict[_dt.date, float] | List[Tuple[_dt.date, float]],
) -> List[Tuple[_dt.date, float]]:
    """Build a gap-free daily TSS series.

    Accepts a mapping (or list of pairs) of calendar date -> TSS (summed per
    day). Fills every missing calendar day between the first and last date with
    0 TSS. Returns a chronologically ordered list of (date, tss) pairs.
    """
    if isinstance(dated_tss, dict):
        items = list(dated_tss.items())
    else:
        items = list(dated_tss)

    if not items:
        return []

    # Sum duplicate dates.
    summed: Dict[_dt.date, float] = {}
    for d, tss in items:
        summed[d] = summed.get(d, 0.0) + float(tss)

    start = min(summed)
    end = max(summed)
    series: List[Tuple[_dt.date, float]] = []
    day = start
    while day <= end:
        series.append((day, summed.get(day, 0.0)))
        day += _dt.timedelta(days=1)
    return series


def compute_load(
    series: List[Tuple[_dt.date, float]],
    ctl_tau: float = CTL_TAU,
    atl_tau: float = ATL_TAU,
) -> List[Dict[str, float]]:
    """Compute CTL/ATL/TSB via EWMA over a daily TSS series (seeded from 0).

    CTL[d] = CTL[d-1]*(1-1/42) + TSS[d]*(1/42)
    ATL[d] = ATL[d-1]*(1-1/7)  + TSS[d]*(1/7)
    TSB[d] = CTL[d] - ATL[d]

    Returns a list of dicts: {date, tss, ctl, atl, tsb}.
    """
    out: List[Dict[str, float]] = []
    ctl = 0.0
    atl = 0.0
    ctl_k = 1.0 / ctl_tau
    atl_k = 1.0 / atl_tau
    for day, tss in series:
        ctl = ctl * (1.0 - ctl_k) + tss * ctl_k
        atl = atl * (1.0 - atl_k) + tss * atl_k
        tsb = ctl - atl
        out.append(
            {
                "date": day.isoformat(),
                "tss": round(float(tss), 2),
                "ctl": round(ctl, 2),
                "atl": round(atl, 2),
                "tsb": round(tsb, 2),
            }
        )
    return out
