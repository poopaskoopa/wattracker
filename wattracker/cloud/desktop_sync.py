"""Opt-in desktop coordinator for the cloud synchronization plane."""
from __future__ import annotations

import logging
import sqlite3
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
from .security import PublicKeyUnavailable
from .snapshot import (
    SnapshotError,
    pending_snapshot_objects,
    snapshot_change_token,
)


_log = logging.getLogger(__name__)


class CloudDependencyUnavailable(RuntimeError):
    """The optional packages required for desktop cloud sync are absent."""


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
        self._snapshot_gate_lock = threading.Lock()
        self._snapshot_gate_connection: Optional[sqlite3.Connection] = None
        self._snapshot_gate_baseline: dict[
            int, tuple[int, tuple[Any, ...]]
        ] = {}

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

    def _snapshot_gate_options(self) -> tuple[Any, ...]:
        return (
            self.batch_limit,
            self.include_streams,
            self.include_derived,
            self.derived_first,
        )

    def _snapshot_gate_token(self, user_id: int) -> Optional[tuple[int, bool]]:
        with self._snapshot_gate_lock:
            connection = self._snapshot_gate_connection
            try:
                if connection is None:
                    connection = sqlite3.connect(
                        db.read_only_uri(self.path),
                        uri=True,
                        check_same_thread=False,
                    )
                    connection.row_factory = sqlite3.Row
                    self._snapshot_gate_connection = connection
                return snapshot_change_token(connection, user_id)
            except (OSError, sqlite3.Error):
                if connection is not None:
                    try:
                        connection.close()
                    except sqlite3.Error:
                        pass
                self._snapshot_gate_connection = None
                return None

    def _snapshot_gate_unchanged(self, user_id: int) -> bool:
        token = self._snapshot_gate_token(user_id)
        if token is None:
            return False
        version, pending = token
        if pending:
            return False
        return self._snapshot_gate_baseline.get(user_id) == (
            version, self._snapshot_gate_options(),
        )

    def _snapshot_gate_mark_current(self, user_id: int) -> None:
        token = self._snapshot_gate_token(user_id)
        if token is None or token[1]:
            self._snapshot_gate_baseline.pop(user_id, None)
            return
        self._snapshot_gate_baseline[user_id] = (
            token[0], self._snapshot_gate_options(),
        )

    def _close_snapshot_gate(self) -> None:
        with self._snapshot_gate_lock:
            if self._snapshot_gate_connection is not None:
                try:
                    self._snapshot_gate_connection.close()
                except sqlite3.Error:
                    pass
                self._snapshot_gate_connection = None
            self._snapshot_gate_baseline.clear()

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
        # Prove that the private credential can be stored before consuming the
        # one-time invitation at the cloud endpoint.
        self.credential_store.probe()
        try:
            credentials = CloudSyncClient.enroll(
                endpoint,
                invitation,
                transport=self.transport,
                mtls_headers=self.mtls_headers,
                clock=self.clock,
            )
        except CloudEnrollmentError as exc:
            if isinstance(exc.__cause__, PublicKeyUnavailable):
                raise CloudDependencyUnavailable(
                    "install wattracker[cloud] to enroll this desktop"
                ) from exc
            raise
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
        if self._snapshot_gate_unchanged(user_id):
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
            self._snapshot_gate_mark_current(user_id)
        else:
            self._record_failure(user_id, self._error_text(failed))
        return results

    def mint_pairing_code(self, user_id: int) -> Optional[dict[str, Any]]:
        user_id = self._user_id(user_id)
        if not self._state_enabled(user_id):
            return None
        try:
            return self._client(user_id).mint_pairing_code()
        except Exception:
            return None

    def list_devices(self, user_id: int) -> list[dict[str, Any]]:
        user_id = self._user_id(user_id)
        # The second deliberate exception to the kill switch, and the one that
        # makes the first reachable.  ``_devices_cache`` lives only in memory,
        # so after a restart with sync off there is no device list on screen
        # and therefore no Revoke control -- and sync off after losing a phone
        # is precisely the state the rider is in when they need to revoke it.
        # Gating the read would leave re-enabling cloud sync as the only route
        # to the button, resuming the outbound pushes the rider just stopped.
        # Reading the device list on an explicit click is the same bargain
        # already struck below for revocation, so do not restore the
        # ``enabled`` gate here.  Minting a pairing code stays gated above:
        # adding a new device is not recovery.
        try:
            devices = self._client(user_id).list_devices()
            self._devices_cache[user_id] = [dict(device) for device in devices]
            return devices
        except Exception:
            return []

    def revoke_device(self, user_id: int, credential_id: str) -> bool:
        user_id = self._user_id(user_id)
        # Revocation deliberately remains available while sync is disabled: it
        # is the rider's recovery path for a lost or compromised paired device.
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
        self._close_snapshot_gate()

    def _run_scheduler(self) -> None:
        while not self._stop_event.is_set():
            with self._scheduler_lock:
                while not self._scheduled and not self._stop_event.is_set():
                    self._scheduler_lock.wait()
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
            except Exception as exc:
                # Keep unexpected failures on the same durable retry path as
                # ordinary offline errors. The fixed text avoids persisting
                # exception details that could contain local or remote data.
                _log.debug(
                    "cloud sync cycle failed for user %s (%s)",
                    user_id,
                    type(exc).__name__,
                )
                if self._state_enabled(user_id):
                    try:
                        self._record_failure(
                            user_id, "Cloud sync encountered an unexpected error."
                        )
                    except Exception:
                        # If the state store itself is unavailable, preserve the
                        # worker; a later import or restart can schedule again.
                        pass
            if self._state_enabled(user_id):
                try:
                    state = db.get_cloud_sync_state(user_id, path=self.path)
                except Exception as exc:
                    _log.debug(
                        "cloud sync requeue state read failed for user %s (%s)",
                        user_id,
                        type(exc).__name__,
                    )
                    state = None
                if state is not None and state["next_retry_at"] is not None:
                    due = float(state["next_retry_at"])
                else:
                    due = self.clock() + self.periodic_interval_seconds
                with self._scheduler_lock:
                    current = self._scheduled.get(user_id)
                    self._scheduled[user_id] = (
                        due if current is None else min(current, due)
                    )
                    self._scheduler_lock.notify_all()
