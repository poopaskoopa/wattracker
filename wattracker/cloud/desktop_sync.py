"""Opt-in desktop coordinator for the cloud synchronization plane."""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .. import db
from .client import (
    CloudEnrollmentError,
    CloudSyncClient,
    SyncResult,
    https_transport,
    validate_cloud_endpoint,
)
from .credentials import CloudCredentialStore, CloudCredentialUnavailable, KeyringBackend
from .snapshot import SnapshotError, pending_snapshot_objects


class _UnavailableSecretBackend:
    """Fail closed when no OS keyring provider is installed."""

    def get(self, account: str) -> Optional[str]:
        del account
        raise CloudCredentialUnavailable("OS secure storage is unavailable")

    def set(self, account: str, value: str) -> None:
        del account, value
        raise CloudCredentialUnavailable("OS secure storage is unavailable")

    def delete(self, account: str) -> None:
        del account
        raise CloudCredentialUnavailable("OS secure storage is unavailable")


@dataclass(frozen=True)
class CloudSyncStatus:
    """Non-secret state suitable for a settings screen or API response."""

    enabled: bool
    enrolled: bool
    endpoint: Optional[str]
    last_success: Optional[float]
    pending: int
    retry: int
    next_retry_at: Optional[float]
    last_error: Optional[str]
    devices: list[dict[str, Any]] = field(default_factory=list)

    @property
    def last_success_at(self) -> Optional[float]:
        return self.last_success

    @property
    def pending_objects(self) -> int:
        return self.pending

    @property
    def pending_count(self) -> int:
        return self.pending

    @property
    def retry_count(self) -> int:
        return self.retry

    def __iter__(self):
        """Support the existing server/UI ``dict(status)`` seam."""
        for name in (
            "enabled", "enrolled", "endpoint", "last_success", "pending",
            "last_success_at", "pending_objects", "retry", "retry_count",
            "next_retry_at", "last_error", "devices",
        ):
            yield name, getattr(self, name)


class DesktopCloudSync:
    """Durable, opt-in desktop cloud sync with a background-only scheduler."""

    def __init__(
        self,
        path: Optional[str] = None,
        credential_store: Optional[CloudCredentialStore] = None,
        *,
        transport: Optional[Callable[..., tuple[int, bytes]]] = None,
        mtls_headers: Optional[dict[str, str]] = None,
        clock: Callable[[], float] = time.time,
        batch_limit: int = 1_000,
        include_streams: bool = False,
        include_derived: bool = True,
        derived_first: bool = True,
        retry_base_seconds: float = 5.0,
        retry_max_seconds: float = 3_600.0,
        periodic_interval_seconds: float = 900.0,
        client_factory: type[CloudSyncClient] = CloudSyncClient,
    ) -> None:
        self.path = path or db.db_path()
        if credential_store is None:
            try:
                credential_store = CloudCredentialStore(KeyringBackend())
            except CloudCredentialUnavailable:
                credential_store = CloudCredentialStore(_UnavailableSecretBackend())
        self.credential_store = credential_store
        self.transport = transport if transport is not None else https_transport()
        self.mtls_headers = dict(mtls_headers or {})
        self.clock = clock
        self.batch_limit = batch_limit
        self.include_streams = include_streams
        self.include_derived = include_derived
        self.derived_first = derived_first
        self.retry_base_seconds = retry_base_seconds
        self.retry_max_seconds = retry_max_seconds
        self.periodic_interval_seconds = periodic_interval_seconds
        self.client_factory = client_factory
        self._scheduler_lock = threading.Condition()
        self._scheduled: dict[int, float] = {}
        self._devices_cache: dict[int, list[dict[str, Any]]] = {}
        self._stop_event = threading.Event()
        self._worker: Optional[threading.Thread] = None

    @staticmethod
    def _user_id(user_id: int) -> int:
        if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id < 1:
            raise ValueError("user_id must be positive")
        return user_id

    def _pending_count(self, user_id: int) -> int:
        try:
            return pending_snapshot_objects(self.path, user_id)
        except Exception:
            return 0

    def _has_credentials(self, user_id: int) -> bool:
        try:
            return self.credential_store.load_writer(user_id=user_id) is not None
        except Exception:
            return False

    def status(self, user_id: int) -> CloudSyncStatus:
        user_id = self._user_id(user_id)
        state = db.get_cloud_sync_state(user_id, path=self.path)
        return CloudSyncStatus(
            enabled=bool(state["enabled"]),
            enrolled=self._has_credentials(user_id),
            endpoint=state["endpoint"],
            last_success=state["last_success_at"],
            pending=self._pending_count(user_id),
            retry=state["retry_count"],
            next_retry_at=state["next_retry_at"],
            last_error=state["last_error"],
            devices=[dict(device) for device in self._devices_cache.get(user_id, [])],
        )

    def set_enabled(self, user_id: int, enabled: bool) -> CloudSyncStatus:
        user_id = self._user_id(user_id)
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be a boolean")
        db.save_cloud_sync_state(
            user_id,
            {"enabled": enabled, "next_retry_at": None if not enabled else db.get_cloud_sync_state(user_id, path=self.path)["next_retry_at"]},
            path=self.path,
        )
        with self._scheduler_lock:
            if not enabled:
                self._scheduled.pop(user_id, None)
            self._scheduler_lock.notify_all()
        if enabled:
            self.request_sync(user_id)
        return self.status(user_id)

    def enroll(self, user_id: int, endpoint: str, invitation: str) -> CloudSyncStatus:
        """Complete enrollment, then persist the endpoint and private credential."""
        user_id = self._user_id(user_id)
        endpoint = validate_cloud_endpoint(endpoint)
        credentials = CloudSyncClient.enroll(
            endpoint,
            invitation,
            transport=self.transport,
            mtls_headers=self.mtls_headers,
            clock=self.clock,
        )
        # The single credential record is written only after a valid server
        # response.  The endpoint contains no credential material and is
        # persisted only after keyring storage succeeds.
        self.credential_store.save_writer(credentials, user_id=user_id)
        db.save_cloud_sync_state(
            user_id,
            {"endpoint": endpoint, "last_error": None, "retry_count": 0, "next_retry_at": None},
            path=self.path,
        )
        return self.status(user_id)

    def _state_enabled(self, user_id: int) -> bool:
        try:
            return bool(db.get_cloud_sync_state(user_id, path=self.path)["enabled"])
        except Exception:
            return False

    def _client(self, user_id: int) -> CloudSyncClient:
        state = db.get_cloud_sync_state(user_id, path=self.path)
        endpoint = state["endpoint"]
        if not endpoint:
            raise CloudEnrollmentError("cloud endpoint is unavailable")
        endpoint = validate_cloud_endpoint(endpoint)
        credentials = self.credential_store.load_writer(user_id=user_id)
        if credentials is None:
            raise CloudCredentialUnavailable("cloud credentials are unavailable")
        return self.client_factory(
            endpoint,
            credentials,
            transport=self.transport,
            mtls_headers=self.mtls_headers,
            clock=self.clock,
        )

    @staticmethod
    def _error_text(result: Optional[SyncResult] = None, fallback: str = "cloud sync unavailable") -> str:
        if result is None:
            return fallback
        if result.status_code is None:
            return result.detail[:2048]
        return f"cloud sync failed ({result.status_code})"

    def _record_failure(self, user_id: int, error: str) -> None:
        state = db.get_cloud_sync_state(user_id, path=self.path)
        retry_count = int(state["retry_count"]) + 1
        delay = min(
            self.retry_max_seconds,
            self.retry_base_seconds * (2 ** min(retry_count - 1, 30)),
        )
        db.save_cloud_sync_state(
            user_id,
            {
                "retry_count": retry_count,
                "next_retry_at": self.clock() + delay,
                "last_error": error[:2048],
            },
            path=self.path,
        )

    def _record_success(self, user_id: int) -> None:
        db.save_cloud_sync_state(
            user_id,
            {
                "last_success_at": self.clock(),
                "retry_count": 0,
                "next_retry_at": None,
                "last_error": None,
            },
            path=self.path,
        )

    def sync_once(self, user_id: int) -> list[SyncResult]:
        """Drain prepared snapshot pages while the local kill switch remains on."""
        user_id = self._user_id(user_id)
        if not self._state_enabled(user_id):
            return []
        try:
            client = self._client(user_id)
            results = client.push_snapshot(
                self.path,
                user_id,
                limit=self.batch_limit,
                include_streams=self.include_streams,
                include_derived=self.include_derived,
                derived_first=self.derived_first,
                should_continue=lambda: self._state_enabled(user_id),
            )
        except (CloudCredentialUnavailable, CloudEnrollmentError, SnapshotError, ValueError) as exc:
            if self._state_enabled(user_id):
                self._record_failure(user_id, str(exc)[:2048])
            return []
        if not self._state_enabled(user_id):
            return results
        failed = next((result for result in results if not result.ok), None)
        if failed is None:
            self._record_success(user_id)
        else:
            self._record_failure(user_id, self._error_text(failed))
        return results

    def mint_pairing_code(self, user_id: int) -> Optional[dict[str, Any]]:
        user_id = self._user_id(user_id)
        try:
            return self._client(user_id).mint_pairing_code()
        except Exception:
            return None

    def list_devices(self, user_id: int) -> list[dict[str, Any]]:
        user_id = self._user_id(user_id)
        try:
            devices = self._client(user_id).list_devices()
            self._devices_cache[user_id] = [dict(device) for device in devices]
            return devices
        except Exception:
            return []

    def revoke_device(self, user_id: int, credential_id: str) -> bool:
        user_id = self._user_id(user_id)
        try:
            revoked = self._client(user_id).revoke_device(credential_id)
            if revoked:
                for device in self._devices_cache.get(user_id, []):
                    if device.get("credential_id") == credential_id:
                        device["revoked"] = True
            return revoked
        except Exception:
            return False

    def request_sync(self, user_id: int) -> bool:
        """Wake the opt-in worker; this method performs no network I/O."""
        user_id = self._user_id(user_id)
        now = self.clock()
        with self._scheduler_lock:
            current = self._scheduled.get(user_id)
            self._scheduled[user_id] = now if current is None else min(current, now)
            self._scheduler_lock.notify_all()
        return True

    def start(self) -> None:
        with self._scheduler_lock:
            if self._worker is not None and self._worker.is_alive():
                return
            now = self.clock()
            try:
                enabled_users = db.enabled_cloud_sync_users(path=self.path)
            except Exception:
                enabled_users = []
            for entry in enabled_users:
                user_id = int(entry["user_id"])
                deadline = entry.get("next_retry_at")
                due = now if deadline is None else max(now, float(deadline))
                self._scheduled[user_id] = min(
                    due, self._scheduled.get(user_id, due)
                )
            self._stop_event.clear()
            self._worker = threading.Thread(
                target=self._run_scheduler,
                name="wattracker-cloud-sync",
                daemon=True,
            )
            self._worker.start()

    def stop(self) -> None:
        self._stop_event.set()
        with self._scheduler_lock:
            self._scheduler_lock.notify_all()
            worker = self._worker
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=5)
        with self._scheduler_lock:
            if self._worker is worker:
                self._worker = None

    def _run_scheduler(self) -> None:
        while not self._stop_event.is_set():
            with self._scheduler_lock:
                while not self._scheduled and not self._stop_event.is_set():
                    self._scheduler_lock.wait(timeout=1.0)
                if self._stop_event.is_set():
                    return
                user_id, due = min(self._scheduled.items(), key=lambda item: item[1])
                wait_for = due - self.clock()
                if wait_for > 0:
                    self._scheduler_lock.wait(timeout=min(wait_for, 60.0))
                    continue
                self._scheduled.pop(user_id, None)
            try:
                self.sync_once(user_id)
            except Exception:
                # A worker failure must not kill the opt-in scheduler or the
                # local application; the next explicit request can retry.
                continue
            if self._state_enabled(user_id):
                state = db.get_cloud_sync_state(user_id, path=self.path)
                next_retry = state["next_retry_at"]
                if next_retry is not None:
                    due = float(next_retry)
                else:
                    due = self.clock() + self.periodic_interval_seconds
                if self._state_enabled(user_id):
                    with self._scheduler_lock:
                        current = self._scheduled.get(user_id)
                        self._scheduled[user_id] = (
                            due if current is None else min(current, due)
                        )
                        self._scheduler_lock.notify_all()
