"""Read-only iCalendar (RFC 5545) feed of a user's scheduled workouts.

Why this is not behind the normal session cookie
------------------------------------------------
A phone calendar client (iOS Calendar, Google Calendar) subscribes to a URL and
re-fetches it on a timer. It has no cookie jar and no way to log in, so the
signed-cookie SessionMiddleware that guards every other route cannot protect
this one. The feed is therefore authenticated by a per-user bearer token
carried in the URL, with these properties:

* The token is 32 random bytes from ``secrets.token_urlsafe`` - never derived
  from the user id, username, or password, so knowing a user tells you nothing
  about their token and one user's token reveals nothing about another's.
* Only ``sha256(token)`` is stored, exactly like a password hash: the database
  (and its backups, which cover every user) is not a second copy of a live
  credential. The plaintext exists only in the response that created it.
* Rotation is "write a new hash": there is one hash per user, so generating a
  new token instantly makes the old one unresolvable.
* Resolution is by hash, then re-verified with ``hmac.compare_digest``.
* Every query the feed makes is scoped by the resolved ``user_id``. A token can
  only ever reach its own owner's rows.

A URL-borne bearer token is a real trade-off: anyone who obtains the URL can
read the schedule until it is rotated. That is disclosed in the UI, the feed
carries ``Cache-Control: private, no-store``, and the token is redacted from
the access log (see ``install_access_log_redaction``).
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import hmac
import logging
import re
import secrets
import urllib.parse as _url
from typing import List, Optional, Tuple

from . import db
from .timeutil import local_today, utc_now

_log = logging.getLogger(__name__)

# Feed window, in days either side of the user's local today.
FEED_PAST_DAYS = 30
FEED_FUTURE_DAYS = 180

FEED_PATH = "/calendar.ics"

# secrets.token_urlsafe(32) -> 32 random bytes -> 43 base64url characters.
TOKEN_BYTES = 32

# Shape gate applied before any hashing or database work, so a hostile query
# string cannot make the server hash megabytes. token_urlsafe emits only
# [A-Za-z0-9_-]; the bounds are generous enough to survive a future change of
# TOKEN_BYTES without rejecting live tokens.
_TOKEN_RE = re.compile(r"\A[A-Za-z0-9_-]{20,256}\Z")

# Compared against when no row matched, so the "unknown token" path performs
# the same digest comparison as the "known token" path.
_DUMMY_HASH = hashlib.sha256(b"wattracker::no-such-calendar-token").hexdigest()

_PRODID = "-//wattracker//Training Calendar//EN"
_UID_DOMAIN = "wattracker.local"

# Discriminators baked into every UID. plan_workouts.id and
# standalone_workouts.id are independent AUTOINCREMENT sequences, so id 7
# exists in both; without this prefix the two would collide in the subscriber's
# calendar and one would silently overwrite the other.
_KIND_PLAN = "plan"
_KIND_STANDALONE = "standalone"


# --------------------------------------------------------------- tokens
def hash_token(token: str) -> str:
    """sha256 hex of a feed token. The only form ever persisted."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def generate_token(user_id: int, path: Optional[str] = None) -> Optional[str]:
    """Mint a fresh feed token for a user and store its hash.

    Returns the plaintext token - the caller's single chance to show it, since
    only the hash is kept. Any previously issued token for this user stops
    working the moment this returns. None if there is no such user.
    """
    token = secrets.token_urlsafe(TOKEN_BYTES)
    if not db.set_calendar_token_hash(user_id, hash_token(token), path=path):
        return None
    return token


def token_is_set(user_id: int, path: Optional[str] = None) -> bool:
    """Whether this user currently has a feed link (no secret is returned)."""
    return bool(db.get_calendar_token_hash(user_id, path=path))


def user_for_token(token: object, path: Optional[str] = None) -> Optional[dict]:
    """Resolve the owning user from a plaintext feed token, or None.

    None covers every failure - missing, empty, malformed, and unknown - so the
    caller has a single "no" to answer with and cannot accidentally tell the
    four cases apart in its response.
    """
    if not isinstance(token, str) or not _TOKEN_RE.match(token):
        return None
    digest = hash_token(token)
    row = db.user_by_calendar_token_hash(digest, path=path)
    if row is None:
        # Same comparison work as a hit, so a valid-but-unknown token is not
        # distinguishable from a known one by response time.
        hmac.compare_digest(digest, _DUMMY_HASH)
        return None
    stored = row.get("calendar_token_hash") or ""
    if not hmac.compare_digest(digest, stored):
        return None
    return row


def feed_url(base_url: str, token: str) -> str:
    """Absolute subscription URL for a freshly minted token."""
    return (
        base_url.rstrip("/")
        + FEED_PATH
        + "?token="
        + _url.quote(token, safe="")
    )


class _TokenRedactingFilter(logging.Filter):
    """Strip ``token=...`` out of access-log lines.

    uvicorn's access logger writes the full request target, query string
    included, so an unfiltered log file would be a plaintext store of every
    subscriber's feed token - the exact thing hashing the column avoids.
    """

    # Matches any query parameter whose name STARTS with "token", not just the
    # exact "token=" this app mints: "token[]=", "token%5B%5D=", "tokens=" and
    # friends all reach the same handler through some client or framework, and
    # a redaction filter that under-matches is worse than useless. Deliberately
    # anchored to a parameter separator (? & ;) so it cannot chew through
    # unrelated log text that happens to contain the word.
    #
    # EVERY separator character must stay excluded from the name class, ? most
    # of all. Omitting it made this quadratic and remotely exploitable: on
    # "?token?token?token..." each of the n/6 start positions ran the name
    # class to end-of-string hunting for an "=" that was never there. A 64,800
    # character request line - which fits under httptools' ~65KB cap, so it is
    # served rather than rejected - cost 1.96s of event-loop CPU in this
    # filter, from an unauthenticated request, on the path that logs every
    # 404. Stopping the class at the next separator makes each start position
    # O(1) and the whole scan linear: the same input now takes 0.0005s.
    _PATTERN = re.compile(
        r"((?:\?|&|&amp;|;)(?i:token)[^\s&;=?\"']*=)[^\s&;\"']*"
    )

    @classmethod
    def redact(cls, value: str) -> str:
        return cls._PATTERN.sub(r"\1[REDACTED]", value)

    @staticmethod
    def _may_contain_token(value: str) -> bool:
        """Cheap pre-filter so the regex is not run on every log line.

        Tests only for the word, never for "token=" - the parameter can arrive
        as "token[]=" or "token%5B%5D=", and a guard narrower than the pattern
        it guards would silently let those through unredacted.
        """
        return "token" in value.lower()

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if isinstance(record.msg, str) and self._may_contain_token(record.msg):
                record.msg = self.redact(record.msg)
            if isinstance(record.args, tuple):
                record.args = tuple(
                    self.redact(a)
                    if isinstance(a, str) and self._may_contain_token(a)
                    else a
                    for a in record.args
                )
        except Exception:  # logging must never break the request
            _log.debug("access-log redaction failed", exc_info=True)
        return True


def install_access_log_redaction() -> None:
    """Attach the redaction filter to uvicorn's access logger (idempotent)."""
    logger = logging.getLogger("uvicorn.access")
    if any(isinstance(f, _TokenRedactingFilter) for f in logger.filters):
        return
    logger.addFilter(_TokenRedactingFilter())


# ------------------------------------------------------- ICS primitives
# RFC 5545 3.3.11: in a TEXT value, backslash, semicolon and comma are escaped,
# and a line break becomes a literal "\n". Backslash must go first or it would
# double-escape the escapes added after it. Workout names are user-supplied
# free text, so this is load-bearing, not cosmetic: an unescaped ';' or newline
# terminates the property and lets the name inject arbitrary iCalendar
# properties into the subscriber's calendar.
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def escape_text(value: object) -> str:
    """Escape a Python string for use as an RFC 5545 TEXT value."""
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    # Control characters other than the line breaks handled below have no
    # legal representation in a content line; drop them rather than emit a
    # file no client will parse.
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _CONTROL_CHARS.sub("", text)
    text = text.replace("\\", "\\\\")
    text = text.replace(";", "\\;").replace(",", "\\,")
    return text.replace("\n", "\\n")


def fold(line: str) -> str:
    """Fold one content line to 75 octets per RFC 5545 3.1.

    The limit is octets, not characters, and the split must land on a character
    boundary or the UTF-8 encoding breaks (the "completed" check mark is three
    octets). Continuation lines start with one space, which counts toward their
    75, hence the 74 budget after the first.
    """
    if len(line.encode("utf-8")) <= 75:
        return line
    chunks: List[str] = []
    current: List[str] = []
    used = 0
    limit = 75
    for char in line:
        size = len(char.encode("utf-8"))
        if used + size > limit:
            chunks.append("".join(current))
            current = []
            used = 0
            limit = 74  # the leading space of a continuation line
        current.append(char)
        used += size
    chunks.append("".join(current))
    return "\r\n ".join(chunks)


def _ics_date(value: _dt.date) -> str:
    return value.strftime("%Y%m%d")


def _ics_timestamp(value: _dt.datetime) -> str:
    return value.strftime("%Y%m%dT%H%M%SZ")


def _parse_date(value: object) -> Optional[_dt.date]:
    """Parse a stored 'YYYY-MM-DD' (or ISO datetime) date, or None."""
    if isinstance(value, _dt.date) and not isinstance(value, _dt.datetime):
        return value
    if not isinstance(value, str):
        return None
    try:
        return _dt.date.fromisoformat(value[:10])
    except ValueError:
        return None


def format_duration(seconds: object) -> str:
    """Human duration for a SUMMARY: 5400 -> '1h30m', 2700 -> '45m'."""
    try:
        total = int(round(float(seconds)))
    except (TypeError, ValueError):
        return "0m"
    total = max(total, 0)
    hours, minutes = divmod(total // 60, 60)
    if hours and minutes:
        return f"{hours}h{minutes}m"
    if hours:
        return f"{hours}h"
    return f"{minutes}m"


def _format_tss(value: object) -> Optional[str]:
    try:
        tss = float(value)
    except (TypeError, ValueError):
        return None
    return str(int(round(tss))) if abs(tss - round(tss)) < 0.05 else f"{tss:.1f}"


def event_uid(kind: str, user_id: int, workout_id: int) -> str:
    """Stable, collision-proof UID.

    Stable so a re-fetch updates the existing event instead of duplicating it,
    and discriminated by table so plan_workouts #7 and standalone_workouts #7 -
    separate id sequences - are never the same event.
    """
    return f"wattracker-{kind}-{int(user_id)}-{int(workout_id)}@{_UID_DOMAIN}"


def _vevent(
    kind: str,
    user_id: int,
    workout: dict,
    date_key: str,
    dtstamp: str,
) -> Optional[List[str]]:
    day = _parse_date(workout.get(date_key))
    if day is None:
        return None
    completed = workout.get("completed_activity_id") is not None
    duration = format_duration(workout.get("duration_s"))
    name = (workout.get("name") or "Workout")
    summary = f"{'✓ ' if completed else ''}{name} ({duration})"

    description_parts = [f"Type: {workout.get('type') or 'unspecified'}"]
    tss = _format_tss(workout.get("tss"))
    if tss is not None:
        description_parts.append(f"TSS: {tss}")
    description_parts.append(f"Duration: {duration}")
    if completed:
        description_parts.append("Completed")

    lines = [
        "BEGIN:VEVENT",
        f"UID:{event_uid(kind, user_id, workout['id'])}",
        f"DTSTAMP:{dtstamp}",
        # Workouts carry a date and no time-of-day in the schema
        # (plan_workouts.date / standalone_workouts.scheduled_date are both
        # plain 'YYYY-MM-DD'), so an all-day DATE event is the honest
        # representation. DTEND is the exclusive next day, per RFC 5545 3.8.2.2.
        f"DTSTART;VALUE=DATE:{_ics_date(day)}",
        f"DTEND;VALUE=DATE:{_ics_date(day + _dt.timedelta(days=1))}",
        f"SUMMARY:{escape_text(summary)}",
        f"DESCRIPTION:{escape_text(chr(10).join(description_parts))}",
        # A training day should not make the rider look busy to anyone
        # scheduling against their calendar.
        "TRANSP:TRANSPARENT",
        f"CATEGORIES:{escape_text('wattracker')}",
        "END:VEVENT",
    ]
    return lines


def build_ics(
    user_id: int,
    today: Optional[_dt.date] = None,
    path: Optional[str] = None,
) -> str:
    """Render this user's scheduled workouts as an RFC 5545 calendar.

    Every read is scoped to ``user_id``; nothing here takes a row that was not
    selected by it.
    """
    settings = db.get_user_settings(user_id, path=path)
    if today is None:
        today = local_today(settings.get("timezone"))
    start = today - _dt.timedelta(days=FEED_PAST_DAYS)
    end = today + _dt.timedelta(days=FEED_FUTURE_DAYS)
    start_s, end_s = start.isoformat(), end.isoformat()

    entries: List[Tuple[str, str, int, dict]] = []
    for workout in db.plan_workouts_in_range(user_id, start_s, end_s, path=path):
        entries.append((str(workout.get("date") or ""), _KIND_PLAN,
                        int(workout["id"]), workout))
    for workout in db.standalone_workouts_in_range(
        user_id, start_s, end_s, path=path
    ):
        entries.append((str(workout.get("scheduled_date") or ""),
                        _KIND_STANDALONE, int(workout["id"]), workout))
    entries.sort(key=lambda e: (e[0], e[1], e[2]))

    dtstamp = _ics_timestamp(utc_now())
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{_PRODID}",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{escape_text('wattracker training')}",
        f"X-WR-CALDESC:{escape_text('Scheduled workouts from wattracker')}",
    ]
    for _, kind, _id, workout in entries:
        date_key = "date" if kind == _KIND_PLAN else "scheduled_date"
        event = _vevent(kind, user_id, workout, date_key, dtstamp)
        if event:
            lines.extend(event)
    lines.append("END:VCALENDAR")

    # RFC 5545 3.1: content lines are CRLF-delimited, including the last one.
    return "".join(fold(line) + "\r\n" for line in lines)
