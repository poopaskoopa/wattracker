"""Cost and abuse limits for the cloud API.

Budgets are operator alerts, not request admission controls.  This module is
the request-time control plane: it rejects before storage writes and keeps
counters per verified installation namespace and local-user scope.
"""
from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterator, Optional


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


@dataclass
class _Counter:
    day: str
    uploaded: int = 0
    objects: int = 0
    read_bytes: int = 0
    read_requests: int = 0


class QuotaManager:
    """Best-effort process-local admission controls.

    Production abuse and cost enforcement is the durable APIM policy. These
    counters remain useful as a local backstop and test seam, but reset on a
    process restart or replica change by design.
    """

    durable = False

    def __init__(self, policy: Optional[QuotaPolicy] = None, *, clock=time.monotonic):
        self.policy = policy or QuotaPolicy()
        self._clock = clock
        self._lock = threading.RLock()
        self._global_window = 0.0
        self._global_requests = 0
        self._scopes: dict[tuple[str, str], _Counter] = {}
        self._installations: dict[str, _Counter] = {}
        self.writes_enabled = True
        self.public_enabled = True
        self._backend = threading.BoundedSemaphore(self.policy.max_backend_concurrency)

    @staticmethod
    def _day(now: Optional[datetime] = None) -> str:
        return (now or datetime.now(timezone.utc)).date().isoformat()

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

    def _counter(self, scope: tuple[str, str], now: Optional[datetime]) -> _Counter:
        day = self._day(now)
        current = self._scopes.get(scope)
        if current is None or current.day != day:
            current = _Counter(day=day)
            self._scopes[scope] = current
        return current

    def _installation_counter(self, namespace: str, now: Optional[datetime]) -> _Counter:
        day = self._day(now)
        current = self._installations.get(namespace)
        if current is None or current.day != day:
            current = _Counter(day=day)
            self._installations[namespace] = current
        return current

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
            counter = self._counter((namespace, local_user_scope), now)
            installation = self._installation_counter(namespace, now)
            if counter.uploaded + request_bytes > self.policy.max_upload_bytes_per_day:
                raise QuotaExceeded("upload quota exceeded")
            if installation.uploaded + request_bytes > self.policy.max_upload_bytes_per_day:
                raise QuotaExceeded("installation upload quota exceeded")
            if counter.objects + object_count > self.policy.max_objects_per_day:
                raise QuotaExceeded("object quota exceeded")
            if installation.objects + object_count > self.policy.max_objects_per_day:
                raise QuotaExceeded("installation object quota exceeded")
            installation_bytes = (
                stored_bytes
                if installation_stored_bytes is None
                else installation_stored_bytes
            )
            if stored_bytes > self.policy.max_stored_bytes_per_scope:
                raise QuotaExceeded("stored-byte quota exceeded", status_code=403)
            if installation_bytes > self.policy.max_stored_bytes_per_scope:
                raise QuotaExceeded(
                    "installation stored-byte quota exceeded", status_code=403
                )
            counter.uploaded += request_bytes
            counter.objects += object_count
            installation.uploaded += request_bytes
            installation.objects += object_count

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
            counter = self._counter((namespace, local_user_scope), now)
            installation = self._installation_counter(namespace, now)
            increment = 1 if count_request else 0
            if counter.read_requests + increment > self.policy.max_read_requests_per_day:
                raise QuotaExceeded("read request quota exceeded")
            if installation.read_requests + increment > self.policy.max_read_requests_per_day:
                raise QuotaExceeded("installation read request quota exceeded")
            if counter.read_bytes + max(0, response_bytes) > self.policy.max_read_bytes_per_day:
                raise QuotaExceeded("read-byte quota exceeded")
            if installation.read_bytes + max(0, response_bytes) > self.policy.max_read_bytes_per_day:
                raise QuotaExceeded("installation read-byte quota exceeded")
            counter.read_requests += increment
            counter.read_bytes += max(0, response_bytes)
            installation.read_requests += increment
            installation.read_bytes += max(0, response_bytes)

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
            counter = self._counter((namespace, local_user_scope), now)
            installation = self._installation_counter(namespace, now)
            if counter.read_bytes + response_bytes > self.policy.max_read_bytes_per_day:
                raise QuotaExceeded("read-byte quota exceeded")
            if installation.read_bytes + response_bytes > self.policy.max_read_bytes_per_day:
                raise QuotaExceeded("installation read-byte quota exceeded")
            counter.read_bytes += response_bytes
            installation.read_bytes += response_bytes

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
        with self._lock:
            counter = self._counter((namespace, local_user_scope), now)
            return {
                "uploaded_bytes": counter.uploaded,
                "objects_today": counter.objects,
                "read_bytes": counter.read_bytes,
                "read_requests": counter.read_requests,
                "writes_enabled": self.writes_enabled,
                "public_enabled": self.public_enabled,
            }
