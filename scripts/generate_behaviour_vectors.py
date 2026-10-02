#!/usr/bin/env python3
"""Regenerate the shared behaviour vectors for the native mobile clients.

``canonical_request_v1.json`` pinned a *data shape* that Python, Swift and
Kotlin all have to reproduce.  The files written here pin *behaviour rules*
the same way, because the iOS and Android clients are two hand transcriptions
of rules that live somewhere else, and hand transcriptions drift:

* ``calendar_grid_v1.json`` -- the Monday-to-Sunday month grid.  The desktop
  calendar (``_calendar_data`` in ``wattracker/server.py``) is the reference;
  the iOS grid once put 1 October 2026 on a Tuesday (#431).
* ``pairing_code_v1.json`` -- pairing-code normalization and grouping.  The
  server (``normalize_pairing_code`` / ``format_pairing_code`` in
  ``wattracker/cloud/security.py``) is the reference; the clients had already
  drifted on ``\\r`` / ``\\n`` when this file was introduced.
* ``removal_decision_v1.json`` -- whether "Remove this device" may finish
  locally after the revoke request failed (#410/#411).  This rule only exists
  on the clients, so this file *is* its reference.

Each file carries a version, a description and the source of truth it was
generated from.  Regenerate only when the rule deliberately changes; a
regeneration with no matching client change is a break.

This script must never open the wattracker database: it imports only the
stdlib and ``wattracker.cloud.security``, which is pure.

Usage:  python scripts/generate_behaviour_vectors.py
"""
from __future__ import annotations

import calendar
import json
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from wattracker.cloud.security import (  # noqa: E402
    format_pairing_code,
    normalize_pairing_code,
)

VECTOR_DIR = ROOT / "tests" / "vectors"
CALENDAR_OUTPUT = VECTOR_DIR / "calendar_grid_v1.json"
PAIRING_OUTPUT = VECTOR_DIR / "pairing_code_v1.json"
REMOVAL_OUTPUT = VECTOR_DIR / "removal_decision_v1.json"

GENERATED_BY = "scripts/generate_behaviour_vectors.py"

# ---------------------------------------------------------------------------
# Calendar grid
# ---------------------------------------------------------------------------

CALENDAR_MONTHS: list[dict] = [
    {
        "year": 2026, "month": 9,
        "why": "Five rows; leading 2026-08-31 and trailing into October.",
    },
    {
        "year": 2026, "month": 10,
        "why": (
            "The #431 month: 1 October 2026 is a Thursday (index 3), and the "
            "grid runs Monday 2026-09-28 to Sunday 2026-11-01."
        ),
    },
    {
        "year": 2026, "month": 6,
        "why": "Starts on a Monday: no leading cells from May.",
    },
    {
        "year": 2026, "month": 5,
        "why": "Ends on a Sunday: no trailing cells from June.",
    },
    {
        "year": 2026, "month": 3,
        "why": "Six rows: the 1st is a Sunday, so six leading cells.",
    },
    {
        "year": 2026, "month": 8,
        "why": "Six rows: the 1st is a Saturday and the month has 31 days.",
    },
    {
        "year": 2026, "month": 12,
        "why": "Year boundary: trailing cells are January 2027.",
    },
    {
        "year": 2027, "month": 1,
        "why": "Year boundary: leading cells are December 2026.",
    },
    {
        "year": 2027, "month": 2,
        "why": "Exactly four rows: a 28-day February that starts on a Monday.",
    },
    {
        "year": 2028, "month": 2,
        "why": "Leap February: 29 days, the 29th is a Tuesday.",
    },
]


def calendar_grid(year: int, month: int) -> list[dict]:
    """The desktop's grid, cell for cell: Monday-first monthdatescalendar."""

    cal = calendar.Calendar(firstweekday=0)  # Monday, as _calendar_data uses
    return [
        {"iso_date": d.isoformat(), "day": d.day, "in_month": d.month == month}
        for week in cal.monthdatescalendar(year, month)
        for d in week
    ]


def _check_grid(entry: dict, cells: list[dict]) -> None:
    # Sanity checks on the reference itself, so a stdlib surprise cannot be
    # frozen into the file unnoticed.
    assert len(cells) % 7 == 0, entry
    first = date.fromisoformat(cells[0]["iso_date"])
    last = date.fromisoformat(cells[-1]["iso_date"])
    assert first.weekday() == 0 and last.weekday() == 6, entry
    assert (last - first).days + 1 == len(cells), entry
    in_month = [c for c in cells if c["in_month"]]
    assert in_month[0]["day"] == 1, entry
    assert len(in_month) == calendar.monthrange(entry["year"], entry["month"])[1]


def _calendar_case(entry: dict) -> dict:
    cells = calendar_grid(entry["year"], entry["month"])
    _check_grid(entry, cells)
    return {
        "year": entry["year"],
        "month": entry["month"],
        "why": entry["why"],
        "rows": len(cells) // 7,
        "cells": cells,
    }


def build_calendar() -> dict:
    return {
        "version": 1,
        "generated_by": GENERATED_BY,
        "source_of_truth": (
            "wattracker/server.py _calendar_data: "
            "calendar.Calendar(firstweekday=0).monthdatescalendar(year, month)"
        ),
        "description": (
            "The month grid every calendar view lays out: full Monday-to-Sunday "
            "weeks covering the month. The first cell is the Monday on or "
            "before the 1st, the last cell is the Sunday on or after the last "
            "day, and the cells outside the month are the adjacent months' "
            "real dates with in_month false. Cells are listed row by row, "
            "seven per row; iso_date is the plain calendar date with no "
            "timezone, so a device's timezone must never move a cell. Read by "
            "tests/test_behaviour_vectors.py and the Swift "
            "BehaviourVectorTests; Android consumes it once it has a calendar "
            "grid (#198)."
        ),
        "months": [_calendar_case(entry) for entry in CALENDAR_MONTHS],
    }


# ---------------------------------------------------------------------------
# Pairing code
# ---------------------------------------------------------------------------

CANONICAL_CODE = "ABCDEFGHJKMN"
GROUPED_CODE = "ABCD-EFGH-JKMN"

# Characters a client may strip on top of the server's strip set.  See the
# description in build_pairing() for why this is harmless and why it is the
# only leniency allowed.
CLIENT_EXTRA_STRIP = "\r\n"

PAIRING_CASES: list[dict] = [
    {"name": "canonical", "input": CANONICAL_CODE,
     "why": "Already canonical: returned unchanged."},
    {"name": "grouped-dashes", "input": GROUPED_CODE,
     "why": "The display form the desktop shows."},
    {"name": "lowercase", "input": "abcd-efgh-jkmn",
     "why": "Typed in lowercase: upper-cased before anything else."},
    {"name": "mixed-case", "input": "aBcD-eFgH-jKmN", "why": "Mixed case."},
    {"name": "grouped-spaces", "input": "ABCD EFGH JKMN",
     "why": "Spaces are stripped like dashes."},
    {"name": "grouped-tabs", "input": "ABCD\tEFGH\tJKMN",
     "why": "Tabs are in the server's strip set too."},
    {"name": "surrounding-spaces", "input": "  ABCD-EFGH-JKMN  ",
     "why": "Leading and trailing spaces are stripped."},
    {"name": "irregular-separators", "input": "A-B C\tD--EFGH  JK-MN",
     "why": "Separators anywhere, repeated: grouping is not checked."},
    {"name": "all-digits", "input": "0123-4567-8901", "why": "Digits only."},
    {"name": "fold-i-l-o", "input": "IL0O-1234-5678",
     "why": "I and L fold to 1, O folds to 0."},
    {"name": "fold-lowercase", "input": "ilo0-ilo0-ilo0",
     "why": "Folding happens after upper-casing, so i, l and o fold too."},
    {"name": "illegal-u", "input": "ABCD-EFGH-JKMU",
     "why": "U is not folded: it is simply not a symbol."},
    {"name": "illegal-lowercase-u", "input": "abcd-efgh-jkmu",
     "why": "Nor is u."},
    {"name": "illegal-punctuation", "input": "ABCD.EFGH.JKMN",
     "why": "Only dash, space and tab are separators."},
    {"name": "illegal-underscore", "input": "ABCD_EFGH_JKMN",
     "why": "Underscore is not a separator."},
    {"name": "too-short", "input": "ABCD-EFGH-JKM", "why": "11 symbols."},
    {"name": "too-long", "input": "ABCD-EFGH-JKMNP", "why": "13 symbols."},
    {"name": "empty", "input": "", "why": "Empty is not a code."},
    {"name": "only-separators", "input": "- \t-", "why": "Zero symbols."},
    {"name": "max-input-length-64", "input": GROUPED_CODE + " " * 50,
     "why": "Exactly 64 characters including padding: still accepted."},
    {"name": "overlong-input-65", "input": GROUPED_CODE + " " * 51,
     "why": (
         "65 characters: refused before normalizing, even though the "
         "symbols inside are a valid code. The bound is on raw input length."
     )},
    {"name": "overlong-input-garbage", "input": "X" * 200,
     "why": "Far over the bound."},
    {"name": "fullwidth-letters",
     "input": "ＡＢＣＤ-ＥＦＧＨ-ＪＫＭＮ",
     "why": "Fullwidth Latin lookalikes are not the ASCII symbols."},
    {"name": "fullwidth-digits",
     "input": "０１２３-4567-8901",
     "why": "Fullwidth digits are not digits here either."},
    {"name": "cyrillic-lookalike", "input": "АBCD-EFGH-JKMN",
     "why": "Cyrillic capital A looks like A and is not."},
    {"name": "kelvin-sign", "input": "ABCD-EFGH-JKMN",
     "why": "KELVIN SIGN upper-cases to itself, not to K."},
    {"name": "zero-width-space", "input": "ABCD​EFGH​JKMN",
     "why": "Zero-width space is not in the strip set."},
    {"name": "no-break-space", "input": "ABCD EFGH JKMN",
     "why": "NO-BREAK SPACE is not an ASCII space."},
    {"name": "combining-accent", "input": "ABCD-EFGH-JKMÉ",
     "why": "A combining mark makes the last symbol not a symbol."},
    {"name": "emoji", "input": "ABCD-EFGH-JK\U0001f6b4",
     "why": "Astral-plane characters are refused."},
    {"name": "dotted-capital-i", "input": "ABCD-EFGH-JKMİ",
     "why": "Turkish dotted capital I upper-cases to itself: refused."},
    {"name": "dotless-small-i", "input": "abcd-efgh-jkmı",
     "why": (
         "Turkish dotless small i upper-cases to plain I, which then folds "
         "to 1. Locale-independent upper-casing is part of the rule."
     )},
    {"name": "sharp-s-expands", "input": "abcd-efgh-jkß",
     "why": (
         "German sharp s upper-cases to SS: one input character becomes two "
         "symbols. Upper-case the whole string first, then iterate."
     )},
    {"name": "ligature-ff-expands", "input": "ABCD-EFGH-JKﬀ",
     "why": "The ff ligature upper-cases to FF, also two symbols."},
    # The newline family: the one place a client may be more lenient.
    {"name": "trailing-newline", "input": GROUPED_CODE + "\n",
     "why": "A pasted code with a trailing newline."},
    {"name": "trailing-crlf", "input": GROUPED_CODE + "\r\n",
     "why": "A pasted code with a Windows line ending."},
    {"name": "embedded-newlines", "input": "ABCD\nEFGH\nJKMN",
     "why": "Groups on separate lines."},
    {"name": "embedded-cr", "input": "ABCD\rEFGH\rJKMN",
     "why": "Bare carriage returns between groups."},
    {"name": "newline-does-not-rescue-illegal", "input": "ABCD-EFGH-JKMU\n",
     "why": "Stripping newlines never makes an illegal symbol legal."},
    {"name": "newline-does-not-rescue-length", "input": "ABCD-EFGH-JKM\n",
     "why": "Nor a wrong length."},
]


def _pairing_case(entry: dict) -> dict:
    value = entry["input"]
    normalized = normalize_pairing_code(value)
    grouped = format_pairing_code(value) if normalized is not None else None
    case = {
        "name": entry["name"],
        "why": entry["why"],
        "input": value,
        "normalized": normalized,
        "grouped": grouped,
    }
    if normalized is None and any(c in value for c in CLIENT_EXTRA_STRIP):
        lenient = normalize_pairing_code(
            "".join(c for c in value if c not in CLIENT_EXTRA_STRIP)
        )
        if lenient is not None:
            case["client_may_accept"] = True
            case["client_normalized"] = lenient
            case["client_grouped"] = format_pairing_code(lenient)
    return case


def build_pairing() -> dict:
    return {
        "version": 1,
        "generated_by": GENERATED_BY,
        "source_of_truth": (
            "wattracker/cloud/security.py normalize_pairing_code and "
            "format_pairing_code (_PAIRING_ALPHABET, _PAIRING_FOLD, "
            "_PAIRING_STRIP, _MAX_PAIRING_INPUT)"
        ),
        "description": (
            "Pairing-code normalization. The server is the truth: reject input "
            "that is empty or longer than max_input_length characters; "
            "upper-case the whole string (locale-independent); drop every "
            "character in server_strip; fold I and L to 1 and O to 0; any "
            "other character outside the alphabet makes the input not a code "
            "(U included); exactly symbol_count symbols must remain. "
            "'normalized' is the canonical ungrouped code and 'grouped' the "
            "XXXX-XXXX-XXXX display form, both null when the input is not a "
            "code. A client must produce exactly these values, with ONE "
            "permitted leniency: a case marked client_may_accept differs from "
            "a valid code only by carriage returns and line feeds "
            "(client_extra_strip), and a client may either refuse it like the "
            "server (null) or accept it as exactly client_normalized / "
            "client_grouped. That leniency is harmless only because a client "
            "that applies it sends the server the normalized code, never the "
            "raw input; a client that forwards raw typed input must not use "
            "it to decide anything the server would decide differently."
        ),
        "alphabet": "0123456789ABCDEFGHJKMNPQRSTVWXYZ",
        "folds": {"I": "1", "L": "1", "O": "0"},
        "server_strip": "- \t",
        "client_extra_strip": CLIENT_EXTRA_STRIP,
        "symbol_count": 12,
        "group_size": 4,
        "max_input_length": 64,
        "cases": [_pairing_case(entry) for entry in PAIRING_CASES],
    }


# ---------------------------------------------------------------------------
# Removal decision
# ---------------------------------------------------------------------------

# The device clock every case is evaluated against.  Whole seconds, so an
# IMF-fixdate header can represent "now + offset" exactly.
REMOVAL_NOW = datetime(2026, 10, 1, 16, 0, 0, tzinfo=timezone.utc)
# iOS CloudSession.clockSkewTolerance.  Deliberately inside the server's
# signed-request timestamp window (_MAX_TIMESTAMP in wattracker/cloud/api.py):
# past 240 s the device clock is the likeliest reason for a refusal.
CLOCK_SKEW_TOLERANCE_SECONDS = 240
SERVER_TIMESTAMP_WINDOW_SECONDS = 300

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_IMF_FIXDATE = re.compile(
    r"(Mon|Tue|Wed|Thu|Fri|Sat|Sun), (\d{2}) ("
    + "|".join(_MONTHS)
    + r") (\d{4}) (\d{2}):(\d{2}):(\d{2}) GMT"
)


def imf_fixdate(moment: datetime) -> str:
    """RFC 9110 IMF-fixdate, without depending on the process locale."""

    moment = moment.astimezone(timezone.utc)
    return (
        f"{_WEEKDAYS[moment.weekday()]}, {moment.day:02d} "
        f"{_MONTHS[moment.month - 1]} {moment.year:04d} "
        f"{moment.hour:02d}:{moment.minute:02d}:{moment.second:02d} GMT"
    )


def parse_imf_fixdate(header: str | None) -> datetime | None:
    """IMF-fixdate only; the two obsolete HTTP date formats are 'absent'."""

    if not header:
        return None
    match = _IMF_FIXDATE.fullmatch(header.strip())
    if match is None:
        return None
    _, day, month, year, hour, minute, second = match.groups()
    try:
        return datetime(
            int(year), _MONTHS.index(month) + 1, int(day),
            int(hour), int(minute), int(second), tzinfo=timezone.utc,
        )
    except ValueError:
        return None


def removal_completes_locally(
    status: int | None, date_header: str | None, now: datetime
) -> bool:
    """The #410/#411 rule, stated once."""

    if status not in (401, 404):
        return False
    server_date = parse_imf_fixdate(date_header)
    if server_date is None:
        return False
    skew = abs((server_date - now).total_seconds())
    return skew <= CLOCK_SKEW_TOLERANCE_SECONDS


T = CLOCK_SKEW_TOLERANCE_SECONDS

REMOVAL_CASES: list[dict] = [
    {"name": "404-in-sync", "status": 404, "offset": 0,
     "why": "The server forgot the credential and the clocks agree."},
    {"name": "401-in-sync", "status": 401, "offset": 0,
     "why": "401 and 404 are indistinguishable by design."},
    {"name": "404-ahead-at-tolerance", "status": 404, "offset": T,
     "why": "Exactly at +tolerance: inclusive."},
    {"name": "404-behind-at-tolerance", "status": 404, "offset": -T,
     "why": "Exactly at -tolerance: inclusive."},
    {"name": "404-ahead-past-tolerance", "status": 404, "offset": T + 1,
     "why": "One second past: the device clock may be the reason."},
    {"name": "404-behind-past-tolerance", "status": 404, "offset": -(T + 1),
     "why": "One second past, the other way."},
    {"name": "401-ahead-at-tolerance", "status": 401, "offset": T,
     "why": "The boundary holds for 401 too."},
    {"name": "401-behind-past-tolerance", "status": 401, "offset": -(T + 1),
     "why": "And so does the refusal past it."},
    {"name": "404-badly-skewed", "status": 404, "offset": 3600,
     "why": "An hour off: every signed request would be refused anyway."},
    {"name": "404-no-date", "status": 404, "date_header": None,
     "why": "No Date header: skew cannot be ruled out, so keep."},
    {"name": "401-no-date", "status": 401, "date_header": None,
     "why": "Same for 401."},
    {"name": "404-empty-date", "status": 404, "date_header": "",
     "why": "An empty header is an absent one."},
    {"name": "404-garbage-date", "status": 404, "date_header": "not a date",
     "why": "Unparseable is absent."},
    {"name": "404-rfc850-date", "status": 404,
     "date_header": "Thursday, 01-Oct-26 16:00:00 GMT",
     "why": "The obsolete RFC 850 form is not parsed, so it counts as absent."},
    {"name": "404-asctime-date", "status": 404,
     "date_header": "Thu Oct  1 16:00:00 2026",
     "why": "Nor is asctime."},
    {"name": "429-in-sync", "status": 429, "offset": 0,
     "why": "Rate limited: transient, keep the credential."},
    {"name": "503-in-sync", "status": 503, "offset": 0,
     "why": "Unavailable: transient."},
    {"name": "500-in-sync", "status": 500, "offset": 0,
     "why": "Server error: unexplained."},
    {"name": "502-in-sync", "status": 502, "offset": 0,
     "why": "Bad gateway: transient."},
    {"name": "403-in-sync", "status": 403, "offset": 0,
     "why": "Only 401 and 404 mean 'no such credential'."},
    {"name": "400-in-sync", "status": 400, "offset": 0,
     "why": "Malformed request: our bug, not the server forgetting us."},
    {"name": "410-in-sync", "status": 410, "offset": 0,
     "why": "Not one of the two statuses, however suggestive."},
    {"name": "offline", "status": None, "date_header": None,
     "why": "No response at all: keep the credential."},
]


def _removal_case(entry: dict) -> dict:
    offset = entry.get("offset")
    if offset is not None:
        header = imf_fixdate(REMOVAL_NOW + timedelta(seconds=offset))
    else:
        header = entry["date_header"]
    return {
        "name": entry["name"],
        "why": entry["why"],
        "status": entry["status"],
        "date_offset_seconds": offset,
        "date_header_raw": header,
        "complete_locally": removal_completes_locally(
            entry["status"], header, REMOVAL_NOW
        ),
    }


def build_removal() -> dict:
    return {
        "version": 1,
        "generated_by": GENERATED_BY,
        "source_of_truth": (
            "This file. The rule exists only on the clients (#410/#411); the "
            "iOS reference is CloudSession.removalCompletesLocally, the "
            "tolerance is CloudSession.clockSkewTolerance."
        ),
        "description": (
            "'Remove this device' sends a revoke. When the revoke FAILS, the "
            "client finishes removal locally (drops the credential) iff the "
            "status is 401 or 404 AND the response Date header is present, "
            "parses as an RFC 9110 IMF-fixdate, and is within "
            "clock_skew_tolerance_seconds of the device clock (inclusive). "
            "Everything else keeps the credential: other statuses (429, 503, "
            "any 5xx, 403, 400, 410), no response (status null), and a Date "
            "that is missing, empty, unparseable, in an obsolete format, or "
            "skewed. Evaluate each case with the device clock at now_epoch; "
            "date_header_raw is the header exactly as received (null when "
            "absent), and date_offset_seconds is what it encodes relative to "
            "now_epoch (null when it encodes nothing usable)."
        ),
        "now_epoch": int(REMOVAL_NOW.timestamp()),
        "now_iso": REMOVAL_NOW.isoformat().replace("+00:00", "Z"),
        "clock_skew_tolerance_seconds": CLOCK_SKEW_TOLERANCE_SECONDS,
        "server_timestamp_window_seconds": SERVER_TIMESTAMP_WINDOW_SECONDS,
        "complete_statuses": [401, 404],
        "cases": [_removal_case(entry) for entry in REMOVAL_CASES],
    }


# ---------------------------------------------------------------------------

OUTPUTS = {
    CALENDAR_OUTPUT: (build_calendar, False),
    PAIRING_OUTPUT: (build_pairing, True),  # escaped: lookalikes stay visible
    REMOVAL_OUTPUT: (build_removal, False),
}


def render(build, ensure_ascii: bool) -> str:
    return json.dumps(build(), indent=2, ensure_ascii=ensure_ascii) + "\n"


def main() -> int:
    VECTOR_DIR.mkdir(parents=True, exist_ok=True)
    for path, (build, ensure_ascii) in OUTPUTS.items():
        path.write_text(render(build, ensure_ascii), encoding="utf-8")
        print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
