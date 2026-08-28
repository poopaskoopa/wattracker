"""The declared FTP test: measure the result, and offer it to the rider.

The rider declares the test by picking "Ramp Test" in the Ride section, so
identification needs no inference at all - which is the point. Shape
recognition (:func:`wattracker.metrics.power.ramp_test_ftp_candidate`) exists
to answer "was this a ramp, and where?" about a file that arrived with no
context, and it is documented as still accepting a progressive climb whose
recording ends at the summit. That is exactly why it must not be the
identifier. Here it runs as an independent CROSS-CHECK on a number computed
structurally, and a material disagreement is shown to the rider rather than
silently resolved: this is the one value that rewrites their whole training
basis.

Nothing here writes anything. The result is *offered*; the write happens only
when a human accepts it, which is what makes ``ramp_test`` admissible in
``ftp_provenance.ASSERTED_FTP_SOURCES``.
"""
from __future__ import annotations

from typing import Optional, Sequence

from .metrics.power import (
    RAMP_DETECTOR_MAX_SLOPE_W,
    RAMP_DETECTOR_MIN_SLOPE_W,
    RAMP_TEST_FTP_FRACTION,
    is_plausible_ftp,
    ramp_test_ftp_candidate,
    ramp_test_ftp_in_window,
)
from .prescribe.planner import RAMP_TEST_SLOPE_FRACTION

#: ``ftp_history.source`` for an accepted ramp-test result.
SOURCE = "ramp_test"

#: How far the cross-check may sit from the structural result before the
#: rider is told about it. 5% of ~210 W is ~10 W, which is one step of the
#: ramp - a difference of one step is the two methods disagreeing about which
#: minute was the best one, and that is worth showing.
CROSS_CHECK_TOLERANCE = 0.05

# Cross-check outcomes.
AGREES = "agrees"
DIFFERS = "differs"
OUT_OF_RANGE = "out_of_range"
UNRECOGNIZED = "unrecognized"


def evaluate(
    power: Sequence[float],
    window,
    prescribed_ftp: float,
    *,
    path: Optional[str] = None,
) -> dict:
    """Compute a ramp-test result from a recorded stream and a KNOWN window.

    ``window`` is the (start, end) second pair the prescribed session puts the
    stepped ramp at. ``prescribed_ftp`` is the FTP the ramp was built against,
    used only to say whether the cross-check could have run at all.

    The returned ``offer`` flag is the gate on ever showing the number to the
    rider: an implausible value is a failed measurement, not a low FTP, and
    must not be put in front of somebody to accept.
    """
    start, end = (int(window[0]), int(window[1])) if window else (0, 0)
    ftp = ramp_test_ftp_in_window(power, start, end)
    best_minute = ftp / RAMP_TEST_FTP_FRACTION if ftp else 0.0

    slope_w = RAMP_TEST_SLOPE_FRACTION * float(prescribed_ftp or 0.0)
    slope_in_band = (
        RAMP_DETECTOR_MIN_SLOPE_W <= slope_w <= RAMP_DETECTOR_MAX_SLOPE_W
    )
    cross = ramp_test_ftp_candidate(power)

    if cross <= 0.0:
        # A 0.0 is the detector declining to recognize a ramp, never a rival
        # measurement of 0 W. Below the slope band it CANNOT recognize this
        # one however clean the ride was - a 120 W rider at 5% of FTP a minute
        # rises 6 W a minute - so saying so is the honest report. Either way
        # it is the cross-check being unavailable, not a discrepancy.
        status = OUT_OF_RANGE if not slope_in_band else UNRECOGNIZED
    elif ftp > 0 and abs(cross - ftp) > CROSS_CHECK_TOLERANCE * ftp:
        status = DIFFERS
    else:
        status = AGREES

    offer = bool(ftp > 0) and is_plausible_ftp(ftp, path=path)
    return {
        "ftp": round(ftp, 1),
        "best_minute_watts": round(best_minute, 1),
        "window": [start, end],
        "prescribed_ftp": round(float(prescribed_ftp or 0.0), 1),
        "cross_check_ftp": round(cross, 1) if cross > 0.0 else None,
        "cross_check_status": status,
        "cross_check_slope_w_per_min": round(slope_w, 1),
        "disagreement": status == DIFFERS,
        "offer": offer,
        "message": _message(ftp, cross, status, slope_w, offer),
    }


def _message(ftp: float, cross: float, status: str, slope_w: float,
             offer: bool) -> str:
    if not offer:
        return (
            "No usable result: the test did not record a full minute of "
            "power inside the ramp, or the number it produced is not a "
            "wattage a human holds for an hour. Nothing has been saved."
        )
    if status == DIFFERS:
        return (
            f"Your best minute gives {ftp:.0f} W, but an independent check of "
            f"the recording reads it as {cross:.0f} W. The two disagree by "
            "more than one step of the ramp, so look at the ride before you "
            "accept this."
        )
    if status == OUT_OF_RANGE:
        return (
            f"Cross-check unavailable: at your current FTP the ramp rises "
            f"{slope_w:.0f} W a minute, below the "
            f"{RAMP_DETECTOR_MIN_SLOPE_W:.0f}-"
            f"{RAMP_DETECTOR_MAX_SLOPE_W:.0f} W/min a shape detector can "
            "recognize. That is the check being out of range, not a problem "
            "with your test."
        )
    if status == UNRECOGNIZED:
        return (
            "Cross-check unavailable: the recording does not have the shape "
            "of a completed ramp - most likely the test ended early. The "
            "result below is still measured from your best minute of the ramp."
        )
    return (
        f"Your best minute of the ramp was {ftp / RAMP_TEST_FTP_FRACTION:.0f} W. "
        f"An independent check of the recording agrees at {cross:.0f} W."
    )
