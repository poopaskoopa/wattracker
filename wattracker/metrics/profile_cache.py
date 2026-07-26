"""Cached access to the rider's measured capacities (``rider.for_user``).

``rider.for_user`` is not a cheap call. It builds a ``TrainingState`` (which
decompresses 90 days of power streams to compute the mean-maximal curve, plus
another 57 days for plateau detection) and then inflates up to a YEAR of
heart-rate streams to detect HRmax. That is fine once a night; it is not fine
on every ride preview, plan-detail request or ERG session start, all of which
now need the profile so their prescription matches the exported ``.zwo``.

Nothing here changes what a profile IS - it is the same object
``rider.for_user`` returns. This module only decides when it may be reused.

Invalidation follows exactly the trigger ``analysis.activity_cache`` uses: the
fingerprint of the user's activity set (count, max id, duplicate links), which
moves on any import, plus the handful of user settings the profile reads
directly. Both are single cheap aggregate queries, so a stale entry is
impossible rather than merely unlikely, and no caller has to remember to
invalidate anything. ``invalidate`` exists anyway for callers that mutate
settings and want the next read to be immediate rather than merely correct.

Known bound: HRmax detection looks back a fixed number of days from *now*, so a
long-lived process holds an entry whose window has drifted. The sweep re-warms
daily and a real deployment restarts far more often than that matters; the
alternative (a wall-clock component in the key) would defeat the cache
entirely.
"""
from __future__ import annotations

import datetime as _dt
import threading
from typing import Any, Dict, Optional, Tuple

from .. import db
from ..analysis import activity_cache
from ..analysis.state import TrainingState
from . import rider
from .rider import RiderMetrics

# Settings ``rider.for_user`` reads directly (weight and a manual HRmax) or
# that feed the FTP it is expressed against. Listed explicitly rather than
# fingerprinting the whole settings blob: unrelated keys (activities_dir, the
# Zwift id, export paths) change without touching a single derived capacity,
# and hashing them would throw the cache away for nothing.
_SETTINGS_KEYS = ("weight_kg", "hr_max", "ftp")

_lock = threading.Lock()
_cache: Dict[int, Tuple[tuple, RiderMetrics]] = {}


def _settings_fingerprint(user_id: int) -> tuple:
    try:
        settings = db.get_user_settings(user_id) or {}
    except Exception:
        return ()
    return tuple(settings.get(k) for k in _SETTINGS_KEYS)


def _fingerprint(user_id: int) -> tuple:
    return (activity_cache.fingerprint(user_id), _settings_fingerprint(user_id))


def for_user(
    user_id: int,
    state: Optional[TrainingState] = None,
    now: Optional[_dt.datetime] = None,
) -> RiderMetrics:
    """``rider.for_user``, memoized per user until their data changes.

    Passing an explicit ``state`` or ``now`` bypasses the cache in both
    directions - the result is specific to those arguments, so it is neither
    served from nor written to a cache keyed only on the user.
    """
    if state is not None or now is not None:
        return rider.for_user(user_id, state=state, now=now)
    fp = _fingerprint(user_id)
    with _lock:
        cached = _cache.get(user_id)
        if cached is not None and cached[0] == fp:
            return cached[1]
    profile = rider.for_user(user_id)  # expensive; computed outside the lock
    with _lock:
        _cache[user_id] = (fp, profile)
    return profile


def warm(user_id: int) -> None:
    """Populate the cache for a user (e.g. at the start of the daily sweep)."""
    for_user(user_id)


def invalidate(user_id: Optional[int] = None) -> None:
    """Drop cached profiles (all users when ``user_id`` is None)."""
    with _lock:
        if user_id is None:
            _cache.clear()
        else:
            _cache.pop(user_id, None)


def stats() -> Dict[str, Any]:
    """Cache occupancy, for diagnostics and tests."""
    with _lock:
        return {"users": len(_cache)}
