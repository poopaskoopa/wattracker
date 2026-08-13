"""Server-mediated tenant storage interfaces and a test backend.

The production deployment can replace :class:`MemoryTenantStore` with an
Azure Blob/Table implementation.  The interface intentionally exposes no
list-all-tenants operation and takes a verified namespace/scope supplied by
the API, never a caller-selected partition key or path.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional, Sequence

from .models import MAX_PAYLOAD_BYTES, CloudObject, SyncBatch

RECOVERY_RETENTION = timedelta(days=7)


class StorageConflict(RuntimeError):
    """A batch id or object revision conflicts with an existing write."""


class StaleRevision(StorageConflict):
    """A write would move a scope or object backwards in time."""


@dataclass(frozen=True)
class ApplyResult:
    revision: int
    accepted: int
    stored_bytes: int
    replay: bool = False


@dataclass
class _BatchRecord:
    digest: str
    result: ApplyResult
    created_at: datetime


@dataclass
class _Stored:
    value: CloudObject
    deleted_at: Optional[datetime] = None


class MemoryTenantStore:
    """A deterministic bounded store for tests and local emulators."""

    def __init__(self, *, recovery_retention: timedelta = RECOVERY_RETENTION):
        self._lock = threading.RLock()
        self._scopes: dict[tuple[str, str], dict[str, _Stored]] = {}
        self._revisions: dict[tuple[str, str], int] = {}
        self._batches: dict[tuple[str, str, str], _BatchRecord] = {}
        self._retention = recovery_retention

    @staticmethod
    def _scope(namespace: str, local_user_scope: str) -> tuple[str, str]:
        if not isinstance(namespace, str) or not namespace:
            raise ValueError("namespace is required")
        if not isinstance(local_user_scope, str) or not local_user_scope:
            raise ValueError("local user scope is required")
        return namespace, local_user_scope

    @staticmethod
    def _object_size(value: CloudObject) -> int:
        return len(json.dumps(value.wire(), sort_keys=True,
                              separators=(",", ":"), ensure_ascii=False).encode())

    def apply(
        self,
        namespace: str,
        local_user_scope: str,
        batch: SyncBatch,
        *,
        now: Optional[datetime] = None,
    ) -> ApplyResult:
        """Atomically apply a batch in exactly one verified scope."""
        scope = self._scope(namespace, local_user_scope)
        now = now or datetime.now(timezone.utc)
        digest = hashlib.sha256(batch.digest_material()).hexdigest()
        batch_key = (*scope, batch.batch_id)
        with self._lock:
            prior = self._batches.get(batch_key)
            if prior is not None:
                if prior.digest != digest:
                    raise StorageConflict("idempotency key has another payload")
                return ApplyResult(
                    revision=prior.result.revision,
                    accepted=prior.result.accepted,
                    stored_bytes=prior.result.stored_bytes,
                    replay=True,
                )

            current_revision = self._revisions.get(scope, 0)
            if batch.revision <= current_revision:
                raise StaleRevision("batch revision is stale")
            rows = self._scopes.setdefault(scope, {})
            for value in batch.objects:
                prior_row = rows.get(value.object_id)
                if prior_row is not None and value.revision <= prior_row.value.revision:
                    raise StaleRevision("object revision is stale")

            stored_bytes = 0
            for value in batch.objects:
                rows[value.object_id] = _Stored(
                    value=value,
                    deleted_at=now if value.deleted else None,
                )
                stored_bytes += self._object_size(value)
            self._revisions[scope] = batch.revision
            result = ApplyResult(
                revision=batch.revision,
                accepted=len(batch.objects),
                stored_bytes=stored_bytes,
            )
            self._batches[batch_key] = _BatchRecord(
                digest=digest, result=result, created_at=now
            )
            return result

    def get(
        self,
        namespace: str,
        local_user_scope: str,
        object_id: str,
        *,
        include_deleted: bool = False,
    ) -> Optional[CloudObject]:
        scope = self._scope(namespace, local_user_scope)
        with self._lock:
            row = self._scopes.get(scope, {}).get(object_id)
            if row is None or (row.value.deleted and not include_deleted):
                return None
            return row.value

    def list_objects(
        self,
        namespace: str,
        local_user_scope: str,
        *,
        kinds: Optional[Iterable[str]] = None,
        limit: int = 100,
        include_deleted: bool = False,
    ) -> list[CloudObject]:
        if limit < 1 or limit > 100:
            raise ValueError("limit must be between 1 and 100")
        scope = self._scope(namespace, local_user_scope)
        allowed = set(kinds) if kinds is not None else None
        with self._lock:
            rows = self._scopes.get(scope, {})
            values = [row.value for row in rows.values()]
            values.sort(key=lambda item: item.object_id)
            return [
                value
                for value in values
                if (allowed is None or value.kind in allowed)
                and (include_deleted or not value.deleted)
            ][:limit]

    def usage(self, namespace: str, local_user_scope: str) -> int:
        scope = self._scope(namespace, local_user_scope)
        with self._lock:
            return sum(self._object_size(row.value)
                       for row in self._scopes.get(scope, {}).values())

    def usage_for_namespace(self, namespace: str) -> int:
        if not isinstance(namespace, str) or not namespace:
            raise ValueError("namespace is required")
        with self._lock:
            return sum(
                self._object_size(row.value)
                for (stored_namespace, _scope), rows in self._scopes.items()
                if stored_namespace == namespace
                for row in rows.values()
            )

    def revision(self, namespace: str, local_user_scope: str) -> int:
        return self._revisions.get(self._scope(namespace, local_user_scope), 0)

    def recover_deleted(
        self,
        namespace: str,
        local_user_scope: str,
        object_id: str,
        *,
        now: Optional[datetime] = None,
    ) -> Optional[CloudObject]:
        """Restore a tombstoned object while it remains in recovery retention."""
        scope = self._scope(namespace, local_user_scope)
        now = now or datetime.now(timezone.utc)
        with self._lock:
            row = self._scopes.get(scope, {}).get(object_id)
            if row is None or row.deleted_at is None:
                return None
            if now - row.deleted_at > self._retention:
                return None
            row.value = CloudObject(
                object_id=row.value.object_id,
                kind=row.value.kind,
                revision=row.value.revision,
                data=row.value.data,
                deleted=False,
            )
            row.deleted_at = None
            return row.value

    def purge_expired_tombstones(self, *, now: Optional[datetime] = None) -> int:
        now = now or datetime.now(timezone.utc)
        removed = 0
        with self._lock:
            for rows in self._scopes.values():
                for object_id, row in list(rows.items()):
                    if row.deleted_at is not None and now - row.deleted_at > self._retention:
                        del rows[object_id]
                        removed += 1
        return removed


class AzureDependencyUnavailable(RuntimeError):
    """Azure SDK dependencies were not installed in the cloud deployment."""


class AzureTenantStore:
    """Blob/Table-backed tenant store using managed identity data-plane clients.

    The clients are injected so the local package and tests need no Azure SDK.
    This adapter never accepts a caller-provided path or partition key: the
    verified namespace, local scope, and validated object ID are the only
    inputs from which storage coordinates are constructed.
    """

    def __init__(
        self,
        blob_service: object,
        table_service: object,
        *,
        container_name: str = "wattracker-objects",
        table_name: str = "CloudObjects",
        ensure_resources: bool = False,
    ) -> None:
        self._blob_service = blob_service
        self._table_service = table_service
        self._container = blob_service.get_container_client(container_name)
        self._table = table_service.get_table_client(table_name)
        if ensure_resources:
            try:
                self._container.create_container()
            except Exception as exc:
                if not self._is_conflict(exc):
                    raise
            try:
                self._table.create_table()
            except Exception as exc:
                if not self._is_conflict(exc):
                    raise

    @classmethod
    def from_managed_identity(
        cls,
        storage_account_name: str,
        *,
        container_name: str = "wattracker-objects",
        table_name: str = "CloudObjects",
    ) -> "AzureTenantStore":
        """Construct clients without account keys, SAS, or public storage access."""
        try:
            from azure.data.tables import TableServiceClient
            from azure.identity import DefaultAzureCredential
            from azure.storage.blob import BlobServiceClient
        except ImportError as exc:
            raise AzureDependencyUnavailable(
                "install the cloud Azure storage dependencies"
            ) from exc
        if not isinstance(storage_account_name, str) or not storage_account_name:
            raise ValueError("storage account name is required")
        credential = DefaultAzureCredential(exclude_interactive_browser_credential=True)
        blob_service = BlobServiceClient(
            account_url=f"https://{storage_account_name}.blob.core.windows.net",
            credential=credential,
        )
        table_service = TableServiceClient(
            endpoint=f"https://{storage_account_name}.table.core.windows.net",
            credential=credential,
        )
        return cls(
            blob_service,
            table_service,
            container_name=container_name,
            table_name=table_name,
        )

    @staticmethod
    def _is_conflict(exc: Exception) -> bool:
        return getattr(exc, "status_code", None) in (409, 412) or "already exists" in str(exc).lower()

    @staticmethod
    def _not_found(exc: Exception) -> bool:
        return getattr(exc, "status_code", None) == 404 or "not found" in str(exc).lower()

    @staticmethod
    def _partition(namespace: str, local_user_scope: str) -> str:
        if not isinstance(namespace, str) or not re.fullmatch(r"[0-9a-f]{64}", namespace):
            raise ValueError("namespace is invalid")
        if not isinstance(local_user_scope, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._~-]{0,255}", local_user_scope
        ):
            raise ValueError("local user scope is invalid")
        return f"{namespace}:{local_user_scope}"

    @staticmethod
    def _row_key(object_id: str) -> str:
        if not isinstance(object_id, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._~-]{0,127}", object_id
        ):
            raise ValueError("object id is invalid")
        return f"object:{object_id}"

    @staticmethod
    def _batch_row(batch_id: str) -> str:
        if not isinstance(batch_id, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._~-]{0,127}", batch_id
        ):
            raise ValueError("batch id is invalid")
        return f"batch:{batch_id}"

    @staticmethod
    def _scope_row() -> str:
        return "scope"

    @staticmethod
    def _blob_name(partition: str, object_id: str) -> str:
        return f"{partition}/{AzureTenantStore._row_key(object_id)}.json"

    def _entity(self, partition: str, row_key: str) -> Optional[dict]:
        try:
            return dict(self._table.get_entity(partition_key=partition, row_key=row_key))
        except Exception as exc:
            if self._not_found(exc):
                return None
            raise

    @contextmanager
    def _scope_lock(self, partition: str):
        """Serialize a scope across replicas with a short Blob lease.

        The sync app is configured with one replica, but the lease also keeps
        retries and a future scale-out from racing the revision/idempotency
        checks.  A pending marker remains recoverable if a process dies after
        a blob write and before the marker is committed.
        """
        lock_blob = self._container.get_blob_client(f"{partition}/__lock")
        try:
            lock_blob.upload_blob(b"", overwrite=False)
        except Exception as exc:
            if not self._is_conflict(exc):
                raise
        try:
            from azure.storage.blob import BlobLeaseClient
        except ImportError:
            # Injected test doubles do not need an Azure lease implementation.
            yield
            return
        lease = BlobLeaseClient(lock_blob)
        lease.acquire(lease_duration=60)
        try:
            yield
        finally:
            lease.release()

    def _put_object(self, partition: str, value: CloudObject, batch_id: str) -> int:
        payload = json.dumps(
            value.wire(), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        name = self._blob_name(partition, value.object_id)
        self._container.get_blob_client(name).upload_blob(payload, overwrite=True)
        self._table.upsert_entity({
            "PartitionKey": partition,
            "RowKey": self._row_key(value.object_id),
            "Revision": value.revision,
            "Kind": value.kind,
            "Deleted": value.deleted,
            "BlobName": name,
            "Bytes": len(payload),
            "BatchId": batch_id,
        })
        return len(payload)

    def apply(
        self,
        namespace: str,
        local_user_scope: str,
        batch: SyncBatch,
        *,
        now: Optional[datetime] = None,
    ) -> ApplyResult:
        del now
        partition = self._partition(namespace, local_user_scope)
        digest = hashlib.sha256(batch.digest_material()).hexdigest()
        marker_row = self._batch_row(batch.batch_id)
        with self._scope_lock(partition):
            marker = self._entity(partition, marker_row)
            if marker is not None:
                if marker.get("Digest") != digest:
                    raise StorageConflict("idempotency key has another payload")
                if marker.get("Committed", True):
                    return ApplyResult(
                        revision=int(marker["Revision"]),
                        accepted=int(marker["Accepted"]),
                        stored_bytes=int(marker.get("Bytes", 0)),
                        replay=True,
                    )
            else:
                marker = {
                    "PartitionKey": partition,
                    "RowKey": marker_row,
                    "Revision": batch.revision,
                    "Accepted": len(batch.objects),
                    "Bytes": 0,
                    "Digest": digest,
                    "Committed": False,
                }
                try:
                    self._table.create_entity(marker)
                except Exception as exc:
                    if not self._is_conflict(exc):
                        raise
                    marker = self._entity(partition, marker_row)
                    if marker is None or marker.get("Digest") != digest:
                        raise StorageConflict("idempotency key has another payload")

            scope = self._entity(partition, self._scope_row())
            current_revision = int(scope.get("Revision", 0)) if scope else 0
            if batch.revision <= current_revision:
                if not (
                    current_revision == batch.revision
                    and marker.get("Committed") is False
                ):
                    raise StaleRevision("batch revision is stale")

            stored_bytes = 0
            for value in batch.objects:
                prior = self._entity(partition, self._row_key(value.object_id))
                if prior is not None and int(prior.get("Revision", 0)) >= value.revision:
                    if (
                        int(prior.get("Revision", 0)) == value.revision
                        and prior.get("BatchId") == batch.batch_id
                    ):
                        stored_bytes += int(prior.get("Bytes", 0))
                        continue
                    raise StaleRevision("object revision is stale")
                stored_bytes += self._put_object(partition, value, batch.batch_id)

            self._table.upsert_entity({
                "PartitionKey": partition,
                "RowKey": self._scope_row(),
                "Revision": batch.revision,
                "BatchId": batch.batch_id,
            })
            marker["Bytes"] = stored_bytes
            marker["Committed"] = True
            self._table.upsert_entity(marker)
            return ApplyResult(batch.revision, len(batch.objects), stored_bytes)

    def get(
        self,
        namespace: str,
        local_user_scope: str,
        object_id: str,
        *,
        include_deleted: bool = False,
    ) -> Optional[CloudObject]:
        partition = self._partition(namespace, local_user_scope)
        entity = self._entity(partition, self._row_key(object_id))
        if entity is None or (entity.get("Deleted", False) and not include_deleted):
            return None
        try:
            payload = self._container.get_blob_client(entity["BlobName"]).download_blob(
                offset=0, length=MAX_PAYLOAD_BYTES + 1
            ).readall()
            if len(payload) > MAX_PAYLOAD_BYTES:
                return None
            value = json.loads(payload.decode("utf-8"))
            return CloudObject(
                object_id=value["id"], kind=value["kind"], revision=value["revision"],
                data=value.get("data", {}), deleted=bool(value.get("deleted", False)),
            )
        except (KeyError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
            return None

    def list_objects(
        self,
        namespace: str,
        local_user_scope: str,
        *,
        kinds: Optional[Iterable[str]] = None,
        limit: int = 100,
        include_deleted: bool = False,
    ) -> list[CloudObject]:
        if limit < 1 or limit > 100:
            raise ValueError("limit must be between 1 and 100")
        partition = self._partition(namespace, local_user_scope)
        allowed = set(kinds) if kinds is not None else None
        entities = self._table.query_entities(
            query_filter=f"PartitionKey eq '{partition}'"
        )
        result: list[CloudObject] = []
        for entity in entities:
            row_key = str(entity.get("RowKey", ""))
            if not row_key.startswith("object:"):
                continue
            if entity.get("Deleted", False) and not include_deleted:
                continue
            if allowed is not None and entity.get("Kind") not in allowed:
                continue
            value = self.get(
                namespace, local_user_scope, row_key[len("object:"):],
                include_deleted=include_deleted,
            )
            if value is not None:
                result.append(value)
            if len(result) >= limit:
                break
        result.sort(key=lambda value: value.object_id)
        return result

    def usage(self, namespace: str, local_user_scope: str) -> int:
        partition = self._partition(namespace, local_user_scope)
        entities = self._table.query_entities(
            query_filter=f"PartitionKey eq '{partition}'"
        )
        return sum(int(entity.get("Bytes", 0)) for entity in entities
                   if str(entity.get("RowKey", "")).startswith("object:"))

    def usage_for_namespace(self, namespace: str) -> int:
        if not isinstance(namespace, str) or not re.fullmatch(r"[0-9a-f]{64}", namespace):
            raise ValueError("namespace is invalid")
        total = 0
        for entity in self._table.query_entities(
            query_filter=f"PartitionKey ge '{namespace}:' and PartitionKey lt '{namespace};'"
        ):
            if str(entity.get("RowKey", "")).startswith("object:"):
                total += int(entity.get("Bytes", 0))
        return total

    def revision(self, namespace: str, local_user_scope: str) -> int:
        entity = self._entity(self._partition(namespace, local_user_scope), self._scope_row())
        return int(entity.get("Revision", 0)) if entity else 0
