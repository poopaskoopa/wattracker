"""Single-use tickets that turn a connector's device token into a login.

The connector runs beside Zwift and can open a window onto this server's web
UI (WP-8). That UI is session-cookie authenticated and the connector has no
cookie - it has a device token. Rather than teach the tray about passwords,
the connector exchanges its token for a ticket over the bearer-authenticated
HTTP path it already uses for buffered ride uploads, and the ticket is spent
once to establish an ordinary session.

The security model follows ``connectorauth`` and ``calendarfeed`` deliberately,
because a third shape of credential handling in this app would be a third thing
to get subtly wrong:

* only a sha256 digest is ever stored, never the ticket;
* a shape check runs before any hashing, so a hostile value cannot make the
  server digest megabytes;
* every failure mode - malformed, unknown, expired, already spent - collapses
  to a single ``None``, so the caller has one "no" to answer with;
* a miss performs the same digest comparison as a hit, so timing distinguishes
  nothing.

Two things are different from a device token, both narrowing:

* it lives in memory only. A ticket is worth an account session, and there is
  no reason for one to survive a restart - the connector simply mints another.
* it expires in a minute and can be redeemed once. It exists to cross the gap
  between a double-click and a page load, and nothing longer.

**A ticket travels in a query string**, which is why the parameter that carries
it is named ``token``: uvicorn's access logger writes the full request target,
and ``calendarfeed._TokenRedactingFilter`` scrubs parameters whose name starts
with "token". A ticket under any other name would be written to the access log
in plaintext. See the route in server.py.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import threading
import time as _time
from typing import Optional

# secrets.token_urlsafe(32) -> 32 random bytes -> 43 base64url characters, the
# same size as a device token and a calendar token.
TICKET_BYTES = 32

# How long a ticket is worth anything. It is minted in response to a
# double-click and spent by the window that opens immediately afterwards, so
# this only has to cover a slow page load, not a session.
TICKET_TTL_S = 60.0

# Shape gate applied before any hashing. token_urlsafe emits only
# [A-Za-z0-9_-]; the bounds are generous enough to survive a change of
# TICKET_BYTES without rejecting live tickets.
_TICKET_RE = re.compile(r"\A[A-Za-z0-9_-]{20,256}\Z")

# Compared against when nothing matched, so an unknown ticket costs the same
# comparison a known one does.
_DUMMY_HASH = hashlib.sha256(b"wattracker::no-such-session-ticket").hexdigest()


def hash_ticket(ticket: str) -> str:
    """sha256 hex of a ticket. The only form ever held."""
    return hashlib.sha256(ticket.encode("utf-8")).hexdigest()


class TicketStore:
    """In-memory, single-use, per-device session tickets.

    One outstanding ticket per device: minting again replaces the previous one,
    so a connector that asks twice cannot leave a spare valid credential behind
    it. Keyed by device id rather than user id so two paired machines do not
    invalidate each other's windows.

    ``clock`` is injectable, which is what lets expiry be tested without
    sleeping.
    """

    def __init__(self, ttl: float = TICKET_TTL_S, clock=_time.monotonic) -> None:
        self._ttl = ttl
        self._clock = clock
        self._lock = threading.Lock()
        # device_id -> (digest, expires_at, user_id, username)
        self._tickets: "dict[int, tuple[str, float, int, str]]" = {}

    def mint(self, user_id: int, username: str, device_id: int) -> str:
        """Issue a ticket for a device, replacing any it already had."""
        # Outside the lock below, which is not reentrant.
        self.purge_expired()
        ticket = secrets.token_urlsafe(TICKET_BYTES)
        with self._lock:
            self._tickets[device_id] = (
                hash_ticket(ticket),
                self._clock() + self._ttl,
                user_id,
                username,
            )
        return ticket

    def redeem(self, ticket: object) -> Optional[dict]:
        """Spend a ticket. Returns ``{user_id, username, device_id}`` or None.

        The entry is removed before anything is decided, so a ticket cannot be
        redeemed twice even if two requests race: exactly one of them finds it.
        """
        if not isinstance(ticket, str) or not _TICKET_RE.match(ticket):
            return None
        digest = hash_ticket(ticket)
        now = self._clock()
        with self._lock:
            found = None
            for device_id, entry in self._tickets.items():
                if hmac.compare_digest(digest, entry[0]):
                    found = (device_id, entry)
                    break
            if found is None:
                # Same work as a hit, so a valid-but-unknown ticket is not
                # distinguishable from a known one by response time.
                hmac.compare_digest(digest, _DUMMY_HASH)
                return None
            device_id, (_digest, expires_at, user_id, username) = found
            # Spent on sight, whether or not it turns out to be in time: an
            # expired ticket is not a retry, it is a mint.
            del self._tickets[device_id]
        if now > expires_at:
            return None
        return {"user_id": user_id, "username": username, "device_id": device_id}

    def revoke_device(self, device_id: int) -> None:
        """Drop a device's outstanding ticket - it was unpaired."""
        with self._lock:
            self._tickets.pop(device_id, None)

    def purge_expired(self) -> None:
        """Drop timed-out tickets.

        Redemption already removes what it touches, so this only matters for
        tickets nobody ever spends - a double-click whose window never opened.
        Called on mint, which bounds the store by the number of paired devices
        rather than by how many times anyone has ever double-clicked.
        """
        now = self._clock()
        with self._lock:
            for device_id in [
                d for d, entry in self._tickets.items() if now > entry[1]
            ]:
                del self._tickets[device_id]

    @property
    def outstanding(self) -> int:
        """How many tickets are held. For tests and logging."""
        with self._lock:
            return len(self._tickets)
