"""Cost and abuse limits for the cloud API.

Budgets are operator alerts, not request admission controls.  This module is
the request-time control plane: it rejects before storage writes and keeps
counters per verified installation namespace and local-user scope.

Daily counters are *durable* wherever a durable state backend is supplied.
They are the deployment's abuse and cost control, not a decoration in front of
a gateway policy: the containers run at ``minReplicas: 0``, so anything that
only lives in a process resets several times a day and enforces nothing.  The
durable counters use the same etag-guarded compare-and-swap against one table
row that :meth:`SecurityStateBackend.claim_replay` uses for nonce claims, so
this package has one concurrency story rather than two.
"""
from __future__ import annotations

import hashlib
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Final, Iterator, Mapping, Optional, Protocol, Sequence

from .security import SecurityStateBackend


# The record kind counters occupy in the shared state backend.  It is distinct
# from every auth kind, so a counter row can never be addressed as a
# credential, context, invitation, pairing code, or replay claim.
QUOTA_RECORD_KIND: Final = "quota-counter"
_COUNTER_DOMAIN: Final = b"wattracker-cloud-quota-counter-v1\x00"

# Counter subjects.  Every metric is charged twice: once to the rider's
# (namespace, local_user_scope) and once to the whole installation, so one
# scope cannot consume an installation's day and a caller who invents scope
# names cannot multiply its own allowance.
SCOPE_SUBJECT: Final = "scope"
INSTALLATION_SUBJECT: Final = "installation"

METRIC_UPLOAD_BYTES: Final = "upload_bytes"
METRIC_OBJECTS: Final = "objects"
METRIC_READ_BYTES: Final = "read_bytes"
METRIC_READ_REQUESTS: Final = "read_requests"


@dataclass(frozen=True)
class QuotaPolicy:
    max_request_bytes: int = 8 * 1024 * 1024
    max_decompressed_batch_bytes: int = 32 * 1024 * 1024
    max_objects_per_batch: int = 1_000
    max_upload_bytes_per_day: int = 256 * 1024 * 1024
    max_stored_bytes_per_scope: int = 2 * 1024 * 1024 * 1024
    max_objects_per_day: int = 100_000
    max_read_bytes_per_day: int = 512 * 1024 * 1024
    max_read_requests_per_day: int = 50_000
    global_requests_per_second: int = 100
    max_backend_concurrency: int = 2

    def __post_init__(self) -> None:
        for name in self.__dataclass_fields__:
            value = getattr(self, name)
            if not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")


class QuotaExceeded(RuntimeError):
    """A request was refused before a backend write/read was attempted."""

    def __init__(self, reason: str, *, status_code: int = 429, retry_after: int = 60):
        super().__init__(reason)
        self.reason = reason
        self.status_code = status_code
        self.retry_after = retry_after


def _utc_day(now: datetime) -> str:
    return now.date().isoformat()


def _end_of_utc_day(now: datetime) -> float:
    """Epoch seconds at the next UTC midnight.

    A counter row expires exactly when its day ends, with no grace period.
    Grace would keep yesterday's row live into today, and today's first
    charges would then land in yesterday's total.
    """

    start = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    return (start + timedelta(days=1)).timestamp()


def counter_key(subject: str, namespace: str, scope: str, metric: str) -> str:
    """Address one counter as ``(namespace, scope-or-installation, metric)``.

    The UTC day is deliberately *not* part of the row address.  It lives in
    the row, and the first charge of a new day reclaims that row in place,
    which is what keeps the table bounded by the number of (subject, metric)
    pairs instead of growing a row per day forever.  Deleting yesterday's rows
    is not an option available to this deployment: no managed identity holds a
    table ``entities/delete`` action, so a design that needs a cleanup job to
    stay bounded would repeat the unbounded ``CloudAuth`` growth that
    ``docs/cloud-sync-followups.md`` already records.

    The parts are NUL-joined and a NUL inside any part is refused: without
    that, one rider's scope could be spelled so that it digests to another
    rider's counter and spend a stranger's daily allowance.
    """

    parts = (subject, namespace, scope, metric)
    for part in parts:
        if not isinstance(part, str) or "\x00" in part:
            raise ValueError("quota counter key parts must be NUL-free text")
    joined = "\x00".join(parts).encode("utf-8")
    return hashlib.sha256(_COUNTER_DOMAIN + joined).hexdigest()


@dataclass(frozen=True)
class _Charge:
    """One metric to charge, with the ceiling that refuses it."""

    subject: str
    namespace: str
    scope: str
    metric: str
    amount: int
    ceiling: int
    reason: str

    @property
    def key(self) -> tuple[str, str, str, str]:
        return (self.subject, self.namespace, self.scope, self.metric)


class QuotaCounters(Protocol):
    """Storage for the daily counters, process-local or durable."""

    durable: bool

    def charge(self, charges: Sequence[_Charge], *, now: datetime) -> None: ...

    def value(
        self, subject: str, namespace: str, scope: str, metric: str, *, now: datetime
    ) -> int: ...


class ProcessQuotaCounters:
    """Process-local counters, for tests and local development.

    Correct within one process and worth nothing across a restart or a second
    replica, which is exactly why a production runtime refuses them.
    """

    durable = False

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._values: dict[tuple[str, str, str, str], tuple[str, int]] = {}

    def _stored(self, key: tuple[str, str, str, str], day: str) -> int:
        stored = self._values.get(key)
        if stored is None or stored[0] != day:
            return 0
        return stored[1]

    def charge(self, charges: Sequence[_Charge], *, now: datetime) -> None:
        day = _utc_day(now)
        with self._lock:
            # All-or-nothing within one process: check every metric, then
            # apply every metric.  A durable store cannot promise this across
            # replicas and does not pretend to; see DurableQuotaCounters.
            for charge in charges:
                if self._stored(charge.key, day) + charge.amount > charge.ceiling:
                    raise QuotaExceeded(charge.reason)
            for charge in charges:
                self._values[charge.key] = (
                    day,
                    self._stored(charge.key, day) + charge.amount,
                )

    def value(
        self, subject: str, namespace: str, scope: str, metric: str, *, now: datetime
    ) -> int:
        with self._lock:
            return self._stored((subject, namespace, scope, metric), _utc_day(now))


class DurableQuotaCounters:
    """Daily counters in a shared state backend, safe across replicas.

    Each metric is charged with one atomic check-and-increment against its own
    row (:meth:`SecurityStateBackend.charge_counter`).  Two consequences are
    worth being explicit about:

    * The ceiling is enforced *inside* the compare-and-swap.  A limit read in
      one round trip and applied in another is not a limit once two replicas
      run at once, which is the entire failure this replaces.
    * A multi-metric admission is therefore not atomic across metrics.  If a
      later metric refuses, the earlier ones stay charged for the day.  That
      over-counts a refused request and never under-counts an admitted one,
      which is the direction a cost control has to round in.
    """

    durable = True

    def __init__(self, backend: SecurityStateBackend, *, kind: str = QUOTA_RECORD_KIND):
        if not bool(getattr(backend, "durable", False)):
            raise ValueError("a durable quota backend is required")
        if not callable(getattr(backend, "charge_counter", None)):
            raise ValueError("quota backend cannot charge counters")
        self._backend = backend
        self._kind = kind

    def charge(self, charges: Sequence[_Charge], *, now: datetime) -> None:
        day = _utc_day(now)
        expires_at = _end_of_utc_day(now)
        epoch = now.timestamp()
        for charge in charges:
            key = counter_key(
                charge.subject, charge.namespace, charge.scope, charge.metric
            )
            try:
                total = self._backend.charge_counter(
                    self._kind,
                    key,
                    day=day,
                    amount=charge.amount,
                    ceiling=charge.ceiling,
                    expires_at=expires_at,
                    now=epoch,
                )
            except Exception as exc:
                # Fail closed.  A backend that cannot count must not also hand
                # out the resource: every caller of this path is admitting a
                # request that has not happened yet, so refusing costs a rider
                # one retry, while admitting costs an uncounted resource.
                raise QuotaExceeded(
                    "quota state unavailable", status_code=503, retry_after=30
                ) from exc
            if total is None:
                raise QuotaExceeded(charge.reason)

    def value(
        self, subject: str, namespace: str, scope: str, metric: str, *, now: datetime
    ) -> int:
        key = counter_key(subject, namespace, scope, metric)
        try:
            record = self._backend.read(self._kind, key)
        except Exception as exc:
            raise QuotaExceeded(
                "quota state unavailable", status_code=503, retry_after=30
            ) from exc
        if not isinstance(record, Mapping):
            return 0
        try:
            if float(record["expires_at"]) <= now.timestamp():
                return 0
            return max(0, int(record["value"]))
        except (KeyError, TypeError, ValueError):
            return 0


class QuotaManager:
    """Request-time admission control for the cloud planes.

    The daily counters are durable whenever the manager is constructed with a
    durable :class:`QuotaCounters`; ``durable`` reports what is actually in
    use, and ``CloudState.create(..., require_persistent_security=True)``
    refuses a manager that reports False.

    The per-second global rate window and the backend concurrency semaphore
    stay process-local on purpose: they shape one replica's instantaneous
    load, and unlike a daily budget they mean nothing once the process is
    gone.
    """

    def __init__(
        self,
        policy: Optional[QuotaPolicy] = None,
        *,
        clock=time.monotonic,
        counters: Optional[QuotaCounters] = None,
        utcnow=None,
    ):
        self.policy = policy or QuotaPolicy()
        self._clock = clock
        # `clock` is the monotonic source for the per-second window; the
        # day a counter belongs to is wall-clock UTC and never a caller's
        # claim.  They are separate seams because they answer different
        # questions and a monotonic clock has no date.
        self._utcnow = utcnow or (lambda: datetime.now(timezone.utc))
        self._lock = threading.RLock()
        self._global_window = 0.0
        self._global_requests = 0
        self._counters: QuotaCounters = counters or ProcessQuotaCounters()
        self.writes_enabled = True
        self.public_enabled = True
        self._backend = threading.BoundedSemaphore(self.policy.max_backend_concurrency)

    @property
    def durable(self) -> bool:
        """Whether the daily counters survive a restart and a replica change."""

        return bool(getattr(self._counters, "durable", False))

    def _moment(self, now: Optional[datetime] = None) -> datetime:
        if now is None:
            moment = self._utcnow()
            if moment.tzinfo is None:
                return moment.replace(tzinfo=timezone.utc)
            return moment.astimezone(timezone.utc)
        if now.tzinfo is None:
            # A naive timestamp is read as UTC, which is what every caller in
            # this package means; guessing local time would silently move a
            # deployment's day boundary.
            return now.replace(tzinfo=timezone.utc)
        return now.astimezone(timezone.utc)

    def set_writes_enabled(self, enabled: bool) -> None:
        with self._lock:
            self.writes_enabled = bool(enabled)

    def set_public_enabled(self, enabled: bool) -> None:
        with self._lock:
            self.public_enabled = bool(enabled)

    def _global_admit(self) -> None:
        now = self._clock()
        with self._lock:
            if now - self._global_window >= 1.0:
                self._global_window = now
                self._global_requests = 0
            if self._global_requests >= self.policy.global_requests_per_second:
                raise QuotaExceeded("global request rate exceeded")
            self._global_requests += 1

    def _charge(self, charges: Sequence[_Charge], now: Optional[datetime]) -> None:
        # A zero-amount charge cannot cross a ceiling that has never been
        # crossed, so it is dropped rather than paid for with a round trip.
        applied = [charge for charge in charges if charge.amount > 0]
        if not applied:
            return
        self._counters.charge(applied, now=self._moment(now))

    def admit_write(
        self,
        namespace: str,
        local_user_scope: str,
        *,
        request_bytes: int,
        decompressed_bytes: int,
        object_count: int,
        stored_bytes: int,
        installation_stored_bytes: Optional[int] = None,
        now: Optional[datetime] = None,
    ) -> None:
        self._global_admit()
        if request_bytes > self.policy.max_request_bytes:
            raise QuotaExceeded("request body too large", status_code=413)
        if decompressed_bytes > self.policy.max_decompressed_batch_bytes:
            raise QuotaExceeded("decompressed batch too large", status_code=413)
        if object_count < 1 or object_count > self.policy.max_objects_per_batch:
            raise QuotaExceeded("object quota exceeded")
        with self._lock:
            if not self.public_enabled:
                raise QuotaExceeded("public API disabled", status_code=403)
            if not self.writes_enabled:
                raise QuotaExceeded("writes disabled", status_code=403)
        installation_bytes = (
            stored_bytes
            if installation_stored_bytes is None
            else installation_stored_bytes
        )
        # Stored bytes are a *level*, read from the object store's own usage,
        # so that limit is already durable and is asserted before anything is
        # charged: a scope over its storage cap must not also burn its daily
        # upload allowance on requests that were never going to land.
        if stored_bytes > self.policy.max_stored_bytes_per_scope:
            raise QuotaExceeded("stored-byte quota exceeded", status_code=403)
        if installation_bytes > self.policy.max_stored_bytes_per_scope:
            raise QuotaExceeded(
                "installation stored-byte quota exceeded", status_code=403
            )
        self._charge(
            (
                _Charge(SCOPE_SUBJECT, namespace, local_user_scope,
                        METRIC_UPLOAD_BYTES, request_bytes,
                        self.policy.max_upload_bytes_per_day,
                        "upload quota exceeded"),
                _Charge(INSTALLATION_SUBJECT, namespace, "",
                        METRIC_UPLOAD_BYTES, request_bytes,
                        self.policy.max_upload_bytes_per_day,
                        "installation upload quota exceeded"),
                _Charge(SCOPE_SUBJECT, namespace, local_user_scope,
                        METRIC_OBJECTS, object_count,
                        self.policy.max_objects_per_day,
                        "object quota exceeded"),
                _Charge(INSTALLATION_SUBJECT, namespace, "",
                        METRIC_OBJECTS, object_count,
                        self.policy.max_objects_per_day,
                        "installation object quota exceeded"),
            ),
            now,
        )

    def admit_read(
        self,
        namespace: str,
        local_user_scope: str,
        *,
        response_bytes: int,
        count_request: bool = True,
        now: Optional[datetime] = None,
    ) -> None:
        self._global_admit()
        with self._lock:
            if not self.public_enabled:
                raise QuotaExceeded("public API disabled", status_code=403)
        requests = 1 if count_request else 0
        self._charge(
            (
                _Charge(SCOPE_SUBJECT, namespace, local_user_scope,
                        METRIC_READ_REQUESTS, requests,
                        self.policy.max_read_requests_per_day,
                        "read request quota exceeded"),
                _Charge(INSTALLATION_SUBJECT, namespace, "",
                        METRIC_READ_REQUESTS, requests,
                        self.policy.max_read_requests_per_day,
                        "installation read request quota exceeded"),
                _Charge(SCOPE_SUBJECT, namespace, local_user_scope,
                        METRIC_READ_BYTES, max(0, response_bytes),
                        self.policy.max_read_bytes_per_day,
                        "read-byte quota exceeded"),
                _Charge(INSTALLATION_SUBJECT, namespace, "",
                        METRIC_READ_BYTES, max(0, response_bytes),
                        self.policy.max_read_bytes_per_day,
                        "installation read-byte quota exceeded"),
            ),
            now,
        )

    def record_read_bytes(
        self,
        namespace: str,
        local_user_scope: str,
        response_bytes: int,
        *,
        now: Optional[datetime] = None,
    ) -> None:
        """Charge the exact response after a request was admitted."""
        if response_bytes < 0:
            raise ValueError("response_bytes must be nonnegative")
        with self._lock:
            if not self.public_enabled:
                raise QuotaExceeded("public API disabled", status_code=403)
        self._charge(
            (
                _Charge(SCOPE_SUBJECT, namespace, local_user_scope,
                        METRIC_READ_BYTES, response_bytes,
                        self.policy.max_read_bytes_per_day,
                        "read-byte quota exceeded"),
                _Charge(INSTALLATION_SUBJECT, namespace, "",
                        METRIC_READ_BYTES, response_bytes,
                        self.policy.max_read_bytes_per_day,
                        "installation read-byte quota exceeded"),
            ),
            now,
        )

    @contextmanager
    def backend_slot(self, *, timeout: float = 0.0) -> Iterator[None]:
        acquired = self._backend.acquire(timeout=timeout)
        if not acquired:
            raise QuotaExceeded("backend concurrency exceeded")
        try:
            yield
        finally:
            self._backend.release()

    def scope_status(self, namespace: str, local_user_scope: str,
                     *, now: Optional[datetime] = None) -> dict:
        moment = self._moment(now)

        def _value(metric: str) -> int:
            return self._counters.value(
                SCOPE_SUBJECT, namespace, local_user_scope, metric, now=moment
            )

        with self._lock:
            writes_enabled = self.writes_enabled
            public_enabled = self.public_enabled
        return {
            "uploaded_bytes": _value(METRIC_UPLOAD_BYTES),
            "objects_today": _value(METRIC_OBJECTS),
            "read_bytes": _value(METRIC_READ_BYTES),
            "read_requests": _value(METRIC_READ_REQUESTS),
            "writes_enabled": writes_enabled,
            "public_enabled": public_enabled,
            "durable": self.durable,
        }
