"""The durable budget kill switch (#181).

The kill switch is the last line of cost protection: ``infra/azure/main.bicep``
wires budget actions that disable writes at 80% and the public API at 100%.
Before this it was enforced in two places, an APIM named value and a boolean on
the running ``QuotaManager``.  #164 removes APIM, which takes the durable half
with it -- and the half that remains is worth nothing in a deployment whose
container apps run at ``minReplicas: 0`` and cycle constantly: a budget action
would stop only the replicas that happened to be up, and the next cold start
would come back ENABLED.  The switch would disable spending and re-enable
itself minutes later.

Every test here is therefore about one of four properties:

* it survives a cold start, on every replica, not just the ones that were up;
* it is read at request time, with a staleness window that is a ceiling;
* it fails **closed** -- an unreadable kill state refuses the request;
* clearing it restores service, as an update and never a delete.
"""

import json
import pathlib
import threading

import pytest
from fastapi.testclient import TestClient

from wattracker.cloud.api import CloudConfig, CloudState, create_cloud_app
from wattracker.cloud.limits import (
    DurableKillSwitch,
    DurableQuotaCounters,
    KILL_SWITCH_ENABLED,
    KILL_SWITCH_KEY,
    KILL_SWITCH_MAX_TTL_SECONDS,
    KILL_SWITCH_RECORD_KIND,
    KILL_SWITCH_TTL_SECONDS,
    KillSwitchState,
    KillSwitchUnavailable,
    ProcessKillSwitch,
    QUOTA_RECORD_KIND,
    QuotaExceeded,
    QuotaManager,
    QuotaPolicy,
    SCOPE_SUBJECT,
    clear_kill_switch,
    counter_key,
    disable_public_api,
    disable_writes,
    read_kill_switch,
    set_kill_switch,
)
from wattracker.cloud.security import (
    AzureTableSecurityStateBackend,
    MemorySecurityStateBackend,
    canonical_request,
    digest_body,
    generate_signing_keypair,
    new_installation_id,
    sign_request,
    sign_request_ed25519,
)
from wattracker.cloud.storage import MemoryTenantStore


SECRET = b"cloud-test-server-secret-32-bytes-long"
NAMESPACE = "a" * 64
MINT_PATH = "/api/v1/devices/pairing-codes"
PAIR_PATH = "/api/v1/devices/pair"


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------


class _DurableMemoryBackend(MemorySecurityStateBackend):
    """One shared table, standing in for the one several replicas talk to."""

    durable = True


class _DelayedKillUpdateBackend(_DurableMemoryBackend):
    def __init__(self) -> None:
        super().__init__()
        self.update_started = threading.Event()
        self.allow_update = threading.Event()

    def update(self, kind, key, transform):
        self.update_started.set()
        if not self.allow_update.wait(timeout=2):
            raise RuntimeError("test update timed out")
        return super().update(kind, key, transform)


class _KillStateDown(_DurableMemoryBackend):
    """Every record kind works except the kill switch, whose read fails.

    Isolating the outage to one record kind is what makes the per-route
    assertions below mean anything: credentials still resolve, so a refusal is
    the kill state talking and not a dead auth table refusing everything.
    """

    def __init__(self, *, failing: bool = True) -> None:
        super().__init__()
        self.failing = failing
        self.kill_reads = 0

    def read(self, kind, key):
        if kind == KILL_SWITCH_RECORD_KIND:
            self.kill_reads += 1
            if self.failing:
                raise RuntimeError("table unavailable")
        return super().read(kind, key)


class _Clock:
    """A monotonic source the test advances by hand."""

    def __init__(self, value: float = 0.0) -> None:
        self.value = float(value)

    def __call__(self) -> float:
        return self.value


class _StorageError(Exception):
    def __init__(self, status_code):
        super().__init__(str(status_code))
        self.status_code = status_code


class _NoDeleteTable:
    """An Azure table client with no delete, because the deployment has none.

    ``main.bicep`` grants the managed identities read, add and update actions
    and no ``entities/delete``.  A design that cleared the switch by removing
    its row would work in every memory-backed test and fail in production, so
    the delete here is a tripwire rather than a stub.
    """

    def __init__(self) -> None:
        self.entities: dict = {}

    def create_entity(self, entity):
        key = (entity["PartitionKey"], entity["RowKey"])
        if key in self.entities:
            raise _StorageError(409)
        self.entities[key] = dict(entity, etag='W/"1"')
        return dict(self.entities[key])

    def get_entity(self, *, partition_key, row_key):
        try:
            return dict(self.entities[(partition_key, row_key)])
        except KeyError as exc:
            raise _StorageError(404) from exc

    def upsert_entity(self, entity):
        key = (entity["PartitionKey"], entity["RowKey"])
        self.entities[key] = dict(entity, etag='W/"2"')

    def delete_entity(self, *args, **kwargs):  # pragma: no cover - a tripwire
        raise AssertionError("no deployed managed identity holds a table delete")

    def payloads(self):
        return [json.loads(entity["Payload"]) for entity in self.entities.values()]


# ---------------------------------------------------------------------------
# Request builders (same envelopes the other cloud suites sign)
# ---------------------------------------------------------------------------


def _config(plane="all"):
    return CloudConfig(
        server_secret=SECRET,
        operator_token="operator-token",
        plane=plane,
        require_gateway_proof=False,
        clock=lambda: 1_000,
    )


def _writer(state, seed=b"w", scope="scope"):
    return state.credentials.register_writer(
        new_installation_id(), scope, seed * 32, seed[::-1] * 32
    )


def _batch(revision=1):
    return json.dumps({
        "batch_id": f"batch-{revision}",
        "revision": revision,
        "objects": [{
            "id": "activity-1", "kind": "activity", "revision": revision,
            "data": {"duration_s": 10, "watts": 250},
        }],
    }, separators=(",", ":")).encode()


def _signed(writer, method, path, body=b"", *, nonce, idem, revision):
    canonical = canonical_request(
        method, path, writer.namespace, 1_000, nonce,
        digest_body(body), idem, str(revision),
    )
    return {
        "Ocp-Apim-Subscription-Key": writer.subscription_key.decode(),
        "X-APIM-Client-Certificate-Verified": "true",
        "X-Verified-Entra-Subject": "entra-user",
        "X-Writer-Credential": writer.credential_id,
        "X-Writer-Timestamp": "1000",
        "X-Writer-Nonce": nonce,
        "X-Writer-Idempotency-Key": idem,
        "X-Writer-Revision": str(revision),
        "X-Writer-Signature": sign_request(writer.signing_key, canonical),
    }


def _batch_headers(writer, body, *, nonce, revision=1):
    return _signed(
        writer, "POST", "/api/v1/sync/batches", body,
        nonce=nonce, idem=f"batch-{revision}", revision=revision,
    )


def _status_headers(writer, *, nonce):
    return _signed(
        writer, "GET", "/api/v1/sync/status",
        nonce=nonce, idem="status-1", revision=0,
    )


def _mint_headers(writer, *, nonce):
    return _signed(
        writer, "POST", MINT_PATH,
        nonce=nonce, idem="device-pairing-code", revision=0,
    )


def _reader_headers(token):
    return {
        "Authorization": f"Bearer {token}",
        "X-Verified-Entra-Subject": "entra-user",
        "X-APIM-Client-Certificate-Verified": "true",
    }


def _refresh_headers(device, private_key, *, nonce):
    canonical = canonical_request(
        "POST", "/api/v1/context/refresh", device.namespace, 1_000, nonce,
        digest_body(b""), "context-refresh", "",
    )
    return {
        "X-Device-Credential": device.credential_id,
        "X-Device-Timestamp": "1000",
        "X-Device-Nonce": nonce,
        "X-Device-Signature": sign_request_ed25519(private_key, canonical),
        "X-Verified-Entra-Subject": "entra-user",
        "X-APIM-Client-Certificate-Verified": "true",
    }


# ---------------------------------------------------------------------------
# A killed deployment comes back killed
# ---------------------------------------------------------------------------


def test_every_replica_that_starts_after_the_switch_comes_up_disabled():
    """The property the process-local flag never had.

    Not one restart: four ``CloudState``s over one shared backend, none of
    which existed when the switch was thrown, exactly as #179 tested the
    counters.  The contrast at the end is the point -- the same traffic
    against a process-local manager is admitted all over again, which is what
    a scale-to-zero deployment did several times a day.
    """

    backend = _DurableMemoryBackend()
    set_kill_switch(
        backend, writes_enabled=False, public_enabled=True, reason="budget-80"
    )

    replicas = [
        CloudState.create(_config(), security_backend=backend) for _ in range(4)
    ]
    for replica in replicas:
        assert replica.quotas.kill_switch_durable
        with pytest.raises(QuotaExceeded, match="writes disabled") as refused:
            replica.quotas.admit_write(
                NAMESPACE, "scope", request_bytes=10, decompressed_bytes=10,
                object_count=1, stored_bytes=0,
            )
        assert refused.value.status_code == 403
        # 80% stops writes and nothing else.
        replica.quotas.admit_read(NAMESPACE, "scope", response_bytes=0)

    # What the durable switch is compensating for.
    QuotaManager().admit_write(
        NAMESPACE, "scope", request_bytes=10, decompressed_bytes=10,
        object_count=1, stored_bytes=0,
    )


def test_the_hundred_percent_action_stops_reads_on_every_new_replica():
    backend = _DurableMemoryBackend()
    disable_public_api(backend)

    for _ in range(3):
        replica = CloudState.create(_config(), security_backend=backend)
        with pytest.raises(QuotaExceeded, match="public API disabled") as read:
            replica.quotas.admit_read(NAMESPACE, "scope", response_bytes=0)
        assert read.value.status_code == 403
        # And the write refusal names the wider level, not the narrower one.
        with pytest.raises(QuotaExceeded, match="public API disabled"):
            replica.quotas.admit_write(
                NAMESPACE, "scope", request_bytes=10, decompressed_bytes=10,
                object_count=1, stored_bytes=0,
            )


def test_the_kill_switch_survives_restarting_every_replica_over_http():
    """End to end: kill it, restart the whole deployment, still refused.

    The store is shared so that "restart" means the replicas, not the data,
    and the writer credential is resolved by each fresh process too -- so a
    403 is the switch talking and not a lost credential.
    """

    backend = _DurableMemoryBackend()
    store = MemoryTenantStore()
    config = _config()

    def replica():
        return CloudState.create(config, store=store, security_backend=backend)

    first = replica()
    writer = _writer(first)
    body = _batch(1)
    with TestClient(create_cloud_app(config, state=first)) as client:
        accepted = client.post(
            "/api/v1/sync/batches",
            headers=_batch_headers(writer, body, nonce="pre-kill"),
            content=body,
        )
    assert accepted.status_code == 200, accepted.text

    disable_writes(backend, reason="budget-80")

    # Every replica is new; none of them was running when the switch was
    # thrown, and none of them was told about it at startup.
    for index in range(3):
        body = _batch(2 + index)
        with TestClient(create_cloud_app(config, state=replica())) as client:
            refused = client.post(
                "/api/v1/sync/batches",
                headers=_batch_headers(
                    writer, body, nonce=f"post-kill-{index}", revision=2 + index
                ),
                content=body,
            )
        assert refused.status_code == 403, refused.text
        assert refused.json() == {"detail": "write quota exceeded"}
        # Reads are untouched at the 80% level.
        with TestClient(create_cloud_app(config, state=replica())) as client:
            assert client.get(
                "/api/v1/sync/status",
                headers=_status_headers(writer, nonce=f"status-{index}"),
            ).status_code == 200

    # Clearing restores service, on a replica that never saw it disabled.
    clear_kill_switch(backend, reason="incident-closed")
    body = _batch(9)
    with TestClient(create_cloud_app(config, state=replica())) as client:
        restored = client.post(
            "/api/v1/sync/batches",
            headers=_batch_headers(writer, body, nonce="cleared", revision=9),
            content=body,
        )
    assert restored.status_code == 200, restored.text


def test_one_switch_stops_both_planes_although_their_counters_do_not_share():
    """The kill switch does not follow the quota backend, on purpose.

    The counters split by plane because each identity may write only its own
    table (the read plane counts in ``CloudAuth``, the sync plane in
    ``CloudReplay``).  A switch that split the same way would be two switches,
    and throwing one would leave the other plane serving.
    """

    auth = _DurableMemoryBackend()
    replay = _DurableMemoryBackend()
    read_plane = CloudState.create(_config("read"), security_backend=auth)
    sync_plane = CloudState.create(
        _config("sync"), security_backend=auth, replay_backend=replay
    )

    disable_public_api(auth)

    for plane in (read_plane, sync_plane):
        with pytest.raises(QuotaExceeded, match="public API disabled"):
            plane.quotas.admit_read(NAMESPACE, "scope", response_bytes=0)

    assert auth.read(KILL_SWITCH_RECORD_KIND, KILL_SWITCH_KEY) is not None
    # Never in the plane-local table, which only one plane can read.
    assert replay.read(KILL_SWITCH_RECORD_KIND, KILL_SWITCH_KEY) is None


# ---------------------------------------------------------------------------
# Fail closed, per route
# ---------------------------------------------------------------------------


def test_an_unreadable_kill_state_refuses_the_writer_and_reader_routes():
    """Per call site: every route that admits traffic must refuse.

    An assertion no test pins is not a control, so each affected path is named
    here rather than trusting that they all go through one helper.
    """

    backend = _KillStateDown()
    config = _config()
    state = CloudState.create(config, security_backend=backend)
    writer = _writer(state)
    token, _context = state.credentials.issue_reader_context(
        new_installation_id(), "reader-scope", "entra-user"
    )
    body = _batch(1)
    unavailable = {"detail": "kill state unavailable"}

    with TestClient(create_cloud_app(config, state=state)) as client:
        cases = {
            "sync batch": client.post(
                "/api/v1/sync/batches",
                headers=_batch_headers(writer, body, nonce="down-1"),
                content=body,
            ),
            "sync status": client.get(
                "/api/v1/sync/status", headers=_status_headers(writer, nonce="down-2")
            ),
            "mint pairing code": client.post(
                MINT_PATH, headers=_mint_headers(writer, nonce="down-3")
            ),
            "redeem pairing code": client.post(
                PAIR_PATH,
                headers={"X-Verified-Entra-Subject": "entra-user"},
                json={"code": "AAAA-BBBB-CCCC", "public_key": "00" * 32},
            ),
            "context": client.get("/api/v1/context", headers=_reader_headers(token)),
            "calendar": client.get(
                "/api/v1/context/calendar", headers=_reader_headers(token)
            ),
            "activities": client.get(
                "/api/v1/context/activities", headers=_reader_headers(token)
            ),
            "profile": client.get(
                "/api/v1/context/profile", headers=_reader_headers(token)
            ),
            "races": client.get(
                "/api/v1/context/races", headers=_reader_headers(token)
            ),
            "activity detail": client.get(
                "/api/v1/context/activities/activity-1",
                headers=_reader_headers(token),
            ),
        }

    for name, response in cases.items():
        assert response.status_code == 503, f"{name}: {response.status_code}"
        assert response.json() == unavailable, name
        assert response.headers["Retry-After"] == "30", name

    # Nothing was written by the batch that was refused.
    assert state.store.revision(writer.namespace, "scope") == 0
    # The credential store is healthy; only the kill state is not.
    assert state.credentials.resolve_reader(token, now=1_001) is not None


def test_an_unreadable_kill_state_refuses_the_device_routes():
    pytest.importorskip("cryptography")
    backend = _KillStateDown(failing=False)
    config = _config("read")
    state = CloudState.create(config, security_backend=backend)
    private_key, public_key = generate_signing_keypair()
    device = state.credentials.register_device(
        new_installation_id(), "scope", public_key,
        signature_algorithm="ed25519", capabilities=("read",), subject="entra-user",
    )
    code = state.pairings.create(device.namespace, "scope", subject="entra-user").code

    backend.failing = True
    with TestClient(create_cloud_app(config, state=state)) as client:
        refreshed = client.post(
            "/api/v1/context/refresh",
            headers=_refresh_headers(device, private_key, nonce="down-refresh"),
        )
        paired = client.post(
            PAIR_PATH,
            headers={"X-Verified-Entra-Subject": "entra-user"},
            json={"code": code, "public_key": public_key.hex()},
        )
    assert refreshed.status_code == 503, refreshed.text
    assert paired.status_code == 503, paired.text

    # The single-use code was refused before it was spent, so service resumes
    # with it intact once the state is readable again.
    backend.failing = False
    with TestClient(create_cloud_app(config, state=state)) as client:
        assert client.post(
            PAIR_PATH,
            headers={"X-Verified-Entra-Subject": "entra-user"},
            json={"code": code, "public_key": public_key.hex()},
        ).status_code == 200


def test_the_device_refresh_is_refused_before_it_spends_its_replay_nonce():
    """Why the check sits in ``_resolve_device`` and not only in ``admit_read``.

    Both would answer 503, so the status alone does not pin it.  What does is
    the order: resolving the device leads to ``_verify_device_request``, which
    consumes the nonce.  A refusal that arrives after that has already spent
    a single-use value on a request the deployment never served, and the
    device's retry -- with the same signed envelope -- would then be rejected
    as a replay long after the backend recovered.

    The disabled case pins the same call site from the other side: this route
    answers a uniform 404 for every credential outcome, and a kill switch must
    not become the one condition that answers differently.
    """

    pytest.importorskip("cryptography")
    backend = _KillStateDown(failing=False)
    config = _config("read")
    state = CloudState.create(config, security_backend=backend)
    private_key, public_key = generate_signing_keypair()
    device = state.credentials.register_device(
        new_installation_id(), "scope", public_key,
        signature_algorithm="ed25519", capabilities=("read",), subject="entra-user",
    )
    headers = _refresh_headers(device, private_key, nonce="only-nonce")

    backend.failing = True
    with TestClient(create_cloud_app(config, state=state)) as client:
        refused = client.post("/api/v1/context/refresh", headers=headers)
    assert refused.status_code == 503

    backend.failing = False
    with TestClient(create_cloud_app(config, state=state)) as client:
        retried = client.post("/api/v1/context/refresh", headers=headers)
    assert retried.status_code == 200, retried.text

    # And a disabled public API keeps this route's uniform 404.
    disable_public_api(backend)
    with TestClient(
        create_cloud_app(config, state=CloudState.create(config, security_backend=backend))
    ) as client:
        disabled = client.post(
            "/api/v1/context/refresh",
            headers=_refresh_headers(device, private_key, nonce="disabled-nonce"),
        )
    assert disabled.status_code == 404


def test_an_unreadable_kill_state_refuses_before_a_credential_is_even_looked_up():
    """The check cannot be short-circuited by sending nothing.

    Folding it into the reader's ``context is None`` test would let an unknown
    token skip it and answer 404, which is the one flag that must never be
    skipped.  A 503 here leaks nothing that a 404 hides: the condition is
    global and identical for every caller, authenticated or not.
    """

    backend = _KillStateDown()
    config = _config("read")
    state = CloudState.create(config, security_backend=backend)
    with TestClient(create_cloud_app(config, state=state)) as client:
        anonymous = client.get("/api/v1/context")
        unknown = client.get("/api/v1/context", headers=_reader_headers("no-such"))
    assert anonymous.status_code == 503
    assert unknown.status_code == 503


def test_the_quota_paths_refuse_directly_too():
    backend = _KillStateDown()
    manager = CloudState.create(_config(), security_backend=backend).quotas
    for call in (
        lambda: manager.admit_read(NAMESPACE, "scope", response_bytes=0),
        lambda: manager.record_read_bytes(NAMESPACE, "scope", 10),
        lambda: manager.scope_status(NAMESPACE, "scope"),
        lambda: manager.admit_write(
            NAMESPACE, "scope", request_bytes=10, decompressed_bytes=10,
            object_count=1, stored_bytes=0,
        ),
    ):
        with pytest.raises(QuotaExceeded, match="kill state unavailable") as refused:
            call()
        assert refused.value.status_code == 503
        assert refused.value.retry_after == 30


def test_a_row_that_cannot_be_understood_is_not_read_as_enabled():
    """The one place this module's direction differs from #179's counters.

    Both readings are on the same backend and both are deliberate: a counter
    row that cannot be parsed is reset and healed, because refusing forever
    would strand a rider and the ceiling immediately re-applies.  The kill
    switch has no such bound -- it is thrown when spending has already gone
    wrong -- so an unintelligible row is reported unavailable, not "enabled".
    """

    backend = _DurableMemoryBackend()
    for garbage in (
        {"writes_enabled": "yes", "public_enabled": "no"},
        {"writes_enabled": 0, "public_enabled": 1},
        {"writes_enabled": False},
        {},
    ):
        backend.write(KILL_SWITCH_RECORD_KIND, KILL_SWITCH_KEY, garbage)
        with pytest.raises(KillSwitchUnavailable, match="malformed"):
            read_kill_switch(backend)

    class _NotAMapping(_DurableMemoryBackend):
        def read(self, kind, key):
            return ["not", "a", "mapping"]

    with pytest.raises(KillSwitchUnavailable, match="malformed"):
        read_kill_switch(_NotAMapping())

    # The contrast, executed rather than described: a counter row this broken
    # is reclaimed and charged.
    key = counter_key(SCOPE_SUBJECT, NAMESPACE, "scope", "read_requests")
    backend.write(QUOTA_RECORD_KIND, key, {"nonsense": True})
    assert backend.charge_counter(
        QUOTA_RECORD_KIND, key, day="2026-03-01", amount=1, ceiling=10,
        expires_at=2_000_000_000.0, now=1_000.0,
    ) == 1


def test_an_absent_row_is_the_only_reading_of_enabled():
    """A row is created the first time the switch is set and never deleted.

    So "no row" is not a guess about a state nobody could read; it is the
    steady state of a deployment nobody has ever killed.
    """

    backend = _DurableMemoryBackend()
    assert read_kill_switch(backend) == KILL_SWITCH_ENABLED
    disable_public_api(backend)
    clear_kill_switch(backend)
    # Cleared, not removed.
    assert backend.read(KILL_SWITCH_RECORD_KIND, KILL_SWITCH_KEY) is not None


# ---------------------------------------------------------------------------
# The staleness window
# ---------------------------------------------------------------------------


def test_the_cache_cannot_serve_a_stale_enabled_value_past_its_window():
    """The window is a ceiling, checked at the boundary, not a hint.

    The switch is thrown through a *different* ``DurableKillSwitch``, which is
    what another replica doing it looks like: nothing tells this one.
    """

    backend = _DurableMemoryBackend()
    clock = _Clock()
    switch = DurableKillSwitch(backend, clock=clock)
    assert switch.state() == KILL_SWITCH_ENABLED

    set_kill_switch(backend, writes_enabled=False, public_enabled=False)

    clock.value = KILL_SWITCH_TTL_SECONDS - 0.001
    assert switch.state().public_enabled is True
    clock.value = KILL_SWITCH_TTL_SECONDS
    assert switch.state().public_enabled is False
    assert switch.state().writes_enabled is False


def test_the_window_bounds_table_reads_rather_than_requests():
    backend = _KillStateDown(failing=False)
    clock = _Clock()
    switch = DurableKillSwitch(backend, clock=clock)
    for _ in range(50):
        switch.state()
    assert backend.kill_reads == 1
    clock.value = KILL_SWITCH_TTL_SECONDS
    switch.state()
    assert backend.kill_reads == 2


def test_the_staleness_window_is_thirty_seconds_and_bounded():
    """A misconfigured hour-long window is the failure this replaces.

    It is refused at construction rather than left to review, and the default
    is pinned so that widening it is a deliberate edit to a test.
    """

    assert KILL_SWITCH_TTL_SECONDS == 30.0
    assert KILL_SWITCH_MAX_TTL_SECONDS == 60.0
    backend = _DurableMemoryBackend()
    assert DurableKillSwitch(backend).ttl_seconds == KILL_SWITCH_TTL_SECONDS
    assert DurableKillSwitch(
        backend, ttl_seconds=KILL_SWITCH_MAX_TTL_SECONDS
    ).ttl_seconds == KILL_SWITCH_MAX_TTL_SECONDS
    for bad in (3_600, KILL_SWITCH_MAX_TTL_SECONDS + 0.001, 0, -1, True, "30", None):
        with pytest.raises(ValueError, match="staleness window"):
            DurableKillSwitch(backend, ttl_seconds=bad)


def test_a_failed_refresh_drops_the_cache_instead_of_falling_back_to_it():
    backend = _KillStateDown(failing=False)
    clock = _Clock()
    switch = DurableKillSwitch(backend, clock=clock)
    assert switch.state() == KILL_SWITCH_ENABLED

    backend.failing = True
    clock.value = KILL_SWITCH_TTL_SECONDS
    for _ in range(3):
        # Not once: a dropped cache cannot be resurrected by asking again.
        with pytest.raises(KillSwitchUnavailable, match="unreadable"):
            switch.state()

    backend.failing = False
    assert switch.state() == KILL_SWITCH_ENABLED


def test_setting_the_switch_drops_this_replicas_cache_at_once():
    backend = _DurableMemoryBackend()
    clock = _Clock()
    switch = DurableKillSwitch(backend, clock=clock)
    assert switch.state().public_enabled is True
    switch.set(writes_enabled=False, public_enabled=False)
    # Same instant on the clock: the replica that threw it does not wait out
    # its own window.
    assert switch.state().public_enabled is False


def test_a_cold_replica_reads_the_state_on_its_first_request():
    """There is no cache to inherit, which is the whole point."""

    backend = _KillStateDown(failing=False)
    disable_public_api(backend)
    before = backend.kill_reads
    replica = CloudState.create(_config(), security_backend=backend)
    assert backend.kill_reads == before  # nothing is read at startup
    with pytest.raises(QuotaExceeded, match="public API disabled"):
        replica.quotas.admit_read(NAMESPACE, "scope", response_bytes=0)
    assert backend.kill_reads == before + 1


def test_concurrent_readers_share_one_window():
    backend = _KillStateDown(failing=False)
    switch = DurableKillSwitch(backend, clock=_Clock())
    results = []
    barrier = threading.Barrier(8)

    def _read():
        barrier.wait()
        results.append(switch.state())

    threads = [threading.Thread(target=_read) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results == [KILL_SWITCH_ENABLED] * 8
    # A race may cost an extra read; it may never cost a wrong answer.
    assert 1 <= backend.kill_reads <= 8


# ---------------------------------------------------------------------------
# Two levels, and the operator path
# ---------------------------------------------------------------------------


def test_the_two_levels_are_independent():
    backend = _DurableMemoryBackend()
    manager = CloudState.create(_config(), security_backend=backend).quotas

    set_kill_switch(backend, writes_enabled=False, public_enabled=True)
    fresh = CloudState.create(_config(), security_backend=backend).quotas
    fresh.admit_read(NAMESPACE, "scope", response_bytes=0)
    with pytest.raises(QuotaExceeded, match="writes disabled"):
        fresh.admit_write(
            NAMESPACE, "scope", request_bytes=10, decompressed_bytes=10,
            object_count=1, stored_bytes=0,
        )

    set_kill_switch(backend, writes_enabled=True, public_enabled=False)
    fresh = CloudState.create(_config(), security_backend=backend).quotas
    with pytest.raises(QuotaExceeded, match="public API disabled"):
        fresh.admit_read(NAMESPACE, "scope", response_bytes=0)
    assert manager is not fresh


def test_a_late_eighty_percent_action_cannot_re_enable_the_public_api():
    """Budget actions fire independently and may arrive in either order."""

    backend = _DurableMemoryBackend()
    disable_public_api(backend, reason="budget-100")
    disable_writes(backend, reason="budget-80")
    assert read_kill_switch(backend) == KillSwitchState(
        writes_enabled=False,
        public_enabled=False,
        reason="budget-80",
        updated_at=read_kill_switch(backend).updated_at,
    )


def test_a_concurrent_eighty_percent_action_cannot_re_enable_the_public_api():
    backend = _DelayedKillUpdateBackend()
    errors = []

    def apply_eighty_percent_action():
        try:
            disable_writes(backend, reason="budget-80")
        except Exception as exc:  # pragma: no cover - assertion below reports it
            errors.append(exc)

    thread = threading.Thread(target=apply_eighty_percent_action)
    thread.start()
    assert backend.update_started.wait(timeout=2)
    disable_public_api(backend, reason="budget-100")
    backend.allow_update.set()
    thread.join(timeout=2)

    assert not thread.is_alive()
    assert not errors
    state = read_kill_switch(backend)
    assert state.writes_enabled is False
    assert state.public_enabled is False


def test_the_severest_action_needs_no_readable_state():
    """The more severe the action, the fewer preconditions it has.

    ``disable_writes`` must read first so it cannot widen the public level, so
    it raises when the read fails.  ``disable_public_api`` only ever removes
    capability, so it must work when nothing can be read -- which is exactly
    when an operator reaches for it.
    """

    backend = _KillStateDown()
    with pytest.raises(KillSwitchUnavailable, match="unreadable"):
        disable_writes(backend)
    disable_public_api(backend)
    backend.failing = False
    assert read_kill_switch(backend) == KillSwitchState(
        writes_enabled=False,
        public_enabled=False,
        reason="budget-100",
        updated_at=read_kill_switch(backend).updated_at,
    )


def test_re_enabling_the_public_api_restores_the_write_level_in_force():
    backend = _DurableMemoryBackend()
    manager = CloudState.create(_config(), security_backend=backend).quotas
    manager.set_writes_enabled(False, reason="budget-80")
    manager.set_public_enabled(False, reason="budget-100")
    manager.set_public_enabled(True, reason="budget-under")
    assert read_kill_switch(backend).public_enabled is True
    # Not silently re-enabled with it.
    assert read_kill_switch(backend).writes_enabled is False


def test_a_partial_setter_raises_rather_than_guessing_the_other_level():
    backend = _KillStateDown()
    manager = CloudState.create(_config(), security_backend=backend).quotas
    for call in (
        lambda: manager.set_writes_enabled(False),
        lambda: manager.set_public_enabled(False),
    ):
        with pytest.raises(QuotaExceeded, match="kill state unavailable"):
            call()
    # The whole desired state needs no read and lands anyway.
    set_kill_switch(backend, writes_enabled=False, public_enabled=False)
    backend.failing = False
    assert read_kill_switch(backend).public_enabled is False


def test_the_operator_note_is_bounded_before_it_is_persisted():
    backend = _DurableMemoryBackend()
    for bad in ("x" * 201, "budget\x00100", "line\nbreak", 7, object()):
        with pytest.raises(ValueError, match="kill switch reason"):
            set_kill_switch(
                backend, writes_enabled=False, public_enabled=False, reason=bad
            )
    assert read_kill_switch(backend) == KILL_SWITCH_ENABLED
    set_kill_switch(
        backend, writes_enabled=False, public_enabled=False, reason="budget-100"
    )
    assert read_kill_switch(backend).reason == "budget-100"


# ---------------------------------------------------------------------------
# Storage shape: clearing is an update, never a delete
# ---------------------------------------------------------------------------


def test_clearing_the_switch_is_an_update_and_never_a_delete():
    """No deployed managed identity holds a table ``entities/delete`` action.

    ``main.bicep`` grants read, add and update only -- the same constraint that
    made #179's counters reclaim their row in place.  The table stand-in fails
    the test if anything reaches for a delete.
    """

    table = _NoDeleteTable()
    backend = AzureTableSecurityStateBackend(table)
    disable_public_api(backend)
    clear_kill_switch(backend)
    assert len(table.entities) == 1
    payload = table.payloads()[0]
    assert payload["writes_enabled"] is True
    assert payload["public_enabled"] is True
    assert read_kill_switch(backend) == KillSwitchState(
        writes_enabled=True,
        public_enabled=True,
        reason="",
        updated_at=payload["updated_at"],
    )


def test_the_kill_switch_row_address_is_one_the_azure_table_accepts():
    """The memory backend validates no keys, so only this catches a bad one.

    A record kind or key that ``AzureTableSecurityStateBackend._row_key``
    rejects would pass every memory-backed test above and fail in production
    on the first request.
    """

    row_key = AzureTableSecurityStateBackend._row_key(
        KILL_SWITCH_RECORD_KIND, KILL_SWITCH_KEY
    )
    assert row_key == f"{KILL_SWITCH_RECORD_KIND}:{KILL_SWITCH_KEY}"
    # Its own kind: a kill-switch row can never be addressed as a counter, and
    # no counter can be addressed as the switch.
    assert KILL_SWITCH_RECORD_KIND != QUOTA_RECORD_KIND


# ---------------------------------------------------------------------------
# Production refuses a switch that does not survive it
# ---------------------------------------------------------------------------


def test_production_refuses_a_process_local_kill_switch():
    backend = _DurableMemoryBackend()
    config = CloudConfig(
        server_secret=SECRET, operator_token="operator-token",
        require_verified_subject=False, gateway_proof_value="proof-value",
    )
    with pytest.raises(RuntimeError, match="durable kill switch"):
        CloudState.create(
            config,
            security_backend=backend,
            quotas=QuotaManager(
                QuotaPolicy(), counters=DurableQuotaCounters(backend)
            ),
            require_persistent_security=True,
        )
    built = CloudState.create(
        config, security_backend=backend, require_persistent_security=True
    )
    assert built.quotas.kill_switch_durable
    # And a development state keeps the process-local one.
    assert not CloudState.create(config).quotas.kill_switch_durable


def test_a_durable_kill_switch_refuses_a_backend_that_does_not_survive():
    with pytest.raises(ValueError, match="durable kill switch backend"):
        DurableKillSwitch(MemorySecurityStateBackend())


def test_the_runbook_describes_the_switch_as_it_behaves():
    """Including the staleness window, which is the number an operator needs.

    Pinned here rather than in ``tests/test_cloud_deployment.py``, which the
    Bicep issues (#164, #165, #168, #170) are all editing.
    """

    runbook = (
        pathlib.Path(__file__).resolve().parents[1] / "docs" / "cloud-sync.md"
    ).read_text()
    assert "**The budget kill switch is durable.**" in runbook
    assert f"Staleness window: {int(KILL_SWITCH_TTL_SECONDS)} seconds" in runbook
    assert "It fails closed" in runbook
    assert "Clearing it is an update, never a delete" in runbook
    assert "process-local kill switch at boot" in runbook
    # The old claim must not survive alongside the new one.
    assert "kill switch (`writes_enabled` / `public_enabled`) is also" not in runbook


def test_the_process_local_switch_still_works_where_it_is_allowed():
    switch = ProcessKillSwitch()
    assert switch.durable is False
    assert switch.state() == KILL_SWITCH_ENABLED
    switch.set(writes_enabled=False, public_enabled=True, reason="local")
    assert switch.state().writes_enabled is False
    assert switch.state().public_enabled is True
