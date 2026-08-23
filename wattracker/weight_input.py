"""One policy for every surface where a rider types a body weight.

Body weight is a display value, not a scoring basis. It labels W/kg and
rescales weight-dependent numbers; it never feeds the load math the way FTP
does, and ``wattracker.metrics.power`` keeps owning that rail. A wrong weight
therefore mislabels rather than corrupts: a rider at 70 kg stored as 75 still
gets their TSS, their zones, their history - the only thing that is off is
the W/kg attached to them. The policy for a value with that failure mode is a
single hard range, not a layered one.

So the window is 20-300 kg - the range onboarding has always used - with no
confirmation band. There is no band to confirm into because there is no
scoring consequence to confirm against: below 20 kg is not a rider, above
300 kg is a typo or a unit mix-up, and both are refused exactly like
``parse_ftp_input`` refuses values outside its window.

This module is input policy only. Date validity (ISO-8601, not in the
future) is the routes' job, since it depends on the user's timezone.
"""
from __future__ import annotations

import math
from typing import NamedTuple, Optional

WEIGHT_INPUT_MIN_KG = 20.0
WEIGHT_INPUT_MAX_KG = 300.0

RANGE_ERROR = (
    f"Enter weight in kilograms, from {WEIGHT_INPUT_MIN_KG:.0f} to "
    f"{WEIGHT_INPUT_MAX_KG:.0f}."
)


class WeightInput(NamedTuple):
    """The verdict on one typed body weight.

    ``kg`` is set only when the value may be stored; otherwise ``error`` says
    why. ``needs_confirmation`` is always False - there is no confirmation
    band (see the module docstring) - but it is part of the shape so routes
    can treat ``WeightInput`` and ``FTPInput`` identically.
    """

    kg: Optional[float]
    error: Optional[str] = None
    needs_confirmation: bool = False


def parse_weight_input(raw, confirmed: bool = False) -> WeightInput:
    """Validate a rider-typed body weight against the single input policy."""
    if isinstance(raw, bool) or raw is None:
        return WeightInput(None, RANGE_ERROR)
    if isinstance(raw, str):
        raw = raw.strip()
        if not raw:
            return WeightInput(None, RANGE_ERROR)
    try:
        value = float(raw)
    except (TypeError, ValueError, OverflowError):
        return WeightInput(None, RANGE_ERROR)
    if not math.isfinite(value):
        return WeightInput(None, RANGE_ERROR)
    if value < WEIGHT_INPUT_MIN_KG or value > WEIGHT_INPUT_MAX_KG:
        return WeightInput(None, RANGE_ERROR)
    return WeightInput(value)
