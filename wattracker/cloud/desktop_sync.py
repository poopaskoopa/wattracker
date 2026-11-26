"""Opt-in desktop coordinator for the cloud synchronization plane."""
from __future__ import annotations

import datetime as _dt
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .. import db
from ..timeutil import parse_naive
from .client import (
    CloudEditsNotSupported,
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
    clear_snapshot_publication,
    pending_snapshot_objects,
    snapshot_change_token,
)

DEFAULT_BATCH_LIMIT = 100
_EDIT_PULL_OFFSETS = (0.0, 600.0, 1_800.0, 7_200.0, 21_600.0, 43_200.0, 64_800.0, 86_400.0)
_EDIT_PULL_LIMIT_PER_DAY = 8
_EDIT_UNSUPPORTED_BACKOFF = 86_400.0


_log = logging.getLogger(__name__)


class CloudDependencyUnavailable(RuntimeError):
    """The optional packages required for desktop cloud sync are absent."""


class EditPullSchedule:
    """In-memory pull cadence with bounded idle polling and injectable time."""

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self.clock = clock
        self.anchor: Optional[float] = None
        self.slot = 0
        self.day: Optional[int] = None
        self.pulls_today = 0
        self.pending = False
        self.immediate = False
        self.retry_required = False
        self.unsupported_until: Optional[float] = None

    @staticmethod
    def _utc_day(now: float) -> int:
        return int(now // 86_400)

    def _roll_day(self, now: float) -> None:
        day = self._utc_day(now)
        if self.day != day:
            self.day = day
            self.pulls_today = 0

    def _clear_expired_unsupported(self, now: float) -> None:
        if self.unsupported_until is not None and now >= self.unsupported_until:
            self.unsupported_until = None
            # The old anchor is at least a day behind. Reset it so all of the
            # decayed slots do not become due as a burst after the 404 window.
            self.anchor = now
            self.slot = 0
            self.immediate = True

    def start(self, now: Optional[float] = None) -> None:
        current = self.clock() if now is None else float(now)
        if self.anchor is None:
            self.anchor = current
            self.slot = 0
            self.immediate = True
        self._roll_day(current)

    def due(self, now: Optional[float] = None) -> bool:
        current = self.clock() if now is None else float(now)
        self.start(current)
        self._clear_expired_unsupported(current)
        if self.unsupported_until is not None and current < self.unsupported_until:
            return False
        if self.pending:
            return True
        if self.pulls_today >= _EDIT_PULL_LIMIT_PER_DAY:
            return False
        if self.immediate:
            return True
        assert self.anchor is not None
        return current >= self.anchor + _EDIT_PULL_OFFSETS[self.slot]

    def next_due(self, now: Optional[float] = None) -> float:
        current = self.clock() if now is None else float(now)
        self.start(current)
        if self.unsupported_until is not None:
            if current < self.unsupported_until:
                return self.unsupported_until
            self._clear_expired_unsupported(current)
        if self.pending:
            return current
        if self.pulls_today >= _EDIT_PULL_LIMIT_PER_DAY:
            return (self._utc_day(current) + 1) * 86_400.0
        if self.immediate:
            return current
        assert self.anchor is not None
        return max(current, self.anchor + _EDIT_PULL_OFFSETS[self.slot])

    def pulled(self, now: float, *, has_edits: bool) -> None:
        current = float(now)
        self.start(current)
        self.pulls_today += 1
        was_immediate = self.immediate
        self.immediate = False
        self.retry_required = False
        self.pending = bool(has_edits)
        if not has_edits:
            if was_immediate:
                self.slot = max(self.slot, 1)
            elif self.slot == len(_EDIT_PULL_OFFSETS) - 1:
                # Once the final +24h slot fires, start a fresh 24h wait.
                # Leaving the old anchor in place makes this slot immediately
                # due again after the UTC daily cap rolls over.
                self.anchor = current
            else:
                self.slot += 1

    def published(self, now: float) -> None:
        """Reset the decay after a successful publish containing objects."""
        self.anchor = float(now)
        self.slot = 1
        self.immediate = False

    def acknowledged(self, *, remaining: bool = False) -> None:
        self.pending = remaining
        self.immediate = not remaining

    def retry_pull(self) -> None:
        self.immediate = True
        self.retry_required = True

    def unsupported(self, now: float) -> None:
        self.unsupported_until = float(now) + _EDIT_UNSUPPORTED_BACKOFF
        self.pending = False
        self.immediate = False
        self.retry_required = False


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
    read_endpoint: Optional[str]
    sync_endpoint: Optional[str]
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
            "enabled", "enrolled", "read_endpoint", "sync_endpoint",
            "last_success", "pending",
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
        batch_limit: int = DEFAULT_BATCH_LIMIT,
        include_streams: bool = True,
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
            int,
            tuple[int, tuple[Any, ...], Optional[tuple[float, Optional[float]]]],
        ] = {}
        self._edit_schedule_lock = threading.Lock()
        self._edit_schedules: dict[int, EditPullSchedule] = {}

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

    def _snapshot_gate_connection_locked(self) -> sqlite3.Connection:
        connection = self._snapshot_gate_connection
        if connection is None:
            connection = sqlite3.connect(
                self.path,
                timeout=10,
                check_same_thread=False,
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout=10000")
            self._snapshot_gate_connection = connection
        return connection

    def _snapshot_gate_token(self, user_id: int) -> Optional[tuple[int, bool]]:
        with self._snapshot_gate_lock:
            connection = None
            try:
                connection = self._snapshot_gate_connection_locked()
                return snapshot_change_token(connection, user_id)
            except (OSError, sqlite3.Error):
                if connection is not None:
                    try:
                        connection.close()
                    except sqlite3.Error:
                        pass
                self._snapshot_gate_connection = None
                self._snapshot_gate_baseline.clear()
                return None

    def _snapshot_gate_validity_locked(
        self, connection: sqlite3.Connection, user_id: int,
    ) -> Optional[tuple[float, Optional[float]]]:
        if not self.include_derived:
            return None
        now_epoch = float(self.clock())
        now = _dt.datetime.fromtimestamp(
            now_epoch, tz=_dt.timezone.utc,
        ).replace(tzinfo=None)
        tomorrow = _dt.datetime.combine(
            now.date() + _dt.timedelta(days=1), _dt.time.min,
        )
        next_midnight = tomorrow.replace(tzinfo=_dt.timezone.utc).timestamp()
        next_curve_expiry = None

        # The calendar's ``missed`` flag changes at UTC midnight. The measured
        # power curve also drops activities at their exact 90-day boundary,
        # which can happen within a day. Reading timestamps after a successful
        # rebuild is cheap compared with decoding every stream, and lets the
        # gate remain valid until the first possible derived-object change.
        try:
            rows = connection.execute(
                "SELECT start_time FROM activities WHERE user_id = ?",
                (user_id,),
            ).fetchall()
        except sqlite3.Error:
            return now_epoch, now_epoch
        for row in rows:
            started = parse_naive(row["start_time"])
            if started is None:
                continue
            try:
                expires = (started + _dt.timedelta(days=90)).replace(
                    tzinfo=_dt.timezone.utc,
                ).timestamp()
            except (OverflowError, OSError, ValueError):
                continue
            if now_epoch <= expires and (
                next_curve_expiry is None or expires < next_curve_expiry
            ):
                next_curve_expiry = expires
        return next_midnight, next_curve_expiry

    def _snapshot_gate_unchanged(self, user_id: int) -> bool:
        token = self._snapshot_gate_token(user_id)
        if token is None:
            return False
        version, pending = token
        if pending:
            return False
        baseline = self._snapshot_gate_baseline.get(user_id)
        if baseline is None or baseline[:2] != (
            version, self._snapshot_gate_options(),
        ):
            return False
        validity = baseline[2]
        if validity is None:
            return True
        next_midnight, next_curve_expiry = validity
        now = self.clock()
        return now < next_midnight and (
            next_curve_expiry is None or now <= next_curve_expiry
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
            read_endpoint=state["read_endpoint"],
            sync_endpoint=state["sync_endpoint"],
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

    def enroll(
        self,
        user_id: int,
        read_endpoint: str,
        invitation: str,
    ) -> CloudSyncStatus:
        """Enroll on read, then persist the server-supplied sync endpoint."""
        user_id = self._user_id(user_id)
        read_endpoint = validate_cloud_endpoint(read_endpoint)
        # Prove that the private credential can be stored before consuming the
        # one-time invitation at the cloud endpoint.
        self.credential_store.probe()
        try:
            enrollment = CloudSyncClient.enroll(
                read_endpoint,
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
        credentials = enrollment.credentials
        sync_endpoint = enrollment.sync_endpoint
        # The single credential record is written only after a valid server
        # response.  The endpoints contain no credential material and are
        # persisted only after keyring storage succeeds.
        self.credential_store.save_writer(credentials, user_id=user_id)
        db.save_cloud_sync_state(
            user_id,
            {
                "read_endpoint": read_endpoint,
                "sync_endpoint": sync_endpoint,
                "last_error": None,
                "retry_count": 0,
                "next_retry_at": None,
            },
            path=self.path,
        )
        return self.status(user_id)

    def _state_enabled(self, user_id: int) -> bool:
        try:
            return bool(db.get_cloud_sync_state(user_id, path=self.path)["enabled"])
        except Exception:
            return False

    def _edit_schedule(self, user_id: int) -> EditPullSchedule:
        with self._edit_schedule_lock:
            schedule = self._edit_schedules.get(user_id)
            if schedule is None:
                schedule = EditPullSchedule(clock=self.clock)
                schedule.start(self.clock())
                self._edit_schedules[user_id] = schedule
            return schedule

    def _client(self, user_id: int) -> CloudSyncClient:
        state = db.get_cloud_sync_state(user_id, path=self.path)
        read_endpoint = state["read_endpoint"]
        if not read_endpoint:
            raise CloudEnrollmentError("cloud read endpoint is unavailable")
        read_endpoint = validate_cloud_endpoint(read_endpoint)
        sync_endpoint = state["sync_endpoint"]
        if sync_endpoint:
            sync_endpoint = validate_cloud_endpoint(sync_endpoint)
        credentials = self.credential_store.load_writer(user_id=user_id)
        if credentials is None:
            raise CloudCredentialUnavailable("cloud credentials are unavailable")
        return self.client_factory(
            read_endpoint,
            credentials,
            sync_endpoint=sync_endpoint,
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

    def _record_success(
        self, user_id: int, snapshot_token: Optional[tuple[int, bool]],
    ) -> None:
        with self._snapshot_gate_lock:
            connection = None
            try:
                connection = self._snapshot_gate_connection_locked()
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "UPDATE cloud_sync_state SET last_success_at = ?, "
                    "retry_count = 0, next_retry_at = NULL, last_error = NULL "
                    "WHERE user_id = ?",
                    (self.clock(), user_id),
                )
                token = snapshot_change_token(connection, user_id)
                validity = self._snapshot_gate_validity_locked(
                    connection, user_id,
                )
                connection.commit()
            except Exception:
                if connection is not None:
                    try:
                        connection.rollback()
                        connection.close()
                    except sqlite3.Error:
                        pass
                self._snapshot_gate_connection = None
                self._snapshot_gate_baseline.clear()
                raise

            if token is None or token[1] or token != snapshot_token:
                self._snapshot_gate_baseline.pop(user_id, None)
            else:
                self._snapshot_gate_baseline[user_id] = (
                    token[0], self._snapshot_gate_options(), validity,
                )

    def sync_once(self, user_id: int) -> list[SyncResult]:
        """Pull edits before the snapshot gate, then publish and acknowledge."""
        user_id = self._user_id(user_id)
        if not self._state_enabled(user_id):
            return []
        now = float(self.clock())
        schedule = self._edit_schedule(user_id)
        edit_acks: list[dict[str, str]] = []
        pull_error = False
        unprocessed_edits = False
        client: Optional[CloudSyncClient] = None
        client_error: Optional[Exception] = None
        client_attempted = False

        def get_client() -> CloudSyncClient:
            nonlocal client, client_error, client_attempted
            if client is not None:
                return client
            if client_attempted:
                assert client_error is not None
                raise client_error
            client_attempted = True
            try:
                client = self._client(user_id)
            except Exception as exc:
                client_error = exc
                raise
            return client

        # A read endpoint can exist before enrollment has supplied a write
        # endpoint. Keep the unsupported edit route dormant until publishing
        # is configured, preserving the no-request behavior of sync_once.
        if schedule.due(now):
            try:
                sync_endpoint = db.get_cloud_sync_state(
                    user_id, path=self.path,
                ).get("sync_endpoint")
            except Exception:
                sync_endpoint = None
                schedule.pulled(now, has_edits=False)
                schedule.retry_pull()
                pull_error = True
            if sync_endpoint:
                try:
                    edits = get_client().pull_edits()
                except CloudEditsNotSupported:
                    schedule.pulled(now, has_edits=False)
                    schedule.unsupported(now)
                except Exception:
                    # A read failure must not prevent normal snapshot publication.
                    schedule.pulled(now, has_edits=False)
                    schedule.retry_pull()
                    pull_error = True
                else:
                    schedule.pulled(now, has_edits=bool(edits))
                    if edits:
                        from ..ingest import importer

                        for edit in edits:
                            try:
                                status = importer.apply_cloud_rpe_edit(
                                    user_id,
                                    edit.activity_id,
                                    edit.rpe,
                                    edit.base_rpe,
                                    now=_dt.datetime.fromtimestamp(
                                        now, tz=_dt.timezone.utc,
                                    ),
                                    path=self.path,
                                )
                            except Exception:
                                _log.info(
                                    "cloud RPE edit activity=%s status=deferred",
                                    edit.activity_id,
                                )
                                unprocessed_edits = True
                                continue
                            _log.info(
                                "cloud RPE edit activity=%s status=%s rpe=%s",
                                edit.activity_id,
                                status,
                                edit.rpe,
                            )
                            edit_acks.append(edit.ack(status))
                        if unprocessed_edits:
                            schedule.retry_pull()
            else:
                # Without a writer endpoint there is no pull to perform. Move
                # past the immediate start slot instead of requeueing now.
                schedule.pulled(now, has_edits=False)

        snapshot_token = None
        snapshot_gated = self._snapshot_gate_unchanged(user_id)

        def should_continue() -> bool:
            nonlocal snapshot_token
            if not self._state_enabled(user_id):
                return False
            snapshot_token = self._snapshot_gate_token(user_id)
            return True

        try:
            results = [] if snapshot_gated else get_client().push_snapshot(
                self.path,
                user_id,
                limit=self.batch_limit,
                include_streams=self.include_streams,
                include_derived=self.include_derived,
                derived_first=self.derived_first,
                should_continue=should_continue,
            )
        except (CloudCredentialUnavailable, CloudEnrollmentError, SnapshotError, ValueError) as exc:
            if self._state_enabled(user_id):
                self._record_failure(user_id, str(exc)[:2048])
            return []
        if not self._state_enabled(user_id):
            return results
        failed = next((result for result in results if not result.ok), None)
        if failed is None:
            if not snapshot_gated:
                self._record_success(user_id, snapshot_token)
            if results:
                schedule.published(float(self.clock()))
        else:
            self._record_failure(user_id, self._error_text(failed))
        if failed is None and edit_acks:
            try:
                assert client is not None
                ack_ok = client.ack_edits(edit_acks)
            except Exception:
                ack_ok = False
            if ack_ok:
                schedule.acknowledged(remaining=unprocessed_edits)
                if unprocessed_edits:
                    self._record_failure(
                        user_id, "Cloud RPE edit application was deferred."
                    )
            else:
                schedule.retry_pull()
                self._record_failure(user_id, "Cloud RPE edit acknowledgement failed.")
        elif failed is None and unprocessed_edits:
            self._record_failure(user_id, "Cloud RPE edit application was deferred.")
        if failed is None and unprocessed_edits:
            # A changed publication resets the normal decay schedule. Keep
            # deferred items due for the next retry cycle after that reset.
            schedule.retry_pull()
        if pull_error and failed is None:
            self._record_failure(user_id, "Cloud RPE edit pull failed.")
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

    def set_device_capabilities(self, user_id: int, credential_id: str, annotate: bool) -> bool:
        user_id = self._user_id(user_id)
        # Withdrawing a grant remains possible even while publication is off.
        try:
            capabilities = self._client(user_id).set_device_capabilities(credential_id, annotate)
            for device in self._devices_cache.get(user_id, []):
                if device.get("credential_id") == credential_id:
                    device["capabilities"] = sorted(capabilities)
            return True
        except Exception:
            return False

    def wipe_cloud_data(self, user_id: int) -> Optional[bool]:
        """Permanently remove the user's cloud scope, with no blind retry."""
        user_id = self._user_id(user_id)
        try:
            result = self._client(user_id).wipe_scope()
        except Exception:
            return None
        if result not in (True, False):
            return result
        # A 404 means the server no longer recognizes this writer.  Treat it
        # as a revoked local credential, while retaining False so the UI can
        # tell the rider that an operator may still need to finish the wipe.
        # The same cleanup after success is what lets a later enrollment
        # republish a clean scope instead of trusting stale acknowledgements.
        cleanup_ok = True
        try:
            self.set_enabled(user_id, False)
        except Exception:
            cleanup_ok = False
        try:
            self.credential_store.revoke_local_writer(user_id=user_id)
        except Exception:
            cleanup_ok = False
        try:
            clear_snapshot_publication(self.path, user_id)
        except Exception:
            cleanup_ok = False
        with self._snapshot_gate_lock:
            self._snapshot_gate_baseline.pop(user_id, None)
        self._devices_cache.pop(user_id, None)
        return result if cleanup_ok else None

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
                schedule = self._edit_schedule(user_id)
                schedule.start(now)
                deadline = entry.get("next_retry_at")
                if deadline is None:
                    due = schedule.next_due(now)
                else:
                    due = max(now, float(deadline))
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
            try:
                state = db.get_cloud_sync_state(user_id, path=self.path)
            except Exception as exc:
                _log.debug(
                    "cloud sync requeue state read failed for user %s (%s)",
                    user_id,
                    type(exc).__name__,
                )
                due = self.clock() + self.periodic_interval_seconds
            else:
                if not state["enabled"]:
                    continue
                if state["next_retry_at"] is not None:
                    due = float(state["next_retry_at"])
                else:
                    due = self.clock() + self.periodic_interval_seconds
                schedule = self._edit_schedule(user_id)
                pull_due = schedule.next_due(self.clock())
                if state["next_retry_at"] is not None:
                    pull_due = max(pull_due, float(state["next_retry_at"]))
                due = min(due, pull_due)
            with self._scheduler_lock:
                current = self._scheduled.get(user_id)
                self._scheduled[user_id] = (
                    due if current is None else min(current, due)
                )
                self._scheduler_lock.notify_all()
