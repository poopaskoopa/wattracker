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

#: The largest ``?limit=`` the read API will accept.  ``wattracker.cloud.api``
#: re-exports this as its own bound; storage owns it because storage cannot
#: import the API without a cycle.
MAX_QUERY_LIMIT = 100
#: Callers ask for one row beyond the page so they can tell whether a further
#: page exists without a second query, so the store accepts exactly one more
#: than the API bound.  Deriving it keeps raising ``MAX_QUERY_LIMIT`` from
#: turning every read into a ``ValueError`` -- that is, a 500.
_MAX_LIST_LIMIT = MAX_QUERY_LIMIT + 1


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
        after: Optional[str] = None,
        min_revision: Optional[int] = None,
    ) -> list[CloudObject]:
        if limit < 1 or limit > _MAX_LIST_LIMIT:
            raise ValueError(f"limit must be between 1 and {_MAX_LIST_LIMIT}")
        scope = self._scope(namespace, local_user_scope)
        allowed = set(kinds) if kinds is not None else None
        with self._lock:
            return self._list_objects_locked(
                scope,
                allowed=allowed,
                limit=limit,
                include_deleted=include_deleted,
                after=after,
                min_revision=min_revision,
            )

    def list_objects_with_revision(
        self,
        namespace: str,
        local_user_scope: str,
        *,
        kinds: Optional[Iterable[str]] = None,
        limit: int = 100,
        include_deleted: bool = False,
        after: Optional[str] = None,
        min_revision: Optional[int] = None,
    ) -> tuple[int, list[CloudObject]]:
        """Read a page and its checkpoint from one locked scope snapshot."""
        if limit < 1 or limit > _MAX_LIST_LIMIT:
            raise ValueError(f"limit must be between 1 and {_MAX_LIST_LIMIT}")
        scope = self._scope(namespace, local_user_scope)
        allowed = set(kinds) if kinds is not None else None
        with self._lock:
            return (
                self._revisions.get(scope, 0),
                self._list_objects_locked(
                    scope,
                    allowed=allowed,
                    limit=limit,
                    include_deleted=include_deleted,
                    after=after,
                    min_revision=min_revision,
                ),
            )

    def _list_objects_locked(
        self,
        scope: tuple[str, str],
        *,
        allowed: Optional[set[str]],
        limit: int,
        include_deleted: bool,
        after: Optional[str],
        min_revision: Optional[int],
    ) -> list[CloudObject]:
        rows = self._scopes.get(scope, {})
        values = [row.value for row in rows.values()]
        values.sort(key=lambda item: item.object_id)
        return [
            value
            for value in values
            if (allowed is None or value.kind in allowed)
            and (after is None or value.object_id > after)
            and (min_revision is None or value.revision > min_revision)
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
        if not (
            hasattr(lock_blob, "blob_name") or hasattr(lock_blob, "container_name")
        ):
            # A storage-protocol test double is intentionally not an SDK
            # BlobClient. Real Azure clients expose one of these coordinates.
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
        after: Optional[str] = None,
        min_revision: Optional[int] = None,
    ) -> list[CloudObject]:
        if limit < 1 or limit > _MAX_LIST_LIMIT:
            raise ValueError(f"limit must be between 1 and {_MAX_LIST_LIMIT}")
        partition = self._partition(namespace, local_user_scope)
        allowed = set(kinds) if kinds is not None else None
        entities = self._table.query_entities(
            query_filter=f"PartitionKey eq '{partition}'"
        )
        # Two passes, and the split is the whole point.  The table query is
        # one cheap round trip; ``self.get`` is a blob download per object.
        # Selecting candidates from the *entity* -- kind, tombstone, cursor
        # position and ``Revision`` (written next to the blob by
        # ``_put_object``) -- means a delta poll that matches nothing
        # downloads nothing, and ``limit=1`` downloads one blob, not one per
        # object in the scope.
        candidates: list[str] = []
        for entity in entities:
            row_key = str(entity.get("RowKey", ""))
            if not row_key.startswith("object:"):
                continue
            if entity.get("Deleted", False) and not include_deleted:
                continue
            if allowed is not None and entity.get("Kind") not in allowed:
                continue
            object_id = row_key[len("object:"):]
            if after is not None and object_id <= after:
                continue
            if min_revision is not None and int(entity.get("Revision", 0)) <= min_revision:
                continue
            candidates.append(object_id)
        # Sorting the candidate ids -- rather than trusting the order the
        # table happened to yield -- is what makes the early exit below safe.
        # Azure Tables does return a partition ordered by RowKey, but nothing
        # in this class enforces that and the injected test doubles do not
        # provide it, so the page boundary is established here instead of
        # assumed.  Stopping on an unsorted stream would silently truncate a
        # rider's data.
        candidates.sort()
        result: list[CloudObject] = []
        for object_id in candidates:
            if len(result) >= limit:
                break
            value = self.get(
                namespace, local_user_scope, object_id,
                include_deleted=include_deleted,
            )
            # A blob that is missing, oversized or unparseable is skipped and
            # the next candidate fills its slot, exactly as before.
            if value is not None:
                result.append(value)
        result.sort(key=lambda value: value.object_id)
        return result[:limit]

    def list_objects_with_revision(
        self,
        namespace: str,
        local_user_scope: str,
        *,
        kinds: Optional[Iterable[str]] = None,
        limit: int = 100,
        include_deleted: bool = False,
        after: Optional[str] = None,
        min_revision: Optional[int] = None,
    ) -> tuple[int, list[CloudObject]]:
        """Read a page and a checkpoint that is a safe floor for it.

        Deliberately lock-free.  ``_scope_lock`` is the *writer's* exclusive
        blob lease: taking it here would charge every phone read an extra blob
        PUT plus a lease acquire/release, turn a second concurrent read (or a
        read overlapping ``apply``) into a lease-conflict 500, and -- worst --
        let a read-only credential stall desktop writes for the full 60s lease
        if the reading process died mid-read.  A read must never be able to
        block a write.

        The ordering below is the substitute for that lock and is not
        incidental: the revision is read **before** the listing.  Any write
        that lands during or after the listing therefore carries a revision
        greater than the one returned, so the client's checkpoint stays behind
        it and the changed objects are simply re-delivered on the next poll.
        Reading the revision *after* the listing would invert this and let the
        checkpoint advance past a change the page did not contain -- silent
        data loss.  This yields at-least-once delivery, which is what a delta
        feed wants; a duplicate is free, a dropped object is not.
        """
        if limit < 1 or limit > _MAX_LIST_LIMIT:
            raise ValueError(f"limit must be between 1 and {_MAX_LIST_LIMIT}")
        revision = self.revision(namespace, local_user_scope)
        return (
            revision,
            self.list_objects(
                namespace,
                local_user_scope,
                kinds=kinds,
                limit=limit,
                include_deleted=include_deleted,
                after=after,
                min_revision=min_revision,
            ),
        )

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
