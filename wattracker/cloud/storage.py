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
import secrets
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Final, Iterable, Optional, Sequence

from .models import MAX_PAYLOAD_BYTES, CloudObject, SyncBatch

#: How long a *tombstoned* object stays recoverable through
#: :meth:`MemoryTenantStore.recover_deleted`.  This window belongs to ordinary
#: sync deletions -- the rider deleted one ride and wants it back.
#:
#: :meth:`purge_scope` deliberately does not use it.  A scope wipe is a rider
#: asking for their heart rate, body weight and ride history to be gone; a
#: 7-day window in which every byte of that is still stored, still readable
#: with ``include_deleted=True``, and restorable by anyone who can reach the
#: store, is the opposite of what was asked for.  See ``wattracker.cloud.wipe``
#: for the full statement of that choice.
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


#: The reserved names the wipe capability probe deletes (#170).  A probe is a
#: delete of something guaranteed not to exist: an identity allowed to delete
#: gets "not found", one that is not gets 403.  Nothing in this package ever
#: writes a name under either of these, and neither can collide with one that
#: is written: object blobs are ``<partition>/object:<id>.json`` and the lease
#: blob is ``<partition>/__lock``; object-table rows are ``object:<id>``,
#: ``batch:<id>`` and ``scope``.  Each probe also appends 128 random bits, so
#: even a row somebody wrote under the reserved prefix by hand is not the one
#: a probe addresses.
WIPE_PROBE_BLOB_STEM: Final = "__wipe-probe-"
WIPE_PROBE_ROW_PREFIX: Final = "wipe-probe:"


def _wipe_probe_token() -> str:
    return secrets.token_hex(16)


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


@dataclass(frozen=True)
class ScopePurge:
    """What :meth:`purge_scope` removed from one ``(namespace, scope)``.

    Every count is of something actually deleted, so these can be read as a
    receipt rather than as an intent:

    * ``objects`` -- object rows removed from the table.
    * ``blobs`` -- blobs removed from the container by the row pass, each one
      at the name this class derives for its partition and object id.  It is
      lower than ``objects`` when a row's blob was already gone.
    * ``orphans`` -- blobs removed by the container pass, which enumerates the
      partition's blob prefix after the rows are done.  Each one is a blob no
      surviving row named.  It is normally zero, and a non-zero value is not
      an error: ``_put_object`` writes the blob before the row, so any
      interruption between the two leaves exactly one of these.  The wipe is
      defined by the container's contents, not by the table's, so they go.
    * ``markers`` -- every other row in the partition: batch idempotency
      markers and the scope revision row.
    * ``skipped`` -- everything this purge declined to follow rather than
      guessed at.  Three things count here:

      - an object row whose stored ``BlobName`` did **not** match the derived
        name, so the stored name was not followed.  A row like that names a
        blob somewhere else, possibly another rider's; that blob is left
        exactly where it is.  The row's *own* blob, at the derived name inside
        this partition, is still deleted and still counted in ``blobs`` --
        refusing to follow a foreign name is not a reason to leave a rider's
        data behind unreachable;
      - an object row whose key yields no valid object id, so no blob name can
        be derived from it.  That row is **kept**; see :meth:`purge_scope`;
      - a name the blob listing returned that lies outside this partition's
        prefix.  It is not deleted.

      None of the three is an error and none stops the purge.  A non-zero
      ``skipped`` means something in this partition disagrees with the thing
      that addresses it, which on this table is evidence worth looking at.
    """

    objects: int = 0
    blobs: int = 0
    markers: int = 0
    skipped: int = 0
    orphans: int = 0


class MemoryTenantStore:
    """A deterministic bounded store for tests and local emulators."""

    def __init__(self, *, recovery_retention: timedelta = RECOVERY_RETENTION):
        self._lock = threading.RLock()
        self._scopes: dict[tuple[str, str], dict[str, _Stored]] = {}
        self._revisions: dict[tuple[str, str], int] = {}
        self._batches: dict[tuple[str, str, str], _BatchRecord] = {}
        self._retention = recovery_retention
        #: Test knob: simulate an identity that holds no delete grant on the
        #: store, as a deployment does while a new role assignment is still
        #: propagating.  :meth:`can_purge_scope` then reports False and
        #: :meth:`purge_scope` raises, the way a 403 from Azure would.
        self.delete_denied = False

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

    def purge_scope(self, namespace: str, local_user_scope: str) -> ScopePurge:
        """Remove one scope's objects, batch markers and revision outright.

        Irreversible, and deliberately not a tombstone write -- see the module
        note on :data:`RECOVERY_RETENTION` and ``wattracker.cloud.wipe``.

        The revision and the batch markers go with the objects, and that is
        not housekeeping.  A scope emptied of objects but left at revision 42
        refuses the first batch a re-enrolled install sends (``StaleRevision``,
        because 1 <= 42) and keeps refusing until the new install happens to
        pass 42.  A surviving batch marker is worse: the same ``batch_id``
        replays the old ``ApplyResult`` and reports bytes that no longer
        exist.  Either one is the "half-restored scope" a wipe exists to make
        impossible, so the scope leaves this call in exactly the state it
        would be in had it never existed.
        """

        scope = self._scope(namespace, local_user_scope)
        if self.delete_denied:
            raise PermissionError("simulated: this identity may not delete")
        with self._lock:
            rows = self._scopes.pop(scope, {})
            markers = 1 if self._revisions.pop(scope, None) is not None else 0
            for key in [key for key in self._batches if key[:2] == scope]:
                del self._batches[key]
                markers += 1
        return ScopePurge(objects=len(rows), blobs=len(rows), markers=markers)

    def can_purge_scope(self, namespace: str, local_user_scope: str) -> bool:
        """Whether :meth:`purge_scope` would be allowed to delete here.

        The memory store has no permissions, so this answers the
        :attr:`delete_denied` knob and nothing else.  It deletes nothing.
        """

        self._scope(namespace, local_user_scope)
        return not self.delete_denied


class AzureDependencyUnavailable(RuntimeError):
    """Azure SDK dependencies were not installed in the cloud deployment."""


class _AbsentAzureError(Exception):
    """Stand-in for an ``azure.core`` class when the SDK is not installed.

    Nothing raises it, so ``isinstance(exc, _AbsentAzureError)`` is always
    ``False`` -- the correct answer when no ``azure.*`` package is present and
    the only exceptions reaching the classifiers below are injected doubles
    carrying an HTTP status.
    """


def _azure_core_error(name: str) -> type:
    """Look up an ``azure.core.exceptions`` class without depending on it.

    The Azure SDK is optional in this package: the local app never installs
    it and the tests inject storage doubles.  The import is therefore deferred
    to the call, exactly as ``AzureTenantStore.from_managed_identity`` and
    ``AzureTenantStore._scope_lock`` defer theirs.  It is deliberately not
    cached: the classifiers only run on an exception path, so a ``sys.modules``
    lookup costs nothing that matters, and an uncached lookup stays correct
    when a test swaps ``sys.modules['azure.core']``.
    """
    try:
        from azure.core import exceptions
    except ImportError:
        return _AbsentAzureError
    found = getattr(exceptions, name, None)
    if isinstance(found, type) and issubclass(found, BaseException):
        return found
    return _AbsentAzureError


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
        client_id: str | None = None,
    ) -> "AzureTenantStore":
        """Construct clients without account keys, SAS, or public storage access."""
        try:
            from azure.data.tables import TableServiceClient
            from azure.identity import DefaultAzureCredential
            from azure.identity import ManagedIdentityCredential
            from azure.storage.blob import BlobServiceClient
        except ImportError as exc:
            raise AzureDependencyUnavailable(
                "install the cloud Azure storage dependencies"
            ) from exc
        if not isinstance(storage_account_name, str) or not storage_account_name:
            raise ValueError("storage account name is required")
        if client_id is not None and (
            not isinstance(client_id, str) or not client_id
        ):
            raise ValueError("managed identity client id is invalid")
        credential = (
            ManagedIdentityCredential(client_id=client_id)
            if client_id is not None
            else DefaultAzureCredential(exclude_interactive_browser_credential=True)
        )
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

    #: The statuses that mean "the resource is already there" at the four
    #: create-if-absent call sites in this class.  409 is the service's own
    #: ``ContainerAlreadyExists`` / ``TableAlreadyExists`` /
    #: ``EntityAlreadyExists`` / ``BlobAlreadyExists``; 412 is the
    #: ``If-None-Match: *`` precondition that ``upload_blob(overwrite=False)``
    #: sends for the scope lock, which some Blob paths answer with instead.
    _CONFLICT_STATUSES = frozenset({409, 412})

    @staticmethod
    def _http_status(exc: Exception) -> Optional[int]:
        """The SDK's structured HTTP status, or ``None`` when it carries none.

        ``bool`` is an ``int`` in Python, so it is excluded explicitly: a
        double that set ``status_code = True`` must not be read as status 1.
        """
        status = getattr(exc, "status_code", None)
        if isinstance(status, bool) or not isinstance(status, int):
            return None
        return status

    @staticmethod
    def _is_conflict(exc: Exception) -> bool:
        """True only for "this resource already exists", never for a refusal.

        Classification is on the SDK's structured signal -- the mapped
        exception class, else the HTTP status -- and never on the message
        text.  Substring matching on ``str(exc)`` read any error mentioning
        "already exists" as a benign collision, so a 403 quoting the resource
        it refused was swallowed and the caller carried on to create or
        overwrite.  That both hid a misconfigured role assignment and defeated
        the least-privilege roles in ``infra/azure``.  Everything else -- 403
        included, and anything with no structured status at all -- propagates.
        """
        if isinstance(exc, _azure_core_error("ResourceExistsError")):
            return True
        # 412; the Blob SDK maps the failed ``If-None-Match: *`` precondition
        # to this rather than to a plain ``HttpResponseError``.
        if isinstance(exc, _azure_core_error("ResourceModifiedError")):
            return True
        return AzureTenantStore._http_status(exc) in AzureTenantStore._CONFLICT_STATUSES

    @staticmethod
    def _not_found(exc: Exception) -> bool:
        """True only for a genuine 404, never for "you may not look".

        The same reasoning as :meth:`_is_conflict`.  A 403 whose body quotes
        "the specified resource was not found" used to be reported to callers
        as absent data, silently converting an authorization failure into a
        missing object.  Only ``ResourceNotFoundError`` or a structured 404
        counts; a 403, a 401, a 5xx, or a transport error with no status at
        all propagates.
        """
        if isinstance(exc, _azure_core_error("ResourceNotFoundError")):
            return True
        return AzureTenantStore._http_status(exc) == 404

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

    def purge_scope(self, namespace: str, local_user_scope: str) -> ScopePurge:
        """Delete every blob and table row of exactly one partition.

        Irreversible: no tombstone is written and ``recover_deleted`` cannot
        bring any of it back.  See ``wattracker.cloud.wipe`` for why, and for
        the one copy that does outlive this call -- the storage account's own
        7-day blob soft-delete window, which is a platform setting this code
        cannot reach.

        **The container is what is emptied, not the table.**  The rows are
        walked first, because a row is the only thing that can pair a blob
        with an object id and a byte count.  But the rows are not the
        definition of what is here: ``_put_object`` uploads the blob and
        *then* upserts the row, so a crash, a dropped connection or a 500
        between those two writes leaves a blob holding the rider's heart rate
        and body weight with no row naming it.  ``get`` never returns it, so
        nobody notices -- and a row-driven purge never enumerates it, so it
        would outlive the wipe.  That needs no attacker; it is ordinary
        failure.  So after the row pass, everything still under this
        partition's blob prefix is enumerated and deleted, and counted in
        ``ScopePurge.orphans``.

        Two things live under that prefix that are not object blobs, and both
        are decided deliberately:

        * ``__lock``, the scope lease blob, is **excluded from the prefix
          pass** and deleted afterwards, at the bottom of this method.  Not
          because it should survive -- it should not, and it does not -- but
          because this pass runs while the lease on it is held, and deleting a
          leased blob without its lease id fails the call and then fails the
          release in ``_scope_lock``'s ``finally``.  It holds no rider data:
          it is written as zero bytes and only ever used as a lease target.
        * Anything else under the prefix is deleted.  Nothing in this class
          writes such a name, so one appearing is either a future blob kind or
          something that should not be there; either way the rider asked for
          the scope to be empty, and the prefix is provably theirs alone.

        Neither pass is bounded, and that is the point.  Every *scan* in this
        package takes a limit because a scan that runs long is worse than a
        scan that reports "call me again".  A wipe is the opposite: a bound
        here would leave a blob behind and call the scope empty, which is the
        defect this pass exists to close.  Both passes are bounded in practice
        by the per-scope storage quota, and the prefix pass is materialised
        before anything is deleted, the same way the row query already is.

        Three properties below are load-bearing and none is incidental:

        * **The partition is the boundary.**  ``_partition`` is the same
          constructor every other method here uses, and it validates the
          namespace as 64 hexadecimal characters and the local scope against a
          charset with no quote in it, so the ``PartitionKey eq`` filter below
          carries no caller-chosen text and cannot be widened into a range.
          One scope's rows are the only rows this query can return.  The same
          validation bounds the blob prefix: a scope cannot contain ``/``, so
          ``f"{partition}/"`` cannot be a prefix of any other partition's
          names -- ``ns:rider/`` does not prefix ``ns:rider2/...``.  The
          listing is still a coordinate source the service controls rather
          than one derived here, so a returned name that does not start with
          the prefix is refused and counted in ``skipped``, the same rule
          ``BlobName`` gets.
        * **A blob is deleted by the name this class derives, never by the
          name the row stores.**  ``BlobName`` is data in a table row.  An
          edited row naming ``<other partition>/object:x.json`` would, if
          followed, make a wipe of one rider delete another rider's blob.  So
          the name is recomputed from the partition and the object id, and
          *that* name is the one deleted -- always, whether or not the row
          agrees with it.  The derived name cannot leave this partition:
          ``_partition`` validates the namespace as 64 hexadecimal characters
          and the scope against a charset containing neither a quote nor a
          slash, and ``_row_key`` validates the object id the same way.

          A disagreement is still counted in ``skipped``, because the stored
          name is never followed and whatever it points at is left alone.
          What it must not do is stop the deletion: the row is removed either
          way, so skipping the blob left the rider's own data sitting at the
          derived name with nothing pointing at it -- unreachable by
          ``get``, by ``recover_deleted`` and by any later purge, which is the
          exact opposite of what a wipe promises.  Retrying would not have
          helped: the stored name is attacker-controlled data, so a retry
          reads the same disagreement forever.

        * **A row whose key yields no object id keeps its row.**  ``_row_key``
          validates on write, so an unparseable ``object:`` row key is not
          reachable through the application at all; producing one needs
          ``entities/write`` on ``CloudObjects``, the same compromised sync
          identity as the tampered ``BlobName`` above.  No blob name can be
          derived from it, so the row cannot be paired with anything.
          Deleting it anyway destroyed the only surviving evidence that an
          unvalidated key had been written here, so the row is kept and
          counted in ``skipped`` instead -- and never in ``objects``, which is
          a receipt of rows this call actually removed.  The rider's data is
          not what is being kept: the prefix pass above reaches the blob that
          the row key can no longer name, so the data goes and only the empty
          inconsistent row remains, visible to the next operator who looks.

        Rows that are already gone are not failures.  A 404 from
        ``delete_entity`` means the row reached the state this call wanted, so
        it is swallowed exactly as ``_delete_blob`` swallows a missing blob;
        raising instead abandoned the rest of the partition part-way through,
        after the wipe's earlier phase had already deleted the credentials
        that could reach it.  ``_not_found`` is strict -- only a mapped
        ``ResourceNotFoundError`` or a structured 404 -- so a 403, a 401 or a
        5xx still propagates and still stops the purge.

        The scope lease is held for the deletions so a concurrent ``apply``
        cannot interleave a new object into a partition being emptied, and the
        lock blob itself is removed afterwards, once its lease is released.
        """

        partition = self._partition(namespace, local_user_scope)
        prefix = f"{partition}/"
        lock_name = f"{prefix}__lock"
        objects = blobs = markers = skipped = orphans = 0
        with self._scope_lock(partition):
            entities = list(self._table.query_entities(
                query_filter=f"PartitionKey eq '{partition}'"
            ))
            for entity in entities:
                row_key = str(entity.get("RowKey", ""))
                if row_key.startswith("object:"):
                    object_id = row_key[len("object:"):]
                    try:
                        expected = self._blob_name(partition, object_id)
                    except ValueError:
                        # The row key is not a well-formed object id, so no
                        # name can be derived for it and none is guessed.  The
                        # row stays; the prefix pass below takes the blob.
                        skipped += 1
                        continue
                    if entity.get("BlobName") != expected:
                        skipped += 1
                    if self._delete_blob(expected):
                        blobs += 1
                    if self._delete_entity(partition, row_key):
                        objects += 1
                elif self._delete_entity(partition, row_key):
                    markers += 1
            # What the rows did not account for.  The lease blob is left for
            # the release below; everything else under this rider's prefix is
            # this rider's and goes.
            for name in self._blob_names(prefix):
                if not isinstance(name, str) or not name.startswith(prefix):
                    # A listed entry that is not a name inside this partition
                    # is not a coordinate this purge will act on, any more
                    # than ``BlobName`` is.  It is reported, not swallowed.
                    skipped += 1
                    continue
                if ".." in name[len(prefix):].split("/"):
                    # ``startswith`` alone is not containment: the service
                    # resolves ``..`` segments, so a listed name could climb
                    # back out of the prefix it appears to sit inside.  The
                    # rider's own names never carry one -- ``_blob_name``
                    # builds them from a validated object id.
                    skipped += 1
                    continue
                if name != lock_name and self._delete_blob(name):
                    orphans += 1
        self._delete_blob(lock_name)
        return ScopePurge(
            objects=objects,
            blobs=blobs,
            markers=markers,
            skipped=skipped,
            orphans=orphans,
        )

    def can_purge_scope(self, namespace: str, local_user_scope: str) -> bool:
        """Prove, without deleting anything real, that a purge can finish.

        Azure RBAC propagation lags a deployment by minutes, and a wipe
        deletes the rider's credentials *before* it purges the data.  A purge
        that met a 403 then would leave the data stranded with nobody holding
        a credential to ask again.  So the caller asks this first, and every
        storage permission :meth:`purge_scope` needs and a probe can test
        without touching rider data is exercised here:

        1. **Blob delete**, by deleting a random name under this partition's
           own prefix that nothing ever writes (:data:`WIPE_PROBE_BLOB_STEM`).
        2. **Object-row delete**, by deleting a random row key in this
           partition that nothing ever writes (:data:`WIPE_PROBE_ROW_PREFIX`).
        3. **The scope lease** that :meth:`purge_scope` holds while it
           deletes, by taking it and letting it go through the very same
           :meth:`_scope_lock`.  Creating the lease blob and leasing it need a
           blob *write* action, which a delete-only grant does not carry; a
           probe that skipped this would pass and the purge would 403 after
           the credentials were already gone.  This step may create the
           zero-byte ``__lock`` blob if the scope never synced -- exactly what
           the sync plane does on every write, holding no rider data, and
           removed again at the end of :meth:`purge_scope`.  If a sync holds
           the lease right now, the answer is "not now", which is also the
           right answer for a wipe.

        Classification reuses :meth:`_not_found`: an authorized delete of a
        missing item is a 404 (``ResourceNotFoundError``), or no error at all
        from the Tables SDK, which swallows a 404 on ``delete_entity``.  A
        403 (``AuthorizationPermissionMismatch``), a 401, a 5xx, a transport
        error, or anything without a structured status means "cannot", and
        so does any failure taking the lease.  Fail closed: a wrong "cannot"
        costs a retry; a wrong "can" strands a rider's data.
        """

        partition = self._partition(namespace, local_user_scope)
        token = _wipe_probe_token()
        try:
            self._container.get_blob_client(
                f"{partition}/{WIPE_PROBE_BLOB_STEM}{token}"
            ).delete_blob()
        except Exception as exc:
            if not self._not_found(exc):
                return False
        try:
            self._table.delete_entity(
                partition_key=partition, row_key=f"{WIPE_PROBE_ROW_PREFIX}{token}"
            )
        except Exception as exc:
            if not self._not_found(exc):
                return False
        try:
            with self._scope_lock(partition):
                pass
        except Exception:
            return False
        return True

    def _blob_names(self, prefix: str) -> list[object]:
        """What the container lists under ``prefix``, as names, unvalidated.

        The listing is drained into a list before anything is deleted: a
        server-side paged iterator being consumed while the pages underneath
        it are deleted is not a combination worth relying on.

        Nothing is filtered here.  The SDK yields ``BlobProperties`` and the
        caller wants a name, but deciding *which* names may be acted on is the
        caller's job and is not a decision to make silently -- an entry that
        carries no usable name comes back as-is so the caller reports it.
        """

        return [
            blob if isinstance(blob, str) else getattr(blob, "name", None)
            for blob in self._container.list_blobs(name_starts_with=prefix)
        ]

    def _delete_entity(self, partition: str, row_key: str) -> bool:
        """Delete one row, treating "already gone" as the desired end state.

        Returns whether this call was the one that removed it, so a purge
        counts rows it actually deleted rather than rows it enumerated.
        """

        try:
            self._table.delete_entity(partition_key=partition, row_key=row_key)
        except Exception as exc:
            if self._not_found(exc):
                return False
            raise
        return True

    def _delete_blob(self, name: str) -> bool:
        """Delete one blob, treating "already gone" as success."""

        try:
            self._container.get_blob_client(name).delete_blob()
        except Exception as exc:
            if self._not_found(exc):
                return False
            raise
        return True
