"""The account wipe proves it may delete before it deletes anything (#170).

``wipe_scope`` removes a rider's credentials first and their data last.  Azure
RBAC propagation lags a deployment by minutes, so a wipe that ran while a new
delete grant had reached the auth table but not the data stores would delete
the credentials, 403 on the purge, and leave the data in the cloud with no
credential left to ask again (the HIGH finding on #357).  The route now proves
delete capability on every store first -- by deleting keys that cannot exist
-- and answers ``503 cloud wipe unavailable`` with nothing deleted otherwise.

Everything here runs against in-test doubles.  Nothing opens a network
connection or a real store: this is deletion code.
"""

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from wattracker.cloud import api as cloud_api
from wattracker.cloud import storage as storage_module
from wattracker.cloud.api import CloudConfig, CloudState, create_cloud_app
from wattracker.cloud.models import CloudObject, SyncBatch
from wattracker.cloud.security import (
    NEVER_SWEEP_RECORD_KINDS,
    SWEEPABLE_RECORD_KINDS,
    WIPE_PROBE_RECORD_KIND,
    AzureTableSecurityStateBackend,
    MemorySecurityStateBackend,
    canonical_request,
    digest_body,
    new_installation_id,
    sign_request,
)
from wattracker.cloud.storage import (
    WIPE_PROBE_BLOB_STEM,
    WIPE_PROBE_ROW_PREFIX,
    AzureTenantStore,
    MemoryTenantStore,
)
from wattracker.cloud.wipe import (
    WIPE_PROTECTED_RECORD_KINDS,
    WIPE_RECORD_KINDS,
    wipe_capability_proven,
)


SECRET = b"cloud-test-server-secret-32-bytes-long"
WIPE_PATH = "/api/v1/account/wipe"
WIPE_IDEMPOTENCY_KEY = "account-wipe"
NAMESPACE = "a" * 64
OTHER_NAMESPACE = "b" * 64
SCOPE = "rider"
ROOT = Path(__file__).parents[1]


# ---------------------------------------------------------------------------
# Doubles that answer like the Azure SDKs, with a switch for "no grant yet"
# ---------------------------------------------------------------------------


class _StorageError(Exception):
    """An SDK error carrying only a structured status, as ``_not_found`` reads."""

    def __init__(self, status_code):
        super().__init__(f"status {status_code}")
        self.status_code = status_code


class _Blob:
    # Deliberately no ``blob_name`` / ``container_name``: the store then skips
    # the real SDK lease client, exactly as for the other storage doubles.
    def __init__(self, container, name):
        self._container = container
        self.name = name

    def upload_blob(self, payload, *, overwrite=False):
        if self._container.deny_write:
            raise _StorageError(403)
        if not overwrite and self.name in self._container.blobs:
            raise _StorageError(409)
        self._container.blobs[self.name] = bytes(payload)

    def download_blob(self, **_kwargs):
        payload = self._container.blobs[self.name]
        return type("Download", (), {"readall": lambda _self: payload})()

    def delete_blob(self, **_kwargs):
        self._container.delete_attempts.append(self.name)
        # Authorization is decided before existence, as the service does.
        if self._container.deny_delete is not None:
            raise self._container.deny_delete
        try:
            del self._container.blobs[self.name]
        except KeyError as exc:
            raise _StorageError(404) from exc


class _BlobProperties:
    def __init__(self, name):
        self.name = name


class _Container:
    def __init__(self):
        self.blobs = {}
        self.deny_delete = None
        self.deny_write = False
        self.delete_attempts = []

    def get_blob_client(self, name):
        return _Blob(self, name)

    def list_blobs(self, *, name_starts_with):
        return [
            _BlobProperties(name)
            for name in sorted(self.blobs)
            if name.startswith(name_starts_with)
        ]


class _BlobService:
    def __init__(self):
        self.container = _Container()

    def get_container_client(self, _name):
        return self.container


class _Table:
    """A Tables double.  ``raise_missing`` picks between the two SDK shapes.

    ``azure-data-tables`` 12.x swallows a 404 from ``delete_entity`` and
    returns; an older or different client raises it.  The probe must read
    both as "authorized".
    """

    def __init__(self, *, raise_missing=False):
        self.entities = {}
        self.deny_delete = None
        self.raise_missing = raise_missing
        self.delete_attempts = []

    def get_entity(self, *, partition_key, row_key):
        try:
            return dict(self.entities[(partition_key, row_key)])
        except KeyError as exc:
            raise _StorageError(404) from exc

    def create_entity(self, entity):
        key = (entity["PartitionKey"], entity["RowKey"])
        if key in self.entities:
            raise _StorageError(409)
        self.entities[key] = dict(entity)

    def upsert_entity(self, entity):
        self.entities[(entity["PartitionKey"], entity["RowKey"])] = dict(entity)

    def delete_entity(self, *, partition_key, row_key):
        self.delete_attempts.append((partition_key, row_key))
        if self.deny_delete is not None:
            raise self.deny_delete
        if self.entities.pop((partition_key, row_key), None) is None:
            if self.raise_missing:
                raise _StorageError(404)

    def query_entities(self, *, query_filter, **_kwargs):
        partition = query_filter.split("PartitionKey eq '", 1)[1].split("'", 1)[0]
        return [
            dict(entity)
            for (stored_partition, _), entity in self.entities.items()
            if stored_partition == partition
        ]


class _TableService:
    def __init__(self, table):
        self.table = table

    def get_table_client(self, _name):
        return self.table


def _azure_store(*, raise_missing=False):
    blobs = _BlobService()
    table = _Table(raise_missing=raise_missing)
    return AzureTenantStore(blobs, _TableService(table)), blobs.container, table


def _batch(batch_id, revision, object_id):
    return SyncBatch(
        batch_id=batch_id,
        revision=revision,
        objects=(CloudObject(
            object_id=object_id, kind="activity", revision=revision,
            data={"watts": 250},
        ),),
    )


def _wipe_headers(writer, *, nonce="wipe-1", timestamp=1_000):
    canonical = canonical_request(
        "POST", WIPE_PATH, writer.namespace, timestamp, nonce,
        digest_body(b""), WIPE_IDEMPOTENCY_KEY, "0",
    )
    return {
        "Ocp-Apim-Subscription-Key": writer.subscription_key.decode(),
        "X-Writer-Credential": writer.credential_id,
        "X-Writer-Timestamp": str(timestamp),
        "X-Writer-Nonce": nonce,
        "X-Writer-Idempotency-Key": WIPE_IDEMPOTENCY_KEY,
        "X-Writer-Revision": "0",
        "X-Writer-Signature": sign_request(writer.signing_key, canonical),
    }


def _config():
    return CloudConfig(
        server_secret=SECRET,
        operator_token="operator-token",
        plane="read",
        allow_account_wipe=True,
        require_gateway_proof=False,
        require_verified_subject=False,
        clock=lambda: 1_000,
    )


class _Deployment:
    """One rider with synced data, on an Azure store double and a memory auth table."""

    def __init__(self):
        self.store, self.container, self.table = _azure_store()
        self.backend = MemorySecurityStateBackend()
        self.config = _config()
        self.state = CloudState.create(
            self.config, store=self.store, security_backend=self.backend
        )
        self.writer = self.state.credentials.register_writer(
            new_installation_id(), SCOPE, b"w" * 32, b"sub-w"
        )
        self.store.apply(
            self.writer.namespace, self.writer.local_user_scope,
            _batch("b1", 1, "ride-1"),
        )
        self.partition = f"{self.writer.namespace}:{self.writer.local_user_scope}"

    def snapshot(self):
        return (
            dict(self.backend._records),
            dict(self.container.blobs),
            {key: dict(value) for key, value in self.table.entities.items()},
        )

    def wipe(self):
        with TestClient(create_cloud_app(self.config, state=self.state)) as client:
            return client.post(WIPE_PATH, headers=_wipe_headers(self.writer))


def _deny(deployment, store):
    if store == "blobs":
        deployment.container.deny_delete = _StorageError(403)
    elif store == "object-rows":
        deployment.table.deny_delete = _StorageError(403)
    elif store == "auth":
        deployment.backend.delete_denied = True
    elif store == "lease":
        deployment.container.deny_write = True
    else:  # pragma: no cover - test bug
        raise AssertionError(store)


# ---------------------------------------------------------------------------
# The route: refuse with nothing deleted, or proceed exactly as before
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("store", ["blobs", "object-rows", "auth", "lease"])
def test_a_wipe_without_delete_permission_on_any_store_deletes_nothing(store):
    """The stranding scenario: one store has not received the grant yet.

    Before the probe, "blobs" and "object-rows" deleted the rider's writer
    credential and then 403'd in the purge, leaving the data in the cloud with
    nothing able to ask for it again.  "lease" is the same failure one step
    earlier: the purge takes the scope lease before it deletes, and that needs
    a blob write a delete-only grant does not carry.
    """

    deployment = _Deployment()
    records, blobs, entities = deployment.snapshot()
    _deny(deployment, store)

    response = deployment.wipe()

    assert response.status_code == 503
    assert response.json() == {"detail": "cloud wipe unavailable"}
    # The credential still authenticates, so the rider can retry.
    assert deployment.state.credentials.lookup_writer(
        deployment.writer.credential_id
    ) is not None
    # Every record that existed still exists, unchanged.  (The request itself
    # claims a replay nonce, so the table may have gained a row; it must not
    # have lost one.)
    after_records, after_blobs, after_entities = deployment.snapshot()
    for key, value in records.items():
        assert after_records.get(key) == value
    assert after_blobs == blobs
    assert after_entities == entities
    assert deployment.store.get(
        deployment.writer.namespace, deployment.writer.local_user_scope, "ride-1"
    ) is not None


def test_with_every_grant_in_place_the_wipe_proceeds_as_before():
    deployment = _Deployment()
    other = deployment.state.credentials.register_writer(
        new_installation_id(), SCOPE, b"o" * 32, b"sub-o"
    )
    deployment.store.apply(
        other.namespace, other.local_user_scope, _batch("b1", 1, "ride-1")
    )

    response = deployment.wipe()

    assert response.status_code == 200
    assert response.json() == {"wiped": True}
    assert deployment.state.credentials.lookup_writer(
        deployment.writer.credential_id
    ) is None
    assert not [
        name for name in deployment.container.blobs
        if name.startswith(f"{deployment.partition}/")
    ]
    assert not [
        key for key in deployment.table.entities if key[0] == deployment.partition
    ]
    # The other rider is untouched.
    assert deployment.state.credentials.lookup_writer(other.credential_id) is not None
    assert deployment.store.get(
        other.namespace, other.local_user_scope, "ride-1"
    ) is not None


def test_the_memory_store_knob_refuses_the_wipe_with_nothing_deleted():
    store = MemoryTenantStore()
    backend = MemorySecurityStateBackend()
    config = _config()
    state = CloudState.create(config, store=store, security_backend=backend)
    writer = state.credentials.register_writer(
        new_installation_id(), SCOPE, b"m" * 32, b"sub-m"
    )
    store.apply(writer.namespace, writer.local_user_scope, _batch("b1", 1, "ride-1"))
    store.delete_denied = True

    with TestClient(create_cloud_app(config, state=state)) as client:
        response = client.post(WIPE_PATH, headers=_wipe_headers(writer))

    assert response.status_code == 503
    assert state.credentials.lookup_writer(writer.credential_id) is not None
    assert store.revision(writer.namespace, writer.local_user_scope) == 1

    store.delete_denied = False
    with TestClient(create_cloud_app(config, state=state)) as client:
        retried = client.post(
            WIPE_PATH, headers=_wipe_headers(writer, nonce="wipe-2")
        )
    # Once the grant arrives, the same credential completes the wipe.
    assert retried.status_code == 200
    assert state.credentials.lookup_writer(writer.credential_id) is None
    assert store.revision(writer.namespace, writer.local_user_scope) == 0


def test_the_route_asks_the_probe_before_it_deletes(monkeypatch):
    """Ordering, directly: the probe runs, and a "no" stops ``wipe_scope``."""

    deployment = _Deployment()
    calls = []

    def probe(*_args, **_kwargs):
        calls.append("probe")
        return False

    def wipe(*_args, **_kwargs):  # pragma: no cover - must not run
        calls.append("wipe")
        raise AssertionError("wipe_scope ran after a failed probe")

    monkeypatch.setattr(cloud_api, "wipe_capability_proven", probe)
    monkeypatch.setattr(cloud_api, "wipe_scope", wipe)

    assert deployment.wipe().status_code == 503
    assert calls == ["probe"]


# ---------------------------------------------------------------------------
# The Azure tenant store probe: 404 is "may delete", everything else is not
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raise_missing", [False, True])
def test_the_store_probe_reads_not_found_as_permission(raise_missing):
    store, container, table = _azure_store(raise_missing=raise_missing)
    assert store.can_purge_scope(NAMESPACE, SCOPE) is True
    assert len(container.delete_attempts) == 1
    assert len(table.delete_attempts) == 1


@pytest.mark.parametrize("target", ["blob", "table"])
@pytest.mark.parametrize(
    "error",
    [
        _StorageError(403),
        _StorageError(401),
        _StorageError(500),
        _StorageError(True),  # a bool is not a status
        RuntimeError("no structured status at all"),
    ],
    ids=["403", "401", "500", "bool-status", "no-status"],
)
def test_the_store_probe_reads_anything_but_not_found_as_no_permission(target, error):
    store, container, table = _azure_store()
    if target == "blob":
        container.deny_delete = error
    else:
        table.deny_delete = error
    assert store.can_purge_scope(NAMESPACE, SCOPE) is False


def test_the_store_probe_uses_the_sdks_own_exception_classes():
    """The installed azure-core, where present: 403 is an HttpResponseError,
    404 is a ResourceNotFoundError.  Classified by class and status, never by
    the message -- a 403 that quotes "not found" is still a 403."""

    exceptions = pytest.importorskip("azure.core.exceptions")
    store, container, _table = _azure_store()

    forbidden = exceptions.HttpResponseError(
        message="The specified resource was not found; AuthorizationPermissionMismatch"
    )
    forbidden.status_code = 403
    container.deny_delete = forbidden
    assert store.can_purge_scope(NAMESPACE, SCOPE) is False

    container.deny_delete = exceptions.ResourceNotFoundError(message="BlobNotFound")
    assert store.can_purge_scope(NAMESPACE, SCOPE) is True


def test_the_store_probe_refuses_when_the_scope_lease_cannot_be_taken():
    store, container, _table = _azure_store()
    container.deny_write = True
    assert store.can_purge_scope(NAMESPACE, SCOPE) is False


def test_the_store_probe_never_deletes_a_real_item():
    store, container, table = _azure_store()
    for namespace in (NAMESPACE, OTHER_NAMESPACE):
        store.apply(namespace, SCOPE, _batch("b1", 1, "ride-1"))
        store.apply(namespace, SCOPE, _batch("b2", 2, "ride-2"))
    blobs = dict(container.blobs)
    entities = {key: dict(value) for key, value in table.entities.items()}

    for _ in range(3):
        assert store.can_purge_scope(NAMESPACE, SCOPE) is True

    assert container.blobs == blobs
    assert table.entities == entities
    partition = f"{NAMESPACE}:{SCOPE}"
    for name in container.delete_attempts:
        assert name.startswith(f"{partition}/{WIPE_PROBE_BLOB_STEM}")
        assert name not in blobs
    for partition_key, row_key in table.delete_attempts:
        assert partition_key == partition
        assert row_key.startswith(WIPE_PROBE_ROW_PREFIX)
        assert (partition_key, row_key) not in entities
    # A fresh random key each time, so no probe can be aimed at a fixed name.
    assert len(set(container.delete_attempts)) == 3
    assert len(set(table.delete_attempts)) == 3


def test_the_store_probe_rejects_a_scope_the_store_would_not_address():
    store, container, table = _azure_store()
    # Invalid coordinates raise before any request is made.
    with pytest.raises(ValueError):
        store.can_purge_scope("not-a-namespace", SCOPE)
    with pytest.raises(ValueError):
        store.can_purge_scope(NAMESPACE, "../other")
    assert container.delete_attempts == []
    assert table.delete_attempts == []


def test_the_probe_names_cannot_collide_with_a_real_name():
    partition = f"{NAMESPACE}:{SCOPE}"
    real_blob_names = [
        AzureTenantStore._blob_name(partition, "ride-1"),
        AzureTenantStore._blob_name(partition, "__wipe-probe-x"[2:]),
        f"{partition}/__lock",
    ]
    for name in real_blob_names:
        assert not name.startswith(f"{partition}/{WIPE_PROBE_BLOB_STEM}")
    # Every object blob is ``<partition>/object:...``; the probe stem is not.
    assert not WIPE_PROBE_BLOB_STEM.startswith("object:")
    assert WIPE_PROBE_BLOB_STEM != "__lock"
    # Object ids cannot start with ``_`` at all, so no validated id yields a
    # name under the stem even if the ``object:`` prefix were ever dropped.
    with pytest.raises(ValueError):
        AzureTenantStore._row_key(WIPE_PROBE_BLOB_STEM + "0")

    real_row_keys = [
        AzureTenantStore._row_key("ride-1"),
        AzureTenantStore._batch_row("b1"),
        AzureTenantStore._scope_row(),
    ]
    for row_key in real_row_keys:
        assert not row_key.startswith(WIPE_PROBE_ROW_PREFIX)
    for prefix in ("object:", "batch:", "scope"):
        assert not WIPE_PROBE_ROW_PREFIX.startswith(prefix)


# ---------------------------------------------------------------------------
# The auth table probe
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raise_missing", [False, True])
def test_the_auth_probe_reads_not_found_as_permission(raise_missing):
    table = _Table(raise_missing=raise_missing)
    backend = AzureTableSecurityStateBackend(table)
    assert backend.can_delete() is True
    [(partition, row_key)] = table.delete_attempts
    assert partition == "__wattracker_auth_v1__"
    kind, _, key = row_key.partition(":")
    assert kind == WIPE_PROBE_RECORD_KIND
    assert re.fullmatch(r"[0-9a-f]{64}", key)


@pytest.mark.parametrize(
    "error",
    [
        _StorageError(403),
        _StorageError(401),
        _StorageError(503),
        RuntimeError("no structured status at all"),
    ],
    ids=["403", "401", "503", "no-status"],
)
def test_the_auth_probe_reads_anything_but_not_found_as_no_permission(error):
    table = _Table()
    table.deny_delete = error
    assert AzureTableSecurityStateBackend(table).can_delete() is False


def test_the_auth_probe_never_deletes_a_real_row():
    table = _Table()
    backend = AzureTableSecurityStateBackend(table)
    key = "c" * 64
    for kind in (*WIPE_RECORD_KINDS, *sorted(WIPE_PROTECTED_RECORD_KINDS)):
        backend.write(kind, key, {"namespace": NAMESPACE})
    entities = {k: dict(v) for k, v in table.entities.items()}
    for _ in range(3):
        assert backend.can_delete() is True
    assert table.entities == entities
    assert len(set(table.delete_attempts)) == 3


def test_the_memory_auth_probe_follows_its_knob_and_deletes_nothing():
    backend = MemorySecurityStateBackend()
    backend.write("writer", "d" * 64, {"namespace": NAMESPACE})
    records = dict(backend._records)
    assert backend.can_delete() is True
    assert backend._records == records
    backend.delete_denied = True
    assert backend.can_delete() is False
    with pytest.raises(PermissionError):
        backend.delete("writer", "d" * 64)
    assert backend._records == records


def test_the_probe_record_kind_is_written_by_nothing():
    """A real row of this kind is the only way a probe could hit real data.

    The kind appears in the package only where the probe is defined, and it
    is in none of the kind lists that name what the application stores.
    """

    assert WIPE_PROBE_RECORD_KIND not in WIPE_RECORD_KINDS
    assert WIPE_PROBE_RECORD_KIND not in WIPE_PROTECTED_RECORD_KINDS
    assert WIPE_PROBE_RECORD_KIND not in SWEEPABLE_RECORD_KINDS
    assert WIPE_PROBE_RECORD_KIND not in NEVER_SWEEP_RECORD_KINDS
    sources = {
        path.relative_to(ROOT).as_posix(): path.read_text()
        for path in (ROOT / "wattracker").rglob("*.py")
    }
    users = sorted(
        name for name, text in sources.items() if '"wipe-probe' in text
    )
    assert users == ["wattracker/cloud/security.py", "wattracker/cloud/storage.py"]
    assert sources["wattracker/cloud/security.py"].count('"wipe-probe"') == 1
    assert sources["wattracker/cloud/storage.py"].count('"wipe-probe:"') == 1


# ---------------------------------------------------------------------------
# The combinator the route calls
# ---------------------------------------------------------------------------


class _NoProbe:
    """A collaborator that predates the probe: it must read as "cannot"."""


class _RaisingProbe:
    def can_purge_scope(self, *_args):
        raise RuntimeError("probe blew up")

    def can_delete(self):
        raise RuntimeError("probe blew up")


class _TruthyProbe:
    """``1`` is not ``True``: only a real yes counts."""

    def can_purge_scope(self, *_args):
        return 1

    def can_delete(self):
        return "yes"


@pytest.mark.parametrize("bad", [_NoProbe(), _RaisingProbe(), _TruthyProbe()])
def test_the_combinator_fails_closed_on_either_collaborator(bad):
    good_store = MemoryTenantStore()
    good_backend = MemorySecurityStateBackend()
    assert wipe_capability_proven(
        NAMESPACE, SCOPE, store=good_store, security_backend=good_backend
    ) is True
    assert wipe_capability_proven(
        NAMESPACE, SCOPE, store=bad, security_backend=good_backend
    ) is False
    assert wipe_capability_proven(
        NAMESPACE, SCOPE, store=good_store, security_backend=bad
    ) is False


def test_the_combinator_refuses_an_empty_call():
    assert wipe_capability_proven(
        NAMESPACE, SCOPE, store=None, security_backend=None
    ) is False


# ---------------------------------------------------------------------------
# The purge waits out a sync that holds the scope lease (review round 2)
#
# The probe takes and releases the scope lease before ``wipe_scope`` starts,
# and ``wipe_scope`` deletes the credentials before it purges.  A sync that
# took the lease in between used to fail the purge at once: credentials gone,
# data stranded.  The purge -- and only the purge -- now retries a lease
# conflict until a bounded deadline.
# ---------------------------------------------------------------------------


class _LeaseError(_StorageError):
    """A lease SDK error: structured status plus the service's error code."""

    def __init__(self, status_code, error_code=None):
        super().__init__(status_code)
        if error_code is not None:
            self.error_code = error_code


def _lease_held():
    return _LeaseError(409, "LeaseAlreadyPresent")


class _FakeClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        assert seconds > 0
        self.sleeps.append(seconds)
        self.now += seconds


class _Lease:
    """A lease double.  ``busy(attempt, now)`` says whether somebody else
    holds the lease at that attempt; ``error`` fails every attempt instead."""

    def __init__(self, clock, *, busy=lambda attempt, now: False, error=None):
        self.clock = clock
        self.busy = busy
        self.error = error
        self.attempts = []
        self.durations = []
        self.releases = 0

    def acquire(self, *, lease_duration):
        attempt = len(self.attempts)
        self.attempts.append(self.clock.now)
        self.durations.append(lease_duration)
        if self.error is not None:
            raise self.error
        if self.busy(attempt, self.clock.now):
            raise _lease_held()

    def release(self):
        self.releases += 1


def _leased_store():
    clock = _FakeClock()
    blobs = _BlobService()
    table = _Table()
    store = AzureTenantStore(
        blobs, _TableService(table), monotonic=clock.monotonic, sleep=clock.sleep
    )
    lease = _Lease(clock)
    store._new_lease = lambda _blob: lease
    store.apply(NAMESPACE, SCOPE, _batch("b1", 1, "ride-1"))
    lease.attempts.clear()
    lease.releases = 0
    return store, blobs.container, table, clock, lease


def test_the_purge_waits_out_a_sync_that_holds_the_scope_lease_briefly():
    store, container, table, clock, lease = _leased_store()
    # A sync holds the lease for the next three seconds, then releases it.
    lease.busy = lambda _attempt, now: now < 3.0

    purge = store.purge_scope(NAMESPACE, SCOPE)

    assert purge.objects == 1 and purge.blobs == 1
    assert container.blobs == {}
    assert table.entities == {}
    assert len(lease.attempts) > 1
    assert 3.0 <= clock.now < storage_module.PURGE_LEASE_DEADLINE_SECONDS
    assert lease.releases == 1
    assert set(lease.durations) == {storage_module.SCOPE_LEASE_SECONDS}


def test_the_purge_gives_up_at_the_deadline_having_deleted_nothing():
    """A lease that is never released: the purge fails at the deadline, as it
    failed at once before, and it has deleted nothing -- it deletes only
    under the lease, and it never got the lease."""

    store, container, table, clock, lease = _leased_store()
    blobs_before = dict(container.blobs)
    rows_before = {key: dict(value) for key, value in table.entities.items()}
    lease.busy = lambda _attempt, _now: True

    with pytest.raises(_LeaseError) as raised:
        store.purge_scope(NAMESPACE, SCOPE)

    assert raised.value.status_code == 409
    deadline = storage_module.PURGE_LEASE_DEADLINE_SECONDS
    assert deadline <= 65.0
    assert clock.now == pytest.approx(deadline)
    assert sum(clock.sleeps) == pytest.approx(deadline)
    assert max(clock.sleeps) <= 4.0
    # The last attempt is made at the deadline, and none after it.
    assert lease.attempts[-1] == pytest.approx(deadline)
    assert container.delete_attempts == []
    assert table.delete_attempts == []
    assert container.blobs == blobs_before
    assert table.entities == rows_before
    assert lease.releases == 0


@pytest.mark.parametrize(
    "error",
    [
        _LeaseError(403),
        _LeaseError(409, "LeaseIdMismatchWithLeaseOperation"),
        _LeaseError(409),
        _LeaseError(412, "LeaseAlreadyPresent"),
        _LeaseError(500, "LeaseAlreadyPresent"),
        RuntimeError("409 LeaseAlreadyPresent: there is already a lease present"),
    ],
    ids=[
        "403", "409-other-code", "409-no-code", "412-lease-code",
        "500-lease-code", "message-text-only",
    ],
)
def test_the_purge_does_not_retry_anything_but_a_held_lease(error):
    store, container, table, clock, lease = _leased_store()
    lease.error = error

    with pytest.raises(type(error)) as raised:
        store.purge_scope(NAMESPACE, SCOPE)

    assert raised.value is error
    assert len(lease.attempts) == 1
    assert clock.sleeps == []
    assert container.delete_attempts == []
    assert table.delete_attempts == []


def test_the_sync_writer_and_the_probe_still_fail_fast_on_a_held_lease():
    """Only the purge waits.  The sync client retries its own batch, and a
    probe that meets a held lease answers "not now" with nothing deleted."""

    store, _container, _table, clock, lease = _leased_store()
    lease.busy = lambda _attempt, _now: True

    with pytest.raises(_LeaseError):
        store.apply(NAMESPACE, SCOPE, _batch("b2", 2, "ride-2"))
    assert len(lease.attempts) == 1

    assert store.can_purge_scope(NAMESPACE, SCOPE) is False
    assert len(lease.attempts) == 2
    assert clock.sleeps == []


def test_the_lease_conflict_is_read_from_the_sdks_own_error_shape():
    """The real Blob SDK path: a 409 lease response run through the SDK's
    ``process_storage_error`` comes out as ``ResourceExistsError`` with
    ``status_code`` 409 and ``error_code`` ``LeaseAlreadyPresent``."""

    exceptions = pytest.importorskip("azure.core.exceptions")
    try:
        from azure.core.rest import HttpRequest
        from azure.core.rest._http_response_impl import HttpResponseImpl
        from azure.storage.blob._shared.response_handlers import (
            process_storage_error,
        )
    except ImportError:  # pragma: no cover - SDK layout changed
        pytest.skip("Azure Blob SDK internals not available")

    def sdk_error(status, code):
        response = HttpResponseImpl(
            request=HttpRequest("PUT", "https://account.invalid/c/b?comp=lease"),
            internal_response=None,
            status_code=status,
            reason="Conflict",
            content_type="application/xml",
            headers={"x-ms-error-code": code, "Content-Type": "application/xml"},
            stream_download_generator=None,
        )
        response._content = (
            f"<?xml version=\"1.0\" encoding=\"utf-8\"?><Error><Code>{code}"
            "</Code><Message>There is already a lease present.</Message></Error>"
        ).encode()
        response._is_closed = True
        response._is_stream_consumed = True
        try:
            process_storage_error(exceptions.ResourceExistsError(response=response))
        except exceptions.HttpResponseError as exc:
            return exc
        raise AssertionError("process_storage_error did not raise")

    held = sdk_error(409, "LeaseAlreadyPresent")
    assert isinstance(held, exceptions.ResourceExistsError)
    assert AzureTenantStore._is_lease_conflict(held) is True
    assert AzureTenantStore._is_lease_conflict(
        sdk_error(409, "LeaseIdMismatchWithLeaseOperation")
    ) is False

    store, container, _table, clock, lease = _leased_store()
    outcomes = [held, held]

    def acquire(*, lease_duration):
        lease.attempts.append(clock.now)
        if outcomes:
            raise outcomes.pop(0)

    lease.acquire = acquire
    store.purge_scope(NAMESPACE, SCOPE)
    assert len(lease.attempts) == 3
    assert container.blobs == {}


def test_a_wipe_that_meets_a_sync_holding_the_lease_still_completes():
    """End to end through the route: the probe gets the lease, a sync takes
    it before the purge does, and the purge waits it out.  The rider's
    credentials and data are both gone, and the route says so."""

    deployment = _Deployment()
    clock = _FakeClock()
    deployment.store._monotonic = clock.monotonic
    deployment.store._sleep = clock.sleep
    lease = _Lease(clock)
    deployment.store._new_lease = lambda _blob: lease
    # Attempt 0 is the probe's.  Then a sync holds the lease for two seconds.
    sync_took_it_at = []

    def busy(attempt, now):
        if attempt == 1:
            sync_took_it_at.append(now)
        return attempt >= 1 and now < sync_took_it_at[0] + 2.0

    lease.busy = busy

    response = deployment.wipe()

    assert response.status_code == 200
    assert response.json() == {"wiped": True}
    assert len(lease.attempts) > 2
    assert deployment.state.credentials.lookup_writer(
        deployment.writer.credential_id
    ) is None
    assert not [
        name for name in deployment.container.blobs
        if name.startswith(f"{deployment.partition}/")
    ]
    assert not [
        key for key in deployment.table.entities if key[0] == deployment.partition
    ]
