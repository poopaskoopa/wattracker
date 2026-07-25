"""Datetime helpers.

FIT files carry timezone-aware (UTC) timestamps, while the app computes
windows from the naive local ``datetime.now()``. Mixing the two raises
"can't compare offset-naive and offset-aware datetimes". Everything that
parses or stores an activity timestamp funnels through here so comparisons
are always naive-vs-naive.
"""
from __future__ import annotations

import datetime as _dt
from typing import List, Optional, Tuple


def to_naive(dt: Optional[_dt.datetime]) -> Optional[_dt.datetime]:
    """Drop tzinfo from a datetime (no-op if already naive or None)."""
    if dt is not None and dt.tzinfo is not None:
        return dt.replace(tzinfo=None)
    return dt


def parse_naive(value: Optional[str]) -> Optional[_dt.datetime]:
    """Parse an ISO-8601 string to a naive datetime, or None if unparseable."""
    if not value:
        return None
    try:
        return to_naive(_dt.datetime.fromisoformat(value))
    except (ValueError, TypeError):
        return None


def utc_now() -> _dt.datetime:
    """Current time as a naive UTC datetime.

    Naive UTC is the storage format for activity timestamps: FIT files carry
    UTC, so an in-app ride recorded with the local wall clock would otherwise
    land hours away from the same ride's imported .fit.
    """
    return to_naive(_dt.datetime.now(_dt.timezone.utc))


def utc_to_local(dt: Optional[_dt.datetime]) -> Optional[_dt.datetime]:
    """Convert a naive-UTC datetime to naive local time (for display/naming)."""
    if dt is None:
        return None
    return to_naive(dt.replace(tzinfo=_dt.timezone.utc).astimezone())


def local_offset_seconds(dt: _dt.datetime) -> int:
    """The system local UTC offset in effect at a given naive local datetime."""
    offset = dt.astimezone().utcoffset()
    return int(offset.total_seconds()) if offset else 0


def local_offset_ranges(
    start: _dt.datetime, end: _dt.datetime
) -> List[Tuple[_dt.datetime, int]]:
    """Contiguous local-time ranges of constant UTC offset over [start, end].

    Returns ``(range_start, offset_seconds)`` ascending, the first entry
    starting at ``start``. Boundaries (DST transitions) are resolved to the
    second, so a historical timestamp is converted with the offset that was
    actually in force then rather than today's.
    """
    step = _dt.timedelta(days=7)
    second = _dt.timedelta(seconds=1)
    ranges: List[Tuple[_dt.datetime, int]] = [(start, local_offset_seconds(start))]
    cur = start
    while cur < end:
        nxt = min(cur + step, end)
        offset = local_offset_seconds(nxt)
        if offset != ranges[-1][1]:
            lo, hi = cur, nxt
            while hi - lo > second:
                mid = lo + (hi - lo) / 2
                if local_offset_seconds(mid) == ranges[-1][1]:
                    lo = mid
                else:
                    hi = mid
            ranges.append((hi.replace(microsecond=0), offset))
        cur = nxt
    return ranges
