import json
import sqlite3
import zlib

import pytest
from fastapi.testclient import TestClient

from wattracker.cloud.api import CloudConfig, CloudState, create_cloud_app
from wattracker.cloud.client import CloudSyncClient, SyncCredentials, SyncResult
from wattracker.cloud.models import (
    MAX_PAYLOAD_ARRAY_ITEMS,
    CloudObject,
    ModelError,
    SyncBatch,
)
from wattracker.cloud.security import (
    canonical_request,
    digest_body,
    generate_signing_keypair,
    new_installation_id,
    sign_request,
)
from wattracker.cloud.storage import (
    AzureTenantStore,
    MemoryTenantStore,
    StorageConflict,
    StaleRevision,
)
from wattracker.cloud.snapshot import snapshot_digest, snapshot_objects


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


@pytest.mark.parametrize("store_factory", [
    MemoryTenantStore,
    lambda: AzureTenantStore(_FakeBlobService(), _FakeTableService()),
])
def test_tenant_stores_page_revision_deltas_after_a_stable_object_cursor(store_factory):
    store = store_factory()
    namespace = "a" * 64
    store.apply(
        namespace,
        "scope",
        SyncBatch(
            batch_id="delta-before",
            revision=1,
            objects=(
                CloudObject("a", "profile", 1, {"ftp": 240}),
                CloudObject("b", "profile", 1, {"ftp": 250}),
            ),
        ),
    )
    store.apply(
        namespace,
        "scope",
        SyncBatch(
            batch_id="delta-after",
            revision=2,
            objects=(
                CloudObject("b", "profile", 2, {}, deleted=True),
                CloudObject("c", "profile", 2, {"ftp": 260}),
            ),
        ),
    )

    first = store.list_objects(
        namespace,
        "scope",
        kinds={"profile"},
        limit=1,
        include_deleted=True,
        min_revision=1,
        after="a",
    )
    second = store.list_objects(
        namespace,
        "scope",
        kinds={"profile"},
        limit=1,
        include_deleted=True,
        min_revision=1,
        after=first[-1].object_id,
    )
    assert [(item.object_id, item.deleted) for item in first] == [("b", True)]
    assert [(item.object_id, item.deleted) for item in second] == [("c", False)]


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


def _configure_container_runtime(monkeypatch, plane):
    from wattracker.cloud import runtime
    from wattracker.cloud.security import MemorySecurityStateBackend

    sentinel = object()
    security_backend = MemorySecurityStateBackend()
    security_backend.durable = True
    access_checks = []
    security_backend.verify_access = lambda *, writable: access_checks.append(writable)
    table_names = []
    monkeypatch.setenv(
        "WATTRACKER_CLOUD_SERVER_SECRET",
        "YWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWE=",
    )
    monkeypatch.setenv("WATTRACKER_CLOUD_OPERATOR_TOKEN", "operator-token")
    monkeypatch.setenv("WATTRACKER_APIM_PROOF_VALUE", "private-apim-proof")
    monkeypatch.setenv("WATTRACKER_CLOUD_PLANE", plane)
    monkeypatch.setenv("WATTRACKER_STORAGE_ACCOUNT_NAME", "storageacct")
    monkeypatch.setattr(
        runtime.AzureTenantStore,
        "from_managed_identity",
        staticmethod(lambda name: sentinel),
    )
    monkeypatch.setattr(
        runtime.AzureTableSecurityStateBackend,
        "from_managed_identity",
        staticmethod(
            lambda name, *, table_name="CloudAuth": (
                table_names.append(table_name) or security_backend
            )
        ),
    )
    return runtime.create_runtime_app(), sentinel, security_backend, table_names, access_checks


def test_container_runtime_sync_plane_injects_azure_store_and_replay_backend(monkeypatch):
    app, sentinel, security_backend, table_names, access_checks = _configure_container_runtime(
        monkeypatch, "sync"
    )
    assert app.state.cloud.store is sentinel
    assert app.state.cloud.credentials._backend is security_backend
    assert app.state.cloud.enrollments._backend is security_backend
    assert app.state.cloud.nonces._backend is security_backend
    assert table_names == ["CloudAuth", "CloudReplay"]
    assert access_checks == [False, True]
    assert app.state.cloud.config.apim_proof_value == "private-apim-proof"
    assert app.state.cloud.config.plane == "sync"
    assert app.state.cloud.config.apim_proof_header == "X-APIM-Request-Proof"


def test_container_runtime_read_plane_does_not_open_replay_table(monkeypatch):
    app, sentinel, security_backend, table_names, access_checks = _configure_container_runtime(
        monkeypatch, "read"
    )
    assert app.state.cloud.store is sentinel
    assert app.state.cloud.credentials._backend is security_backend
    assert app.state.cloud.enrollments._backend is security_backend
    # The read plane still never opens CloudReplay -- its managed identity has
    # no role there.  It does now claim replay nonces, because
    # POST /api/v1/context/refresh is a signed route, and a process-local
    # guard would re-open a captured refresh across a scale-to-zero restart.
    # Those claims go to the CloudAuth table the read identity already writes.
    assert app.state.cloud.nonces._backend is security_backend
    assert table_names == ["CloudAuth"]
    assert access_checks == [True]
    assert app.state.cloud.config.plane == "read"


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


def test_payload_array_bound_allows_realistic_streams_but_stays_bounded():
    CloudObject("a", "activity", 1, {"samples": [0] * MAX_PAYLOAD_ARRAY_ITEMS})
    with pytest.raises(ModelError):
        CloudObject("a", "activity", 1, {"samples": [0] * (MAX_PAYLOAD_ARRAY_ITEMS + 1)})


def test_readonly_snapshot_bounds_stream_decompression(tmp_path):
    path = tmp_path / "local.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE activities (id INTEGER, user_id INTEGER, start_time TEXT, "
        "duration_s INTEGER, distance_m REAL, avg_power REAL, avg_hr REAL, "
        "np REAL, if_ REAL, tss REAL, rpe INTEGER, duplicate_of INTEGER, streams BLOB)"
    )
    conn.execute(
        "CREATE TABLE power_sample_corrections (activity_id INTEGER, user_id INTEGER, "
        "start_index INTEGER, end_index INTEGER, undone_at TEXT)"
    )
    conn.execute(
        "INSERT INTO activities VALUES (1, 7, '2026-01-01', 1, 0, 1, 1, 1, 1, 1, 1, NULL, ?)",
        (zlib.compress(json.dumps({"power": [1] * 900_000}).encode()),),
    )
    conn.commit()
    conn.close()
    objects = snapshot_objects(path, 7, include_streams=True)
    assert objects[0].data.get("streams") is None


def test_snapshot_publishes_effective_power_and_digest_changes(tmp_path):
    path = tmp_path / "local.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE activities (id INTEGER, user_id INTEGER, start_time TEXT, "
        "duration_s INTEGER, distance_m REAL, avg_power REAL, avg_hr REAL, "
        "np REAL, if_ REAL, tss REAL, rpe INTEGER, duplicate_of INTEGER, streams BLOB)"
    )
    conn.execute(
        "CREATE TABLE power_sample_corrections (activity_id INTEGER, user_id INTEGER, "
        "start_index INTEGER, end_index INTEGER, undone_at TEXT)"
    )
    power = [100] * 4473
    power[4471:4473] = [2000, 2000]
    conn.execute(
        "INSERT INTO activities VALUES (749, 7, '2026-01-01', 4473, 0, 100, 1, "
        "100, 1, 1, 5, NULL, ?)",
        (zlib.compress(json.dumps({"power": power}).encode()),),
    )
    conn.execute(
        "INSERT INTO power_sample_corrections VALUES (749, 7, 4471, 4472, NULL)"
    )
    conn.commit()
    conn.close()

    objects = snapshot_objects(path, 7, include_streams=True)
    effective = objects[0].data["streams"]["power"]
    assert effective[4471:4473] == [None, None]
    assert 2000 not in effective

    before = snapshot_digest(objects)
    conn = sqlite3.connect(path)
    conn.execute(
        "UPDATE power_sample_corrections SET undone_at = '2026-01-02' "
        "WHERE activity_id = 749"
    )
    conn.commit()
    conn.close()
    after = snapshot_digest(snapshot_objects(path, 7, include_streams=True))
    assert before != after


def test_snapshot_excludes_hidden_duplicate_activities(tmp_path):
    path = tmp_path / "local.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE activities (id INTEGER, user_id INTEGER, start_time TEXT, "
        "duration_s INTEGER, distance_m REAL, avg_power REAL, avg_hr REAL, "
        "np REAL, if_ REAL, tss REAL, rpe INTEGER, duplicate_of INTEGER, streams BLOB)"
    )
    values = (1, 7, "2026-01-01", 1, 0, 1, 1, 1, 1, 1, 1, None, None)
    conn.executemany(
        "INSERT INTO activities VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (values, (*values[:1], 7, *values[2:11], 1, None)),
    )
    conn.commit()
    conn.close()
    assert [obj.object_id for obj in snapshot_objects(path, 7)] == ["activity-1"]


def test_snapshot_accepts_legacy_schema_without_duplicate_column(tmp_path):
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE activities (id INTEGER, user_id INTEGER, start_time TEXT, "
        "duration_s INTEGER, distance_m REAL, avg_power REAL, avg_hr REAL, "
        "np REAL, if_ REAL, tss REAL, rpe INTEGER, streams BLOB)"
    )
    conn.execute(
        "INSERT INTO activities VALUES (1, 7, '2026-01-01', 1, 0, 1, 1, 1, 1, 1, 1, ?)",
        (zlib.compress(json.dumps({"power": [2000]}).encode()),),
    )
    conn.execute(
        "INSERT INTO activities VALUES (2, 7, '2026-01-01', 1, 0, 1, 1, 1, 1, 1, 1, ?)",
        (zlib.compress(json.dumps([2000]).encode()),),
    )
    conn.commit()
    conn.close()
    objects = snapshot_objects(path, 7, include_streams=True)
    assert objects[0].data["streams"] == {"power": [2000]}
    assert objects[1].data["streams"] == [2000]


def test_snapshot_treats_incomplete_corrections_as_absent_and_omits_bad_json(tmp_path):
    path = tmp_path / "incomplete.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE activities (id INTEGER, user_id INTEGER, start_time TEXT, "
        "duration_s INTEGER, distance_m REAL, avg_power REAL, avg_hr REAL, "
        "np REAL, if_ REAL, tss REAL, rpe INTEGER, streams BLOB)"
    )
    conn.execute(
        "CREATE TABLE power_sample_corrections (activity_id INTEGER, user_id INTEGER, "
        "start_index INTEGER, end_index INTEGER)"
    )
    conn.executemany(
        "INSERT INTO activities VALUES (?, 7, '2026-01-01', 1, 0, 1, 1, 1, 1, 1, 1, ?)",
        (
            (1, zlib.compress(json.dumps({"power": [2000]}).encode())),
            (2, zlib.compress(b"not json")),
        ),
    )
    conn.commit()
    conn.close()
    objects = snapshot_objects(path, 7, include_streams=True)
    assert objects[0].data["streams"] == {"power": [2000]}
    assert "streams" not in objects[1].data


def test_snapshot_omits_nonfinite_json_streams(tmp_path):
    path = tmp_path / "nonfinite.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE activities (id INTEGER, user_id INTEGER, start_time TEXT, "
        "duration_s INTEGER, distance_m REAL, avg_power REAL, avg_hr REAL, "
        "np REAL, if_ REAL, tss REAL, rpe INTEGER, streams BLOB)"
    )
    conn.execute(
        "INSERT INTO activities VALUES (1, 7, '2026-01-01', 1, 0, 1, 1, 1, 1, 1, 1, ?)",
        (zlib.compress(b'{"power":[NaN, Infinity, -Infinity]}'),),
    )
    conn.commit()
    conn.close()
    assert "streams" not in snapshot_objects(path, 7, include_streams=True)[0].data


def test_snapshot_omits_valid_oversized_stream_but_keeps_activity(tmp_path):
    path = tmp_path / "oversized.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE activities (id INTEGER, user_id INTEGER, start_time TEXT, "
        "duration_s INTEGER, distance_m REAL, avg_power REAL, avg_hr REAL, "
        "np REAL, if_ REAL, tss REAL, rpe INTEGER, streams BLOB)"
    )
    conn.execute(
        "INSERT INTO activities VALUES (1, 7, '2026-01-01', 1, 0, 1, 1, 1, 1, 1, 1, ?)",
        (zlib.compress(json.dumps({"power": [100] * 16_385}).encode()),),
    )
    conn.commit()
    conn.close()
    objects = snapshot_objects(path, 7, include_streams=True)
    assert objects[0].data["avg_power"] == 1
    assert "streams" not in objects[0].data


# ---------------------------------------------------------------------------
# Two riders, two devices each, one colliding local scope name (#152)
# ---------------------------------------------------------------------------

_ISOLATION_SECRET = b"cloud-test-server-secret-32-bytes-long"


def _pairing_mint_headers(writer, nonce):
    canonical = canonical_request(
        "POST", "/api/v1/devices/pairing-codes", writer.namespace, 1_000, nonce,
        digest_body(b""), "device-pairing-code", "0",
    )
    return {
        "Ocp-Apim-Subscription-Key": writer.subscription_key.decode(),
        "X-Writer-Credential": writer.credential_id,
        "X-Writer-Timestamp": "1000",
        "X-Writer-Nonce": nonce,
        "X-Writer-Idempotency-Key": "device-pairing-code",
        "X-Writer-Revision": "0",
        "X-Writer-Signature": sign_request(writer.signing_key, canonical),
    }


def _pair_a_device(client, writer, nonce):
    """Mint a code as the desktop, redeem it as a phone, return the response.

    No subject header is sent anywhere in this flow: the namespace binding has
    to come from the code alone.
    """
    minted = client.post(
        "/api/v1/devices/pairing-codes",
        headers=_pairing_mint_headers(writer, nonce),
    )
    assert minted.status_code == 200, minted.text
    _private, public_key = generate_signing_keypair()
    paired = client.post("/api/v1/devices/pair", json={
        "code": minted.json()["pairing_code"], "public_key": public_key.hex()})
    assert paired.status_code == 200, paired.text
    return paired.json()


def test_paired_devices_are_isolated_across_installations_sharing_a_scope_name():
    """Two riders, the same local scope name, two devices each.

    This is the colliding-scope invariant above carried all the way to the
    read plane.  ``MemoryTenantStore`` already proves that ``"same-scope"``
    under two namespaces is two partitions; what #152 has to prove is that
    pairing cannot collapse them -- both of one rider's devices land in that
    rider's namespace, and neither can read across.

    Run with no identity provider and no subject header anywhere, which is the
    configuration that survives the gateway being removed.  The point is that
    the namespace binding comes from the pairing code and from nothing else:
    there is no subject here to carry it, and no header for a device to
    influence it with.
    """
    pytest.importorskip("cryptography")
    store = MemoryTenantStore()
    config = CloudConfig(
        server_secret=_ISOLATION_SECRET,
        operator_token="operator-token",
        require_apim_proof=False,
        require_verified_subject=False,
        clock=lambda: 1_000,
    )
    state = CloudState.create(config, store=store)
    client = TestClient(create_cloud_app(config, state=state))

    riders = {}
    for name, seed, object_id in (("a", b"a", "ride-a"), ("b", b"b", "ride-b")):
        writer = state.credentials.register_writer(
            new_installation_id(), "same-scope", seed * 32, seed[::-1] * 32
        )
        store.apply(writer.namespace, "same-scope", _batch(object_id=object_id))
        devices = [
            _pair_a_device(client, writer, f"{name}-{index}")
            for index in ("phone", "tablet")
        ]
        riders[name] = (writer, object_id, devices)

    writer_a, object_a, devices_a = riders["a"]
    writer_b, object_b, devices_b = riders["b"]
    assert writer_a.namespace != writer_b.namespace
    assert writer_a.local_user_scope == writer_b.local_user_scope == "same-scope"

    # Both of a rider's devices share that rider's namespace, and no device
    # was ever issued the other rider's.
    for writer, _object_id, devices in riders.values():
        assert {issued["signing_namespace"] for issued in devices} == {
            writer.namespace
        }
        for issued in devices:
            credential = state.credentials.resolve_device(issued["device_credential"])
            assert credential.namespace == writer.namespace
            assert credential.local_user_scope == "same-scope"
            # Nothing attests a subject, so no device carries one to be
            # checked against a header nobody vouches for.
            assert credential.subject is None
    credential_ids = {
        issued["device_credential"] for issued in devices_a + devices_b
    }
    assert len(credential_ids) == 4

    # Each device sees exactly its own rider's object and nothing else.
    audience = (
        [(device, object_a, object_b) for device in devices_a]
        + [(device, object_b, object_a) for device in devices_b]
    )
    assert len(audience) == 4
    for issued, mine, theirs in audience:
        # The bearer context and nothing else.  No subject header is sent, so
        # the scope these reads resolve to can only have come from the code.
        headers = {"Authorization": "Bearer " + issued["reader_context"]}
        listed = client.get("/api/v1/context/activities", headers=headers)
        assert listed.status_code == 200, listed.text
        assert [item["id"] for item in listed.json()["items"]] == [mine]

        # A cross-namespace read is a miss, never a refusal: 403 would confirm
        # the object exists somewhere.
        own = client.get(f"/api/v1/context/activities/{mine}", headers=headers)
        crossed = client.get(f"/api/v1/context/activities/{theirs}", headers=headers)
        assert own.status_code == 200
        assert crossed.status_code == 404
        assert crossed.json() == {"detail": "not found"}


class _CountingBlob(_FakeBlob):
    def download_blob(self, **kwargs):
        self.container.downloads.append(self.name)
        return super().download_blob(**kwargs)


class _CountingContainer(_FakeContainer):
    """A container that records every blob download, the way the cost
    regression on the mobile read path was actually measured."""

    def __init__(self):
        super().__init__()
        self.downloads = []

    def get_blob_client(self, name):
        return _CountingBlob(self, name)


class _CountingBlobService:
    def __init__(self):
        self.container = _CountingContainer()

    def get_container_client(self, _name):
        return self.container


def _azure_scope_with_objects(count, *, kind="load_point", revision=1):
    blobs = _CountingBlobService()
    tables = _FakeTableService()
    store = AzureTenantStore(blobs, tables)
    namespace = "d" * 64
    scope = "azure-scope"
    if count == 0:
        return store, blobs.container, namespace, scope
    store.apply(namespace, scope, SyncBatch(
        batch_id="seed",
        revision=revision,
        objects=tuple(
            CloudObject(f"o{index:03d}", kind, revision, {"tss": index})
            for index in range(count)
        ),
    ))
    blobs.container.downloads.clear()
    return store, blobs.container, namespace, scope


def test_azure_list_objects_downloads_at_most_one_blob_per_returned_item():
    store, container, namespace, scope = _azure_scope_with_objects(300)

    page = store.list_objects(namespace, scope, kinds={"load_point"}, limit=1)
    assert [value.object_id for value in page] == ["o000"]
    # Before the fix this downloaded every object in the scope to return one.
    assert len(container.downloads) == 1

    container.downloads.clear()
    page = store.list_objects(namespace, scope, kinds={"load_point"}, limit=10)
    assert [value.object_id for value in page] == [f"o{i:03d}" for i in range(10)]
    assert len(container.downloads) == 10

    # A caught-up delta poll matches nothing, so it must download nothing:
    # the ``min_revision`` filter comes off the table entity's ``Revision``,
    # which ``_put_object`` writes beside the blob.
    container.downloads.clear()
    assert store.list_objects(
        namespace, scope, kinds={"load_point"}, limit=100, min_revision=1
    ) == []
    assert container.downloads == []

    # Cursor position is also an entity-level filter.
    container.downloads.clear()
    page = store.list_objects(
        namespace, scope, kinds={"load_point"}, limit=2, after="o100"
    )
    assert [value.object_id for value in page] == ["o101", "o102"]
    assert len(container.downloads) == 2

    # A kind that matches nothing costs nothing.
    container.downloads.clear()
    assert store.list_objects(namespace, scope, kinds={"activity"}, limit=100) == []
    assert container.downloads == []


def test_azure_list_objects_pages_in_object_id_order_from_an_unordered_table():
    """The early exit must not depend on the order the table yields rows.

    ``_FakeTable`` returns a partition in insertion order, and nothing in
    ``AzureTenantStore`` enforces RowKey ordering, so the page boundary is
    established by sorting candidates -- not by trusting the stream.
    """
    store, container, namespace, scope = _azure_scope_with_objects(0)
    store.apply(namespace, scope, SyncBatch(
        batch_id="unordered",
        revision=2,
        objects=tuple(
            CloudObject(object_id, "load_point", 2, {})
            for object_id in ("z", "m", "a", "q", "b")
        ),
    ))
    container.downloads.clear()
    page = store.list_objects(namespace, scope, kinds={"load_point"}, limit=2)
    assert [value.object_id for value in page] == ["a", "b"]
    assert len(container.downloads) == 2
    revision, page = store.list_objects_with_revision(
        namespace, scope, kinds={"load_point"}, limit=3, after="b"
    )
    assert revision == 2
    assert [value.object_id for value in page] == ["m", "q", "z"]


def test_azure_mobile_read_never_takes_the_writer_scope_lease():
    """A read must not be able to block, or be blocked by, a write.

    ``_scope_lock`` is an exclusive 60-second blob lease.  On the read path it
    charged every poll an extra blob PUT, turned a second concurrent read into
    a bare 500 (``BlobLeaseClient.acquire`` fails fast on a leased blob), and
    let a read-only phone credential stall desktop writes for the lease
    duration if the reading process died mid-read.
    """
    import threading
    from contextlib import contextmanager

    store, _container, namespace, scope = _azure_scope_with_objects(5)
    held = threading.Lock()
    taken = []
    real_scope_lock = store._scope_lock

    @contextmanager
    def _exclusive(partition):
        # Model the real client: acquiring an already-held lease fails fast
        # rather than waiting.
        if not held.acquire(blocking=False):
            raise RuntimeError("lease conflict")
        taken.append(partition)
        try:
            with real_scope_lock(partition):
                yield
        finally:
            held.release()

    store._scope_lock = _exclusive

    # Two overlapping reads, and a read taken while a write holds the lease.
    with _exclusive(store._partition(namespace, scope)):
        taken.clear()
        revision, items = store.list_objects_with_revision(
            namespace, scope, kinds={"load_point"}, limit=3
        )
    assert [value.object_id for value in items] == ["o000", "o001", "o002"]
    assert revision == 1
    assert taken == [], "the read path took the writer's exclusive lease"

    # The writer still holds it.
    store.apply(namespace, scope, SyncBatch(
        batch_id="after-read",
        revision=2,
        objects=(CloudObject("o000", "load_point", 2, {"tss": 99}),),
    ))
    assert taken == [store._partition(namespace, scope)]


def test_azure_list_objects_with_revision_reads_the_checkpoint_before_the_page():
    """The floor guarantee: a write racing the listing must not be checkpointed.

    Reading the revision first means a concurrent write lands with a revision
    greater than the one returned, so the client's checkpoint stays behind it
    and the change is simply re-delivered.  The reverse order would let the
    checkpoint advance past a change the page did not carry.
    """
    store, _container, namespace, scope = _azure_scope_with_objects(3)
    order = []
    real_revision = store.revision
    real_list = store.list_objects

    def _revision(*args, **kwargs):
        order.append("revision")
        return real_revision(*args, **kwargs)

    def _list(*args, **kwargs):
        order.append("list")
        # A write lands mid-listing.
        store.apply(namespace, scope, SyncBatch(
            batch_id="racing",
            revision=9,
            objects=(CloudObject("o000", "load_point", 9, {"tss": 1}),),
        ))
        return real_list(*args, **kwargs)

    store.revision = _revision
    store.list_objects = _list
    revision, _items = store.list_objects_with_revision(
        namespace, scope, kinds={"load_point"}, limit=10
    )
    assert order == ["revision", "list"]
    # The checkpoint is the pre-write floor, so o000@9 is offered next poll.
    assert revision == 1
    store.revision = real_revision
    store.list_objects = real_list
    assert [value.object_id for value in store.list_objects(
        namespace, scope, kinds={"load_point"}, limit=10, min_revision=revision
    )] == ["o000"]
