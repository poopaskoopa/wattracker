"""The one-time token that authorizes the FIRST account on an install.

WHY THIS EXISTS
---------------
``POST /register`` has always allowed the first account unconditionally, on the
reasoning that a server with no users has nothing to protect and every install
has to bootstrap somehow. That reasoning holds exactly as long as the only
person who can reach the port is the person who started the process. It stops
holding the moment the app is bound past loopback - a documented, supported
configuration (``WATTRACKER_ALLOW_NON_LOOPBACK``, so a phone can be a ride
screen) - because then "whoever reaches /register first owns this instance"
includes every other device on the network. On a fresh install that is a land
grab: the app-global LLM settings, the rider's stored API key, and the
``via=connector`` laundering path described in ``config.allow_registration``
all fall out of it. Item 4 of issue #132.

The fix is the smallest thing that re-attaches "may create the first account"
to "can see the console this server is running in": a random token printed at
startup while the database has no users, which the first registration must
present. It is not an enrollment system - no server-side records, no expiry
clock, no state that outlives the process. It is one secret, held in memory,
that says "this request came from someone the operator told".

WHY IT IS REGENERATED ON EVERY START, AND NEVER WRITTEN TO DISK
--------------------------------------------------------------
A restart before the first account is created invalidates the old token and
prints a new one. That is deliberate, and it is the safe direction rather than
the convenient one:

* **Nothing to clean up, nothing to leak.** A persisted token would be a
  credential at rest that has to be deleted reliably at exactly the right
  moment; a delete that silently fails leaves a dormant key to the one account
  that matters. It would also land in every backup of the data directory taken
  during setup. In memory, the token cannot outlive the process that printed
  it, so there is no window to get wrong.
* **It bounds the value of the printed copy.** ``start.sh`` runs the server
  under ``nohup`` with stdout appended to ``~/.wattracker/server.log``, so the
  banner does persist in a file. Regenerating means every token in that log is
  already dead: the live one is only ever the newest, and only while the
  process that printed it is still running with no account created.
* **It cannot lock the owner out.** The failure this trades against is "I
  restarted and my token stopped working", and the recovery is to read the new
  banner in the same place the old one came from. By definition there is no
  account yet, so there is nothing to be locked out OF - the only thing lost is
  a string that was seconds old.

A stable, persisted token would only buy something if the operator could not
see the server's output at all, and in every supported launch path they can:
``start.sh`` names the log file it appends to (and prints the token it finds
there), the container image runs the app in the foreground under ``docker
logs``, and the frozen macOS/Windows builds are deliberately console builds
(see ``packaging/wattracker.spec``) precisely so startup output has somewhere
to go.

The token is a per-app-instance object rather than a process global so that the
tests can hold one, spend it, and assert against it without reaching into
module state some earlier test has already consumed.
"""
from __future__ import annotations

import hmac
import secrets
import sys
import threading
from typing import Optional, TextIO

# secrets.token_urlsafe(32) -> 32 random bytes -> 43 base64url characters. The
# same size as the connector device token and the calendar feed token, for the
# same reason: 256 bits is not guessable, and there is no case for the one
# credential that hands over the whole install being weaker than the ones that
# hand over a subset of it.
TOKEN_BYTES = 32

# Shape gate applied BEFORE the comparison, mirroring connectorauth and
# calendarfeed: the submitted value is a form field, and a form field can be
# megabytes. compare_digest on a huge string is not expensive enough to matter
# by itself, but the rule in this codebase is that nothing unauthenticated gets
# to choose how much work the server does, and a cap costs one len().
MAX_SUBMITTED_LEN = 256

# Compared against when there is nothing real to compare against (no candidate,
# wrong shape, or an already-spent token), so every refusal costs the same
# compare_digest as a wrong-but-plausible guess. Same pattern as
# connectorsession's _DUMMY_HASH.
_DUMMY = secrets.token_urlsafe(TOKEN_BYTES)


class SetupToken:
    """One secret authorizing one account: the first one on this install.

    Thread-safe because the ASGI app runs sync handlers in a threadpool, and
    ``spend`` is what makes the token single-use: two requests that both carry
    a valid token must not both be able to create the bootstrap account. The
    registration route serializes creation under its own lock and spends the
    token inside it; this lock only protects the object's own fields.
    """

    def __init__(self, value: Optional[str] = None) -> None:
        self._lock = threading.Lock()
        # Generated eagerly. There is no lazy path worth having: it is 32 bytes
        # from the OS CSPRNG, and a token that exists only once something has
        # asked for it is a token whose printing depends on call order.
        self._value = value or secrets.token_urlsafe(TOKEN_BYTES)
        self._spent = False
        self._announced = False
        self._refusals = 0

    @property
    def value(self) -> str:
        """The plaintext. Only ever shown to the console, never to a request."""
        return self._value

    @property
    def spent(self) -> bool:
        with self._lock:
            return self._spent

    @property
    def announced(self) -> bool:
        with self._lock:
            return self._announced

    @property
    def refusals(self) -> int:
        with self._lock:
            return self._refusals

    def matches(self, candidate: object) -> bool:
        """Whether ``candidate`` is this token, in constant time.

        False for a spent token, for a non-string, for the empty string and for
        anything longer than ``MAX_SUBMITTED_LEN`` - but every one of those
        paths still runs one ``compare_digest``, so the refusals are not
        distinguishable from each other by timing. ``hmac.compare_digest`` is
        the only comparison used here on purpose: ``==`` on a str short-circuits
        at the first differing character, which is exactly the leak that lets a
        guesser walk a secret out one character at a time.
        """
        if not isinstance(candidate, str) or not candidate:
            return hmac.compare_digest(_DUMMY, _DUMMY + "x")
        if len(candidate) > MAX_SUBMITTED_LEN:
            return hmac.compare_digest(_DUMMY, _DUMMY + "x")
        with self._lock:
            expected = _DUMMY if self._spent else self._value
        # compare_digest on str operands requires them to be ASCII-only and
        # raises otherwise; a browser can post any UTF-8, so encode both sides.
        return hmac.compare_digest(
            expected.encode("utf-8"), candidate.encode("utf-8")
        )

    def spend(self) -> None:
        """Burn the token. Idempotent, and there is no way back.

        Called once the bootstrap account actually exists. Strictly speaking
        the route would already refuse a second use - the token is only ever
        consulted while the database has no users - but "the token works once"
        should be a property of the token, not an emergent property of the
        caller's ordering. If some future path empties the users table, a token
        printed before the first account was created must not come back to life.
        """
        with self._lock:
            self._spent = True

    def announce(self, out: Optional[TextIO] = None) -> bool:
        """Print the banner, at most once per instance. True if it printed.

        Idempotent because there are two callers: startup (the normal one) and
        the registration refusal (the fallback, for a server that started with
        an account and somehow no longer has one, which would otherwise leave
        the operator needing a token that was never shown). Neither should be
        able to spam the console, and an unauthenticated request must never be
        able to make the server print the token more than once.

        Written to stdout with an explicit flush: ``start.sh`` appends the
        server's output to a log file, and a block-buffered pipe would hold the
        one line the operator is waiting for until something else happened to
        fill the buffer.
        """
        with self._lock:
            if self._announced or self._spent:
                return False
            self._announced = True
            token = self._value
        stream = out if out is not None else sys.stdout
        rule = "-" * 68
        print(
            f"\n{rule}\n"
            "  wattracker setup token (first account only)\n\n"
            f"      {token}\n\n"
            "  This install has no account yet. Enter this token on the\n"
            '  "Create account" page to claim it - without it, registration\n'
            "  is refused, so nobody else who can reach this server can take\n"
            "  the account instead.\n\n"
            "  It is never written to disk, it stops working as soon as the\n"
            "  first account exists, and restarting before then prints a new\n"
            "  one and invalidates this one.\n"
            f"{rule}\n",
            file=stream,
            flush=True,
        )
        return True

    def record_refusal(self) -> int:
        """Count one refused token and return the running total.

        Visibility only - nothing here refuses anything beyond the single
        request, and deliberately so. Locking out after wrong tokens would let
        anyone who can reach the port stop the owner from completing setup,
        which is a cheaper version of the attack this whole file exists to
        stop.
        """
        with self._lock:
            self._refusals += 1
            return self._refusals
