"""Bearer tokens that let a connector machine authenticate to the server.

A connector is the small process that runs on the box where Zwift lives. It
has no browser and no session cookie, so it presents a per-device token on
every connection instead.

The security model is deliberately the same as the calendar feed's, for the
same reasons: only the sha256 digest is stored, every failure mode collapses
to a single ``None`` so the caller cannot accidentally distinguish them, and a
shape check runs before any hashing so a hostile value cannot make the server
digest megabytes. It differs in one way - a token is per *device* rather than
per user, so one machine can be revoked without disturbing another, and the
server can say when each last checked in.

Tokens are also secrets that travel in a header, so they must never reach a
log. ``calendarfeed.install_access_log_redaction`` already scrubs the access
log; this module's tokens never appear in a URL, which is the stronger
guarantee - but do not put one in a query string later.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from typing import List, Optional

from . import db

# secrets.token_urlsafe(32) -> 32 random bytes -> 43 base64url characters.
TOKEN_BYTES = 32

# Shape gate applied before any hashing or database work. token_urlsafe emits
# only [A-Za-z0-9_-]; the bounds are generous enough to survive a future change
# of TOKEN_BYTES without rejecting live tokens.
_TOKEN_RE = re.compile(r"\A[A-Za-z0-9_-]{20,256}\Z")

# Compared against when no row matched, so the "unknown token" path performs
# the same digest comparison as the "known token" path.
_DUMMY_HASH = hashlib.sha256(b"wattracker::no-such-connector-token").hexdigest()

# A label is shown back to the user in Settings, so it is bounded and stripped
# of anything that is not plainly a machine name.
MAX_LABEL_LEN = 64


def hash_token(token: str) -> str:
    """sha256 hex of a device token. The only form ever persisted."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def clean_label(label: object) -> str:
    """Normalise a user-supplied device label to something safe to show."""
    raw = label if isinstance(label, str) else ""
    # Control characters would let a label corrupt a log line or the page.
    kept = "".join(ch for ch in raw if ch.isprintable()).strip()
    return kept[:MAX_LABEL_LEN] or "Connector"


def generate_token(
    user_id: int, label: str, path: Optional[str] = None
) -> "Optional[tuple[int, str]]":
    """Pair a new machine. Returns ``(device_id, plaintext_token)``.

    The plaintext is the caller's single chance to show it - only the hash is
    kept, so it cannot be recovered or re-displayed. ``None`` when the device
    could not be registered (no such user).
    """
    token = secrets.token_urlsafe(TOKEN_BYTES)
    device_id = db.add_connector_device(
        user_id, clean_label(label), hash_token(token), path=path
    )
    if device_id is None:
        return None
    return device_id, token


def list_devices(user_id: int, path: Optional[str] = None) -> List[dict]:
    """A user's paired machines. Never contains a token or a hash."""
    return db.list_connector_devices(user_id, path=path)


def device_exists(
    user_id: object, device_id: object, path: Optional[str] = None
) -> bool:
    """Whether this user still has that paired machine.

    The session layer's question, not the connector's: a browser session opened
    by a connector ticket is only as alive as the device that opened it, and a
    signed session cookie has no server-side record for ``revoke`` to reach
    into. See ``AuthMiddleware`` - without this check, revoking a stolen laptop
    kills its token and leaves the window that token opened working for the
    fortnight the cookie is valid.

    Reads the same list the Settings page shows rather than adding a query: a
    rider has a handful of machines, and a second source of truth about what
    "paired" means is how the two answers come apart later.
    """
    try:
        wanted = int(device_id)
        owner = int(user_id)
    except (TypeError, ValueError):
        return False
    return any(row.get("id") == wanted for row in list_devices(owner, path=path))


def revoke(user_id: int, device_id: int, path: Optional[str] = None) -> bool:
    """Unpair a machine. Its token stops resolving immediately.

    That covers every *future* connection and nothing about a socket already
    open - a connector holds one for as long as it runs. Callers that can
    reach the live registry must also call ``connectorhub.close_device``; this
    module deliberately knows nothing about sockets.
    """
    return db.delete_connector_device(user_id, device_id, path=path)


def device_for_token(token: object, path: Optional[str] = None) -> Optional[dict]:
    """Resolve ``{user_id, username, device_id, label}`` from a token, or None.

    ``None`` covers every failure - missing, malformed, and unknown - so the
    caller has a single "no" to answer with. On success the device's
    ``last_seen`` is stamped, which is the only reason this is not a pure
    function.
    """
    if not isinstance(token, str) or not _TOKEN_RE.match(token):
        return None
    digest = hash_token(token)
    row = db.user_for_connector_token_hash(digest, path=path)
    if row is None:
        # Same comparison work as a hit, so a valid-but-unknown token is not
        # distinguishable from a known one by response time.
        hmac.compare_digest(digest, _DUMMY_HASH)
        return None
    if not hmac.compare_digest(digest, row.get("token_hash") or ""):
        return None
    db.touch_connector_device(row["device_id"], path=path)
    return {
        "user_id": row["user_id"],
        "username": row["username"],
        "device_id": row["device_id"],
        "label": row["label"],
    }
