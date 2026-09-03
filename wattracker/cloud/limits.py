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

The budget kill switch is durable for the same reason and lives in its own
deployment-wide control backend. It is the last line of cost protection, so it
is read on the admission path with a short staleness window rather than latched
at startup, and -- unlike every quota path here -- it fails *closed* when it
cannot be read.
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


# ---------------------------------------------------------------------------
# The budget kill switch
# ---------------------------------------------------------------------------
#
# The budget actions in ``infra/azure/main.bicep`` disable writes at 80% of
# budget and the public API at 100%.  Until #181 both levels were booleans on
# one ``QuotaManager`` instance, which made them worth nothing here: these
# container apps run at ``minReplicas: 0`` and cycle constantly, so a budget
# action stopped only the replicas that happened to be up and the next cold
# start came back enabled.  The switch disabled spending and re-enabled itself
# minutes later.
#
# The state therefore lives in the same durable backend as the daily counters
# and is read on the admission path, not at startup.

# Its own record kind, distinct from every auth kind and from the counters, so
# a kill-switch row can never be addressed as a credential, a replay claim or a
# quota counter -- and neither of those can be addressed as the kill switch.
KILL_SWITCH_RECORD_KIND: Final = "kill-switch"
_KILL_SWITCH_DOMAIN: Final = b"wattracker-cloud-kill-switch-v1\x00"
# One deployment-wide row.  The budget is a property of the Azure subscription,
# not of a rider, so there is nothing here to key by namespace or scope.
KILL_SWITCH_KEY: Final = hashlib.sha256(_KILL_SWITCH_DOMAIN + b"deployment").hexdigest()

# How long a replica may serve a kill state it read earlier.
#
# 30 seconds is chosen against the two costs it sits between.  Downward: a
# budget action must actually stop spending, and an hour of continued traffic
# after the switch is thrown is not a kill switch, it is a log entry.  Upward:
# a read per request would put a table transaction in front of every admission
# on a deployment whose entire monthly bill is a few dollars.  At 30 seconds a
# replica reads at most twice a minute no matter how much traffic it takes,
# which is negligible, and the worst case after an operator throws the switch
# is 30 seconds of writes on replicas that were already warm.  A cold replica
# has no cache at all and reads the state on its first request, which is the
# whole point of the change.
KILL_SWITCH_TTL_SECONDS: Final = 30.0
# A ceiling on what any caller may configure.  A misconfigured hour-long window
# would reintroduce exactly the failure this replaces, quietly, so it is
# refused at construction rather than trusted to review.
KILL_SWITCH_MAX_TTL_SECONDS: Final = 60.0
_MAX_KILL_SWITCH_REASON: Final = 200


class KillSwitchUnavailable(RuntimeError):
    """The durable kill state could not be read, or is not intelligible.

    Every caller turns this into a refusal.  See :meth:`QuotaManager.kill_state`
    for why this one flag fails closed where the quota paths do not.
    """


@dataclass(frozen=True)
class KillSwitchState:
    """The two levels the budget already distinguishes.

    ``public_enabled`` is the wider level: every route, read or write, checks
    it, so clearing it stops the deployment.  ``writes_enabled`` stops only the
    sync plane's admissions.  They are stored independently because the two
    budget thresholds fire independently and may fire in either order.
    """

    writes_enabled: bool
    public_enabled: bool
    reason: str = ""
    updated_at: float = 0.0


KILL_SWITCH_ENABLED: Final = KillSwitchState(writes_enabled=True, public_enabled=True)


def _kill_switch_reason(value: object) -> str:
    """Bound the operator's note before it is persisted.

    It is never reflected in a response, but it is written to shared state by
    an automation hook, so it is length-bounded and refused if it carries
    control characters rather than stored as whatever arrived.
    """

    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError("kill switch reason must be text")
    if len(value) > _MAX_KILL_SWITCH_REASON:
        raise ValueError("kill switch reason is too long")
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        raise ValueError("kill switch reason must not contain control characters")
    return value


def _kill_switch_payload(state: "KillSwitchState") -> dict:
    return {
        "writes_enabled": bool(state.writes_enabled),
        "public_enabled": bool(state.public_enabled),
        "reason": state.reason,
        "updated_at": float(state.updated_at),
    }


def _kill_switch_from_record(record: object) -> KillSwitchState:
    """Parse a stored kill-switch row, refusing anything ambiguous.

    Deliberately unlike ``wattracker.cloud.security._counter_next``, which
    treats an unreadable counter row as expired and resets it.  That is right
    for a counter: refusing forever would strand a rider on one corrupt row,
    and the cost of healing is bounded by the ceiling that is immediately
    re-applied.  Here there is no bound.  A row that cannot be understood is
    the state of a deployment that may already be over budget, so it is
    reported as unavailable -- and every caller refuses -- rather than read as
    "enabled".
    """

    if not isinstance(record, Mapping):
        raise KillSwitchUnavailable("kill switch record is malformed")
    writes = record.get("writes_enabled")
    public = record.get("public_enabled")
    if not isinstance(writes, bool) or not isinstance(public, bool):
        raise KillSwitchUnavailable("kill switch record is malformed")
    reason = record.get("reason", "")
    updated = record.get("updated_at", 0.0)
    return KillSwitchState(
        writes_enabled=writes,
        public_enabled=public,
        # Advisory fields only: a bad one describes the row badly, it does not
        # make the two levels above less true, so it is normalized rather than
        # escalated into an outage.
        reason=reason if isinstance(reason, str) else "",
        updated_at=(
            float(updated)
            if isinstance(updated, (int, float)) and not isinstance(updated, bool)
            else 0.0
        ),
    )


class KillSwitch(Protocol):
    """The kill state, process-local or durable."""

    durable: bool

    def state(self) -> KillSwitchState: ...

    def set(
        self, *, writes_enabled: bool, public_enabled: bool, reason: str = ""
    ) -> KillSwitchState: ...

    def set_writes_enabled(
        self, enabled: bool, *, reason: str = ""
    ) -> KillSwitchState: ...

    def set_public_enabled(
        self, enabled: bool, *, reason: str = ""
    ) -> KillSwitchState: ...


class ProcessKillSwitch:
    """A kill switch in one process, for tests and local development.

    Worth nothing across a restart or a second replica, which is why
    ``CloudState.create(..., require_persistent_security=True)`` refuses it.
    """

    durable = False

    def __init__(self, *, wallclock=time.time) -> None:
        self._lock = threading.RLock()
        self._wallclock = wallclock
        self._state = KILL_SWITCH_ENABLED

    def state(self) -> KillSwitchState:
        with self._lock:
            return self._state

    def set(
        self, *, writes_enabled: bool, public_enabled: bool, reason: str = ""
    ) -> KillSwitchState:
        state = KillSwitchState(
            writes_enabled=bool(writes_enabled),
            public_enabled=bool(public_enabled),
            reason=_kill_switch_reason(reason),
            updated_at=float(self._wallclock()),
        )
        with self._lock:
            self._state = state
        return state

    def set_writes_enabled(
        self, enabled: bool, *, reason: str = ""
    ) -> KillSwitchState:
        with self._lock:
            current = self._state
            state = KillSwitchState(
                writes_enabled=bool(enabled),
                public_enabled=current.public_enabled,
                reason=_kill_switch_reason(reason),
                updated_at=float(self._wallclock()),
            )
            self._state = state
            return state

    def set_public_enabled(
        self, enabled: bool, *, reason: str = ""
    ) -> KillSwitchState:
        with self._lock:
            current = self._state
            state = KillSwitchState(
                writes_enabled=current.writes_enabled,
                public_enabled=bool(enabled),
                reason=_kill_switch_reason(reason),
                updated_at=float(self._wallclock()),
            )
            self._state = state
            return state


class DurableKillSwitch:
    """The kill state in a shared backend row, read on the admission path.

    Two properties matter and both are tested:

    * A replica that has just started has no cache, so its first admitted
      request reads the row.  A deployment that was killed comes back killed.
    * The cache expires on a monotonic clock and is *replaced*, never extended.
      A cached "enabled" cannot outlive :data:`KILL_SWITCH_TTL_SECONDS`, and a
      failed refresh drops the cache instead of falling back to it.

    Explicit operator declarations use the backend's plain upsert. Partial
    level changes use an etag-guarded update so a delayed 80% action cannot
    restore a public API already disabled at 100%. Clearing the switch is an
    *update* that stores both levels enabled and never a delete -- no deployed
    managed identity holds a table ``entities/delete`` action (``main.bicep``
    grants read, add and update only), the same constraint that shaped #179's
    reclaim-in-place counters.
    """

    durable = True

    def __init__(
        self,
        backend: SecurityStateBackend,
        *,
        ttl_seconds: float = KILL_SWITCH_TTL_SECONDS,
        kind: str = KILL_SWITCH_RECORD_KIND,
        key: str = KILL_SWITCH_KEY,
        clock=time.monotonic,
        wallclock=time.time,
    ) -> None:
        if not bool(getattr(backend, "durable", False)):
            raise ValueError("a durable kill switch backend is required")
        if (
            isinstance(ttl_seconds, bool)
            or not isinstance(ttl_seconds, (int, float))
            or ttl_seconds <= 0
            or ttl_seconds > KILL_SWITCH_MAX_TTL_SECONDS
        ):
            raise ValueError(
                "kill switch staleness window must be positive and at most "
                f"{KILL_SWITCH_MAX_TTL_SECONDS} seconds"
            )
        self._backend = backend
        self._ttl = float(ttl_seconds)
        self._kind = kind
        self._key = key
        self._clock = clock
        self._wallclock = wallclock
        self._lock = threading.RLock()
        self._cached: Optional[tuple[float, KillSwitchState]] = None

    @property
    def ttl_seconds(self) -> float:
        return self._ttl

    def _load(self) -> KillSwitchState:
        try:
            record = self._backend.read(self._kind, self._key)
        except Exception as exc:
            raise KillSwitchUnavailable("kill switch state is unreadable") from exc
        if record is None:
            # No row has ever been written.  That is the steady state of a
            # deployment nobody has killed, and the only reading of "absent"
            # that is not a guess: a row is created the first time the switch
            # is set and is never deleted afterwards.
            return KILL_SWITCH_ENABLED
        return _kill_switch_from_record(record)

    def state(self) -> KillSwitchState:
        now = self._clock()
        with self._lock:
            cached = self._cached
        if cached is not None and now < cached[0]:
            return cached[1]
        try:
            state = self._load()
        except Exception:
            # Drop whatever was cached before re-raising.  A refresh that
            # failed must not leave a value behind that a racing thread could
            # then serve, and the caller is refusing this request anyway.
            with self._lock:
                self._cached = None
            raise
        with self._lock:
            self._cached = (now + self._ttl, state)
        return state

    def set(
        self, *, writes_enabled: bool, public_enabled: bool, reason: str = ""
    ) -> KillSwitchState:
        state = KillSwitchState(
            writes_enabled=bool(writes_enabled),
            public_enabled=bool(public_enabled),
            reason=_kill_switch_reason(reason),
            updated_at=float(self._wallclock()),
        )
        try:
            self._backend.write(self._kind, self._key, _kill_switch_payload(state))
        except Exception as exc:
            raise KillSwitchUnavailable(
                "kill switch state could not be written"
            ) from exc
        with self._lock:
            # This replica stops serving its own stale view immediately; every
            # other replica converges within the staleness window.
            self._cached = None
        return state

    def _update_level(self, *, writes_enabled: bool | None = None,
                      public_enabled: bool | None = None,
                      reason: str = "") -> KillSwitchState:
        reason_text = _kill_switch_reason(reason)
        updated_at = float(self._wallclock())
        # Check availability before attempting the transform. The update below
        # is the atomic source of truth, but this read keeps an unavailable
        # control table from being mistaken for a successful state transition.
        self._load()

        def transform(current: Mapping[str, object] | None) -> Mapping[str, object]:
            previous = (
                KILL_SWITCH_ENABLED
                if current is None
                else _kill_switch_from_record(current)
            )
            return _kill_switch_payload(KillSwitchState(
                writes_enabled=(
                    previous.writes_enabled
                    if writes_enabled is None
                    else bool(writes_enabled)
                ),
                public_enabled=(
                    previous.public_enabled
                    if public_enabled is None
                    else bool(public_enabled)
                ),
                reason=reason_text,
                updated_at=updated_at,
            ))

        update = getattr(self._backend, "update", None)
        if not callable(update):
            raise KillSwitchUnavailable(
                "kill switch backend cannot perform atomic updates"
            )
        try:
            value = update(self._kind, self._key, transform)
            state = _kill_switch_from_record(value)
        except KillSwitchUnavailable:
            raise
        except Exception as exc:
            raise KillSwitchUnavailable(
                "kill switch state could not be updated"
            ) from exc
        with self._lock:
            self._cached = None
        return state

    def set_writes_enabled(
        self, enabled: bool, *, reason: str = ""
    ) -> KillSwitchState:
        return self._update_level(writes_enabled=bool(enabled), reason=reason)

    def set_public_enabled(
        self, enabled: bool, *, reason: str = ""
    ) -> KillSwitchState:
        return self._update_level(public_enabled=bool(enabled), reason=reason)


# ---------------------------------------------------------------------------
# Operator entry points
#
# #169's operator CLI is the intended caller.  Until it exists these are the
# supported way to throw and clear the switch, and they take a backend rather
# than a running app so that an operator can act on a deployment whose replicas
# are all scaled to zero.
# ---------------------------------------------------------------------------


def read_kill_switch(backend: SecurityStateBackend, **kwargs) -> KillSwitchState:
    """Read the live kill state, uncached.

    Raises :class:`KillSwitchUnavailable` rather than reporting "enabled" for a
    row it could not read, so an operator is never told the deployment is
    serving when nobody knows.
    """

    return DurableKillSwitch(backend, **kwargs).state()


def set_kill_switch(
    backend: SecurityStateBackend,
    *,
    writes_enabled: bool,
    public_enabled: bool,
    reason: str = "",
    **kwargs,
) -> KillSwitchState:
    """Declare both levels outright.

    Both are required.  A partial update would have to read the level it is not
    changing, and a read that fails during an incident is exactly when the
    operator most needs the write to land; requiring the whole desired state
    removes that dependency and any question of what a repeated or out-of-order
    budget action does.
    """

    return DurableKillSwitch(backend, **kwargs).set(
        writes_enabled=writes_enabled,
        public_enabled=public_enabled,
        reason=reason,
    )


def disable_writes(
    backend: SecurityStateBackend, *, reason: str = "budget-80", **kwargs
) -> KillSwitchState:
    """The 80% budget action: stop writes, leave the public API as it is.

    It reads first and then uses an etag-guarded update, so an 80% action
    delayed behind a concurrent 100% action cannot re-enable the public API.
    It raises rather than guessing if the read or update fails.
    ``disable_public_api`` is the action that needs no read, which is the right
    way round: the more severe the action, the fewer preconditions.
    """

    switch = DurableKillSwitch(backend, **kwargs)
    return switch.set_writes_enabled(False, reason=reason)


def disable_public_api(
    backend: SecurityStateBackend, *, reason: str = "budget-100", **kwargs
) -> KillSwitchState:
    """The 100% budget action: stop everything.

    Writes are disabled with it, so the stored state cannot describe a
    deployment whose public API is off while its write level says otherwise.
    Nothing is read first: this action only ever removes capability, so it must
    work when the current state does not.
    """

    return DurableKillSwitch(backend, **kwargs).set(
        writes_enabled=False, public_enabled=False, reason=reason
    )


def clear_kill_switch(
    backend: SecurityStateBackend, *, reason: str = "", **kwargs
) -> KillSwitchState:
    """Restore service by writing both levels enabled.

    An update, never a delete: no deployed managed identity holds a table
    delete action.
    """

    return DurableKillSwitch(backend, **kwargs).set(
        writes_enabled=True, public_enabled=True, reason=reason
    )


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

    The budget kill switch is durable on the same terms: ``kill_switch_durable``
    reports what is in use and the same boot check refuses a process-local one.

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
        kill_switch: Optional[KillSwitch] = None,
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
        self._kill: KillSwitch = kill_switch or ProcessKillSwitch()
        self._backend = threading.BoundedSemaphore(self.policy.max_backend_concurrency)

    @property
    def durable(self) -> bool:
        """Whether the daily counters survive a restart and a replica change."""

        return bool(getattr(self._counters, "durable", False))

    @property
    def kill_switch(self) -> KillSwitch:
        return self._kill

    @property
    def kill_switch_durable(self) -> bool:
        """Whether the kill state survives a restart and a replica change."""

        return bool(getattr(self._kill, "durable", False))

    def kill_state(self) -> KillSwitchState:
        """Read the kill state, refusing the request if it cannot be read.

        This is the one control in this module that fails *closed* on a
        backend error, and the contrast with #179 is deliberate.  The quota
        paths pick a direction per call site: a charge that cannot be
        persisted refuses (the resource has not been handed out yet), while a
        single counter row that cannot be parsed is reset and healed (refusing
        forever would strand a rider, and the ceiling immediately re-applies).
        Both of those are bounded mistakes.

        The kill switch has no such bound.  It is thrown precisely when
        spending has already gone wrong, so reading "carry on" from an error
        is the exact failure it exists to prevent -- and unlike a quota, the
        cost of being wrong is not one rider's retry but an unbounded bill.
        So: unreadable means refused.
        """

        try:
            return self._kill.state()
        except Exception as exc:
            raise QuotaExceeded(
                "kill state unavailable", status_code=503, retry_after=30
            ) from exc

    def require_public_enabled(self) -> KillSwitchState:
        """Assert the public API level, or raise the refusal for it."""

        state = self.kill_state()
        if not state.public_enabled:
            raise QuotaExceeded("public API disabled", status_code=403)
        return state

    def require_writes_enabled(self) -> KillSwitchState:
        """Assert both levels.  Public first: every write route is public."""

        state = self.require_public_enabled()
        if not state.writes_enabled:
            raise QuotaExceeded("writes disabled", status_code=403)
        return state

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

    def set_writes_enabled(self, enabled: bool, *, reason: str = "") -> None:
        """Move only the write level, leaving the public level as it stands.

        Read-modify-write against whichever kill switch is in use.  When that
        switch is durable and the read fails, this raises rather than guessing
        the level it is not changing: the operator gets an error and can
        declare both levels outright with :func:`set_kill_switch`.
        """

        try:
            self._kill.set_writes_enabled(bool(enabled), reason=reason)
        except KillSwitchUnavailable as exc:
            raise QuotaExceeded(
                "kill state unavailable", status_code=503, retry_after=30
            ) from exc

    def set_public_enabled(self, enabled: bool, *, reason: str = "") -> None:
        """Move only the public level, leaving the write level as it stands.

        Disabling this level already stops writes, because every write route
        checks it first; it does not rewrite the write level, so re-enabling
        the public API restores exactly the write level that was in force
        before.
        """

        try:
            self._kill.set_public_enabled(bool(enabled), reason=reason)
        except KillSwitchUnavailable as exc:
            raise QuotaExceeded(
                "kill state unavailable", status_code=503, retry_after=30
            ) from exc

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
        # Read at request time, not at startup, and never from a boolean this
        # process set for itself.
        self.require_writes_enabled()
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
        self.require_public_enabled()
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
        self.require_public_enabled()
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

        # Reporting the kill state fails closed with everything else: a status
        # response that guessed "enabled" would be the one place a caller
        # could read a serving deployment out of an unreadable switch.
        kill = self.kill_state()
        return {
            "uploaded_bytes": _value(METRIC_UPLOAD_BYTES),
            "objects_today": _value(METRIC_OBJECTS),
            "read_bytes": _value(METRIC_READ_BYTES),
            "read_requests": _value(METRIC_READ_REQUESTS),
            "writes_enabled": kill.writes_enabled,
            "public_enabled": kill.public_enabled,
            "durable": self.durable,
        }
