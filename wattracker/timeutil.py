"""Datetime helpers.

FIT files carry timezone-aware (UTC) timestamps, while the app computes
windows from the naive local ``datetime.now()``. Mixing the two raises
"can't compare offset-naive and offset-aware datetimes". Everything that
parses or stores an activity timestamp funnels through here so comparisons
are always naive-vs-naive.
"""
from __future__ import annotations

import datetime as _dt
from typing import Optional


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
