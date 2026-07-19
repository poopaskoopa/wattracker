"""In-process cache of per-user activity digests.

Decompressing every stored stream BLOB on each dashboard request is the single
biggest cost in the analysis pipeline (a heavy user has ~850 zlib-compressed
activities, ~1.8s just to inflate + scan them). None of the derived quantities
below change unless the user's activity set changes, so we cache them keyed by a
cheap fingerprint of that set.

Fingerprint = (activity count, max activity id). Activities are inserted with
``INSERT OR IGNORE`` and their streams are never updated in place (see
``db.insert_activity``), so any new import strictly increases the count and the
max id - either component moving invalidates the cache. This needs no schema
change: the cache lives entirely in process memory.

The digest holds only small, activity-static artifacts (dates, per-effort best
20-minute powers, a prefix-sum table, one decoupling value) - never the raw
streams - so its memory footprint is tiny regardless of ride length.
"""
from __future__ import annotations

import bisect
import datetime as _dt
import threading
from typing import Dict, List, Optional, Tuple

from .. import db
from ..timeutil import parse_naive
from ..metrics.power import best_20min_power, FTP_DECAY_GRACE_DAYS
from ..metrics.decoupling import aerobic_decoupling


class ActivityDigest:
    """Activity-static analysis inputs for one user.

    Attributes:
      activity_days:  sorted timestamps of every dated ride (power-less rides
                      count too - time on the bike maintains fitness).
      effort_days / effort_b20:  parallel arrays, sorted by time, of the rides
                      that carry a positive best-20-minute power.
      effort_i:       for each effort, ``bisect_right(activity_days, when)`` -
                      the first activity-day index strictly after the effort.
      prefix:         prefix sums of ``max(0, gap - grace)`` over consecutive
                      gaps between ``activity_days`` (index k covers gaps
                      1..k). Lets ``_idle_active_days`` be evaluated in O(1) per
                      (effort, anchor) pair instead of rescanning the calendar.
      decoupling:     aerobic decoupling of the most recent long steady effort,
                      or None (identical to the reversed-scan build_state did).
    """

    __slots__ = (
        "activity_days",
        "effort_days",
        "effort_b20",
        "effort_i",
        "prefix",
        "decoupling",
    )

    def __init__(
        self,
        activity_days: List[_dt.datetime],
        effort_days: List[_dt.datetime],
        effort_b20: List[float],
        effort_i: List[int],
        prefix: List[float],
        decoupling: Optional[float],
    ) -> None:
        self.activity_days = activity_days
        self.effort_days = effort_days
        self.effort_b20 = effort_b20
        self.effort_i = effort_i
        self.prefix = prefix
        self.decoupling = decoupling


_lock = threading.Lock()
_cache: Dict[int, Tuple[Tuple[int, int], ActivityDigest]] = {}


def _fingerprint(user_id: int) -> Tuple[int, int]:
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS c, COALESCE(MAX(id), 0) AS m "
            "FROM activities WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        return (int(row["c"]), int(row["m"]))
    finally:
        conn.close()


def _build(user_id: int) -> ActivityDigest:
    activities = db.full_activities(user_id)

    activity_days: List[_dt.datetime] = []
    efforts: List[Tuple[_dt.datetime, float]] = []
    for a in activities:
        when = parse_naive(a.get("start_time"))
        if when is None:
            continue
        activity_days.append(when)
        power = (a.get("streams") or {}).get("power") or []
        if power:
            b20 = best_20min_power(power)
            if b20 > 0:
                efforts.append((when, b20))
    activity_days.sort()
    efforts.sort(key=lambda x: x[0])

    # prefix[k] = sum over gaps 1..k of max(0, gap_days - grace).
    prefix: List[float] = [0.0]
    for m in range(1, len(activity_days)):
        gap = (activity_days[m] - activity_days[m - 1]).total_seconds() / 86400.0
        prefix.append(prefix[-1] + (gap - FTP_DECAY_GRACE_DAYS
                                    if gap > FTP_DECAY_GRACE_DAYS else 0.0))

    effort_days = [w for w, _ in efforts]
    effort_b20 = [b for _, b in efforts]
    effort_i = [bisect.bisect_right(activity_days, w) for w in effort_days]

    # Aerobic decoupling from the most recent long steady effort (>45min).
    # full_activities is ordered by start_time ASC, so reversed() is newest
    # first - identical to the scan build_state used to run inline.
    decoupling: Optional[float] = None
    for a in reversed(activities):
        streams = a.get("streams") or {}
        d = aerobic_decoupling(streams.get("power") or [], streams.get("heartrate") or [])
        if d is not None:
            decoupling = d
            break

    return ActivityDigest(
        activity_days, effort_days, effort_b20, effort_i, prefix, decoupling
    )


def get_digest(user_id: int) -> ActivityDigest:
    """Return the cached digest for ``user_id``, rebuilding if activities changed."""
    fp = _fingerprint(user_id)
    with _lock:
        cached = _cache.get(user_id)
        if cached is not None and cached[0] == fp:
            return cached[1]
    digest = _build(user_id)  # expensive; done outside the lock
    with _lock:
        _cache[user_id] = (fp, digest)
    return digest


def warm(user_id: int) -> None:
    """Populate the cache for a user (e.g. after an import / daily sweep)."""
    get_digest(user_id)


def invalidate(user_id: Optional[int] = None) -> None:
    """Drop cached digests (all users when ``user_id`` is None)."""
    with _lock:
        if user_id is None:
            _cache.clear()
        else:
            _cache.pop(user_id, None)
