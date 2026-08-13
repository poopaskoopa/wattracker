import json
import sqlite3
import zlib

import pytest

from wattracker.cloud.client import CloudSyncClient, SyncCredentials, SyncResult
from wattracker.cloud.models import CloudObject, ModelError, SyncBatch
from wattracker.cloud.security import new_installation_id
from wattracker.cloud.storage import (
    AzureTenantStore,
    MemoryTenantStore,
    StorageConflict,
    StaleRevision,
)
from wattracker.cloud.snapshot import snapshot_objects


def _batch(batch_id="b1", revision=1, object_id="a1", deleted=False):
    return SyncBatch(
        batch_id=batch_id,
        revision=revision,
        objects=(CloudObject(
            object_id=object_id,
            kind="activity",
            revision=revision,
            data={"watts": 250},
            deleted=deleted,
        ),),
    )


class _StorageError(Exception):
    def __init__(self, status_code):
        super().__init__(str(status_code))
        self.status_code = status_code


class _FakeBlob:
    def __init__(self, container, name):
        self.container = container
        self.name = name

    def upload_blob(self, payload, *, overwrite=False):
        if not overwrite and self.name in self.container.blobs:
            raise _StorageError(409)
        self.container.blobs[self.name] = bytes(payload)

    def download_blob(self, **_kwargs):
        payload = self.container.blobs[self.name]
        return type("Download", (), {"readall": lambda _self: payload})()


class _FakeContainer:
    def __init__(self):
        self.blobs = {}

    def get_blob_client(self, name):
        return _FakeBlob(self, name)


class _FakeBlobService:
    def __init__(self):
        self.container = _FakeContainer()

    def get_container_client(self, _name):
        return self.container


class _FakeTable:
    def __init__(self):
        self.entities = {}

    def get_entity(self, *, partition_key, row_key):
        try:
            return self.entities[(partition_key, row_key)]
        except KeyError as exc:
            raise _StorageError(404) from exc

    def create_entity(self, entity):
        key = (entity["PartitionKey"], entity["RowKey"])
        if key in self.entities:
            raise _StorageError(409)
        self.entities[key] = dict(entity)

    def upsert_entity(self, entity):
        self.entities[(entity["PartitionKey"], entity["RowKey"])] = dict(entity)

    def query_entities(self, *, query_filter):
        if "PartitionKey eq '" in query_filter:
            partition = query_filter.split("PartitionKey eq '", 1)[1].split("'", 1)[0]
            return [
                dict(entity)
                for (stored_partition, _), entity in self.entities.items()
                if stored_partition == partition
            ]
        lower = query_filter.split("PartitionKey ge '", 1)[1].split("'", 1)[0]
        upper = query_filter.split("PartitionKey lt '", 1)[1].split("'", 1)[0]
        return [
            dict(entity)
            for (stored_partition, _), entity in self.entities.items()
            if lower <= stored_partition < upper
        ]


class _FakeTableService:
    def __init__(self):
        self.table = _FakeTable()

    def get_table_client(self, _name):
        return self.table


def test_tenant_store_isolates_colliding_local_scopes_and_retains_tombstones():
    store = MemoryTenantStore()
    namespace_a = "a" * 64
    namespace_b = "b" * 64
    store.apply(namespace_a, "same-scope", _batch())
    store.apply(namespace_b, "same-scope", _batch())
    assert store.get(namespace_a, "same-scope", "a1").data["watts"] == 250
    assert store.get(namespace_b, "same-scope", "a1").data["watts"] == 250
    store.apply(namespace_a, "same-scope", _batch("b2", 2, deleted=True))
    assert store.get(namespace_a, "same-scope", "a1") is None
    assert store.get(namespace_a, "same-scope", "a1", include_deleted=True).deleted
    assert store.get(namespace_b, "same-scope", "a1") is not None


def test_azure_store_uses_verified_coordinates_and_recovers_idempotently():
    store = AzureTenantStore(_FakeBlobService(), _FakeTableService())
    namespace = "a" * 64
    store.apply(namespace, "scope", _batch())
    replay = store.apply(namespace, "scope", _batch())
    assert replay.replay
    assert store.get(namespace, "scope", "a1").data["watts"] == 250
    store.apply(namespace, "scope", _batch("b2", 2, deleted=True))
    assert store.get(namespace, "scope", "a1") is None
    assert store.get(namespace, "scope", "a1", include_deleted=True).deleted
    assert store.usage_for_namespace(namespace) > 0
    with pytest.raises(ValueError):
        store.get(namespace, "../other", "a1")


def test_store_idempotency_and_revision_conflicts_are_atomic():
    store = MemoryTenantStore()
    namespace = "a" * 64
    first = _batch()
    assert store.apply(namespace, "scope", first).accepted == 1
    assert store.apply(namespace, "scope", first).replay
    with pytest.raises(StorageConflict):
        store.apply(namespace, "scope", _batch("b1", 1, object_id="other"))
    with pytest.raises(StaleRevision):
        store.apply(namespace, "scope", _batch("b2", 1))
    assert [item.object_id for item in store.list_objects(namespace, "scope")] == ["a1"]


def test_sync_client_uses_bound_namespace_and_keeps_network_optional():
    batch = _batch()
    captured = {}

    def transport(url, headers, body):
        captured.update(url=url, headers=headers, body=body)
        return 200, b'{"revision":1,"replayed":false}'

    credentials = SyncCredentials(
        "c" * 64, "subscription", b"signing-key", namespace="a" * 64
    )
    client = CloudSyncClient(
        "https://cloud.example", credentials, transport=transport,
        mtls_headers={"X-APIM-Client-Certificate-Verified": "true"},
        clock=lambda: 100,
    )
    result = client.push(batch, namespace="wrong" * 10)
    assert result == SyncResult(True, 200, "ok", 1, False)
    assert captured["url"] == "https://cloud.example/api/v1/sync/batches"
    assert captured["headers"]["X-Writer-Credential"] == "c" * 64

    offline = CloudSyncClient("https://cloud.example", credentials)
    assert offline.push(batch).detail.startswith("Cloud sync offline")


def test_container_runtime_injects_azure_store(monkeypatch):
    from wattracker.cloud import runtime
    from wattracker.cloud.security import MemorySecurityStateBackend

    sentinel = object()
    security_backend = MemorySecurityStateBackend()
    security_backend.durable = True
    security_backend.verify_access = lambda *, writable: None
    monkeypatch.setenv(
        "WATTRACKER_CLOUD_SERVER_SECRET",
        "YWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWE=",
    )
    monkeypatch.setenv("WATTRACKER_CLOUD_OPERATOR_TOKEN", "operator-token")
    monkeypatch.setenv("WATTRACKER_APIM_PROOF_VALUE", "private-apim-proof")
    monkeypatch.setenv("WATTRACKER_CLOUD_PLANE", "sync")
    monkeypatch.setenv("WATTRACKER_STORAGE_ACCOUNT_NAME", "storageacct")
    monkeypatch.setattr(
        runtime.AzureTenantStore,
        "from_managed_identity",
        staticmethod(lambda name: sentinel),
    )
    monkeypatch.setattr(
        runtime.AzureTableSecurityStateBackend,
        "from_managed_identity",
        staticmethod(lambda name: security_backend),
    )
    app = runtime.create_runtime_app()
    assert app.state.cloud.store is sentinel
    assert app.state.cloud.credentials._backend is security_backend
    assert app.state.cloud.enrollments._backend is security_backend
    assert app.state.cloud.config.plane == "sync"
    assert app.state.cloud.config.apim_proof_header == "X-APIM-Request-Proof"


def test_batch_schema_rejects_paths_urls_commands_and_duplicates():
    with pytest.raises(ModelError):
        CloudObject("a", "activity", 1, {"path": "/tmp/file"})
    with pytest.raises(ModelError):
        SyncBatch.from_wire({
            "batch_id": "b",
            "revision": 1,
            "objects": [
                {"id": "a", "kind": "activity", "revision": 1, "data": {}},
                {"id": "a", "kind": "activity", "revision": 1, "data": {}},
            ],
        })


def test_readonly_snapshot_bounds_stream_decompression(tmp_path):
    path = tmp_path / "local.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE activities (id INTEGER, user_id INTEGER, start_time TEXT, "
        "duration_s INTEGER, distance_m REAL, avg_power REAL, avg_hr REAL, "
        "np REAL, if_ REAL, tss REAL, rpe INTEGER, streams BLOB)"
    )
    conn.execute(
        "INSERT INTO activities VALUES (1, 7, '2026-01-01', 1, 0, 1, 1, 1, 1, 1, 1, ?)",
        (zlib.compress(json.dumps({"power": [1] * 900_000}).encode()),),
    )
    conn.commit()
    conn.close()
    objects = snapshot_objects(path, 7, include_streams=True)
    assert objects[0].data.get("streams") is None
