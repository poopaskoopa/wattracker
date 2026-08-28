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
    RAMP_SAMPLE_RATE_MAX,
    RAMP_SAMPLE_RATE_MIN,
    RAMP_TEST_FTP_FRACTION,
    _clean_power,
    is_plausible_ftp,
    ramp_test_ftp_candidate,
    ramp_test_ftp_in_window,
)
from .prescribe.planner import RAMP_TEST_NAME, RAMP_TEST_SLOPE_FRACTION

#: ``ftp_history.source`` for an accepted ramp-test result.
SOURCE = "ramp_test"

#: How far the cross-check may sit from the structural result before the
#: rider is told about it. 5% of ~210 W is ~10 W, which is one step of the
#: ramp - a difference of one step is the two methods disagreeing about which
#: minute was the best one, and that is worth showing.
CROSS_CHECK_TOLERANCE = 0.05

#: How far a stream's sample rate may sit from 1 Hz before the shape detector,
#: which counts in 60-SAMPLE blocks, is no longer reading minutes.
CROSS_CHECK_RATE_TOLERANCE = 0.02

# Cross-check outcomes.
AGREES = "agrees"
DIFFERS = "differs"
OUT_OF_RANGE = "out_of_range"
UNRECOGNIZED = "unrecognized"
NOT_ONE_HZ = "not_one_hz"


def is_declared_ftp_test(activity) -> bool:
    """Was this stored activity the rider's declared FTP test?

    Matched on the name the session gave the ride. This is a ROUTING question,
    not a trust boundary: it decides which rides the passive estimator skips
    and which the accept route will compute from. It is not load-bearing
    against a hostile name, and nothing here should be relied on as if it
    were - the actual gate on writing an FTP is the rider pressing accept,
    plus ``is_plausible_ftp``. In practice the name cannot be chosen freely
    anyway: uploads are forced onto their ``.fit`` filename and in-app rides
    are named by the planner's builders.
    """
    if isinstance(activity, str):
        name = activity
    else:
        name = str((activity or {}).get("filename") or "")
    return name.endswith(RAMP_TEST_NAME)


def sample_rate(power, duration_s) -> float:
    """Samples per workout second for a recorded ride, or 0.0 if unknowable.

    ``RideController`` appends exactly one sample per tick but advances its
    clock by that tick's ``dt``, so "sample index == workout second" holds
    only while dt is one second. It is the live loop's ``minimum_dt=1.0`` that
    makes that true today, which is a fact about one call site rather than a
    property of the data - so the ratio is measured from what was stored (the
    ride's own duration and its own sample count) instead of assumed.
    """
    try:
        seconds = float(duration_s)
    except (TypeError, ValueError):
        return 0.0
    count = int(_clean_power(power).size)
    if not (seconds > 0.0) or count <= 0:
        return 0.0
    return count / seconds


def evaluate(
    power,
    window,
    prescribed_ftp: float,
    duration_s,
    *,
    path: Optional[str] = None,
) -> dict:
    """Compute a ramp-test result from a recorded stream and a KNOWN window.

    ``window`` is the (start, end) second pair the prescribed session puts the
    stepped ramp at, in WORKOUT seconds. ``duration_s`` is the ride's own
    recorded duration, which together with the sample count says how those
    seconds map onto stream indices; it is required rather than defaulted
    because assuming 1 Hz is exactly the bug this guards (a stream at 2 Hz
    read as 1 Hz measures the first half of the ramp and under-reports by
    ~40%, silently). ``prescribed_ftp`` is the FTP the ramp was built against,
    used only to say whether the cross-check could have run at all.

    The returned ``offer`` flag is the gate on ever showing the number to the
    rider: an implausible value, or one whose window cannot be located, is a
    failed measurement rather than a low FTP and must not be put in front of
    somebody to accept.
    """
    start, end = (float(window[0]), float(window[1])) if window else (0.0, 0.0)
    rate = sample_rate(power, duration_s)
    rate_usable = RAMP_SAMPLE_RATE_MIN <= rate <= RAMP_SAMPLE_RATE_MAX

    ftp = (
        ramp_test_ftp_in_window(power, start, end, rate) if rate_usable else 0.0
    )
    best_minute = ftp / RAMP_TEST_FTP_FRACTION if ftp else 0.0
    # Every prescribed step was ridden: the rider never failed, so the ramp
    # ran out before they did.
    completed = bool(rate_usable and _clean_power(power).size >= round(end * rate))

    slope_w = RAMP_TEST_SLOPE_FRACTION * float(prescribed_ftp or 0.0)
    slope_in_band = (
        RAMP_DETECTOR_MIN_SLOPE_W <= slope_w <= RAMP_DETECTOR_MAX_SLOPE_W
    )
    one_hz = abs(rate - 1.0) <= CROSS_CHECK_RATE_TOLERANCE
    # The detector counts fixed 60-SAMPLE blocks, so away from 1 Hz it is not
    # looking at minutes and its answer is not comparable to ours.
    cross = ramp_test_ftp_candidate(power) if one_hz else 0.0

    if not one_hz:
        status = NOT_ONE_HZ
    elif cross <= 0.0:
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
        "window": [round(start, 1), round(end, 1)],
        "prescribed_ftp": round(float(prescribed_ftp or 0.0), 1),
        "sample_rate": round(rate, 3),
        # True when the rider rode every step there was. The number is then a
        # FLOOR on their FTP, not a measurement of it: a ramp test measures
        # the step the rider could not hold, and there wasn't one.
        "completed_ramp": completed,
        "cross_check_ftp": round(cross, 1) if cross > 0.0 else None,
        "cross_check_status": status,
        "cross_check_slope_w_per_min": round(slope_w, 1),
        "disagreement": status == DIFFERS,
        "offer": offer,
        "message": _message(ftp, cross, status, slope_w, offer, completed),
    }


def _message(ftp: float, cross: float, status: str, slope_w: float,
             offer: bool, completed: bool) -> str:
    if not offer:
        if status == NOT_ONE_HZ:
            return (
                "No usable result: this recording is not one sample a second, "
                "so the ramp cannot be located in it reliably. Nothing has "
                "been saved."
            )
        return (
            "No usable result: the test did not record a full minute of "
            "power inside the ramp, or the number it produced is not a "
            "wattage a human holds for an hour. Nothing has been saved."
        )
    if completed:
        # Say this first and say it plainly. A rider who never failed did not
        # take a ramp test, they rode the whole ramp, and reporting a
        # cross-check "agreement" about a number that is only a lower bound
        # would read as confirmation of a measurement that did not happen.
        return (
            f"You completed every step, so the test never found your limit. "
            f"{ftp:.0f} W is a FLOOR on your FTP, not a measurement of it - "
            "your real FTP is at least this and probably higher. Accept it to "
            "raise your training basis to what you have proven, then test "
            "again from the new number."
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
    if status in (UNRECOGNIZED, NOT_ONE_HZ):
        return (
            "Cross-check unavailable: the recording does not have the shape "
            "of a completed ramp - most likely the test ended early. The "
            "result below is still measured from your best minute of the ramp."
        )
    return (
        f"Your best minute of the ramp was {ftp / RAMP_TEST_FTP_FRACTION:.0f} W. "
        f"An independent check of the recording agrees at {cross:.0f} W."
    )
