"""Security primitives for the isolated cloud package.

This module is intentionally self-contained.  It stores only digests for
bearer tokens and uses opaque, randomly generated identifiers for every
credential, context, and invitation.
"""

from __future__ import annotations

import hashlib
import hmac
import base64
import json
import logging
import re
import secrets
import threading
import time
from dataclasses import dataclass, field, replace
from typing import Any, Final, Mapping, Protocol


_log = logging.getLogger(__name__)


INSTALLATION_ID_BYTES: Final = 32
_HEX_ID_RE: Final = re.compile(r"\A[0-9a-f]{64}\Z")
_BODY_DIGEST_RE: Final = re.compile(r"\A[0-9a-f]{64}\Z")
_ID_BYTES: Final = 32
_TOKEN_BYTES: Final = 32
_DEFAULT_TTL_SECONDS: Final = 300.0
READER_CONTEXT_TTL_SECONDS: Final = _DEFAULT_TTL_SECONDS
MIN_REPLAY_TTL_SECONDS: Final = 600.0
# A contended counter row retries its compare-and-swap this many times
# before the charge is reported as unavailable.  Every retry is a lost
# race against another replica, not an error: the losing caller re-reads
# and re-applies, exactly as ``claim_replay`` re-reads before replacing.
_COUNTER_CAS_ATTEMPTS: Final = 5
_COUNTER_DAY_RE: Final = re.compile(r"\A\d{4}-\d{2}-\d{2}\Z")
_DEFAULT_CAPACITY: Final = 10_000
_CANONICAL_DOMAIN: Final = b"wattracker-cloud-request-v1\x00"
_DUMMY_DIGEST: Final = hashlib.sha256(b"wattracker-cloud-dummy").digest()
_AUTH_PARTITION: Final = "__wattracker_auth_v1__"

# Signature algorithms are a property of stored credential state, never of a
# request.  ``ecdsa-p256-sha256`` exists because Apple's Secure Enclave only
# generates P-256 keys.  Its raw ``r || s`` signature is the same 128
# hexadecimal characters as an Ed25519 signature, which is precisely why the
# algorithm must come from the credential and never from the wire.
SIGNATURE_ALGORITHMS: Final = frozenset(
    {"hmac-sha256", "ed25519", "ecdsa-p256-sha256"}
)
# A device keeps its private half in hardware, so a device credential is
# always asymmetric.  A symmetric secret -- which the server would also be
# able to sign with -- is never accepted as a device verification key.
DEVICE_SIGNATURE_ALGORITHMS: Final = frozenset({"ed25519", "ecdsa-p256-sha256"})
_ED25519_PUBLIC_KEY_BYTES: Final = 32
_P256_POINT_BYTES: Final = 65
_P256_ORDER: Final = (
    0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
)
_RAW_SIGNATURE_RE: Final = re.compile(r"\A[0-9a-f]{128}\Z")
_HMAC_SIGNATURE_RE: Final = re.compile(r"\A[0-9a-f]{64}\Z")
_CAPABILITY_RE: Final = re.compile(r"\A[a-z][a-z0-9_-]{0,31}\Z")
_MAX_CAPABILITIES: Final = 16
# Capabilities are data carried on the credential.  Nothing downstream may
# assume a device is read-only: granting a device "write" later is a data
# change plus the route assertion that already exists, not a redesign.
DEFAULT_DEVICE_CAPABILITIES: Final = frozenset({"read"})
DEFAULT_WRITER_CAPABILITIES: Final = frozenset({"read", "write"})

# ---------------------------------------------------------------------------
# Device pairing codes
# ---------------------------------------------------------------------------
# A pairing code is read aloud, typed on a phone in landscape, or scanned from
# a QR image, so its alphabet is Crockford Base32: 32 symbols with I, L, O and
# U removed, which kills the 1/I/l and 0/O confusions and makes accidental
# words unlikely.  Every symbol therefore carries exactly 5 bits.
_PAIRING_ALPHABET: Final = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_PAIRING_SYMBOLS: Final = 12
_PAIRING_GROUP: Final = 4
# 12 symbols x 5 bits.  The arithmetic that justifies this number against the
# APIM rate limit lives in docs/cloud-sync.md and in the entropy test.
DEVICE_PAIRING_CODE_BITS: Final = _PAIRING_SYMBOLS * 5
# Typed input is normalized before it is digested.  Generated codes never
# contain I, L or O, so folding them onto 1 and 0 cannot merge two distinct
# codes and cannot shrink the 2**60 code space.  U is not folded: it is simply
# not a legal symbol.
_PAIRING_FOLD: Final = {"I": "1", "L": "1", "O": "0"}
_PAIRING_STRIP: Final = frozenset("- \t")
_MAX_PAIRING_INPUT: Final = 64
DEFAULT_DEVICE_PAIRING_TTL_SECONDS: Final = 600.0
# Hard ceiling.  A pairing code is a bearer secret displayed on a screen; its
# window is deliberately shorter than an operator invitation's.
MAX_DEVICE_PAIRING_TTL_SECONDS: Final = 900.0
_PAIRING_CODE_DOMAIN: Final = b"wattracker-cloud-device-pairing-code-v1\x00"
_PAIRING_RECORD_KIND: Final = "device-pairing"

# ---------------------------------------------------------------------------
# Record kinds, and which of them the expired-row sweep may remove
# ---------------------------------------------------------------------------
# The shared auth table holds several unrelated kinds of row, and exactly two
# of them are *state a deployment cannot lose*: the budget kill switch and the
# daily quota counters.  Neither carries an expiry, and an absent kill-switch
# row reads as ENABLED -- so deleting one would silently re-enable spending on
# a deployment somebody deliberately killed, with no error and no log.
#
# The sweep is therefore an allowlist, not a filter: a kind is swept only
# because it is named here, never because a scope or expiry test happened to
# match it.  ``NEVER_SWEEP_RECORD_KINDS`` restates the exclusion by name so a
# future kind added to the allowlist by mistake is refused at construction
# rather than deleted in production.  The two sets are asserted disjoint.
#
# The kill switch and counter kinds are spelled literally here because
# ``limits`` imports *this* module; ``tests/test_cloud_security.py`` imports
# both and asserts the constants agree.
DEVICE_SEEN_RECORD_KIND: Final = "device-seen"
SWEEPABLE_RECORD_KINDS: Final = frozenset({
    "context",        # reader contexts, one per refresh
    "context-index",  # their id -> token-digest index, one per context
    "invitation",     # operator enrollment invitations
    "device-pairing", # single-use pairing codes
    "nonce",          # replay claims; the read plane claims these in CloudAuth
})
NEVER_SWEEP_RECORD_KINDS: Final = frozenset({
    "kill-switch",    # absent reads as ENABLED -- deleting re-enables spending
    "quota-counter",  # absent reads as zero -- deleting hands out free quota
    "writer",         # durable credentials; revocation is an update, not a delete
    "device",
    DEVICE_SEEN_RECORD_KIND,
    "health",         # the startup access probe
})
if SWEEPABLE_RECORD_KINDS & NEVER_SWEEP_RECORD_KINDS:  # pragma: no cover
    raise RuntimeError("a sweepable record kind is also marked never-sweep")

# How far past its own expiry a row must be before it is deleted.  Replicas do
# not share a clock, so a row deleted the instant it expires here may still be
# live over there; five minutes is longer than any skew these containers see
# and costs nothing but a handful of rows.
SWEEP_GRACE_SECONDS: Final = 300.0
# Sweeping is opportunistic work attached to a request, so a pass is bounded
# twice over -- how many rows it will look at per kind, and how many it will
# delete in total -- because both are latency somebody's refresh is paying
# for.  Anything a pass does not reach is reached by the next one.
#
# The numbers are set against the write rate they have to keep up with: four
# devices refreshing every five minutes write roughly 3,500 expiring rows a
# day, which is about 36 per quarter-hour.  A 200-row deletion budget every
# 900 seconds is five times that, so a pass is short in the steady state and
# a backlog drains over a few hours rather than in one long request.
SWEEP_BATCH_LIMIT: Final = 200
SWEEP_DELETE_LIMIT: Final = 200
SWEEP_INTERVAL_SECONDS: Final = 900.0

_MAX_DEVICE_LABEL: Final = 48
# Two riders, four devices.  The ceiling exists so a listing is bounded by
# something other than what happens to be in the table.
MAX_DEVICE_LISTING: Final = 64


class SecurityStateUnavailable(RuntimeError):
    """The durable authorization backend is unavailable."""


def _validate_counter_charge(day: object, amount: object, ceiling: object) -> None:
    """Reject a charge a backend must never persist.

    A malformed day, a non-positive amount, or a non-positive ceiling all
    describe a counter that would either never expire or never refuse, so
    they are refused at the boundary rather than stored.
    """

    if not isinstance(day, str) or _COUNTER_DAY_RE.fullmatch(day) is None:
        raise ValueError("counter day must be an ISO UTC date")
    for name, value in (("amount", amount), ("ceiling", ceiling)):
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"counter {name} must be a positive integer")


def _counter_next(
    record: object, *, day: str, amount: int, expires_at: float, now: float
) -> tuple[int, str, float]:
    """Fold one charge into a stored counter record.

    A record whose own day has ended is *reclaimed in place*: the same row
    becomes the new day's counter.  That is what keeps the table bounded by
    the number of (subject, metric) pairs rather than one row per day
    forever, and it needs no delete action -- which no deployed managed
    identity holds.

    A live record carrying a *different* day can only come from a replica
    whose clock runs ahead.  Its charge joins that record instead of
    resetting it, so the day a counter belongs to only ever moves forward
    and a lagging replica can never zero a counter already in force.

    An unreadable or negative record is treated as expired: resetting it
    counts the current request and lets the counter heal, where trusting it
    would hand out free quota and refusing forever would strand the rider.
    """

    if not isinstance(record, Mapping):
        return int(amount), day, float(expires_at)
    try:
        stored_day = str(record["day"])
        stored_value = int(record["value"])
        stored_expiry = float(record["expires_at"])
        if stored_value < 0 or _COUNTER_DAY_RE.fullmatch(stored_day) is None:
            raise ValueError("counter record is invalid")
    except (KeyError, TypeError, ValueError):
        return int(amount), day, float(expires_at)
    if stored_expiry <= now:
        return int(amount), day, float(expires_at)
    return stored_value + int(amount), stored_day, stored_expiry


class SecurityStateBackend(Protocol):
    """Minimal shared persistence contract for cloud authorization state."""

    durable: bool

    def create(self, kind: str, key: str, value: Mapping[str, Any]) -> bool: ...

    def read(self, kind: str, key: str) -> dict[str, Any] | None: ...

    def write(self, kind: str, key: str, value: Mapping[str, Any]) -> None: ...

    def consume(self, kind: str, key: str, *, now: float) -> dict[str, Any] | None: ...

    def claim_replay(
        self, kind: str, key: str, *, expires_at: float, now: float
    ) -> bool: ...

    def charge_counter(
        self,
        kind: str,
        key: str,
        *,
        day: str,
        amount: int,
        ceiling: int,
        expires_at: float,
        now: float,
    ) -> int | None: ...

    def delete(self, kind: str, key: str) -> bool: ...

    def iter_records(
        self, kind: str, *, limit: int
    ) -> list[tuple[str, dict[str, Any] | None]]: ...


class MemorySecurityStateBackend:
    """Shared-process backend used by tests; never accepted by production runtime."""

    durable = False

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._records: dict[tuple[str, str], dict[str, Any]] = {}

    def create(self, kind: str, key: str, value: Mapping[str, Any]) -> bool:
        record_key = (kind, key)
        with self._lock:
            if record_key in self._records:
                return False
            self._records[record_key] = dict(value)
            return True

    def read(self, kind: str, key: str) -> dict[str, Any] | None:
        with self._lock:
            value = self._records.get((kind, key))
            return None if value is None else dict(value)

    def write(self, kind: str, key: str, value: Mapping[str, Any]) -> None:
        with self._lock:
            self._records[(kind, key)] = dict(value)

    def consume(self, kind: str, key: str, *, now: float) -> dict[str, Any] | None:
        record_key = (kind, key)
        with self._lock:
            value = self._records.get(record_key)
            if (
                value is None
                or bool(value.get("consumed", False))
                or float(value.get("expires_at", 0)) <= now
            ):
                return None
            consumed = dict(value)
            consumed["consumed"] = True
            self._records[record_key] = consumed
            return dict(value)

    def claim_replay(
        self, kind: str, key: str, *, expires_at: float, now: float
    ) -> bool:
        record_key = (kind, key)
        with self._lock:
            value = self._records.get(record_key)
            if value is not None and float(value.get("expires_at", 0)) > now:
                return False
            self._records[record_key] = {"expires_at": float(expires_at)}
            return True

    def charge_counter(
        self,
        kind: str,
        key: str,
        *,
        day: str,
        amount: int,
        ceiling: int,
        expires_at: float,
        now: float,
    ) -> int | None:
        """Add to a daily counter, refusing to cross ``ceiling``.

        The check and the increment are one atomic step, as the claim in
        ``claim_replay`` is: a ceiling read in one round trip and applied
        in another stops being a ceiling the moment two callers run at
        once.
        """

        _validate_counter_charge(day, amount, ceiling)
        if amount > ceiling:
            return None
        record_key = (kind, key)
        with self._lock:
            total, stored_day, stored_expiry = _counter_next(
                self._records.get(record_key),
                day=day,
                amount=amount,
                expires_at=expires_at,
                now=now,
            )
            if total > ceiling:
                return None
            self._records[record_key] = {
                "day": stored_day,
                "value": total,
                "expires_at": stored_expiry,
            }
            return total

    def delete(self, kind: str, key: str) -> bool:
        with self._lock:
            return self._records.pop((kind, key), None) is not None

    def iter_records(
        self, kind: str, *, limit: int
    ) -> list[tuple[str, dict[str, Any] | None]]:
        """Return up to ``limit`` stored rows of one kind, as ``(key, value)``.

        Consumed rows are included: "consumed" is not "gone", and the sweep
        decides on the record's own expiry rather than on its consumption.
        """

        bound = _validate_capacity(limit)
        found: list[tuple[str, dict[str, Any] | None]] = []
        with self._lock:
            for (stored_kind, stored_key), value in self._records.items():
                if stored_kind != kind:
                    continue
                found.append((stored_key, dict(value)))
                if len(found) >= bound:
                    break
        return found


class AzureTableSecurityStateBackend:
    """Azure Table implementation shared by the read and sync Container Apps.

    Tokens and credentials are addressed only by SHA-256 digests. Values are
    serialized into one authenticated service-side table entity; Azure RBAC
    and private endpoints remain the storage trust boundary.
    """

    durable = True

    def __init__(self, table_client: object) -> None:
        self._table = table_client

    @classmethod
    def from_managed_identity(
        cls, storage_account_name: str, *, table_name: str = "CloudAuth"
    ) -> "AzureTableSecurityStateBackend":
        try:
            from azure.data.tables import TableServiceClient
            from azure.identity import DefaultAzureCredential
        except ImportError as exc:
            raise SecurityStateUnavailable(
                "install the cloud Azure storage dependencies"
            ) from exc
        if not isinstance(storage_account_name, str) or not storage_account_name:
            raise ValueError("storage account name is required")
        credential = DefaultAzureCredential(exclude_interactive_browser_credential=True)
        service = TableServiceClient(
            endpoint=f"https://{storage_account_name}.table.core.windows.net",
            credential=credential,
        )
        return cls(service.get_table_client(table_name))

    @staticmethod
    def _require_kind(kind: str) -> str:
        if not isinstance(kind, str) or not re.fullmatch(r"[a-z-]{1,32}", kind):
            raise ValueError("security record kind is invalid")
        return kind

    @classmethod
    def _row_key(cls, kind: str, key: str) -> str:
        cls._require_kind(kind)
        if not re.fullmatch(r"[0-9a-f]{64}", key):
            raise ValueError("security record key is invalid")
        return f"{kind}:{key}"

    @staticmethod
    def _require_etag(entity: object) -> str:
        """The concurrency token every compare-and-swap here depends on.

        A missing etag is refused rather than defaulted: an unconditional
        update would silently become a last-writer-wins overwrite, which is
        precisely the lost update these paths exist to prevent.
        """

        etag = entity.get("etag") or entity.get("odata.etag")
        if not etag:
            metadata = getattr(entity, "metadata", None)
            etag = metadata.get("etag") if isinstance(metadata, Mapping) else None
        if not etag:
            raise RuntimeError("Azure security record is missing concurrency metadata")
        return str(etag)

    @staticmethod
    def _not_found(exc: Exception) -> bool:
        return getattr(exc, "status_code", None) == 404

    @staticmethod
    def _conflict(exc: Exception) -> bool:
        return getattr(exc, "status_code", None) in (409, 412)

    @staticmethod
    def _payload(value: Mapping[str, Any]) -> str:
        return json.dumps(dict(value), sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _decode(entity: Mapping[str, Any]) -> dict[str, Any]:
        value = json.loads(str(entity["Payload"]))
        if not isinstance(value, dict):
            raise ValueError("invalid persisted security record")
        return value

    def create(self, kind: str, key: str, value: Mapping[str, Any]) -> bool:
        entity = {
            "PartitionKey": _AUTH_PARTITION,
            "RowKey": self._row_key(kind, key),
            "Payload": self._payload(value),
            "Consumed": False,
        }
        try:
            self._table.create_entity(entity)
            return True
        except Exception as exc:
            if self._conflict(exc):
                return False
            raise

    def _entity(self, kind: str, key: str) -> object | None:
        try:
            return self._table.get_entity(
                partition_key=_AUTH_PARTITION, row_key=self._row_key(kind, key)
            )
        except Exception as exc:
            if self._not_found(exc):
                return None
            raise

    def read(self, kind: str, key: str) -> dict[str, Any] | None:
        entity = self._entity(kind, key)
        if entity is None or bool(entity.get("Consumed", False)):
            return None
        return self._decode(entity)

    def write(self, kind: str, key: str, value: Mapping[str, Any]) -> None:
        self._table.upsert_entity({
            "PartitionKey": _AUTH_PARTITION,
            "RowKey": self._row_key(kind, key),
            "Payload": self._payload(value),
            "Consumed": False,
        })

    def consume(self, kind: str, key: str, *, now: float) -> dict[str, Any] | None:
        entity = self._entity(kind, key)
        if entity is None or bool(entity.get("Consumed", False)):
            return None
        value = self._decode(entity)
        if float(value.get("expires_at", 0)) <= now:
            return None
        etag = self._require_etag(entity)
        try:
            from azure.core import MatchConditions
            from azure.data.tables import UpdateMode

            entity["Consumed"] = True
            self._table.update_entity(
                entity,
                mode=UpdateMode.MERGE,
                etag=etag,
                match_condition=MatchConditions.IfNotModified,
            )
        except Exception as exc:
            if self._conflict(exc):
                return None
            raise
        return value

    def claim_replay(
        self, kind: str, key: str, *, expires_at: float, now: float
    ) -> bool:
        """Atomically claim a replay key, replacing only an expired record."""
        payload = {"expires_at": float(expires_at)}
        row_key = self._row_key(kind, key)
        try:
            self._table.create_entity({
                "PartitionKey": _AUTH_PARTITION,
                "RowKey": row_key,
                "Payload": self._payload(payload),
                "Consumed": False,
            })
            return True
        except Exception as exc:
            if not self._conflict(exc):
                raise

        entity = self._entity(kind, key)
        if entity is None:
            return False
        try:
            current = self._decode(entity)
            if float(current.get("expires_at", 0)) > now:
                return False
        except (KeyError, TypeError, ValueError):
            return False
        etag = self._require_etag(entity)
        entity["Payload"] = self._payload(payload)
        entity["Consumed"] = False
        try:
            from azure.core import MatchConditions
            from azure.data.tables import UpdateMode

            self._table.update_entity(
                entity,
                mode=UpdateMode.MERGE,
                etag=etag,
                match_condition=MatchConditions.IfNotModified,
            )
            return True
        except Exception as exc:
            if self._conflict(exc):
                return False
            raise

    def charge_counter(
        self,
        kind: str,
        key: str,
        *,
        day: str,
        amount: int,
        ceiling: int,
        expires_at: float,
        now: float,
    ) -> int | None:
        """Atomically add to a daily counter, refusing to cross a ceiling.

        Same shape as ``claim_replay``: try to create the row, and on the
        conflict that means somebody got there first, re-read and replace
        it under its etag.  The difference is what a lost race means.  A
        lost replay claim is a replay and ends the call; a lost counter
        race only means another replica charged the same row first, so the
        charge is re-applied to the value that won rather than dropped --
        dropping it is exactly the lost increment this must not have.
        """

        _validate_counter_charge(day, amount, ceiling)
        if amount > ceiling:
            # One charge larger than the whole day is refused without a
            # write: creating the row would persist a total already past
            # the ceiling, and the next charge would inherit it.
            return None
        row_key = self._row_key(kind, key)

        def _entity_for(total: int, counter_day: str, expiry: float) -> dict:
            return {
                "PartitionKey": _AUTH_PARTITION,
                "RowKey": row_key,
                "Payload": self._payload({
                    "day": counter_day,
                    "value": int(total),
                    "expires_at": float(expiry),
                }),
                "Consumed": False,
            }

        try:
            self._table.create_entity(_entity_for(amount, day, expires_at))
            return int(amount)
        except Exception as exc:
            if not self._conflict(exc):
                raise

        for _attempt in range(_COUNTER_CAS_ATTEMPTS):
            entity = self._entity(kind, key)
            if entity is None:
                try:
                    self._table.create_entity(_entity_for(amount, day, expires_at))
                    return int(amount)
                except Exception as exc:
                    if not self._conflict(exc):
                        raise
                    continue
            try:
                current: object = self._decode(entity)
            except (KeyError, TypeError, ValueError):
                current = None
            total, stored_day, stored_expiry = _counter_next(
                current, day=day, amount=amount, expires_at=expires_at, now=now
            )
            if total > ceiling:
                return None
            etag = self._require_etag(entity)
            updated = _entity_for(total, stored_day, stored_expiry)
            entity["Payload"] = updated["Payload"]
            entity["Consumed"] = False
            try:
                from azure.core import MatchConditions
                from azure.data.tables import UpdateMode

                self._table.update_entity(
                    entity,
                    mode=UpdateMode.MERGE,
                    etag=etag,
                    match_condition=MatchConditions.IfNotModified,
                )
                return total
            except Exception as exc:
                if not self._conflict(exc):
                    raise
        # Out of attempts is not "admitted": the caller turns this into a
        # refusal, because a charge that was never persisted must not buy
        # the resource it was meant to pay for.
        raise SecurityStateUnavailable("quota counter is too contended to charge")

    def delete(self, kind: str, key: str) -> bool:
        """Remove one row, addressed exactly as every other method addresses it.

        This is the only delete in the package, and it is reachable only
        through :class:`ExpiredRecordSweeper`, which will not name a kind
        outside :data:`SWEEPABLE_RECORD_KINDS`.  ``_row_key`` validates the
        kind and key here as well, so a caller cannot address a row this
        table has no other way to reach.
        """

        row_key = self._row_key(kind, key)
        try:
            self._table.delete_entity(
                partition_key=_AUTH_PARTITION, row_key=row_key
            )
            return True
        except Exception as exc:
            if self._not_found(exc):
                return False
            raise

    def iter_records(
        self, kind: str, *, limit: int
    ) -> list[tuple[str, dict[str, Any] | None]]:
        """Return up to ``limit`` rows of one kind, as ``(key, value)``.

        Row keys are ``"<kind>:<64 hex>"``, so one kind is a contiguous range
        of the partition: everything from ``"<kind>:"`` up to ``"<kind>:g"``,
        because ``g`` is the first character past the hexadecimal alphabet.
        The bound matters for correctness as well as cost -- ``"context"`` and
        ``"context-index"`` share a prefix, and ``-`` sorts before ``:``, so
        the index rows fall outside the ``context`` range rather than being
        returned as malformed members of it.

        A row whose payload will not decode is returned with a ``None`` value
        rather than skipped or repaired.  The sweep treats that as "not
        expired" and leaves it alone, which is the safe direction: a row
        nobody can read is not a row anybody may delete.
        """

        kind_text = self._require_kind(kind)
        bound = _validate_capacity(limit)
        low = f"{kind_text}:"
        high = f"{kind_text}:g"
        query = (
            f"PartitionKey eq '{_AUTH_PARTITION}' "
            f"and RowKey ge '{low}' and RowKey lt '{high}'"
        )
        found: list[tuple[str, dict[str, Any] | None]] = []
        for entity in self._table.query_entities(query_filter=query):
            row_key = str(entity["RowKey"])
            stored_kind, _, stored_key = row_key.partition(":")
            if stored_kind != kind_text or not re.fullmatch(r"[0-9a-f]{64}", stored_key):
                continue
            try:
                value: dict[str, Any] | None = self._decode(entity)
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                value = None
            found.append((stored_key, value))
            if len(found) >= bound:
                break
        return found

    def verify_access(self, *, writable: bool) -> None:
        """Fail startup if the managed identity lacks required table access."""
        probe_key = hashlib.sha256(b"wattracker-cloud-auth-access-probe").hexdigest()
        if writable:
            self.write("health", probe_key, {"ready": True})
        value = self.read("health", probe_key)
        if writable and value != {"ready": True}:
            raise RuntimeError("durable cloud auth registry is not writable")


def _record_has_expired(value: object, horizon: float) -> bool:
    """Whether a stored row's *own* expiry is already behind ``horizon``.

    Nothing else makes a row deletable.  A row with no expiry, a non-numeric
    one, a boolean one (``bool`` is an ``int`` in Python, and a JSON ``true``
    must not read as the epoch), or a payload that would not decode at all is
    reported as live and is left where it is.
    """

    if not isinstance(value, Mapping):
        return False
    expires_at = value.get("expires_at")
    if isinstance(expires_at, bool) or not isinstance(expires_at, (int, float)):
        return False
    try:
        return float(expires_at) <= horizon
    except (OverflowError, ValueError):
        return False


class ExpiredRecordSweeper:
    """Delete auth rows whose own expiry has passed, and nothing else.

    Reader contexts, their id index, invitations, pairing codes and replay
    claims all accumulate for as long as the deployment runs: a phone
    refreshing every five minutes writes three rows an hour that nothing ever
    reads again.  Azure Tables has no TTL, so somebody has to remove them.

    Three properties make that safe to attach to a request:

    * **An allowlist of kinds.**  A row is a candidate only because its kind
      is named in :data:`SWEEPABLE_RECORD_KINDS`.  The budget kill switch and
      the quota counters are excluded *by kind*, explicitly and twice -- not
      by relying on a scope or expiry test to miss them.  Deleting the
      kill-switch row would re-enable spending on a deployment somebody
      deliberately killed, and ``DurableKillSwitch`` would report the
      deployment healthy within one 30-second TTL, with no error and no log.
    * **Expiry, plus a grace window.**  Only a row already past
      ``expires_at + grace`` goes.  Deleting an *unexpired* replay claim would
      re-open a captured request to replay, so expiry is the whole test and
      the grace window absorbs clock skew between replicas.  Deleting an
      expired one cannot: a request carrying that nonce is outside the
      300-second freshness window long before its claim expires at 600.
    * **Bounded and best-effort.**  A pass touches at most ``batch_limit``
      rows per kind and runs at most once per ``interval_seconds`` per
      process.  A failure is logged and swallowed: a request must never fail
      because housekeeping did.
    """

    def __init__(
        self,
        backend: SecurityStateBackend,
        *,
        kinds: object = SWEEPABLE_RECORD_KINDS,
        grace_seconds: float = SWEEP_GRACE_SECONDS,
        batch_limit: int = SWEEP_BATCH_LIMIT,
        delete_limit: int = SWEEP_DELETE_LIMIT,
        interval_seconds: float = SWEEP_INTERVAL_SECONDS,
        clock=time.time,
    ) -> None:
        if backend is None:
            raise ValueError("a state backend is required")
        for name in ("delete", "iter_records"):
            if not callable(getattr(backend, name, None)):
                raise ValueError("backend cannot sweep expired records")
        selected = frozenset(kinds)  # type: ignore[arg-type]
        forbidden = selected & NEVER_SWEEP_RECORD_KINDS
        if forbidden:
            raise ValueError(
                "these record kinds must never be swept: "
                + ", ".join(sorted(forbidden))
            )
        if not selected or not selected <= SWEEPABLE_RECORD_KINDS:
            raise ValueError("only expiring record kinds may be swept")
        if (
            isinstance(grace_seconds, bool)
            or not isinstance(grace_seconds, (int, float))
            or grace_seconds < 0
        ):
            raise ValueError("grace_seconds must not be negative")
        self._backend = backend
        self._kinds = tuple(sorted(selected))
        self._grace = float(grace_seconds)
        self._batch_limit = _validate_capacity(batch_limit)
        self._delete_limit = _validate_capacity(delete_limit)
        self._interval = _validate_ttl(interval_seconds)
        self._clock = clock
        self._lock = threading.Lock()
        self._next_sweep: float | None = None

    @property
    def kinds(self) -> tuple[str, ...]:
        return self._kinds

    def sweep(self, *, now: float | None = None) -> int:
        """Run one bounded pass and return how many rows were removed."""

        current = self._clock() if now is None else float(now)
        horizon = current - self._grace
        removed = 0
        for kind in self._kinds:
            # Re-asserted per pass rather than trusted from construction: this
            # is the check that must hold even if the allowlist is edited.
            if kind in NEVER_SWEEP_RECORD_KINDS or kind not in SWEEPABLE_RECORD_KINDS:
                continue
            for key, value in self._backend.iter_records(
                kind, limit=self._batch_limit
            ):
                if removed >= self._delete_limit:
                    return removed
                if not _record_has_expired(value, horizon):
                    continue
                if self._backend.delete(kind, key):
                    removed += 1
        return removed

    def maybe_sweep(self, *, now: float | None = None) -> int:
        """Sweep at most once per interval; never raise into a request path."""

        current = self._clock() if now is None else float(now)
        with self._lock:
            if self._next_sweep is not None and current < self._next_sweep:
                return 0
            # Booked before the pass, not after, so a slow or failing sweep
            # cannot be retried on every request.
            self._next_sweep = current + self._interval
        try:
            return self.sweep(now=current)
        except Exception:  # pragma: no cover - defensive
            _log.warning("expired cloud auth record sweep failed", exc_info=True)
            return 0


class PublicKeyUnavailable(RuntimeError):
    """The optional asymmetric signature backend is not installed."""


def _require_bytes(value: object, name: str, *, nonempty: bool = True) -> bytes:
    if not isinstance(value, bytes) or (nonempty and not value):
        raise ValueError(f"{name} must be bytes")
    return value


def _require_text(value: object, name: str, *, nonempty: bool = True) -> str:
    if not isinstance(value, str) or (nonempty and not value):
        raise ValueError(f"{name} must be text")
    if "\x00" in value or "\r" in value or "\n" in value:
        raise ValueError(f"{name} contains invalid characters")
    return value


def _require_opaque_id(value: object, name: str) -> str:
    if not isinstance(value, str) or _HEX_ID_RE.fullmatch(value) is None:
        raise ValueError(f"{name} is invalid")
    return value


def _require_namespace(value: object) -> str:
    return _require_opaque_id(value, "namespace")


def _require_local_scope(value: object) -> str:
    scope = _require_text(value, "local_user_scope")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._~-]{0,255}", scope):
        raise ValueError("local_user_scope is too long")
    return scope


def _require_device_label(value: object) -> str:
    """Validate the human name a rider gives a device before it is stored.

    A label exists so the rider can tell "the phone I lost" from "the iPad on
    the desk" in the listing, which is the whole point of listing devices
    before revoking one.  It is caller-supplied text that is stored and later
    echoed back, so it is bounded and stripped of every control character
    here rather than wherever it is eventually rendered.
    """

    text = _require_text(value, "device label")
    if len(text) > _MAX_DEVICE_LABEL:
        raise ValueError("device label is too long")
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in text):
        raise ValueError("device label must not contain control characters")
    if not text.strip():
        raise ValueError("device label must not be blank")
    return text


def validate_device_label(value: object) -> str:
    """Validate an externally supplied device label at the trust boundary.

    Public because the pairing route must reject a bad label *before* it
    spends the single-use code, exactly as it validates the public key first.
    """

    return _require_device_label(value)


def _require_timestamp_value(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} is invalid")
    number = float(value)
    if number < 0:
        raise ValueError(f"{name} is invalid")
    return number


def _require_signature_algorithm(value: object, allowed: frozenset[str]) -> str:
    algorithm = _require_text(value, "signature_algorithm")
    if algorithm not in allowed:
        raise ValueError("signature algorithm is invalid")
    return algorithm


def _validate_verification_key(algorithm: str, key: bytes) -> None:
    """Structurally bind a stored public key to its stored algorithm.

    Length alone separates the two asymmetric encodings (32 raw Ed25519 bytes
    versus a 65-byte uncompressed SEC1 P-256 point), so a key enrolled under
    one algorithm cannot later be reinterpreted under the other.
    """

    if algorithm == "ed25519" and len(key) != _ED25519_PUBLIC_KEY_BYTES:
        raise ValueError("Ed25519 public key is invalid")
    if algorithm == "ecdsa-p256-sha256" and (
        len(key) != _P256_POINT_BYTES or key[0] != 0x04
    ):
        # Uncompressed SEC1 only: this is what Secure Enclave export produces,
        # and one accepted encoding keeps the parser at the boundary trivial.
        raise ValueError("P-256 public key is invalid")


def validate_public_key(algorithm: str, key: bytes) -> bytes:
    """Validate an externally supplied verification key before it is stored.

    Structural checks run always; the P-256 point is additionally proven to be
    on the curve here.  Callers must run this *before* spending any one-time
    enrollment state, because a key rejected afterwards leaves the exchange
    half finished with the invitation already consumed.

    Raises :class:`ValueError` for a bad key and :class:`PublicKeyUnavailable`
    when the deployment cannot check a P-256 point at all -- the two are
    deliberately distinct, because only the first is the caller's fault.
    """

    algorithm_text = _require_signature_algorithm(algorithm, SIGNATURE_ALGORITHMS)
    key_bytes = _require_bytes(key, "public_key")
    _validate_verification_key(algorithm_text, key_bytes)
    if algorithm_text == "ecdsa-p256-sha256":
        try:
            from cryptography.hazmat.primitives.asymmetric import ec
        except ImportError as exc:
            raise PublicKeyUnavailable(
                "install the cloud extra for P-256 enrollment"
            ) from exc
        try:
            ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), key_bytes)
        except (ValueError, TypeError) as exc:
            raise ValueError("P-256 public key is invalid") from exc
    return key_bytes


def _normalize_capabilities(value: object) -> frozenset[str]:
    if isinstance(value, (str, bytes)):
        raise ValueError("capabilities must be a collection")
    try:
        supplied = tuple(value)  # type: ignore[arg-type]
    except TypeError as exc:
        raise ValueError("capabilities must be a collection") from exc
    if not supplied or len(supplied) > _MAX_CAPABILITIES:
        raise ValueError("capabilities are invalid")
    normalized: set[str] = set()
    for entry in supplied:
        if not isinstance(entry, str) or _CAPABILITY_RE.fullmatch(entry) is None:
            raise ValueError("capabilities are invalid")
        normalized.add(entry)
    return frozenset(normalized)


def _normalize_timestamp(value: object) -> str:
    if isinstance(value, bool):
        raise ValueError("timestamp is invalid")
    if isinstance(value, int):
        return str(value)
    return _require_text(value, "timestamp")


def _field_bytes(value: object, name: str) -> bytes:
    if isinstance(value, bytes):
        if b"\x00" in value or b"\r" in value or b"\n" in value:
            raise ValueError(f"{name} contains invalid characters")
        return value
    return _require_text(value, name).encode("utf-8")


def _digest_token(token: str) -> bytes:
    return hashlib.sha256(token.encode("utf-8")).digest()


def _new_id() -> str:
    return secrets.token_hex(_ID_BYTES)


def _validate_ttl(ttl_seconds: object) -> float:
    if isinstance(ttl_seconds, bool):
        raise ValueError("ttl_seconds must be positive")
    try:
        ttl = float(ttl_seconds)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("ttl_seconds must be positive") from exc
    if not 0 < ttl <= 86_400:
        raise ValueError("ttl_seconds must be positive")
    return ttl


def _validate_capacity(capacity: object) -> int:
    if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity < 1:
        raise ValueError("capacity must be positive")
    return capacity


def new_installation_id() -> str:
    """Return a freshly generated opaque installation identifier."""

    return secrets.token_hex(INSTALLATION_ID_BYTES)


def derive_installation_namespace(server_secret: bytes, installation_id: str) -> str:
    """Derive an installation-specific namespace without exposing the ID."""

    secret = _require_bytes(server_secret, "server_secret")
    _require_opaque_id(installation_id, "installation_id")
    installation_bytes = bytes.fromhex(installation_id)
    if len(installation_bytes) != INSTALLATION_ID_BYTES:
        raise ValueError("installation_id is invalid")
    return hmac.new(
        secret,
        b"wattracker-cloud-installation-namespace-v1\x00" + installation_bytes,
        hashlib.sha256,
    ).hexdigest()


def canonical_request(
    method: str,
    path: str,
    namespace: str,
    timestamp: int | str,
    nonce: str | bytes,
    body_digest: str,
    idempotency_key: str,
    revision: str = "",
) -> bytes:
    """Serialize request fields unambiguously for signing.

    Length framing prevents field-boundary ambiguity.  Textual protocol
    fields reject control-line separators so a signed representation cannot
    be interpreted differently by an HTTP layer.
    """

    method_text = _require_text(method, "method").upper()
    if not re.fullmatch(r"[A-Z][A-Z0-9_\-]{0,31}", method_text):
        raise ValueError("method is invalid")
    path_text = _require_text(path, "path")
    if not path_text.startswith("/"):
        raise ValueError("path is invalid")
    namespace_text = _require_namespace(namespace)
    timestamp_text = _normalize_timestamp(timestamp)
    nonce_bytes = _field_bytes(nonce, "nonce")
    if not nonce_bytes or len(nonce_bytes) > 512:
        raise ValueError("nonce is invalid")
    if _BODY_DIGEST_RE.fullmatch(body_digest) is None:
        raise ValueError("body_digest is invalid")
    idempotency_text = _require_text(idempotency_key, "idempotency_key")
    if len(idempotency_text.encode("utf-8")) > 256:
        raise ValueError("idempotency_key is too long")
    revision_text = _require_text(revision, "revision", nonempty=False)
    values = (
        method_text.encode("ascii"),
        path_text.encode("utf-8"),
        namespace_text.encode("ascii"),
        timestamp_text.encode("utf-8"),
        nonce_bytes,
        body_digest.encode("ascii"),
        idempotency_text.encode("utf-8"),
        revision_text.encode("utf-8"),
    )
    output = bytearray(_CANONICAL_DOMAIN)
    for value in values:
        output.extend(len(value).to_bytes(4, "big"))
        output.extend(value)
    return bytes(output)


def digest_body(body: bytes) -> str:
    """Return the lowercase SHA-256 digest of a request body."""

    return hashlib.sha256(_require_bytes(body, "body", nonempty=False)).hexdigest()


def sign_request(signing_key: bytes, canonical: bytes) -> str:
    """Return a lowercase hexadecimal HMAC-SHA256 request signature."""

    return hmac.new(
        _require_bytes(signing_key, "signing_key"),
        _require_bytes(canonical, "canonical", nonempty=False),
        hashlib.sha256,
    ).hexdigest()


def verify_signature(
    signing_key: bytes,
    canonical: bytes,
    signature: str,
    *,
    algorithm: str = "hmac-sha256",
) -> bool:
    """Verify using the algorithm bound into trusted credential state.

    The algorithm is never inferred from attacker-controlled signature length,
    so an Ed25519 public key cannot be reused as an HMAC secret, and an
    Ed25519 and a raw P-256 signature -- identical in length -- cannot be
    substituted for one another.
    """

    try:
        supplied = _require_text(signature, "signature")
        if algorithm == "ed25519":
            if _RAW_SIGNATURE_RE.fullmatch(supplied) is None:
                return False
            try:
                from cryptography.exceptions import InvalidSignature
                from cryptography.hazmat.primitives.asymmetric.ed25519 import (
                    Ed25519PublicKey,
                )
            except ImportError:
                return False
            try:
                Ed25519PublicKey.from_public_bytes(signing_key).verify(
                    bytes.fromhex(supplied), canonical
                )
                return True
            except (InvalidSignature, ValueError, TypeError):
                return False
        if algorithm == "ecdsa-p256-sha256":
            # Raw 64-byte r||s only.  There is deliberately no DER parser at
            # this trust boundary: the two integers are range-checked here and
            # re-encoded by the library, so no attacker-chosen ASN.1 is parsed.
            #
            # Note the length: a raw P-256 signature is exactly 128 hexadecimal
            # characters, and so is an Ed25519 signature.  Length distinguishes
            # nothing between the two schemes.  Selecting ``algorithm`` from
            # stored credential state is therefore load-bearing twice over --
            # against the classic public-key-as-HMAC-secret downgrade, and
            # against substituting one public-key scheme for the other.  Do not
            # reintroduce any inference from the signature itself.
            if _RAW_SIGNATURE_RE.fullmatch(supplied) is None:
                return False
            r = int(supplied[:64], 16)
            s = int(supplied[64:], 16)
            # ECDSA signatures are malleable and this is ACCEPTED here: low-s
            # is not enforced, so (r, n-s) verifies wherever (r, s) does.  That
            # is safe only because nothing in this system treats a signature as
            # an identity.  Freshness comes from NonceReplayGuard, which keys on
            # (namespace, credential_id, nonce) -- never on signature bytes --
            # so a malleated copy of a spent nonce is refused exactly like the
            # original.  Enforcing low-s instead would break real clients:
            # CryptoKit and the Secure Enclave emit high-s roughly half the
            # time, and rejecting those would fail half of all iOS refreshes.
            # If a signature is ever used as a cache key, a dedup key, or an
            # audit identifier, that assumption breaks and low-s (or a digest
            # of the canonical request) must be enforced first.
            if not (1 <= r < _P256_ORDER and 1 <= s < _P256_ORDER):
                return False
            try:
                from cryptography.exceptions import InvalidSignature
                from cryptography.hazmat.primitives import hashes
                from cryptography.hazmat.primitives.asymmetric import ec
                from cryptography.hazmat.primitives.asymmetric.utils import (
                    encode_dss_signature,
                )
            except ImportError:
                return False
            try:
                public_key = ec.EllipticCurvePublicKey.from_encoded_point(
                    ec.SECP256R1(), signing_key
                )
                public_key.verify(
                    encode_dss_signature(r, s),
                    canonical,
                    ec.ECDSA(hashes.SHA256()),
                )
                return True
            except (InvalidSignature, ValueError, TypeError):
                return False
        if algorithm != "hmac-sha256":
            return False
        if _HMAC_SIGNATURE_RE.fullmatch(supplied) is None:
            return False
        expected = sign_request(signing_key, canonical)
    except (TypeError, ValueError):
        return False
    return hmac.compare_digest(expected, supplied)


def generate_signing_keypair() -> tuple[bytes, bytes]:
    """Generate ``(private_key, public_key)`` using Ed25519."""
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    except ImportError as exc:
        raise PublicKeyUnavailable(
            "install the cloud extra for Ed25519 enrollment"
        ) from exc
    private = Ed25519PrivateKey.generate()
    return private.private_bytes_raw(), private.public_key().public_bytes_raw()


def sign_request_ed25519(private_key: bytes, canonical: bytes) -> str:
    """Sign canonical request bytes with an Ed25519 private key."""
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    except ImportError as exc:
        raise PublicKeyUnavailable(
            "install the cloud extra for Ed25519 request signing"
        ) from exc
    try:
        key = Ed25519PrivateKey.from_private_bytes(
            _require_bytes(private_key, "private_key")
        )
        return key.sign(_require_bytes(canonical, "canonical", nonempty=False)).hex()
    except ValueError as exc:
        raise ValueError("private_key is invalid") from exc


def generate_p256_keypair() -> tuple[bytes, bytes]:
    """Generate ``(private_scalar, uncompressed_point)`` on NIST P-256.

    Client-side helper.  Real devices generate the private half inside the
    Secure Enclave and only ever export the uncompressed public point.
    """

    try:
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives.serialization import (
            Encoding,
            PublicFormat,
        )
    except ImportError as exc:
        raise PublicKeyUnavailable(
            "install the cloud extra for P-256 enrollment"
        ) from exc
    private = ec.generate_private_key(ec.SECP256R1())
    scalar = private.private_numbers().private_value.to_bytes(32, "big")
    point = private.public_key().public_bytes(
        Encoding.X962, PublicFormat.UncompressedPoint
    )
    return scalar, point


def sign_request_ecdsa_p256(private_key: bytes, canonical: bytes) -> str:
    """Sign canonical request bytes as raw ``r || s`` hexadecimal.

    Client-side helper; the wire encoding is fixed-width raw so the verifier
    never has to parse DER.
    """

    try:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives.asymmetric.utils import (
            decode_dss_signature,
        )
    except ImportError as exc:
        raise PublicKeyUnavailable(
            "install the cloud extra for P-256 request signing"
        ) from exc
    scalar = _require_bytes(private_key, "private_key")
    if len(scalar) != 32:
        raise ValueError("private_key is invalid")
    try:
        key = ec.derive_private_key(int.from_bytes(scalar, "big"), ec.SECP256R1())
    except ValueError as exc:
        raise ValueError("private_key is invalid") from exc
    signature = key.sign(
        _require_bytes(canonical, "canonical", nonempty=False),
        ec.ECDSA(hashes.SHA256()),
    )
    r, s = decode_dss_signature(signature)
    return f"{r:064x}{s:064x}"


@dataclass(frozen=True)
class InvitationBinding:
    """The non-secret binding returned after a valid invitation is consumed."""

    namespace: str = field(repr=False)
    local_user_scope: str = field(repr=False)
    invitation_id: str = field(repr=False)
    _proof: bytes = field(repr=False, compare=False)
    subject: str | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        _require_namespace(self.namespace)
        _require_local_scope(self.local_user_scope)
        _require_opaque_id(self.invitation_id, "invitation_id")
        _require_bytes(self._proof, "invitation proof")
        if self.subject is not None:
            _require_text(self.subject, "subject")


@dataclass(frozen=True)
class WriterCredential:
    """A writer credential and its installation/user binding.

    ``verification_key`` is excluded from repr/equality to reduce accidental
    disclosure. The server stores only the public key supplied at initial
    enrollment; lookups return the same credential object because callers
    that already possess a credential need to sign with it.
    """

    credential_id: str = field(repr=False)
    namespace: str = field(repr=False)
    local_user_scope: str = field(repr=False)
    verification_key: bytes = field(repr=False, compare=False)
    subscription_key: bytes = field(repr=False, compare=False, default=b"")
    subscription_verifier: bytes = field(repr=False, compare=False, default=b"")
    signature_algorithm: str = "ed25519"
    active: bool = True
    revoked: bool = False
    subject: str | None = field(default=None, repr=False, compare=False)
    capabilities: frozenset[str] = DEFAULT_WRITER_CAPABILITIES

    def __post_init__(self) -> None:
        _require_opaque_id(self.credential_id, "credential_id")
        _require_namespace(self.namespace)
        _require_local_scope(self.local_user_scope)
        _require_bytes(self.verification_key, "verification_key")
        _require_bytes(self.subscription_key, "subscription_key", nonempty=False)
        _require_bytes(self.subscription_verifier, "subscription_verifier", nonempty=False)
        if not self.subscription_key and not self.subscription_verifier:
            raise ValueError("subscription verifier is required")
        if not self.subscription_verifier:
            object.__setattr__(
                self, "subscription_verifier", _subscription_digest(self.subscription_key)
            )
        _require_signature_algorithm(
            self.signature_algorithm, SIGNATURE_ALGORITHMS
        )
        _validate_verification_key(self.signature_algorithm, self.verification_key)
        object.__setattr__(
            self, "capabilities", _normalize_capabilities(self.capabilities)
        )
        if self.subject is not None:
            _require_text(self.subject, "subject")
        if self.active == self.revoked:
            raise ValueError("credential status is invalid")

    def has_capability(self, capability: str) -> bool:
        """Return whether this credential carries ``capability``."""

        return isinstance(capability, str) and capability in self.capabilities

    @property
    def public_key(self) -> bytes:
        """Compatibility name for the key presented during enrollment."""

        return self.verification_key

    @property
    def signing_key(self) -> bytes:
        """Legacy alias; this value is a public verification key on servers."""

        return self.verification_key


@dataclass(frozen=True)
class DeviceCredential:
    """A paired rider device, its public key, and what it is allowed to do.

    A device credential is durable: it is the thing a phone keeps so that it
    can mint a fresh short-lived reader context without the operator token.
    What it may do is carried in ``capabilities`` rather than implied by the
    type, so widening a device to writes later is a data change plus the
    capability assertion the routes already make.
    """

    credential_id: str = field(repr=False)
    namespace: str = field(repr=False)
    local_user_scope: str = field(repr=False)
    verification_key: bytes = field(repr=False, compare=False)
    subscription_key: bytes = field(repr=False, compare=False, default=b"")
    subscription_verifier: bytes = field(repr=False, compare=False, default=b"")
    signature_algorithm: str = "ed25519"
    capabilities: frozenset[str] = DEFAULT_DEVICE_CAPABILITIES
    active: bool = True
    revoked: bool = False
    subject: str | None = field(default=None, repr=False, compare=False)
    # Listing metadata.  Neither is authorization state: nothing reads them to
    # decide anything, and ``last seen`` deliberately lives in its own row
    # rather than here (see ``CredentialRegistry.record_device_seen``).
    label: str | None = field(default=None, repr=False, compare=False)
    created_at: float = field(default=0.0, repr=False, compare=False)

    def __post_init__(self) -> None:
        _require_opaque_id(self.credential_id, "credential_id")
        _require_namespace(self.namespace)
        _require_local_scope(self.local_user_scope)
        _require_bytes(self.verification_key, "verification_key")
        _require_bytes(self.subscription_key, "subscription_key", nonempty=False)
        _require_bytes(
            self.subscription_verifier, "subscription_verifier", nonempty=False
        )
        if self.label is not None:
            object.__setattr__(self, "label", _require_device_label(self.label))
        object.__setattr__(
            self, "created_at", _require_timestamp_value(self.created_at, "created_at")
        )
        if not self.subscription_key and not self.subscription_verifier:
            raise ValueError("subscription verifier is required")
        if not self.subscription_verifier:
            object.__setattr__(
                self,
                "subscription_verifier",
                _subscription_digest(self.subscription_key),
            )
        _require_signature_algorithm(
            self.signature_algorithm, DEVICE_SIGNATURE_ALGORITHMS
        )
        _validate_verification_key(self.signature_algorithm, self.verification_key)
        object.__setattr__(
            self, "capabilities", _normalize_capabilities(self.capabilities)
        )
        if self.subject is not None:
            _require_text(self.subject, "subject")
        if self.active == self.revoked:
            raise ValueError("credential status is invalid")

    def has_capability(self, capability: str) -> bool:
        """Return whether this device was granted ``capability``."""

        return isinstance(capability, str) and capability in self.capabilities

    @property
    def public_key(self) -> bytes:
        """The key presented when the device was paired."""

        return self.verification_key

    @property
    def signing_key(self) -> bytes:
        """Alias matching :class:`WriterCredential`; a public key on servers."""

        return self.verification_key


@dataclass(frozen=True)
class ReaderContext:
    """A reader context bound to one namespace and local user scope."""

    context_id: str = field(repr=False)
    namespace: str = field(repr=False)
    local_user_scope: str = field(repr=False)
    active: bool = True
    revoked: bool = False
    subject: str | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        _require_opaque_id(self.context_id, "context_id")
        _require_namespace(self.namespace)
        _require_local_scope(self.local_user_scope)
        if self.subject is not None:
            _require_text(self.subject, "subject")
        if self.active == self.revoked:
            raise ValueError("context status is invalid")


@dataclass(frozen=True)
class _InvitationRecord:
    invitation_id: str
    installation_id: str
    namespace: str
    local_user_scope: str
    subject: str | None
    expires_at: float


@dataclass(frozen=True)
class _ContextRecord:
    context: ReaderContext
    token_digest: bytes
    expires_at: float
    # The device that minted this context, when a device did.  A reader
    # context is a bearer token with a 300-second life, so without this a
    # revoked phone keeps reading for up to five minutes after the rider
    # revokes it -- which is not a revocation, it is a delay.  Contexts minted
    # at enrollment carry no device and are unaffected.
    device_credential_id: str | None = None


def _encode_bytes(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _decode_bytes(value: object) -> bytes:
    if not isinstance(value, str):
        raise ValueError("persisted byte value is invalid")
    return base64.b64decode(value.encode("ascii"), validate=True)


def _invitation_value(record: _InvitationRecord) -> dict[str, Any]:
    return {
        "invitation_id": record.invitation_id,
        "installation_id": record.installation_id,
        "namespace": record.namespace,
        "local_user_scope": record.local_user_scope,
        "subject": record.subject,
        "expires_at": record.expires_at,
    }


def _invitation_from_value(value: Mapping[str, Any]) -> _InvitationRecord:
    subject = value.get("subject")
    return _InvitationRecord(
        _require_opaque_id(value["invitation_id"], "invitation_id"),
        _require_opaque_id(value["installation_id"], "installation_id"),
        _require_namespace(value["namespace"]),
        _require_local_scope(value["local_user_scope"]),
        None if subject is None else _require_text(subject, "subject"),
        float(value["expires_at"]),
    )


def _subscription_digest(value: bytes) -> bytes:
    return hashlib.sha256(value).digest()


def _writer_value(credential: WriterCredential) -> dict[str, Any]:
    verifier = credential.subscription_verifier or _subscription_digest(
        credential.subscription_key
    )
    return {
        "namespace": credential.namespace,
        "local_user_scope": credential.local_user_scope,
        "verification_key": _encode_bytes(credential.verification_key),
        "subscription_verifier": _encode_bytes(verifier),
        "signature_algorithm": credential.signature_algorithm,
        "capabilities": sorted(credential.capabilities),
        "active": credential.active,
        "revoked": credential.revoked,
        "subject": credential.subject,
    }


def _writer_from_value(
    credential_id: str, value: Mapping[str, Any]
) -> WriterCredential:
    subject = value.get("subject")
    capabilities = value.get("capabilities")
    return WriterCredential(
        credential_id=_require_opaque_id(credential_id, "credential_id"),
        namespace=_require_namespace(value["namespace"]),
        local_user_scope=_require_local_scope(value["local_user_scope"]),
        verification_key=_decode_bytes(value["verification_key"]),
        subscription_verifier=_decode_bytes(value["subscription_verifier"]),
        signature_algorithm=_require_text(
            value["signature_algorithm"], "signature_algorithm"
        ),
        capabilities=(
            DEFAULT_WRITER_CAPABILITIES
            if capabilities is None
            else _normalize_capabilities(capabilities)
        ),
        active=bool(value["active"]),
        revoked=bool(value["revoked"]),
        subject=None if subject is None else _require_text(subject, "subject"),
    )


def _device_value(credential: DeviceCredential) -> dict[str, Any]:
    verifier = credential.subscription_verifier or _subscription_digest(
        credential.subscription_key
    )
    return {
        # The row is *addressed* by the digest of this id, as every row here
        # is.  Carrying the id in the payload as well is what makes a device
        # listable at all -- a digest cannot be inverted, and a rider who
        # cannot see an id cannot revoke it.  It costs nothing: a credential
        # id authenticates nothing on its own (the signature and the
        # subscription key do), it is handed to the client at pairing, and
        # only the stored *digest* of the subscription key is here.
        # ``_device_id_from_value`` re-derives the digest before trusting it.
        "credential_id": credential.credential_id,
        "label": credential.label,
        "created_at": credential.created_at,
        "namespace": credential.namespace,
        "local_user_scope": credential.local_user_scope,
        "verification_key": _encode_bytes(credential.verification_key),
        "subscription_verifier": _encode_bytes(verifier),
        "signature_algorithm": credential.signature_algorithm,
        "capabilities": sorted(credential.capabilities),
        "active": credential.active,
        "revoked": credential.revoked,
        "subject": credential.subject,
    }


def _device_id_from_value(row_key: str, value: Mapping[str, Any]) -> str | None:
    """The credential id a device row claims, proven against its own address.

    A row is addressed by ``sha256(credential_id)``.  Re-deriving that digest
    from the stored id and comparing it to the row key means a payload edited
    to name a different credential describes a row that is simply skipped,
    rather than a device that appears in some other rider's listing.
    """

    stored = value.get("credential_id")
    if not isinstance(stored, str) or _HEX_ID_RE.fullmatch(stored) is None:
        return None
    try:
        expected = bytes.fromhex(row_key)
    except (TypeError, ValueError):
        return None
    if not hmac.compare_digest(_digest_token(stored), expected):
        return None
    return stored


def _device_from_value(
    credential_id: str, value: Mapping[str, Any]
) -> DeviceCredential:
    subject = value.get("subject")
    label = value.get("label")
    created_at = value.get("created_at", 0.0)
    # A persisted record without an explicit capability set is not assumed to
    # be anything; it is rejected rather than silently granted a default.
    return DeviceCredential(
        credential_id=_require_opaque_id(credential_id, "credential_id"),
        namespace=_require_namespace(value["namespace"]),
        local_user_scope=_require_local_scope(value["local_user_scope"]),
        verification_key=_decode_bytes(value["verification_key"]),
        subscription_verifier=_decode_bytes(value["subscription_verifier"]),
        signature_algorithm=_require_signature_algorithm(
            value["signature_algorithm"], DEVICE_SIGNATURE_ALGORITHMS
        ),
        capabilities=_normalize_capabilities(value["capabilities"]),
        active=bool(value["active"]),
        revoked=bool(value["revoked"]),
        subject=None if subject is None else _require_text(subject, "subject"),
        label=None if label is None else _require_device_label(label),
        created_at=(
            0.0 if created_at is None
            else _require_timestamp_value(created_at, "created_at")
        ),
    )


def _context_value(record: _ContextRecord) -> dict[str, Any]:
    context = record.context
    return {
        "context_id": context.context_id,
        "namespace": context.namespace,
        "local_user_scope": context.local_user_scope,
        "active": context.active,
        "revoked": context.revoked,
        "subject": context.subject,
        "expires_at": record.expires_at,
        "device_credential_id": record.device_credential_id,
    }


def _context_from_value(value: Mapping[str, Any]) -> _ContextRecord:
    subject = value.get("subject")
    device_credential_id = value.get("device_credential_id")
    context = ReaderContext(
        context_id=_require_opaque_id(value["context_id"], "context_id"),
        namespace=_require_namespace(value["namespace"]),
        local_user_scope=_require_local_scope(value["local_user_scope"]),
        active=bool(value["active"]),
        revoked=bool(value["revoked"]),
        subject=None if subject is None else _require_text(subject, "subject"),
    )
    return _ContextRecord(
        context,
        b"",
        float(value["expires_at"]),
        device_credential_id=(
            None if device_credential_id is None
            else _require_opaque_id(device_credential_id, "device_credential_id")
        ),
    )


@dataclass(frozen=True)
class EnrollmentInvitation:
    """Operator-created invitation returned by :meth:`EnrollmentRegistry.create`."""

    token: str = field(repr=False)
    installation_id: str = field(repr=False)
    expires_at: float
    local_user_scope: str = field(repr=False, default="")
    subject: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.token, str) or not self.token:
            raise ValueError("token is invalid")
        _require_opaque_id(self.installation_id, "installation_id")
        if self.local_user_scope:
            _require_local_scope(self.local_user_scope)
        if self.subject is not None:
            _require_text(self.subject, "subject")


class EnrollmentRegistry:
    """Bounded, one-time invitation registry.

    Only token digests are retained.  ``consume`` returns ``None`` for all
    invalid, unknown, expired, and already-consumed tokens.
    """

    def __init__(
        self,
        server_secret: bytes,
        invitation_ttl_seconds: float = 900.0,
        *,
        capacity: int = _DEFAULT_CAPACITY,
        binding_secret: bytes | None = None,
        backend: SecurityStateBackend | None = None,
        clock=time.time,
    ) -> None:
        self._server_secret = _require_bytes(server_secret, "server_secret")
        self._invitation_ttl = _validate_ttl(invitation_ttl_seconds)
        self._capacity = _validate_capacity(capacity)
        self._binding_secret = (
            _require_bytes(binding_secret, "binding_secret")
            if binding_secret is not None
            else hmac.new(
                self._server_secret,
                b"wattracker-cloud-invitation-binding-v1\x00",
                hashlib.sha256,
            ).digest()
        )
        self._backend = backend
        self._clock = clock
        self._lock = threading.RLock()
        self._records: dict[bytes, _InvitationRecord] = {}

    def _prune_locked(self, now: float) -> None:
        for digest, record in tuple(self._records.items()):
            if record.expires_at <= now:
                del self._records[digest]

    def _store_record(self, digest: bytes, record: _InvitationRecord) -> None:
        if self._backend is not None:
            if not self._backend.create("invitation", digest.hex(), _invitation_value(record)):
                raise RuntimeError("invitation identifier collision")
            return
        if len(self._records) >= self._capacity:
            raise RuntimeError("invitation capacity is full")
        self._records[digest] = record

    def create_invitation(
        self,
        namespace: str,
        local_user_scope: str,
        *,
        ttl_seconds: float = _DEFAULT_TTL_SECONDS,
        now: float | None = None,
    ) -> str:
        namespace_text = _require_namespace(namespace)
        scope_text = _require_local_scope(local_user_scope)
        ttl = _validate_ttl(ttl_seconds)
        current = self._clock() if now is None else float(now)
        token = secrets.token_urlsafe(_TOKEN_BYTES)
        digest = _digest_token(token)
        invitation_id = _new_id()
        with self._lock:
            self._prune_locked(current)
            self._store_record(digest, _InvitationRecord(
                invitation_id,
                "".join(("0" for _ in range(INSTALLATION_ID_BYTES * 2))),
                namespace_text,
                scope_text,
                None,
                current + ttl,
            ))
        return token

    def create(
        self,
        installation_id: str,
        local_user_scope: str = "",
        subject: str | None = None,
        *,
        now: float | None = None,
        ttl_seconds: float | None = None,
    ) -> EnrollmentInvitation:
        """Create an opaque, short-lived invitation object.

        The API layer is responsible for operator authorization.  This
        registry only binds the installation, local scope, and optional
        subject and enforces expiry and one-time consumption.
        """

        _require_opaque_id(installation_id, "installation_id")
        scope_text = local_user_scope or (subject or "cloud")
        _require_local_scope(scope_text)
        subject_text = None if subject is None else _require_text(subject, "subject")
        ttl = self._invitation_ttl if ttl_seconds is None else _validate_ttl(ttl_seconds)
        current = self._clock() if now is None else float(now)
        namespace = derive_installation_namespace(self._server_secret, installation_id)
        token = secrets.token_urlsafe(_TOKEN_BYTES)
        digest = _digest_token(token)
        invitation_id = _new_id()
        expires_at = current + ttl
        with self._lock:
            self._prune_locked(current)
            self._store_record(digest, _InvitationRecord(
                invitation_id,
                installation_id,
                namespace,
                scope_text,
                subject_text,
                expires_at,
            ))
        return EnrollmentInvitation(token, installation_id, expires_at, scope_text, subject_text)

    def consume(
        self,
        token: str,
        subject: str | None = None,
        *,
        now: float | None = None,
    ) -> InvitationBinding | None:
        if not isinstance(token, str) or not token or len(token) > 512:
            return None
        current = self._clock() if now is None else float(now)
        supplied = _digest_token(token)
        with self._lock:
            if self._backend is not None:
                value = self._backend.read("invitation", supplied.hex())
                if value is None:
                    hmac.compare_digest(supplied, _DUMMY_DIGEST)
                    return None
                try:
                    record = _invitation_from_value(value)
                except (KeyError, TypeError, ValueError):
                    return None
                found_digest = supplied
            else:
                self._prune_locked(current)
                found_digest: bytes | None = None
                record: _InvitationRecord | None = None
                for stored_digest, candidate in self._records.items():
                    if hmac.compare_digest(supplied, stored_digest):
                        found_digest = stored_digest
                        record = candidate
                        break
                if found_digest is None or record is None or record.expires_at <= current:
                    hmac.compare_digest(supplied, _DUMMY_DIGEST)
                    return None
            supplied_subject: str | None = None
            if subject is not None:
                try:
                    supplied_subject = _require_text(subject, "subject")
                except ValueError:
                    return None
                if record.subject is None or not hmac.compare_digest(
                    record.subject.encode("utf-8"), supplied_subject.encode("utf-8")
                ):
                    return None
            if self._backend is not None:
                consumed = self._backend.consume(
                    "invitation", found_digest.hex(), now=current
                )
                if consumed is None:
                    return None
            if self._backend is None:
                del self._records[found_digest]
            proof = hmac.new(
                self._binding_secret,
                record.invitation_id.encode("ascii")
                + record.namespace.encode("ascii")
                + record.local_user_scope.encode("utf-8")
                + (record.subject or "").encode("utf-8"),
                hashlib.sha256,
            ).digest()
            return InvitationBinding(
                record.namespace, record.local_user_scope, record.invitation_id, proof,
                record.subject,
            )

    consume_invitation = consume

    def verify_binding(self, binding: InvitationBinding) -> bool:
        if not isinstance(binding, InvitationBinding):
            return False
        try:
            expected = hmac.new(
                self._binding_secret,
                binding.invitation_id.encode("ascii")
                + binding.namespace.encode("ascii")
                + binding.local_user_scope.encode("utf-8")
                + (binding.subject or "").encode("utf-8"),
                hashlib.sha256,
            ).digest()
        except (AttributeError, UnicodeEncodeError):
            return False
        return hmac.compare_digest(expected, binding._proof)


def _validate_pairing_ttl(ttl_seconds: object) -> float:
    """Validate a pairing TTL against the hard 900-second ceiling.

    The ceiling is a security control, not a preference: the whole
    brute-force argument for a 60-bit code is stated against a bounded
    window, so a caller must not be able to widen it.
    """

    ttl = _validate_ttl(ttl_seconds)
    if ttl > MAX_DEVICE_PAIRING_TTL_SECONDS:
        raise ValueError("pairing ttl_seconds exceeds the maximum")
    return ttl


@dataclass(frozen=True)
class DevicePairingCode:
    """A freshly minted, single-use pairing code and what it is bound to.

    The desktop displays :attr:`code`; the server keeps only its digest.  The
    namespace and scope are carried here for the minting installation's own
    benefit -- they are never echoed to the device that redeems the code.
    """

    code: str = field(repr=False)
    namespace: str = field(repr=False)
    local_user_scope: str = field(repr=False)
    expires_at: float
    subject: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if normalize_pairing_code(self.code) is None:
            raise ValueError("pairing code is invalid")
        _require_namespace(self.namespace)
        _require_local_scope(self.local_user_scope)
        if self.subject is not None:
            _require_text(self.subject, "subject")


@dataclass(frozen=True)
class DevicePairingBinding:
    """The non-secret binding returned after a valid pairing code is spent."""

    namespace: str = field(repr=False)
    local_user_scope: str = field(repr=False)
    pairing_id: str = field(repr=False)
    _proof: bytes = field(repr=False, compare=False)
    subject: str | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        _require_namespace(self.namespace)
        _require_local_scope(self.local_user_scope)
        _require_opaque_id(self.pairing_id, "pairing_id")
        _require_bytes(self._proof, "pairing proof")
        if self.subject is not None:
            _require_text(self.subject, "subject")


@dataclass(frozen=True)
class _PairingRecord:
    pairing_id: str
    namespace: str
    local_user_scope: str
    subject: str | None
    expires_at: float


def _pairing_value(record: _PairingRecord) -> dict[str, Any]:
    return {
        "pairing_id": record.pairing_id,
        "namespace": record.namespace,
        "local_user_scope": record.local_user_scope,
        "subject": record.subject,
        "expires_at": record.expires_at,
    }


def _pairing_from_value(value: Mapping[str, Any]) -> _PairingRecord:
    subject = value.get("subject")
    return _PairingRecord(
        _require_opaque_id(value["pairing_id"], "pairing_id"),
        _require_namespace(value["namespace"]),
        _require_local_scope(value["local_user_scope"]),
        None if subject is None else _require_text(subject, "subject"),
        float(value["expires_at"]),
    )


def generate_pairing_code() -> str:
    """Return a fresh 60-bit pairing code in grouped display form.

    ``secrets.choice`` draws each symbol from a 32-character alphabet through
    ``randbelow``, so every symbol is uniform and independent and the code
    carries the full :data:`DEVICE_PAIRING_CODE_BITS`.
    """

    symbols = [secrets.choice(_PAIRING_ALPHABET) for _ in range(_PAIRING_SYMBOLS)]
    return "-".join(
        "".join(symbols[start : start + _PAIRING_GROUP])
        for start in range(0, _PAIRING_SYMBOLS, _PAIRING_GROUP)
    )


def normalize_pairing_code(value: object) -> str | None:
    """Return the canonical ungrouped code, or ``None`` if it is not one.

    Only hyphens and spaces are removed and only I/L/O are folded.  Anything
    else -- a wrong length, a symbol outside the alphabet, a ``U`` -- is not a
    code, and the caller must treat that exactly like an unknown code.
    """

    if not isinstance(value, str) or not value or len(value) > _MAX_PAIRING_INPUT:
        return None
    symbols: list[str] = []
    for character in value.upper():
        if character in _PAIRING_STRIP:
            continue
        folded = _PAIRING_FOLD.get(character, character)
        if folded not in _PAIRING_ALPHABET:
            return None
        symbols.append(folded)
    if len(symbols) != _PAIRING_SYMBOLS:
        return None
    return "".join(symbols)


def format_pairing_code(code: str) -> str:
    """Return the grouped display form of a canonical code."""

    canonical = normalize_pairing_code(code)
    if canonical is None:
        raise ValueError("pairing code is invalid")
    return "-".join(
        canonical[start : start + _PAIRING_GROUP]
        for start in range(0, _PAIRING_SYMBOLS, _PAIRING_GROUP)
    )


def _digest_pairing_code(canonical: str) -> bytes:
    # Domain-separated from every other token digest in this module.  A
    # pairing code and an enrollment invitation are stored under different
    # record kinds *and* hash into different spaces, so neither registry can
    # be made to spend the other's token -- which matters because an
    # enrollment token buys a writer credential and a pairing code must only
    # ever buy a read-only device.
    return hashlib.sha256(_PAIRING_CODE_DOMAIN + canonical.encode("ascii")).digest()


class DevicePairingRegistry:
    """Bounded, single-use registry of desktop-minted device pairing codes.

    Deliberately a sibling of :class:`EnrollmentRegistry` rather than a method
    on it.  The two share their one-time/expiring/indistinguishable semantics
    but must never share a token space: an enrollment invitation mints a
    *writer*, a pairing code mints a read-only *device*, and letting one be
    redeemed at the other's route would be a privilege escalation.

    Only code digests are retained.  ``consume`` returns ``None`` for every
    invalid, unknown, expired, already-consumed, and subject-mismatched code.
    """

    def __init__(
        self,
        server_secret: bytes,
        *,
        ttl_seconds: float = DEFAULT_DEVICE_PAIRING_TTL_SECONDS,
        capacity: int = _DEFAULT_CAPACITY,
        binding_secret: bytes | None = None,
        backend: SecurityStateBackend | None = None,
        clock=time.time,
    ) -> None:
        self._server_secret = _require_bytes(server_secret, "server_secret")
        self._ttl = _validate_pairing_ttl(ttl_seconds)
        self._capacity = _validate_capacity(capacity)
        self._binding_secret = (
            _require_bytes(binding_secret, "binding_secret")
            if binding_secret is not None
            else hmac.new(
                self._server_secret,
                b"wattracker-cloud-device-pairing-binding-v1\x00",
                hashlib.sha256,
            ).digest()
        )
        self._backend = backend
        self._clock = clock
        self._lock = threading.RLock()
        self._records: dict[bytes, _PairingRecord] = {}

    @property
    def ttl_seconds(self) -> float:
        return self._ttl

    def _prune_locked(self, now: float) -> None:
        for digest, record in tuple(self._records.items()):
            if record.expires_at <= now:
                del self._records[digest]

    def _store_record(self, digest: bytes, record: _PairingRecord) -> bool:
        if self._backend is not None:
            return bool(
                self._backend.create(
                    _PAIRING_RECORD_KIND, digest.hex(), _pairing_value(record)
                )
            )
        if len(self._records) >= self._capacity:
            raise RuntimeError("pairing capacity is full")
        if digest in self._records:
            return False
        self._records[digest] = record
        return True

    def create(
        self,
        namespace: str,
        local_user_scope: str,
        *,
        subject: str | None = None,
        ttl_seconds: float | None = None,
        now: float | None = None,
    ) -> DevicePairingCode:
        """Mint a code bound to an already server-derived namespace and scope.

        The caller must have authenticated the installation that owns
        ``namespace``; this registry binds what it is given and enforces only
        expiry and one-time consumption.
        """

        namespace_text = _require_namespace(namespace)
        scope_text = _require_local_scope(local_user_scope)
        subject_text = None if subject is None else _require_text(subject, "subject")
        ttl = self._ttl if ttl_seconds is None else _validate_pairing_ttl(ttl_seconds)
        current = self._clock() if now is None else float(now)
        expires_at = current + ttl
        with self._lock:
            self._prune_locked(current)
            # A 60-bit collision with a live code is vanishingly unlikely, but
            # a consumed row keeps its key forever, so retry rather than hand
            # the rider a failure.
            for _attempt in range(4):
                code = generate_pairing_code()
                canonical = normalize_pairing_code(code)
                if canonical is None:  # defensive: the generator emits legal codes
                    raise RuntimeError("pairing code generation failed")
                record = _PairingRecord(
                    _new_id(), namespace_text, scope_text, subject_text, expires_at
                )
                if self._store_record(_digest_pairing_code(canonical), record):
                    return DevicePairingCode(
                        code, namespace_text, scope_text, expires_at, subject_text
                    )
        raise RuntimeError("pairing code collision")

    def consume(
        self,
        code: object,
        subject: str | None = None,
        *,
        now: float | None = None,
    ) -> DevicePairingBinding | None:
        """Spend a pairing code once, or return ``None`` indistinguishably.

        A subject mismatch deliberately does *not* spend the code: a rider who
        redeems on the wrong account should not lose it, and the response is
        identical either way, so nothing is observable.
        """

        canonical = normalize_pairing_code(code)
        current = self._clock() if now is None else float(now)
        if canonical is None:
            hmac.compare_digest(_DUMMY_DIGEST, _DUMMY_DIGEST)
            return None
        supplied = _digest_pairing_code(canonical)
        with self._lock:
            record: _PairingRecord | None = None
            if self._backend is not None:
                value = self._backend.read(_PAIRING_RECORD_KIND, supplied.hex())
                if value is None:
                    hmac.compare_digest(supplied, _DUMMY_DIGEST)
                    return None
                try:
                    record = _pairing_from_value(value)
                except (KeyError, TypeError, ValueError):
                    return None
                if record.expires_at <= current:
                    hmac.compare_digest(supplied, _DUMMY_DIGEST)
                    return None
            else:
                self._prune_locked(current)
                for stored_digest, candidate in self._records.items():
                    if hmac.compare_digest(supplied, stored_digest):
                        record = candidate
                        break
                if record is None or record.expires_at <= current:
                    hmac.compare_digest(supplied, _DUMMY_DIGEST)
                    return None
            if record.subject is not None:
                try:
                    supplied_subject = _require_text(subject, "subject")
                except ValueError:
                    return None
                if not hmac.compare_digest(
                    record.subject.encode("utf-8"), supplied_subject.encode("utf-8")
                ):
                    return None
            if self._backend is not None:
                if self._backend.consume(
                    _PAIRING_RECORD_KIND, supplied.hex(), now=current
                ) is None:
                    return None
            else:
                del self._records[supplied]
            proof = self._proof_for(
                record.pairing_id,
                record.namespace,
                record.local_user_scope,
                record.subject,
            )
            return DevicePairingBinding(
                record.namespace,
                record.local_user_scope,
                record.pairing_id,
                proof,
                record.subject,
            )

    def _proof_for(
        self,
        pairing_id: str,
        namespace: str,
        local_user_scope: str,
        subject: str | None,
    ) -> bytes:
        return hmac.new(
            self._binding_secret,
            pairing_id.encode("ascii")
            + namespace.encode("ascii")
            + local_user_scope.encode("utf-8")
            + (subject or "").encode("utf-8"),
            hashlib.sha256,
        ).digest()

    def verify_binding(self, binding: DevicePairingBinding) -> bool:
        """Prove a binding came from this registry and was not assembled."""

        if not isinstance(binding, DevicePairingBinding):
            return False
        try:
            expected = self._proof_for(
                binding.pairing_id,
                binding.namespace,
                binding.local_user_scope,
                binding.subject,
            )
        except (AttributeError, UnicodeEncodeError):
            return False
        return hmac.compare_digest(expected, binding._proof)


class CredentialRegistry:
    """Writer and reader registry with optional shared durable persistence."""

    def __init__(
        self,
        server_secret: bytes,
        *,
        enrollment_registry: EnrollmentRegistry | None = None,
        pairing_registry: DevicePairingRegistry | None = None,
        capacity: int = _DEFAULT_CAPACITY,
        context_capacity: int = _DEFAULT_CAPACITY,
        backend: SecurityStateBackend | None = None,
        clock=time.time,
    ) -> None:
        self._server_secret = _require_bytes(server_secret, "server_secret")
        self._capacity = _validate_capacity(capacity)
        self._context_capacity = _validate_capacity(context_capacity)
        self._enrollment_registry = enrollment_registry
        self._pairing_registry = pairing_registry
        self._backend = backend
        self._clock = clock
        self._lock = threading.RLock()
        self._writers: dict[bytes, WriterCredential] = {}
        self._devices: dict[bytes, DeviceCredential] = {}
        self._device_seen: dict[bytes, float] = {}
        self._contexts: dict[bytes, _ContextRecord] = {}
        self._contexts_by_id: dict[bytes, bytes] = {}

    @staticmethod
    def _credential_digest(credential_id: str) -> bytes:
        return _digest_token(credential_id)

    @staticmethod
    def _context_digest(context_id: str) -> bytes:
        return _digest_token(context_id)

    def _find_writer_locked(self, credential_id: str) -> WriterCredential | None:
        supplied = self._credential_digest(credential_id)
        if self._backend is not None:
            value = self._backend.read("writer", supplied.hex())
            if value is None:
                return None
            try:
                found = _writer_from_value(credential_id, value)
            except (KeyError, TypeError, ValueError):
                return None
            self._writers[supplied] = found
            return found
        found: WriterCredential | None = None
        for stored_digest, credential in self._writers.items():
            if hmac.compare_digest(supplied, stored_digest):
                found = credential
        return found

    def _store_writer_locked(self, credential: WriterCredential) -> None:
        digest = self._credential_digest(credential.credential_id)
        if len(self._writers) >= self._capacity and digest not in self._writers:
            raise RuntimeError("credential capacity is full")
        if self._backend is not None:
            if not self._backend.create("writer", digest.hex(), _writer_value(credential)):
                raise RuntimeError("credential identifier collision")
        self._writers[digest] = credential

    def _write_writer_locked(self, credential: WriterCredential) -> None:
        digest = self._credential_digest(credential.credential_id)
        if self._backend is not None:
            self._backend.write("writer", digest.hex(), _writer_value(credential))
        self._writers[digest] = credential

    def enroll_writer(
        self,
        binding: InvitationBinding,
        *,
        signing_key: bytes | None = None,
        subscription_key: bytes | None = None,
        enrollment_registry: EnrollmentRegistry | None = None,
    ) -> WriterCredential:
        if not isinstance(binding, InvitationBinding):
            raise ValueError("invalid invitation binding")
        registry = enrollment_registry or self._enrollment_registry
        if registry is not None and not registry.verify_binding(binding):
            raise ValueError("invalid invitation binding")
        key = (
            _require_bytes(signing_key, "signing_key")
            if signing_key is not None
            else secrets.token_bytes(_ID_BYTES)
        )
        if len(key) != 32:
            raise ValueError("Ed25519 public key is invalid")
        subscription = (
            _require_bytes(subscription_key, "subscription_key")
            if subscription_key is not None
            else secrets.token_hex(_ID_BYTES).encode("ascii")
        )
        credential = WriterCredential(
            credential_id=_new_id(),
            namespace=binding.namespace,
            local_user_scope=binding.local_user_scope,
            verification_key=key,
            subscription_key=subscription,
            signature_algorithm="ed25519",
            subject=binding.subject,
        )
        with self._lock:
            self._store_writer_locked(credential)
        return credential

    def register_writer(
        self,
        installation_id: str,
        local_user_scope: str,
        signing_key: bytes,
        subscription_key: bytes,
        subject: str | None = None,
    ) -> WriterCredential:
        """Register a writer bound to an installation and local scope."""

        _require_opaque_id(installation_id, "installation_id")
        namespace = derive_installation_namespace(self._server_secret, installation_id)
        scope = _require_local_scope(local_user_scope)
        signing = _require_bytes(signing_key, "signing_key")
        subscription = _require_bytes(subscription_key, "subscription_key")
        subject_text = None if subject is None else _require_text(subject, "subject")
        credential = WriterCredential(
            credential_id=_new_id(),
            namespace=namespace,
            local_user_scope=scope,
            verification_key=signing,
            subscription_key=subscription,
            signature_algorithm="hmac-sha256",
            subject=subject_text,
        )
        with self._lock:
            self._store_writer_locked(credential)
        return credential

    def register_credential(self, credential: WriterCredential) -> WriterCredential:
        """Store a credential created by a separately consumed invitation."""
        if not isinstance(credential, WriterCredential):
            raise ValueError("invalid writer credential")
        digest = self._credential_digest(credential.credential_id)
        with self._lock:
            self._store_writer_locked(credential)
        return credential

    def set_writer_subject(self, credential_id: str, subject: str) -> bool:
        """Bind the verified Entra subject after one-time enrollment."""
        subject_text = _require_text(subject, "subject")
        with self._lock:
            credential = self._find_writer_locked(credential_id)
            if credential is None or credential.revoked:
                return False
            self._write_writer_locked(replace(credential, subject=subject_text))
            return True

    def authenticate_writer(
        self, credential_id: str, subscription_key: str | bytes
    ) -> WriterCredential | None:
        """Resolve a writer and compare its APIM subscription key."""
        credential = self.resolve_writer(credential_id)
        if credential is None:
            return None
        try:
            supplied = (
                subscription_key
                if isinstance(subscription_key, bytes)
                else _require_text(subscription_key, "subscription_key").encode("utf-8")
            )
        except ValueError:
            return None
        expected = credential.subscription_verifier or _subscription_digest(
            credential.subscription_key
        )
        if not hmac.compare_digest(expected, _subscription_digest(supplied)):
            return None
        return credential

    def enroll_writer_from_invitation(
        self,
        token: str,
        *,
        enrollment_registry: EnrollmentRegistry | None = None,
        signing_key: bytes | None = None,
        now: float | None = None,
    ) -> WriterCredential | None:
        registry = enrollment_registry or self._enrollment_registry
        if registry is None:
            return None
        # Validate caller-supplied key before consuming the one-time binding.
        if signing_key is not None:
            _require_bytes(signing_key, "signing_key")
        binding = registry.consume(token, now=now)
        if binding is None:
            return None
        if isinstance(binding, WriterCredential):
            return binding
        return self.enroll_writer(
            binding, signing_key=signing_key, enrollment_registry=registry
        )

    def lookup_writer(self, credential_id: str) -> WriterCredential | None:
        if not isinstance(credential_id, str) or _HEX_ID_RE.fullmatch(credential_id) is None:
            return None
        with self._lock:
            return self._find_writer_locked(credential_id)

    def get_writer(self, credential_id: str) -> WriterCredential | None:
        """Resolve only active writers; unknown and revoked are both ``None``."""

        return self.resolve_writer(credential_id)

    def resolve_writer(
        self, credential_id: str, namespace: str | None = None
    ) -> WriterCredential | None:
        if not isinstance(credential_id, str) or _HEX_ID_RE.fullmatch(credential_id) is None:
            return None
        if namespace is not None:
            try:
                namespace = _require_namespace(namespace)
            except ValueError:
                return None
        with self._lock:
            credential = self._find_writer_locked(credential_id)
            if (
                credential is None
                or not credential.active
                or credential.revoked
                or (namespace is not None and credential.namespace != namespace)
            ):
                return None
            return credential

    def revoke_writer(self, credential_id: str) -> bool:
        if not isinstance(credential_id, str) or _HEX_ID_RE.fullmatch(credential_id) is None:
            return False
        with self._lock:
            digest = self._credential_digest(credential_id)
            credential = self._find_writer_locked(credential_id)
            if credential is None or credential.revoked:
                return False
            self._write_writer_locked(
                replace(credential, active=False, revoked=True)
            )
            return True

    revoke = revoke_writer

    # ------------------------------------------------------------------
    # Paired rider devices
    # ------------------------------------------------------------------

    def _find_device_locked(self, credential_id: str) -> DeviceCredential | None:
        supplied = self._credential_digest(credential_id)
        if self._backend is not None:
            value = self._backend.read("device", supplied.hex())
            if value is None:
                return None
            try:
                found = _device_from_value(credential_id, value)
            except (KeyError, TypeError, ValueError):
                return None
            self._devices[supplied] = found
            return found
        found_device: DeviceCredential | None = None
        for stored_digest, credential in self._devices.items():
            if hmac.compare_digest(supplied, stored_digest):
                found_device = credential
        return found_device

    def _store_device_locked(self, credential: DeviceCredential) -> None:
        digest = self._credential_digest(credential.credential_id)
        if len(self._devices) >= self._capacity and digest not in self._devices:
            raise RuntimeError("credential capacity is full")
        if self._backend is not None:
            if not self._backend.create(
                "device", digest.hex(), _device_value(credential)
            ):
                raise RuntimeError("credential identifier collision")
        self._devices[digest] = credential

    def _write_device_locked(self, credential: DeviceCredential) -> None:
        digest = self._credential_digest(credential.credential_id)
        if self._backend is not None:
            self._backend.write("device", digest.hex(), _device_value(credential))
        self._devices[digest] = credential

    def register_device_for_scope(
        self,
        namespace: str,
        local_user_scope: str,
        public_key: bytes,
        *,
        signature_algorithm: str = "ed25519",
        capabilities: object = DEFAULT_DEVICE_CAPABILITIES,
        subscription_key: bytes | None = None,
        subject: str | None = None,
        label: object = None,
    ) -> DeviceCredential:
        """Pair a device against an already server-derived namespace.

        The caller supplies only a public key.  The subscription secret is
        generated here so an APIM subscription key presented by the caller is
        never promoted into the credential.
        """

        namespace_text = _require_namespace(namespace)
        scope_text = _require_local_scope(local_user_scope)
        algorithm = _require_signature_algorithm(
            signature_algorithm, DEVICE_SIGNATURE_ALGORITHMS
        )
        key = validate_public_key(algorithm, public_key)
        subscription = (
            _require_bytes(subscription_key, "subscription_key")
            if subscription_key is not None
            else secrets.token_hex(_ID_BYTES).encode("ascii")
        )
        subject_text = None if subject is None else _require_text(subject, "subject")
        label_text = None if label is None else _require_device_label(label)
        credential = DeviceCredential(
            credential_id=_new_id(),
            namespace=namespace_text,
            local_user_scope=scope_text,
            verification_key=key,
            subscription_key=subscription,
            signature_algorithm=algorithm,
            capabilities=_normalize_capabilities(capabilities),
            subject=subject_text,
            label=label_text,
            created_at=float(self._clock()),
        )
        with self._lock:
            self._store_device_locked(credential)
        return credential

    def register_device(
        self,
        installation_id: str,
        local_user_scope: str,
        public_key: bytes,
        *,
        signature_algorithm: str = "ed25519",
        capabilities: object = DEFAULT_DEVICE_CAPABILITIES,
        subscription_key: bytes | None = None,
        subject: str | None = None,
        label: object = None,
    ) -> DeviceCredential:
        """Pair a device, deriving the namespace from the installation id."""

        _require_opaque_id(installation_id, "installation_id")
        namespace = derive_installation_namespace(
            self._server_secret, installation_id
        )
        return self.register_device_for_scope(
            namespace,
            local_user_scope,
            public_key,
            signature_algorithm=signature_algorithm,
            capabilities=capabilities,
            subscription_key=subscription_key,
            subject=subject,
            label=label,
        )

    def pair_device(
        self,
        binding: DevicePairingBinding,
        public_key: bytes,
        *,
        signature_algorithm: str = "ed25519",
        capabilities: object = DEFAULT_DEVICE_CAPABILITIES,
        subscription_key: bytes | None = None,
        subject: str | None = None,
        label: object = None,
        pairing_registry: "DevicePairingRegistry | None" = None,
    ) -> DeviceCredential:
        """Register a device into the namespace a spent pairing code named.

        The namespace and scope come from ``binding`` and nowhere else, and
        the binding's HMAC proof is re-verified here so a caller that
        assembled a :class:`DevicePairingBinding` by hand -- rather than
        obtaining one from :meth:`DevicePairingRegistry.consume` -- cannot
        name a namespace it never proved ownership of.  This mirrors
        :meth:`enroll_writer`.
        """

        if not isinstance(binding, DevicePairingBinding):
            raise ValueError("invalid pairing binding")
        registry = pairing_registry or self._pairing_registry
        if registry is not None and not registry.verify_binding(binding):
            raise ValueError("invalid pairing binding")
        return self.register_device_for_scope(
            binding.namespace,
            binding.local_user_scope,
            public_key,
            signature_algorithm=signature_algorithm,
            capabilities=capabilities,
            subscription_key=subscription_key,
            subject=binding.subject if subject is None else subject,
            label=label,
        )

    def lookup_device(self, credential_id: str) -> DeviceCredential | None:
        """Resolve a device regardless of status; used by revocation tooling."""

        if (
            not isinstance(credential_id, str)
            or _HEX_ID_RE.fullmatch(credential_id) is None
        ):
            return None
        with self._lock:
            return self._find_device_locked(credential_id)

    def resolve_device(
        self, credential_id: str, namespace: str | None = None
    ) -> DeviceCredential | None:
        """Resolve only active devices; unknown and revoked are both ``None``."""

        if (
            not isinstance(credential_id, str)
            or _HEX_ID_RE.fullmatch(credential_id) is None
        ):
            return None
        if namespace is not None:
            try:
                namespace = _require_namespace(namespace)
            except ValueError:
                return None
        with self._lock:
            credential = self._find_device_locked(credential_id)
            if (
                credential is None
                or not credential.active
                or credential.revoked
                or (namespace is not None and credential.namespace != namespace)
            ):
                return None
            return credential

    def authenticate_device(
        self, credential_id: str, subscription_key: str | bytes
    ) -> DeviceCredential | None:
        """Resolve a device and compare its APIM subscription key."""

        credential = self.resolve_device(credential_id)
        if credential is None:
            return None
        try:
            supplied = (
                subscription_key
                if isinstance(subscription_key, bytes)
                else _require_text(subscription_key, "subscription_key").encode("utf-8")
            )
        except ValueError:
            return None
        expected = credential.subscription_verifier or _subscription_digest(
            credential.subscription_key
        )
        if not hmac.compare_digest(expected, _subscription_digest(supplied)):
            return None
        return credential

    def revoke_device(self, credential_id: str) -> bool:
        if (
            not isinstance(credential_id, str)
            or _HEX_ID_RE.fullmatch(credential_id) is None
        ):
            return False
        with self._lock:
            credential = self._find_device_locked(credential_id)
            if credential is None or credential.revoked:
                return False
            self._write_device_locked(
                replace(credential, active=False, revoked=True)
            )
            return True

    def revoke_device_in_scope(
        self, credential_id: str, namespace: str, local_user_scope: str
    ) -> bool | None:
        """Revoke a device that belongs to exactly this namespace and scope.

        ``None`` means "this scope has no such device", and it is deliberately
        the *same* answer for a malformed id, an unknown id, an id that names
        a device in another rider's namespace, an id in another local scope,
        and an id that names a writer credential or any other row kind rather
        than a device.  The route turns all of them into one 404, so an error
        can never confirm that a credential exists somewhere the caller cannot
        see.  ``True`` and ``False`` mean revoked now and revoked already; the
        route answers both identically, so a retry is safe and nothing about
        the caller's own scope is inferred from a status code either.

        The whole decision -- read, scope comparison, write -- happens under
        the registry lock, so a device cannot change hands between the check
        and the write.  A credential's namespace and scope are immutable
        anyway; this is belt and braces.
        """

        if (
            not isinstance(credential_id, str)
            or _HEX_ID_RE.fullmatch(credential_id) is None
        ):
            return None
        try:
            namespace_text = _require_namespace(namespace)
            scope_text = _require_local_scope(local_user_scope)
        except (TypeError, ValueError):
            return None
        with self._lock:
            credential = self._find_device_locked(credential_id)
            if credential is None:
                return None
            if not hmac.compare_digest(
                credential.namespace, namespace_text
            ) or not hmac.compare_digest(
                credential.local_user_scope, scope_text
            ):
                return None
            if credential.revoked or not credential.active:
                return False
            self._write_device_locked(
                replace(credential, active=False, revoked=True)
            )
            return True

    def list_devices_for_scope(
        self,
        namespace: str,
        local_user_scope: str,
        *,
        limit: int = MAX_DEVICE_LISTING,
    ) -> tuple[DeviceCredential, ...]:
        """Every device paired into one ``(namespace, local_user_scope)``.

        The scope is the caller's own, derived from its authenticated
        credential; no route passes a caller-supplied one.  Filtering happens
        here rather than in the route so a device belonging to another rider
        is never handed to a response builder in the first place.

        A row that will not decode, or whose stored id does not hash to its
        own row key, is skipped rather than guessed at.  Rows written before
        device listing existed carry no id and are skipped for the same
        reason -- they remain revocable by id, which is what actually matters.
        """

        namespace_text = _require_namespace(namespace)
        scope_text = _require_local_scope(local_user_scope)
        bound = _validate_capacity(limit)
        found: list[DeviceCredential] = []
        with self._lock:
            if self._backend is not None:
                for row_key, value in self._backend.iter_records(
                    "device", limit=self._capacity
                ):
                    if not isinstance(value, Mapping):
                        continue
                    stored_id = _device_id_from_value(row_key, value)
                    if stored_id is None:
                        continue
                    try:
                        credential = _device_from_value(stored_id, value)
                    except (KeyError, TypeError, ValueError):
                        continue
                    if self._matches_scope(credential, namespace_text, scope_text):
                        found.append(credential)
                        if len(found) >= bound:
                            break
            else:
                for credential in self._devices.values():
                    if self._matches_scope(credential, namespace_text, scope_text):
                        found.append(credential)
                        if len(found) >= bound:
                            break
        found.sort(key=lambda item: (item.created_at, item.credential_id))
        return tuple(found)

    @staticmethod
    def _matches_scope(
        credential: DeviceCredential, namespace: str, local_user_scope: str
    ) -> bool:
        return hmac.compare_digest(
            credential.namespace, namespace
        ) and hmac.compare_digest(credential.local_user_scope, local_user_scope)

    # ------------------------------------------------------------------
    # "Last seen", deliberately not on the credential
    # ------------------------------------------------------------------
    # A rider identifies the lost phone by when it last called home, so the
    # timestamp has to be written on the refresh path -- the hottest path
    # there is.  Writing it onto the credential row would mean a read-modify-
    # write of authorization state on every refresh, and a refresh already in
    # flight when the rider revokes that device would write the pre-revocation
    # record back and *resurrect* it.  Its own row cannot: nothing reads it to
    # authorize anything, and a lost race only misdates a display value.

    def record_device_seen(self, credential_id: str, *, now: float | None = None) -> bool:
        """Note that a device authenticated, in a row of its own."""

        if (
            not isinstance(credential_id, str)
            or _HEX_ID_RE.fullmatch(credential_id) is None
        ):
            return False
        current = self._clock() if now is None else float(now)
        digest = self._credential_digest(credential_id)
        with self._lock:
            if self._backend is not None:
                self._backend.write(
                    DEVICE_SEEN_RECORD_KIND,
                    digest.hex(),
                    {"last_seen_at": float(current)},
                )
            self._device_seen[digest] = float(current)
        return True

    def device_last_seen(self, credential_id: str) -> float | None:
        """When this device last authenticated, or ``None`` if never."""

        if (
            not isinstance(credential_id, str)
            or _HEX_ID_RE.fullmatch(credential_id) is None
        ):
            return None
        digest = self._credential_digest(credential_id)
        with self._lock:
            if self._backend is not None:
                value = self._backend.read(DEVICE_SEEN_RECORD_KIND, digest.hex())
                if not isinstance(value, Mapping):
                    return self._device_seen.get(digest)
                try:
                    seen = _require_timestamp_value(
                        value.get("last_seen_at"), "last_seen_at"
                    )
                except ValueError:
                    return None
                self._device_seen[digest] = seen
                return seen
            return self._device_seen.get(digest)

    def _prune_contexts_locked(self, now: float) -> None:
        for token_digest, record in tuple(self._contexts.items()):
            if record.expires_at <= now:
                del self._contexts[token_digest]
                self._contexts_by_id.pop(self._context_digest(record.context.context_id), None)

    def _store_context_locked(
        self, token_digest: bytes, record: _ContextRecord
    ) -> None:
        context_id_digest = self._context_digest(record.context.context_id)
        if len(self._contexts) >= self._context_capacity:
            raise RuntimeError("context capacity is full")
        if self._backend is not None:
            if not self._backend.create(
                "context", token_digest.hex(), _context_value(record)
            ):
                raise RuntimeError("context token collision")
            if not self._backend.create(
                "context-index",
                context_id_digest.hex(),
                # The expiry is carried here only so the sweep can remove the
                # index when the context it points at is gone.  Nothing reads
                # it to decide whether a context is live -- that is the
                # context row's own ``expires_at``, checked in
                # ``read_context_token``.
                {
                    "token_digest": token_digest.hex(),
                    "expires_at": float(record.expires_at),
                },
            ):
                raise RuntimeError("context identifier collision")
        self._contexts[token_digest] = record
        self._contexts_by_id[context_id_digest] = token_digest

    def _write_context_locked(
        self, token_digest: bytes, record: _ContextRecord
    ) -> None:
        if self._backend is not None:
            self._backend.write("context", token_digest.hex(), _context_value(record))
        self._contexts[token_digest] = record

    def issue_context_token(
        self,
        installation_id: str,
        local_user_scope: str,
        *,
        ttl_seconds: float = _DEFAULT_TTL_SECONDS,
        now: float | None = None,
        subject: str | None = None,
    ) -> str:
        _require_opaque_id(installation_id, "installation_id")
        namespace_text = derive_installation_namespace(self._server_secret, installation_id)
        scope_text = _require_local_scope(local_user_scope)
        subject_text = None if subject is None else _require_text(subject, "subject")
        ttl = _validate_ttl(ttl_seconds)
        current = self._clock() if now is None else float(now)
        token = secrets.token_urlsafe(_TOKEN_BYTES)
        token_digest = _digest_token(token)
        context = ReaderContext(_new_id(), namespace_text, scope_text, subject=subject_text)
        record = _ContextRecord(context, token_digest, current + ttl)
        with self._lock:
            self._prune_contexts_locked(current)
            self._store_context_locked(token_digest, record)
        return token

    def issue_reader_context(
        self,
        installation_id: str,
        local_user_scope: str,
        subject: str | None = None,
        *,
        ttl_seconds: float = _DEFAULT_TTL_SECONDS,
        now: float | None = None,
    ) -> tuple[str, ReaderContext]:
        token = self.issue_context_token(
            installation_id,
            local_user_scope,
            subject=subject,
            ttl_seconds=ttl_seconds,
            now=now,
        )
        context = self.read_context_token(token, now=now)
        if context is None:  # defensive: a just-issued token must resolve
            raise RuntimeError("context issuance failed")
        return token, context

    def issue_reader_context_for_scope(
        self,
        namespace: str,
        local_user_scope: str,
        subject: str | None = None,
        *,
        ttl_seconds: float = _DEFAULT_TTL_SECONDS,
        now: float | None = None,
        device_credential_id: str | None = None,
    ) -> tuple[str, ReaderContext]:
        """Issue a context after enrollment when only the derived namespace remains.

        ``device_credential_id`` binds the context to the device that minted
        it, so revoking that device ends this context too -- see
        :meth:`read_context_token`.  Callers that mint on behalf of a device
        must pass it; enrollment mints without one.
        """
        namespace_text = _require_namespace(namespace)
        scope_text = _require_local_scope(local_user_scope)
        subject_text = None if subject is None else _require_text(subject, "subject")
        device_id = (
            None if device_credential_id is None
            else _require_opaque_id(device_credential_id, "device_credential_id")
        )
        ttl = _validate_ttl(ttl_seconds)
        current = self._clock() if now is None else float(now)
        token = secrets.token_urlsafe(_TOKEN_BYTES)
        token_digest = _digest_token(token)
        context = ReaderContext(_new_id(), namespace_text, scope_text, subject=subject_text)
        record = _ContextRecord(
            context, token_digest, current + ttl, device_credential_id=device_id
        )
        with self._lock:
            self._prune_contexts_locked(current)
            self._store_context_locked(token_digest, record)
        return token, context

    def read_context_token(
        self,
        token: str,
        *,
        namespace: str | None = None,
        local_user_scope: str | None = None,
        subject: str | None = None,
        now: float | None = None,
    ) -> ReaderContext | None:
        if not isinstance(token, str) or not token or len(token) > 512:
            return None
        if namespace is not None:
            try:
                namespace = _require_namespace(namespace)
            except ValueError:
                return None
        if local_user_scope is not None:
            try:
                local_user_scope = _require_local_scope(local_user_scope)
            except ValueError:
                return None
        if subject is not None:
            try:
                subject = _require_text(subject, "subject")
            except ValueError:
                return None
        current = self._clock() if now is None else float(now)
        supplied = _digest_token(token)
        with self._lock:
            self._prune_contexts_locked(current)
            record: _ContextRecord | None = None
            if self._backend is not None:
                value = self._backend.read("context", supplied.hex())
                if value is not None:
                    try:
                        record = _context_from_value(value)
                    except (KeyError, TypeError, ValueError):
                        return None
                    record = replace(record, token_digest=supplied)
                    self._contexts[supplied] = record
            else:
                for stored_digest, candidate in self._contexts.items():
                    if hmac.compare_digest(supplied, stored_digest):
                        record = candidate
                        break
            if (
                record is None
                or not record.context.active
                or record.context.revoked
                or record.expires_at <= current
                or (namespace is not None and record.context.namespace != namespace)
                or (
                    local_user_scope is not None
                    and record.context.local_user_scope != local_user_scope
                )
                or (subject is not None and record.context.subject != subject)
            ):
                hmac.compare_digest(supplied, _DUMMY_DIGEST)
                return None
            # A context minted by a device is only as alive as that device.
            # Without this, revoking a lost phone would leave every context it
            # already holds serving reads until it expires -- up to five
            # minutes of exactly the access the rider just tried to end.  A
            # revoked or unknown device fails identically here, and the
            # response is the same 404 as an unknown token.
            if record.device_credential_id is not None:
                issuer = self._find_device_locked(record.device_credential_id)
                if (
                    issuer is None
                    or not issuer.active
                    or issuer.revoked
                    or not hmac.compare_digest(
                        issuer.namespace, record.context.namespace
                    )
                    or not hmac.compare_digest(
                        issuer.local_user_scope, record.context.local_user_scope
                    )
                ):
                    hmac.compare_digest(supplied, _DUMMY_DIGEST)
                    return None
            return record.context

    resolve_reader = read_context_token

    def lookup_reader(self, context_id: str) -> ReaderContext | None:
        if not isinstance(context_id, str) or _HEX_ID_RE.fullmatch(context_id) is None:
            return None
        with self._lock:
            token_digest = self._contexts_by_id.get(self._context_digest(context_id))
            if token_digest is None and self._backend is not None:
                value = self._backend.read(
                    "context-index", self._context_digest(context_id).hex()
                )
                if value is not None:
                    try:
                        token_digest = bytes.fromhex(str(value["token_digest"]))
                    except (KeyError, TypeError, ValueError):
                        return None
                    self._contexts_by_id[self._context_digest(context_id)] = token_digest
            if token_digest is None:
                return None
            record = None if self._backend is not None else self._contexts.get(token_digest)
            if self._backend is not None:
                value = self._backend.read("context", token_digest.hex())
                if value is not None:
                    try:
                        record = replace(
                            _context_from_value(value), token_digest=token_digest
                        )
                    except (KeyError, TypeError, ValueError):
                        return None
                    self._contexts[token_digest] = record
            return None if record is None else record.context

    def revoke_reader(self, context_id: str) -> bool:
        if not isinstance(context_id, str) or _HEX_ID_RE.fullmatch(context_id) is None:
            return False
        with self._lock:
            token_digest = self._contexts_by_id.get(self._context_digest(context_id))
            if token_digest is None and self._backend is not None:
                value = self._backend.read(
                    "context-index", self._context_digest(context_id).hex()
                )
                if value is not None:
                    try:
                        token_digest = bytes.fromhex(str(value["token_digest"]))
                    except (KeyError, TypeError, ValueError):
                        return False
            if token_digest is None:
                return False
            record = None if self._backend is not None else self._contexts.get(token_digest)
            if self._backend is not None:
                value = self._backend.read("context", token_digest.hex())
                if value is not None:
                    try:
                        record = replace(
                            _context_from_value(value), token_digest=token_digest
                        )
                    except (KeyError, TypeError, ValueError):
                        return False
            if record is None or record.context.revoked:
                return False
            revoked = replace(record.context, active=False, revoked=True)
            self._write_context_locked(
                token_digest, replace(record, context=revoked)
            )
            return True

    revoke_context = revoke_reader


class NonceReplayGuard:
    """Thread-safe bounded replay cache scoped by namespace and credential."""

    def __init__(
        self,
        *,
        ttl_seconds: float = MIN_REPLAY_TTL_SECONDS,
        capacity: int = _DEFAULT_CAPACITY,
        clock=time.time,
        backend: SecurityStateBackend | None = None,
    ) -> None:
        self._ttl = _validate_ttl(ttl_seconds)
        self._capacity = _validate_capacity(capacity)
        self._clock = clock
        self._backend = backend
        self._lock = threading.Lock()
        self._entries: dict[bytes, float] = {}

    @staticmethod
    def _entry_key(namespace: str, credential_id: str, nonce: str | bytes) -> bytes:
        namespace_bytes = namespace.encode("ascii")
        credential_bytes = credential_id.encode("ascii")
        nonce_bytes = _field_bytes(nonce, "nonce")
        return hashlib.sha256(
            b"wattracker-cloud-nonce-v1\x00"
            + len(namespace_bytes).to_bytes(2, "big")
            + namespace_bytes
            + len(credential_bytes).to_bytes(2, "big")
            + credential_bytes
            + len(nonce_bytes).to_bytes(4, "big")
            + nonce_bytes
        ).digest()

    def _prune_locked(self, now: float) -> None:
        for key, expires_at in tuple(self._entries.items()):
            if expires_at <= now:
                del self._entries[key]

    def check_and_record(
        self,
        namespace: str,
        credential_id: str,
        nonce: str | bytes,
        *,
        now: float | None = None,
    ) -> bool:
        try:
            namespace_text = _require_namespace(namespace)
            credential_text = _require_opaque_id(credential_id, "credential_id")
            nonce_bytes = _field_bytes(nonce, "nonce")
            if not nonce_bytes or len(nonce_bytes) > 512:
                return False
            key = self._entry_key(namespace_text, credential_text, nonce_bytes)
        except (TypeError, ValueError):
            return False
        current = self._clock() if now is None else float(now)
        if self._backend is not None:
            return self._backend.claim_replay(
                "nonce", key.hex(), expires_at=current + self._ttl, now=current
            )
        with self._lock:
            self._prune_locked(current)
            if key in self._entries:
                return False
            if len(self._entries) >= self._capacity:
                return False
            self._entries[key] = current + self._ttl
            return True

    accept = check_and_record
    check = check_and_record


__all__ = [
    "INSTALLATION_ID_BYTES",
    "DEVICE_SEEN_RECORD_KIND",
    "ExpiredRecordSweeper",
    "MAX_DEVICE_LISTING",
    "NEVER_SWEEP_RECORD_KINDS",
    "SWEEPABLE_RECORD_KINDS",
    "SWEEP_BATCH_LIMIT",
    "SWEEP_DELETE_LIMIT",
    "SWEEP_GRACE_SECONDS",
    "SWEEP_INTERVAL_SECONDS",
    "DEFAULT_DEVICE_CAPABILITIES",
    "DEFAULT_WRITER_CAPABILITIES",
    "DEVICE_SIGNATURE_ALGORITHMS",
    "SIGNATURE_ALGORITHMS",
    "READER_CONTEXT_TTL_SECONDS",
    "PublicKeyUnavailable",
    "CredentialRegistry",
    "DeviceCredential",
    "EnrollmentRegistry",
    "SecurityStateBackend",
    "SecurityStateUnavailable",
    "MemorySecurityStateBackend",
    "AzureTableSecurityStateBackend",
    "EnrollmentInvitation",
    "InvitationBinding",
    "DEVICE_PAIRING_CODE_BITS",
    "DEFAULT_DEVICE_PAIRING_TTL_SECONDS",
    "MAX_DEVICE_PAIRING_TTL_SECONDS",
    "DevicePairingBinding",
    "DevicePairingCode",
    "DevicePairingRegistry",
    "format_pairing_code",
    "generate_pairing_code",
    "normalize_pairing_code",
    "NonceReplayGuard",
    "MIN_REPLAY_TTL_SECONDS",
    "ReaderContext",
    "WriterCredential",
    "canonical_request",
    "derive_installation_namespace",
    "digest_body",
    "new_installation_id",
    "generate_p256_keypair",
    "generate_signing_keypair",
    "sign_request",
    "sign_request_ecdsa_p256",
    "sign_request_ed25519",
    "validate_device_label",
    "validate_public_key",
    "verify_signature",
]
