"""One policy for every surface where a rider types an FTP.

Before this module the same logical field had three different policies:
``/setup/ftp`` and ``/setup/complete`` bounded it 1-1000 W, ``/profile/ftp``
1-2000 W, and ``/settings`` did not parse it at all - it stored ``-5``, ``0.64``
and the *string* ``'abc'`` into a column every FTP consumer treats as a number
(issue #64). A stored FTP is a scoring basis and TSS is quadratic in ``1/FTP``,
so ``ftp=1`` turns a normal hour into TSS 4,000,000; ``0.64`` reproduces issue
#60 exactly, through a form field.

Where the bounds come from
--------------------------
They are the SCORING bounds, ``FTP_ASSERTION_MIN_WATTS`` / ``_MAX_WATTS``
(20-700 W), deliberately reused. ``wattracker.metrics.power`` says an input
route should not be tempted to reuse them as a UI limit, and that warning is
about not letting a *scoring* rail double as the *only* input check - it is not
an argument for accepting numbers the scorer will then refuse. Admitting a value
the scoring layer will discard is the worst of both worlds: the rider's typed
FTP is stored, echoed back on the page as their setting, and silently scores
nothing. So the input window is exactly the window a basis can be used in.

That tightens every existing route rather than loosening any:

* ``/setup/*``   1-1000  ->  20-700
* ``/profile``   1-2000  ->  20-700
* ``/settings``  anything -> 20-700

1000 W and 2000 W were never reachable by a human: the best hour ever ridden is
~440 W, so 700 W is already ~60% above the strongest cyclist alive. A 4-digit
entry is a typo or a unit mix-up, and one stored forever scores every ride at a
fraction of its true load.

The confirmation band
---------------------
Between 20 W and ``FTP_PLAUSIBLE_MIN_WATTS`` (50 W) a value is *possible* but
overwhelmingly likely to be wrong. A rider mid-rehab genuinely at 40 W is the
case #60's provenance path exists to keep working, and a hard reject would lock
them out of their own app; but a rider who means 400 and types 40, or types
their W/kg, gets a training history that reads as untrained forever. So this
band is neither accepted nor refused: it is *challenged*. The route re-asks with
a confirmation the rider must tick, and a ticked confirmation is honoured
exactly as typed, all the way through scoring (see
``test_a_confirmed_sub_floor_ftp_is_still_honoured_end_to_end``).

Below 20 W nothing is confirmable: an FTP is a wattage a human holds for an
hour, and 4 W is not a claim about a body. That is the same line
``asserted_ftp`` draws.

This module is input policy only. It does not decide what may be *scored* -
``wattracker.metrics.power`` owns that, and keeps owning it, because a rail
every caller has to remember to invoke is not a rail.
"""
from __future__ import annotations

import math
from typing import NamedTuple, Optional

from .metrics.power import (
    FTP_ASSERTION_MAX_WATTS,
    FTP_ASSERTION_MIN_WATTS,
    FTP_PLAUSIBLE_MIN_WATTS,
)

# The range a typed FTP may take at all.
FTP_INPUT_MIN_WATTS = FTP_ASSERTION_MIN_WATTS
FTP_INPUT_MAX_WATTS = FTP_ASSERTION_MAX_WATTS
# Below this a value is accepted only with the rider's explicit confirmation.
FTP_CONFIRM_BELOW_WATTS = FTP_PLAUSIBLE_MIN_WATTS

RANGE_ERROR = (
    f"Enter FTP in watts, from {FTP_INPUT_MIN_WATTS:.0f} to "
    f"{FTP_INPUT_MAX_WATTS:.0f}."
)
CONFIRM_ERROR = (
    f"That is under {FTP_CONFIRM_BELOW_WATTS:.0f} W - far below any usual FTP, "
    "and the sort of number a typo or a W/kg value produces. If it is really "
    "yours, confirm it and it will be used exactly as entered."
)


class FTPInput(NamedTuple):
    """The verdict on one typed FTP.

    ``watts`` is set only when the value may be stored. Otherwise ``error``
    says why, and ``needs_confirmation`` distinguishes "impossible" from "we
    are asking you to confirm it" so a route can offer the confirmation instead
    of just refusing.
    """

    watts: Optional[float]
    error: Optional[str] = None
    needs_confirmation: bool = False


def parse_ftp_input(raw, confirmed: bool = False) -> FTPInput:
    """Validate a rider-typed FTP against the single input policy."""
    if isinstance(raw, bool) or raw is None:
        return FTPInput(None, RANGE_ERROR)
    if isinstance(raw, str):
        raw = raw.strip()
        if not raw:
            return FTPInput(None, RANGE_ERROR)
    try:
        value = float(raw)
    except (TypeError, ValueError, OverflowError):
        return FTPInput(None, RANGE_ERROR)
    if not math.isfinite(value):
        return FTPInput(None, RANGE_ERROR)
    if value < FTP_INPUT_MIN_WATTS or value > FTP_INPUT_MAX_WATTS:
        return FTPInput(None, RANGE_ERROR)
    if value < FTP_CONFIRM_BELOW_WATTS and not confirmed:
        return FTPInput(None, CONFIRM_ERROR, True)
    return FTPInput(value)
