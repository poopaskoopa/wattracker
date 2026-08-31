"""Durable daily quota counters.

These counters are the deployment's abuse and cost control once the API
Management gateway is gone (#164): the container apps run at
``minReplicas: 0``, so a counter that only lives in a process is reset several
times a day by the platform itself and enforces nothing.  Every test here is
therefore about one of three properties -- it survives a restart, it survives
a second replica charging the same row, and it does not grow without bound.
"""

import itertools
import json
import sys
import threading
import types
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from wattracker.cloud.api import CloudConfig, CloudState, create_cloud_app
from wattracker.cloud.limits import (
    DurableQuotaCounters,
    INSTALLATION_SUBJECT,
    METRIC_READ_BYTES,
    METRIC_READ_REQUESTS,
    METRIC_UPLOAD_BYTES,
    QUOTA_RECORD_KIND,
    QuotaExceeded,
    QuotaManager,
    QuotaPolicy,
    SCOPE_SUBJECT,
    counter_key,
)
from wattracker.cloud.security import (
    AzureTableSecurityStateBackend,
    MemorySecurityStateBackend,
    SecurityStateUnavailable,
    canonical_request,
    digest_body,
    new_installation_id,
    sign_request,
)

from fastapi.testclient import TestClient


SECRET = b"cloud-test-server-secret-32-bytes-long"
NAMESPACE = "a" * 64
OTHER_NAMESPACE = "b" * 64
DAY_ONE = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
DAY_TWO = DAY_ONE + timedelta(days=1)


class _DurableMemoryBackend(MemorySecurityStateBackend):
    """A shared-process backend that claims durability, as the tests' stand-in
    for one Azure table shared by several replicas."""

    durable = True


def _durable(policy: QuotaPolicy, backend, **kwargs) -> QuotaManager:
    return QuotaManager(policy, counters=DurableQuotaCounters(backend), **kwargs)


def _read_counter(backend, subject, namespace, scope, metric):
    return backend.read(
        QUOTA_RECORD_KIND, counter_key(subject, namespace, scope, metric)
    )


# ---------------------------------------------------------------------------
# An Azure Table stand-in with real optimistic concurrency
# ---------------------------------------------------------------------------


class _StorageError(Exception):
    def __init__(self, status_code):
        super().__init__(str(status_code))
        self.status_code = status_code


class _EtagTable:
    """A table client with just enough Azure semantics to lose a race.

    Each individual operation is atomic and nothing spans two of them, which
    is exactly the property that makes a read-then-write increment drop
    updates and an etag-guarded one not.
    """

    def __init__(self, *, before_update=None):
        self._lock = threading.Lock()
        self._etags = itertools.count(1)
        self.entities = {}
        self.conflicts = 0
        self.updates = 0
        self._before_update = before_update

    def create_entity(self, entity):
        key = (entity["PartitionKey"], entity["RowKey"])
        with self._lock:
            if key in self.entities:
                raise _StorageError(409)
            stored = dict(entity)
            stored["etag"] = f'W/"{next(self._etags)}"'
            self.entities[key] = stored
            return dict(stored)

    def get_entity(self, *, partition_key, row_key):
        with self._lock:
            try:
                return dict(self.entities[(partition_key, row_key)])
            except KeyError as exc:
                raise _StorageError(404) from exc

    def update_entity(self, entity, *, mode=None, etag=None, match_condition=None):
        if self._before_update is not None:
            # Deliberately outside the lock: this is where a second replica
            # gets to slip in between another one's read and its write.
            self._before_update()
        key = (entity["PartitionKey"], entity["RowKey"])
        with self._lock:
            stored = self.entities.get(key)
            if stored is None:
                raise _StorageError(404)
            if match_condition is not None and stored["etag"] != etag:
                self.conflicts += 1
                raise _StorageError(412)
            merged = dict(stored)
            merged.update({k: v for k, v in entity.items() if k != "etag"})
            merged["etag"] = f'W/"{next(self._etags)}"'
            self.entities[key] = merged
            self.updates += 1

    def payloads(self):
        return [json.loads(entity["Payload"]) for entity in self.entities.values()]


@pytest.fixture()
def azure_sdk_enums(monkeypatch):
    """Stand in for the two SDK enums the table backend imports.

    Requiring the optional ``azure-data-tables`` install just to name two
    sentinels would make every test below skip on a machine without the cloud
    extra, and a concurrency test that skips is precisely the kind of green
    that proves nothing.  The fake table already substitutes the service.
    """

    try:  # pragma: no cover - depends on the machine, not the code
        import azure.core  # noqa: F401
        import azure.data.tables  # noqa: F401
    except ImportError:
        azure = types.ModuleType("azure")
        azure.__path__ = []
        core = types.ModuleType("azure.core")
        core.MatchConditions = types.SimpleNamespace(IfNotModified="IfNotModified")
        data = types.ModuleType("azure.data")
        data.__path__ = []
        tables = types.ModuleType("azure.data.tables")
        tables.UpdateMode = types.SimpleNamespace(MERGE="merge")
        for name, module in (
            ("azure", azure),
            ("azure.core", core),
            ("azure.data", data),
            ("azure.data.tables", tables),
        ):
            monkeypatch.setitem(sys.modules, name, module)
    yield


@pytest.fixture()
def azure_backend(azure_sdk_enums):
    table = _EtagTable()
    return AzureTableSecurityStateBackend(table), table


# ---------------------------------------------------------------------------
# Surviving a restart and a replica change
# ---------------------------------------------------------------------------


def test_an_exhausted_daily_limit_survives_a_restart_and_a_new_replica():
    """The property APIM's quota-by-key provided and a process cannot.

    The contrast at the end is the point: the same traffic against a fresh
    process-local manager is admitted all over again, which is what a
    scale-to-zero deployment does several times a day.
    """

    backend = _DurableMemoryBackend()
    policy = QuotaPolicy(max_upload_bytes_per_day=200)
    first = _durable(policy, backend)
    for _ in range(2):
        first.admit_write(
            NAMESPACE, "scope", request_bytes=100, decompressed_bytes=100,
            object_count=1, stored_bytes=0, now=DAY_ONE,
        )
    with pytest.raises(QuotaExceeded, match="upload quota exceeded"):
        first.admit_write(
            NAMESPACE, "scope", request_bytes=1, decompressed_bytes=1,
            object_count=1, stored_bytes=0, now=DAY_ONE,
        )

    # A different process, a different replica, the same table.
    restarted = _durable(policy, backend)
    with pytest.raises(QuotaExceeded, match="upload quota exceeded"):
        restarted.admit_write(
            NAMESPACE, "scope", request_bytes=1, decompressed_bytes=1,
            object_count=1, stored_bytes=0, now=DAY_ONE,
        )
    assert restarted.scope_status(NAMESPACE, "scope", now=DAY_ONE) == {
        "uploaded_bytes": 200,
        "objects_today": 2,
        "read_bytes": 0,
        "read_requests": 0,
        "writes_enabled": True,
        "public_enabled": True,
        "durable": True,
    }

    # What the durable counter is compensating for.
    QuotaManager(policy).admit_write(
        NAMESPACE, "scope", request_bytes=100, decompressed_bytes=100,
        object_count=1, stored_bytes=0, now=DAY_ONE,
    )


def test_the_read_plane_stays_exhausted_across_a_restart_over_http():
    """End to end: exhaust, restart the app, still refused.

    The reader context is resolved by the restarted process too, so the 429
    is the quota talking and not a lost credential.
    """

    backend = _DurableMemoryBackend()
    config = CloudConfig(
        server_secret=SECRET, operator_token="operator-token", plane="read",
        require_apim_proof=False, clock=lambda: 1_000,
    )
    policy = QuotaPolicy(max_read_requests_per_day=2)

    def _state():
        return CloudState.create(
            config,
            security_backend=backend,
            quotas=_durable(policy, backend, utcnow=lambda: DAY_ONE),
        )

    state = _state()
    token, _context = state.credentials.issue_reader_context(
        new_installation_id(), "reader-scope", "entra-user"
    )
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Verified-Entra-Subject": "entra-user",
        "X-APIM-Request-Verified": "true",
        "X-APIM-Client-Certificate-Verified": "true",
    }
    with TestClient(create_cloud_app(config, state=state)) as client:
        assert client.get("/api/v1/context", headers=headers).status_code == 200
        assert client.get("/api/v1/context", headers=headers).status_code == 200
        assert client.get("/api/v1/context", headers=headers).status_code == 429

    restarted = _state()
    with TestClient(create_cloud_app(config, state=restarted)) as client:
        refused = client.get("/api/v1/context", headers=headers)
        assert refused.status_code == 429
        assert refused.json() == {"detail": "read quota exceeded"}
    # The credential itself is fine; only the day's allowance is gone.
    assert restarted.credentials.resolve_reader(token, now=1_001) is not None


def test_the_stored_byte_ceiling_needs_no_counter_to_survive_a_restart():
    """``max_stored_bytes_per_scope`` is a level, not a daily counter.

    It is asserted against the object store's own usage, which is already
    durable, so a restart cannot forget it.  It is checked before anything is
    charged so that a scope sitting over its cap does not also burn its daily
    upload allowance on requests that were never going to land.
    """

    backend = _DurableMemoryBackend()
    policy = QuotaPolicy(max_stored_bytes_per_scope=1_000, max_upload_bytes_per_day=500)
    manager = _durable(policy, backend)
    with pytest.raises(QuotaExceeded, match="stored-byte quota exceeded"):
        manager.admit_write(
            NAMESPACE, "scope", request_bytes=10, decompressed_bytes=10,
            object_count=1, stored_bytes=1_001, now=DAY_ONE,
        )
    assert _read_counter(
        backend, SCOPE_SUBJECT, NAMESPACE, "scope", METRIC_UPLOAD_BYTES
    ) is None


# ---------------------------------------------------------------------------
# Concurrency across replicas
# ---------------------------------------------------------------------------


def test_concurrent_increments_across_replicas_lose_nothing(azure_backend):
    """Six replicas, one table row, no lost increment.

    Every charge is one etag-guarded compare-and-swap, the same primitive
    ``claim_replay`` uses.  A lost race means another replica charged first,
    so the charge is re-applied to the value that won -- dropping it is the
    lost increment this exists to prevent.
    """

    backend, table = azure_backend
    policy = QuotaPolicy(max_read_requests_per_day=10_000)
    replicas = [_durable(policy, backend) for _ in range(6)]
    barrier = threading.Barrier(24)

    def _charge(index):
        manager = replicas[index % len(replicas)]
        barrier.wait()
        for _ in range(25):
            manager.admit_read(NAMESPACE, "scope", response_bytes=0, now=DAY_ONE)

    with ThreadPoolExecutor(max_workers=24) as pool:
        list(pool.map(_charge, range(24)))

    counted = backend.read(
        QUOTA_RECORD_KIND,
        counter_key(SCOPE_SUBJECT, NAMESPACE, "scope", METRIC_READ_REQUESTS),
    )
    assert counted["value"] == 24 * 25
    # Two rows: this scope and its installation.  Not one row per charge and
    # not one per replica.
    assert len(table.entities) == 2


def test_a_lost_compare_and_swap_is_retried_rather_than_dropped(azure_backend):
    """Deterministic proof that the etag guard is load-bearing.

    A second replica charges the same row in the window between this one's
    read and its write.  Without the guard the write would land
    unconditionally and that replica's charge would vanish; with it, the
    conflict is seen and the charge is re-applied on top.
    """

    backend, table = azure_backend
    policy = QuotaPolicy(max_read_requests_per_day=100)
    manager = _durable(policy, backend)
    other = _durable(policy, backend)
    manager.admit_read(NAMESPACE, "scope", response_bytes=0, now=DAY_ONE)

    sneaked = []

    def _sneak_in():
        if sneaked:
            return
        sneaked.append(True)
        other.admit_read(NAMESPACE, "scope", response_bytes=0, now=DAY_ONE)

    table._before_update = _sneak_in
    manager.admit_read(NAMESPACE, "scope", response_bytes=0, now=DAY_ONE)

    assert table.conflicts == 1
    counted = backend.read(
        QUOTA_RECORD_KIND,
        counter_key(SCOPE_SUBJECT, NAMESPACE, "scope", METRIC_READ_REQUESTS),
    )
    assert counted["value"] == 3


def test_the_ceiling_holds_exactly_under_concurrency(azure_backend):
    """The limit is enforced inside the swap, so no thread over-admits.

    Fifty admissions are available and one hundred and sixty are attempted:
    exactly fifty succeed.  A ceiling checked in one round trip and applied
    in another would let several replicas pass the same last slot.
    """

    backend, table = azure_backend
    policy = QuotaPolicy(max_read_requests_per_day=50)
    replicas = [_durable(policy, backend) for _ in range(4)]
    barrier = threading.Barrier(16)
    admitted = []
    lock = threading.Lock()

    def _charge(index):
        manager = replicas[index % len(replicas)]
        barrier.wait()
        for _ in range(10):
            try:
                manager.admit_read(NAMESPACE, "scope", response_bytes=0, now=DAY_ONE)
            except QuotaExceeded:
                continue
            with lock:
                admitted.append(1)

    with ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(_charge, range(16)))

    assert len(admitted) == 50
    counted = backend.read(
        QUOTA_RECORD_KIND,
        counter_key(SCOPE_SUBJECT, NAMESPACE, "scope", METRIC_READ_REQUESTS),
    )
    assert counted["value"] == 50


def test_several_cloud_states_share_one_durable_counter():
    """The deployment shape: several ``CloudState``s, one backend.

    Each state is a replica.  They must agree on the day's total, and the
    fourth one must see what the first three spent.
    """

    backend = _DurableMemoryBackend()
    config = CloudConfig(
        server_secret=SECRET, operator_token="operator-token",
        require_apim_proof=False, clock=lambda: 1_000,
    )
    policy = QuotaPolicy(max_read_bytes_per_day=100_000)
    states = [
        CloudState.create(
            config,
            security_backend=backend,
            quotas=_durable(policy, backend, utcnow=lambda: DAY_ONE),
        )
        for _ in range(4)
    ]
    assert all(state.quotas.durable for state in states)
    barrier = threading.Barrier(12)

    def _charge(index):
        quotas = states[index % len(states)].quotas
        barrier.wait()
        for _ in range(20):
            quotas.record_read_bytes(NAMESPACE, "scope", 7)

    with ThreadPoolExecutor(max_workers=12) as pool:
        list(pool.map(_charge, range(12)))

    status = states[3].quotas.scope_status(NAMESPACE, "scope")
    assert status["read_bytes"] == 12 * 20 * 7
    assert status["durable"] is True


# ---------------------------------------------------------------------------
# Expiry: the table does not grow with time
# ---------------------------------------------------------------------------


def test_a_new_utc_day_reclaims_the_row_instead_of_adding_one():
    """Yesterday is forgotten, and the table does not grow a row per day.

    No managed identity in this deployment holds a table delete action, so a
    counter that needed a cleanup job to stay bounded would repeat the
    unbounded ``CloudAuth`` growth already recorded in
    ``docs/cloud-sync-followups.md``.  The row is reclaimed in place instead.
    """

    backend = _DurableMemoryBackend()
    # The per-second window is a separate, process-local control; 400 days
    # of traffic in one test second would trip it and prove nothing here.
    policy = QuotaPolicy(max_upload_bytes_per_day=100, global_requests_per_second=10_000)
    manager = _durable(policy, backend)
    manager.admit_write(
        NAMESPACE, "scope", request_bytes=100, decompressed_bytes=100,
        object_count=1, stored_bytes=0, now=DAY_ONE,
    )
    with pytest.raises(QuotaExceeded):
        manager.admit_write(
            NAMESPACE, "scope", request_bytes=1, decompressed_bytes=1,
            object_count=1, stored_bytes=0, now=DAY_ONE,
        )

    rows_after_one_day = len(backend._records)
    for offset in range(1, 400):
        day = DAY_ONE + timedelta(days=offset)
        manager.admit_write(
            NAMESPACE, "scope", request_bytes=100, decompressed_bytes=100,
            object_count=1, stored_bytes=0, now=day,
        )
        assert manager.scope_status(NAMESPACE, "scope", now=day)["uploaded_bytes"] == 100

    assert len(backend._records) == rows_after_one_day
    # The surviving row holds the *last* day only: 399 days of totals were
    # discarded as their days ended, not accumulated and not left behind in
    # rows of their own.
    record = _read_counter(
        backend, SCOPE_SUBJECT, NAMESPACE, "scope", METRIC_UPLOAD_BYTES
    )
    assert record["day"] == (DAY_ONE + timedelta(days=399)).date().isoformat()
    assert record["value"] == 100


def test_the_azure_row_is_reclaimed_at_the_day_boundary(azure_backend):
    backend, table = azure_backend
    policy = QuotaPolicy(max_read_requests_per_day=1)
    manager = _durable(policy, backend)
    manager.admit_read(NAMESPACE, "scope", response_bytes=0, now=DAY_ONE)
    with pytest.raises(QuotaExceeded):
        manager.admit_read(NAMESPACE, "scope", response_bytes=0, now=DAY_ONE)

    before = len(table.entities)
    manager.admit_read(NAMESPACE, "scope", response_bytes=0, now=DAY_TWO)
    assert len(table.entities) == before
    payloads = {payload["day"]: payload for payload in table.payloads()}
    assert set(payloads) == {DAY_TWO.date().isoformat()}
    assert all(payload["value"] == 1 for payload in payloads.values())


def test_a_counter_expires_exactly_at_midnight_utc():
    """No grace period: grace would leave yesterday's row live into today and
    the new day's first charges would land in yesterday's total."""

    backend = _DurableMemoryBackend()
    policy = QuotaPolicy(max_read_requests_per_day=1)
    manager = _durable(policy, backend)
    last_second = datetime(2026, 3, 1, 23, 59, 59, tzinfo=timezone.utc)
    midnight = datetime(2026, 3, 2, 0, 0, 0, tzinfo=timezone.utc)
    manager.admit_read(NAMESPACE, "scope", response_bytes=0, now=last_second)
    with pytest.raises(QuotaExceeded):
        manager.admit_read(NAMESPACE, "scope", response_bytes=0, now=last_second)
    manager.admit_read(NAMESPACE, "scope", response_bytes=0, now=midnight)


def test_a_lagging_replica_cannot_zero_the_counter_already_in_force():
    """Clock skew must not be a way to reset a limit.

    A row's day only ever moves forward: a replica whose clock is behind
    charges into the live counter instead of replacing it with its own,
    older day.
    """

    backend = _DurableMemoryBackend()
    policy = QuotaPolicy(max_read_requests_per_day=2)
    ahead = _durable(policy, backend)
    behind = _durable(policy, backend)
    ahead.admit_read(NAMESPACE, "scope", response_bytes=0, now=DAY_TWO)
    behind.admit_read(NAMESPACE, "scope", response_bytes=0, now=DAY_ONE)
    with pytest.raises(QuotaExceeded):
        behind.admit_read(NAMESPACE, "scope", response_bytes=0, now=DAY_ONE)
    counted = _read_counter(
        backend, SCOPE_SUBJECT, NAMESPACE, "scope", METRIC_READ_REQUESTS
    )
    assert counted == {
        "day": DAY_TWO.date().isoformat(),
        "value": 2,
        "expires_at": pytest.approx(
            datetime(2026, 3, 3, tzinfo=timezone.utc).timestamp()
        ),
    }


# ---------------------------------------------------------------------------
# Failing closed
# ---------------------------------------------------------------------------


class _BrokenCounterBackend(_DurableMemoryBackend):
    """A durable backend whose counter path is down."""

    def charge_counter(self, *args, **kwargs):
        raise SecurityStateUnavailable("table unavailable")


def test_a_counter_backend_failure_refuses_instead_of_serving_uncounted():
    """Fail closed on the admission path.

    Refusing costs a rider a retry; admitting costs an uncounted resource,
    which is the failure this whole issue exists to remove.
    """

    manager = _durable(QuotaPolicy(), _BrokenCounterBackend())
    with pytest.raises(QuotaExceeded) as refused:
        manager.admit_read(NAMESPACE, "scope", response_bytes=10, now=DAY_ONE)
    assert refused.value.status_code == 503
    with pytest.raises(QuotaExceeded) as write_refused:
        manager.admit_write(
            NAMESPACE, "scope", request_bytes=10, decompressed_bytes=10,
            object_count=1, stored_bytes=0, now=DAY_ONE,
        )
    assert write_refused.value.status_code == 503


def test_a_sync_write_is_refused_rather_than_stored_uncounted():
    """Per call site: ``admit_write`` runs before ``store.apply``.

    A backend outage there refuses the batch and nothing is written, so the
    deployment can never hold objects it did not meter.
    """

    backend = _BrokenCounterBackend()
    config = CloudConfig(
        server_secret=SECRET, operator_token="operator-token", plane="sync",
        require_apim_proof=False, clock=lambda: 1_000,
    )
    state = CloudState.create(
        config,
        security_backend=backend,
        quotas=_durable(QuotaPolicy(), backend, utcnow=lambda: DAY_ONE),
    )
    writer = state.credentials.register_writer(
        new_installation_id(), "scope", b"w" * 32, b"s" * 32
    )
    body = json.dumps({
        "batch_id": "batch-1",
        "revision": 1,
        "objects": [{
            "id": "activity-1", "kind": "activity", "revision": 1,
            "data": {"duration_s": 10, "watts": 250},
        }],
    }, separators=(",", ":")).encode()
    canonical = canonical_request(
        "POST", "/api/v1/sync/batches", writer.namespace, 1_000, "nonce-1",
        digest_body(body), "batch-1", "1",
    )
    headers = {
        "Ocp-Apim-Subscription-Key": writer.subscription_key.decode(),
        "X-APIM-Client-Certificate-Verified": "true",
        "X-Writer-Credential": writer.credential_id,
        "X-Writer-Timestamp": "1000",
        "X-Writer-Nonce": "nonce-1",
        "X-Writer-Idempotency-Key": "batch-1",
        "X-Writer-Revision": "1",
        "X-Writer-Signature": sign_request(writer.signing_key, canonical),
    }
    with TestClient(create_cloud_app(config, state=state)) as client:
        refused = client.post("/api/v1/sync/batches", headers=headers, content=body)
    assert refused.status_code == 503
    assert state.store.revision(writer.namespace, "scope") == 0


def test_metering_after_the_fact_never_strands_a_rider():
    """The one place the policy is reversed, and why.

    ``POST /api/v1/devices/pair`` meters the response *after* spending the
    single-use code, following #152: refusing there would leave a rider
    holding a spent code and no credential.  A dead counter backend must
    therefore not turn that route into a failure -- and it cannot be abused,
    because reaching it costs a single-use code minted by an authenticated
    writer.
    """

    pytest.importorskip("cryptography")
    from wattracker.cloud.security import generate_signing_keypair

    backend = _DurableMemoryBackend()
    config = CloudConfig(
        server_secret=SECRET, operator_token="operator-token", plane="read",
        require_apim_proof=False, require_verified_subject=False,
        clock=lambda: 1_000,
    )
    state = CloudState.create(config, security_backend=backend)
    code = state.pairings.create(NAMESPACE, "scope").code
    _private, public_key = generate_signing_keypair()
    # The counter backend dies after the code is minted.
    state.quotas = _durable(QuotaPolicy(), _BrokenCounterBackend())

    with TestClient(create_cloud_app(config, state=state)) as client:
        paired = client.post(
            "/api/v1/devices/pair",
            json={"code": code, "public_key": public_key.hex()},
        )
    assert paired.status_code == 200, paired.text
    assert paired.json()["device_credential"]


def test_an_uncharged_counter_never_admits_the_request(azure_sdk_enums):
    """Contention that outlives the retries is a refusal, not an admission."""

    class _AlwaysConflicts(_EtagTable):
        def update_entity(self, entity, **kwargs):
            self.conflicts += 1
            raise _StorageError(412)

    table = _AlwaysConflicts()
    backend = AzureTableSecurityStateBackend(table)
    manager = _durable(QuotaPolicy(max_read_requests_per_day=10), backend)
    manager.admit_read(NAMESPACE, "scope", response_bytes=0, now=DAY_ONE)
    with pytest.raises(QuotaExceeded) as refused:
        manager.admit_read(NAMESPACE, "scope", response_bytes=0, now=DAY_ONE)
    assert refused.value.status_code == 503
    assert table.conflicts == 5


def test_a_single_charge_larger_than_the_day_is_refused_without_a_row(azure_backend):
    """A row is never created already past its ceiling; the next charge would
    otherwise inherit an over-limit total."""

    backend, table = azure_backend
    manager = _durable(QuotaPolicy(max_read_bytes_per_day=10), backend)
    with pytest.raises(QuotaExceeded, match="read-byte quota exceeded"):
        manager.record_read_bytes(NAMESPACE, "scope", 11, now=DAY_ONE)
    assert table.entities == {}


def test_an_unreadable_counter_row_is_reclaimed_rather_than_trusted():
    """A corrupt row must not be free quota, nor a permanent refusal."""

    backend = _DurableMemoryBackend()
    key = counter_key(SCOPE_SUBJECT, NAMESPACE, "scope", METRIC_READ_REQUESTS)
    manager = _durable(QuotaPolicy(max_read_requests_per_day=2), backend)
    for corrupt in ({"value": -5, "day": "2026-03-01", "expires_at": 4e9},
                    {"nonsense": True}):
        backend.write(QUOTA_RECORD_KIND, key, corrupt)
        manager.admit_read(NAMESPACE, "scope", response_bytes=0, now=DAY_ONE)
        assert backend.read(QUOTA_RECORD_KIND, key)["value"] == 1


# ---------------------------------------------------------------------------
# Production refuses counters that do not survive it
# ---------------------------------------------------------------------------


def test_production_refuses_a_process_local_quota_manager():
    """``durable`` is now a fact about the counters, and a boot condition.

    A replica-local counter is not a weaker control in this deployment, it is
    no control: the apps scale to zero, so it resets several times a day.
    """

    backend = _DurableMemoryBackend()
    config = CloudConfig(
        server_secret=SECRET, operator_token="operator-token",
        require_verified_subject=False, apim_proof_value="proof-value",
    )
    with pytest.raises(RuntimeError, match="durable quota counters"):
        CloudState.create(
            config,
            security_backend=backend,
            quotas=QuotaManager(),
            require_persistent_security=True,
        )
    built = CloudState.create(
        config, security_backend=backend, require_persistent_security=True
    )
    assert built.quotas.durable
    # And a development state keeps the process-local manager.
    assert not CloudState.create(config).quotas.durable


def test_a_durable_counter_refuses_a_backend_that_cannot_count():
    with pytest.raises(ValueError, match="durable quota backend"):
        DurableQuotaCounters(MemorySecurityStateBackend())

    class _OldContract(_DurableMemoryBackend):
        charge_counter = None

    with pytest.raises(ValueError, match="cannot charge counters"):
        DurableQuotaCounters(_OldContract())


# ---------------------------------------------------------------------------
# Isolation between riders
# ---------------------------------------------------------------------------


def test_counters_are_separated_by_namespace_scope_metric_and_subject():
    keys = {
        counter_key(SCOPE_SUBJECT, NAMESPACE, "scope", METRIC_READ_BYTES),
        counter_key(SCOPE_SUBJECT, OTHER_NAMESPACE, "scope", METRIC_READ_BYTES),
        counter_key(SCOPE_SUBJECT, NAMESPACE, "other-scope", METRIC_READ_BYTES),
        counter_key(SCOPE_SUBJECT, NAMESPACE, "scope", METRIC_READ_REQUESTS),
        counter_key(INSTALLATION_SUBJECT, NAMESPACE, "scope", METRIC_READ_BYTES),
    }
    assert len(keys) == 5

    backend = _DurableMemoryBackend()
    manager = _durable(QuotaPolicy(max_read_requests_per_day=1), backend)
    manager.admit_read(NAMESPACE, "scope", response_bytes=0, now=DAY_ONE)
    with pytest.raises(QuotaExceeded):
        manager.admit_read(NAMESPACE, "scope", response_bytes=0, now=DAY_ONE)
    # A second rider is untouched by the first one's exhausted day.
    manager.admit_read(OTHER_NAMESPACE, "scope", response_bytes=0, now=DAY_ONE)


def test_a_scope_name_cannot_be_spelled_into_another_riders_counter():
    """The key is a digest of NUL-joined parts, so the separator must not be
    spellable inside a part."""

    with pytest.raises(ValueError, match="NUL-free"):
        counter_key(SCOPE_SUBJECT, NAMESPACE, "scope\x00extra", METRIC_READ_BYTES)
    assert counter_key(
        SCOPE_SUBJECT, NAMESPACE, "ab", METRIC_READ_BYTES
    ) != counter_key(SCOPE_SUBJECT, NAMESPACE + "ab", "", METRIC_READ_BYTES)


def test_inventing_scopes_does_not_multiply_an_installations_allowance():
    """Every metric is charged to the scope and to the installation, so a
    caller cannot buy a second day's worth by naming a second scope."""

    backend = _DurableMemoryBackend()
    manager = _durable(QuotaPolicy(max_read_requests_per_day=2), backend)
    manager.admit_read(NAMESPACE, "scope-a", response_bytes=0, now=DAY_ONE)
    manager.admit_read(NAMESPACE, "scope-b", response_bytes=0, now=DAY_ONE)
    with pytest.raises(QuotaExceeded, match="installation read request quota"):
        manager.admit_read(NAMESPACE, "scope-c", response_bytes=0, now=DAY_ONE)


def test_a_malformed_charge_is_refused_by_the_backend():
    backend = _DurableMemoryBackend()
    key = counter_key(SCOPE_SUBJECT, NAMESPACE, "scope", METRIC_READ_BYTES)
    for kwargs in (
        {"day": "not-a-day", "amount": 1, "ceiling": 10},
        {"day": "2026-03-01", "amount": 0, "ceiling": 10},
        {"day": "2026-03-01", "amount": True, "ceiling": 10},
        {"day": "2026-03-01", "amount": 1, "ceiling": 0},
    ):
        with pytest.raises(ValueError):
            backend.charge_counter(
                QUOTA_RECORD_KIND, key, expires_at=4e9, now=1.0, **kwargs
            )
