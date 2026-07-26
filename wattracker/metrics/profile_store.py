"""The rider's measured capacities, persisted rather than recomputed on read.

``rider.for_user`` is expensive - it decompresses ~90 days of power streams to
build the mean-maximal curve and up to a year of heart-rate streams to detect
HRmax - so it cannot run on a request. It is also not memoizable in process,
which is the mistake this module exists to correct: the profile's inputs
include WALL-CLOCK TIME. ``current_ftp`` falls back to an estimate that decays
with detraining and HRmax detection has a rolling lookback, so a rider who
takes a month off has a materially different profile with an identical activity
set. Measured on that scenario: a cache keyed on the activity set served
``ftp 269.7`` for 30 days while the true value fell to 238, prescribing 276 W
where 309 W was correct. Any key we invent has a staleness class we have not
thought of; the fix is not a better key but not caching a time-dependent
derivation at all.

So the profile is computed on the WRITE side - the daily maintenance sweep and
activity import, the two places that already iterate users and already know
something changed - and stored. Readers do one indexed row read: no
fingerprint, no lock, no cold miss, and no thundering herd, because there is no
compute-on-read path for concurrent callers to pile onto.

Staleness is then bounded and visible rather than silent: ``computed_at``
records exactly how old the basis of a prescription is, and a missing row means
"not computed yet" - which prescribes the population constants, precisely what
the app did before profiles existed - instead of quietly prescribing against a
rider who no longer exists.
"""
from __future__ import annotations

import logging
from typing import Optional

from .. import db
from . import rider
from .rider import RiderMetrics

log = logging.getLogger(__name__)

# Fields that are floats on RiderMetrics; SQLite hands them back as REAL/None.
_INT_FIELDS = ("n_hr_activities",)


def _to_metrics(row: Optional[dict]) -> RiderMetrics:
    """Rebuild a ``RiderMetrics`` from a stored row (all-None when absent)."""
    if not row:
        return RiderMetrics()
    values = {}
    for field in db.RIDER_PROFILE_FIELDS:
        value = row.get(field)
        if field in _INT_FIELDS:
            values[field] = int(value or 0)
        else:
            values[field] = value
    return RiderMetrics(**values)


def for_user(user_id: int) -> RiderMetrics:
    """The rider's stored measured capacities. Never raises, never computes.

    An unknown or not-yet-computed user gets an all-None profile, which builds
    exactly the population-constant prescription.
    """
    try:
        return _to_metrics(db.get_rider_profile(user_id))
    except Exception:  # noqa: BLE001 - a prescription must not fail on this
        log.warning("rider profile read failed for user %s", user_id,
                    exc_info=True)
        return RiderMetrics()


def computed_at(user_id: int) -> Optional[str]:
    """When this user's stored profile was computed, or None if it never was."""
    try:
        row = db.get_rider_profile(user_id)
    except Exception:  # noqa: BLE001
        return None
    return (row or {}).get("computed_at")


def refresh(user_id: int, state=None) -> RiderMetrics:
    """Recompute the rider's capacities from their data and store them.

    This is the expensive half, and belongs on the write side only (the daily
    sweep and activity import). ``state`` lets a caller that has already built
    a ``TrainingState`` avoid building a second one. Never raises: a failure
    leaves the previous snapshot in place, which is stale but coherent, and the
    next sweep tries again.
    """
    try:
        metrics = rider.for_user(user_id, state=state)
    except Exception:  # noqa: BLE001
        log.warning("rider profile computation failed for user %s", user_id,
                    exc_info=True)
        return for_user(user_id)
    try:
        db.save_rider_profile(
            user_id, {f: getattr(metrics, f, None) for f in db.RIDER_PROFILE_FIELDS}
        )
    except Exception:  # noqa: BLE001
        log.warning("rider profile write failed for user %s", user_id,
                    exc_info=True)
    return metrics
