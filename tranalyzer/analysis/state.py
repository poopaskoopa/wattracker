"""Coggan power zones and the aggregate TrainingState object."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

# Coggan 7 zones as (%FTP low, %FTP high). Boundaries per spec.
# Z1 <55, Z2 56-75, Z3 76-90, Z4 91-105, Z5 106-120, Z6 121-150, Z7 >150.
ZONES: Dict[str, Tuple[float, float]] = {
    "Z1": (0.0, 55.0),
    "Z2": (56.0, 75.0),
    "Z3": (76.0, 90.0),
    "Z4": (91.0, 105.0),
    "Z5": (106.0, 120.0),
    "Z6": (121.0, 150.0),
    "Z7": (150.0, float("inf")),
}

# Sweet spot band (%FTP).
SWEET_SPOT: Tuple[float, float] = (88.0, 94.0)


def zone_for_pct(pct_ftp: float) -> str:
    """Return the Coggan zone label for a given %FTP value."""
    if pct_ftp < 55.0:
        return "Z1"
    if pct_ftp <= 75.0:
        return "Z2"
    if pct_ftp <= 90.0:
        return "Z3"
    if pct_ftp <= 105.0:
        return "Z4"
    if pct_ftp <= 120.0:
        return "Z5"
    if pct_ftp <= 150.0:
        return "Z6"
    return "Z7"


def zone_bounds_watts(zone: str, ftp: float) -> Tuple[float, float]:
    """Absolute watt bounds for a zone at the given FTP."""
    lo, hi = ZONES[zone]
    hi_w = ftp * hi / 100.0 if hi != float("inf") else float("inf")
    return ftp * lo / 100.0, hi_w


@dataclass
class TrainingState:
    """Snapshot of the athlete's current training state."""

    ftp: float = 0.0
    cp: Optional[float] = None
    wprime: Optional[float] = None
    ctl: float = 0.0
    atl: float = 0.0
    tsb: float = 0.0
    decoupling: Optional[float] = None
    readiness: float = 100.0
    plateau: bool = False
    overreach: bool = False
    alerts: List[str] = field(default_factory=list)
    # Latest MMP samples (duration -> watts), used by detectors.
    mmp: Dict[int, float] = field(default_factory=dict)

    def to_dict(self) -> Dict:
        return {
            "ftp": round(self.ftp, 1),
            "cp": round(self.cp, 1) if self.cp is not None else None,
            "wprime": round(self.wprime, 1) if self.wprime is not None else None,
            "ctl": round(self.ctl, 2),
            "atl": round(self.atl, 2),
            "tsb": round(self.tsb, 2),
            "decoupling": round(self.decoupling, 2)
            if self.decoupling is not None
            else None,
            "readiness": round(self.readiness, 1),
            "plateau": self.plateau,
            "overreach": self.overreach,
            "alerts": list(self.alerts),
            "mmp": {str(k): round(v, 1) for k, v in self.mmp.items()},
        }
