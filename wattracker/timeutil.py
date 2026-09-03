"""Datetime helpers.

FIT files carry timezone-aware (UTC) timestamps. Mixing those with a naive
local ``datetime.now()`` raises "can't compare offset-naive and offset-aware
datetimes", and comparing naive local against naive UTC silently skews every
window by the local offset. So the app is UTC end to end: everything that
parses, stores or compares an activity timestamp funnels through here, and
"now"/"today" come from ``utc_now`` / ``utc_today``.
"""
from __future__ import annotations

import datetime as _dt
from typing import List, Optional, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones


DEFAULT_TIMEZONE = "UTC"


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


def utc_today() -> _dt.date:
    """Today's date in UTC.

    The calendar counterpart of ``utc_now``: plan dates, completion dates and
    "is this workout in the future" all compare against UTC-stored activity
    timestamps, so the reference day has to be the UTC one.
    """
    return utc_now().date()


def valid_timezone(value: object) -> bool:
    """Whether *value* is a usable IANA timezone key.

    ZoneInfo also rejects absolute paths and keys containing path traversal.
    The length cap avoids doing filesystem/package lookups for an unbounded
    form value.
    """
    if not isinstance(value, str):
        return False
    key = value.strip()
    if not key or len(key) > 255:
        return False
    try:
        ZoneInfo(key)
    except (
        ZoneInfoNotFoundError,
        ValueError,
        TypeError,
        OSError,
        UnicodeError,
    ):
        return False
    return True


def to_user_timezone(
    dt: _dt.datetime, timezone_name: object
) -> _dt.datetime:
    """Represent a UTC instant in a user's IANA timezone.

    Naive datetimes are interpreted as UTC because that is wattracker's
    storage/runtime convention. Invalid or missing saved values safely retain
    UTC behavior.
    """
    key = (
        timezone_name.strip()
        if isinstance(timezone_name, str) and timezone_name.strip()
        else DEFAULT_TIMEZONE
    )
    try:
        zone = ZoneInfo(key) if len(key) <= 255 else ZoneInfo(DEFAULT_TIMEZONE)
    except (
        ZoneInfoNotFoundError,
        ValueError,
        TypeError,
        OSError,
        UnicodeError,
    ):
        zone = ZoneInfo(DEFAULT_TIMEZONE)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_dt.timezone.utc)
    else:
        dt = dt.astimezone(_dt.timezone.utc)
    return dt.astimezone(zone)


def local_today(
    timezone_name: object, now: Optional[_dt.datetime] = None
) -> _dt.date:
    """The user's local calendar date at a UTC instant."""
    return to_user_timezone(now or utc_now(), timezone_name).date()


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


# Zone names that are canonical "Area/Location" IANA keys. available_timezones()
# also returns ~200 legacy aliases (``US/Eastern``, ``EST5EDT``, ``Etc/GMT+5``,
# bare ``Japan``); they resolve fine but triple the length of a picker without
# naming a single zone the canonical keys below do not already cover, and
# ``Etc/GMT+5`` reads as UTC+5 while actually meaning UTC-5. So the picker
# offers canonical keys only. Anything already stored - alias included - is
# still valid and is added back as an option by the caller (see
# ``timezone_choices``), so no saved value is ever silently rewritten.
_PICKER_AREAS = (
    "Africa",
    "America",
    "Antarctica",
    "Arctic",
    "Asia",
    "Atlantic",
    "Australia",
    "Europe",
    "Indian",
    "Pacific",
)


def timezone_offset_label(timezone_name: str, now: _dt.datetime) -> str:
    """The UTC offset *timezone_name* is on at instant ``now``, as ``UTC-04:00``.

    DISPLAY ONLY, AND ONLY FOR "NOW". What gets stored is the IANA zone name,
    never this offset: ZoneInfo applies the right offset per instant, so a
    January ride and a July ride each bucket with the offset actually in force
    then. This string is a hint that helps a rider recognise their zone in the
    picker, and it is therefore correct only for the instant passed in. Compute
    it per request; caching it in a module-level constant would freeze the
    label at import time and show every rider the wrong offset from the next
    DST transition onwards.
    """
    offset = to_user_timezone(now, timezone_name).utcoffset() or _dt.timedelta(0)
    total_minutes = int(offset.total_seconds() // 60)
    sign = "-" if total_minutes < 0 else "+"
    hours, minutes = divmod(abs(total_minutes), 60)
    return f"UTC{sign}{hours:02d}:{minutes:02d}"


def timezone_choices(
    now: Optional[_dt.datetime] = None, extra: object = None
) -> List[Tuple[str, str]]:
    """Picker options as ``(zone_name, label)``, sorted by current offset.

    ``extra`` is a value already stored for the user: if it is a usable zone
    that the canonical list omits (a legacy alias, or a key this Python's tzdata
    lacks), it is included so saving the form cannot rewrite it.

    Sorted by offset then name because a rider looks up their zone by "I am five
    hours behind London", not alphabetically; the offsets are the ones in force
    at ``now``, so ordering shifts across a DST transition exactly as the labels
    do.
    """
    instant = now or utc_now()
    names = {
        name
        for name in available_timezones()
        if name.split("/", 1)[0] in _PICKER_AREAS
    }
    names.add(DEFAULT_TIMEZONE)
    if isinstance(extra, str) and extra.strip() and valid_timezone(extra):
        names.add(extra.strip())
    decorated = []
    for name in names:
        offset = to_user_timezone(instant, name).utcoffset() or _dt.timedelta(0)
        decorated.append((offset, name))
    decorated.sort(key=lambda item: (item[0], item[1]))
    return [
        (name, f"({timezone_offset_label(name, instant)}) {name}")
        for _offset, name in decorated
    ]
