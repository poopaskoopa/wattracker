"""Deleting one rider's cloud data, and proving it is gone (#170).

Everything here runs against ``MemoryTenantStore`` / ``MemorySecurityStateBackend``
or against ``AzureTenantStore`` driven by in-test doubles. Nothing in this file
opens a database or a network connection, which matters more than usual: this
is deletion code, and a probe that reached the developer's real store would be
the one bug the suite cannot undo.

**The two-installation fixture.** The issue asks to reuse #167's. There is no
such fixture in this repository -- ``tests/test_cloud_device_revocation.py``
says so in its own docstring and builds a second namespace in-test for the same
reason. So ``riders`` below is new, and it is built the way a second rider
actually exists: a second installation id, which derives a second namespace.
Both riders deliberately use the *same* ``local_user_scope`` string, because a
wipe that separated them on the scope name alone would pass a weaker test than
the one that matters.

**The kill switch and the counters share the riders' backend here.** In the
deployed template the switch lives in `CloudControl`, which the operator wipe
role is not scoped to, so a real wipe cannot reach it at all. Putting it in the
same backend as the credentials is the harder case, and it is the one the
application-level exclusion has to survive.
"""

import hashlib
import json

import pytest

from wattracker.cloud.limits import (
    INSTALLATION_SUBJECT,
    KILL_SWITCH_KEY,
    KILL_SWITCH_RECORD_KIND,
    METRIC_UPLOAD_BYTES,
    QUOTA_RECORD_KIND,
    SCOPE_SUBJECT,
    counter_key,
    read_kill_switch,
    set_kill_switch,
)
from wattracker.cloud.models import CloudObject, SyncBatch
from wattracker.cloud.security import (
    DEVICE_SEEN_RECORD_KIND,
    NEVER_SWEEP_RECORD_KINDS,
    SWEEPABLE_RECORD_KINDS,
    CredentialRegistry,
    DevicePairingRegistry,
    EnrollmentRegistry,
    MemorySecurityStateBackend,
    derive_installation_namespace,
    generate_signing_keypair,
    new_installation_id,
)
from wattracker.cloud.storage import (
    AzureTenantStore,
    MemoryTenantStore,
    StaleRevision,
)
from wattracker.cloud.wipe import (
    WIPE_PROTECTED_RECORD_KINDS,
    WIPE_RECORD_KINDS,
    ScopeWipeReport,
    wipe_scope,
)


SECRET = b"cloud-test-server-secret-32-bytes-long"
SCOPE = "rider"


class _DurableMemoryBackend(MemorySecurityStateBackend):
    """Shared-process state that claims durability, as a real table would."""

    durable = True


def _batch(batch_id, revision, object_id="ride-1", deleted=False, watts=250):
    return SyncBatch(
        batch_id=batch_id,
        revision=revision,
        objects=(CloudObject(
            object_id=object_id,
            kind="activity",
            revision=revision,
            data={"watts": watts, "heartrate": 152, "weight_kg": 71.4},
            deleted=deleted,
        ),),
    )


class _Rider:
    """One installation, with every row kind a scope can own."""

    def __init__(self, store, backend, seed):
        self.installation_id = new_installation_id()
        self.namespace = derive_installation_namespace(SECRET, self.installation_id)
        self.scope = SCOPE
        self.store = store
        self.backend = backend

        self.credentials = CredentialRegistry(SECRET, backend=backend)
        self.enrollment = EnrollmentRegistry(SECRET, backend=backend)
        self.pairing = DevicePairingRegistry(SECRET, backend=backend)

        self.writer = self.credentials.register_writer(
            self.installation_id,
            self.scope,
            signing_key=(seed * 32)[:32],
            subscription_key=(seed * 16)[:16],
        )
        public_key, _private = generate_signing_keypair()
        self.device = self.credentials.register_device_for_scope(
            self.namespace, self.scope, public_key, label="phone"
        )
        self.credentials.record_device_seen(self.device.credential_id, now=1_000.0)
        self.context_token, self.context = (
            self.credentials.issue_reader_context_for_scope(
                self.namespace,
                self.scope,
                device_credential_id=self.device.credential_id,
            )
        )
        self.invitation = self.enrollment.create_invitation(
            self.namespace, self.scope
        )
        self.pairing_code = self.pairing.create(self.namespace, self.scope)

        store.apply(self.namespace, self.scope, _batch("b1", 1, "ride-1"))
        store.apply(self.namespace, self.scope, _batch("b2", 2, "ride-2"))
        # A tombstoned object: deleted by ordinary sync, still inside the
        # 7-day recovery window a wipe deliberately does not honour.
        store.apply(
            self.namespace, self.scope, _batch("b3", 3, "ride-1", deleted=True)
        )

    def restarted(self):
        """A fresh registry over the same durable rows.

        The pattern ``test_cloud_device_revocation.py`` uses: a wipe has to
        outlive the process that ran it, and asserting through the same
        long-lived registry would let a process-local cache answer for the
        durable state that is actually under test.
        """

        return CredentialRegistry(SECRET, backend=self.backend)

    def readable_objects(self):
        return self.store.list_objects(self.namespace, self.scope)

    def stored_objects(self):
        return self.store.list_objects(
            self.namespace, self.scope, include_deleted=True
        )

    def devices(self):
        return self.credentials.list_devices_for_scope(self.namespace, self.scope)


@pytest.fixture()
def riders():
    """Two installations sharing one local scope name, in one deployment."""

    pytest.importorskip("cryptography")
    store = MemoryTenantStore()
    backend = _DurableMemoryBackend()
    first = _Rider(store, backend, b"a")
    second = _Rider(store, backend, b"b")
    assert first.namespace != second.namespace
    assert first.scope == second.scope
    return store, backend, first, second


# ---------------------------------------------------------------------------
# The wipe itself
# ---------------------------------------------------------------------------


def test_a_wipe_leaves_no_object_no_credential_and_no_device(riders):
    store, backend, alice, _bob = riders
    assert alice.readable_objects()
    assert alice.devices()

    report = wipe_scope(
        alice.namespace,
        alice.scope,
        irreversible=True,
        store=store,
        security_backend=backend,
    )

    assert isinstance(report, ScopeWipeReport)
    assert report.complete
    assert report.credentials == 2
    assert report.objects == 2  # ride-1 tombstoned + ride-2
    # No readable object, and nothing behind include_deleted either.
    assert alice.readable_objects() == []
    assert alice.stored_objects() == []
    assert store.get(alice.namespace, alice.scope, "ride-2") is None
    assert store.get(
        alice.namespace, alice.scope, "ride-1", include_deleted=True
    ) is None
    # No credential, read back through a restarted registry so that a
    # process-local cache cannot answer for the durable rows.
    after = alice.restarted()
    assert after.lookup_writer(alice.writer.credential_id) is None
    assert after.resolve_writer(alice.writer.credential_id) is None
    assert after.authenticate_writer(
        alice.writer.credential_id, alice.writer.subscription_key
    ) is None
    # No device registration, and no reader context minted from it.
    assert after.list_devices_for_scope(alice.namespace, alice.scope) == ()
    assert alice.devices() == ()
    assert after.lookup_device(alice.device.credential_id) is None
    assert after.device_last_seen(alice.device.credential_id) is None
    assert after.read_context_token(alice.context_token) is None
    # Nothing left that could be redeemed back into a credential.
    assert alice.enrollment.consume(alice.invitation) is None
    assert alice.pairing.consume(alice.pairing_code.code) is None


def test_the_companion_rows_that_carry_no_scope_go_with_their_owners(riders):
    """``device-seen`` and ``context-index`` name no namespace of their own.

    Neither could be matched on a scope, and walking them blind is exactly how
    a wipe would reach into another rider's rows. Each is addressed by the
    same digest that addresses the row that owns it, so the key is derived
    from the owner's payload and deleted with it.
    """

    store, backend, alice, bob = riders
    seen_key = hashlib.sha256(
        alice.device.credential_id.encode("utf-8")
    ).hexdigest()
    index_key = hashlib.sha256(
        alice.context.context_id.encode("utf-8")
    ).hexdigest()
    bob_seen_key = hashlib.sha256(
        bob.device.credential_id.encode("utf-8")
    ).hexdigest()
    bob_index_key = hashlib.sha256(
        bob.context.context_id.encode("utf-8")
    ).hexdigest()
    assert backend.read(DEVICE_SEEN_RECORD_KIND, seen_key) is not None
    assert backend.read("context-index", index_key) is not None

    report = wipe_scope(
        alice.namespace,
        alice.scope,
        irreversible=True,
        store=store,
        security_backend=backend,
    )

    assert backend.read(DEVICE_SEEN_RECORD_KIND, seen_key) is None
    assert backend.read("context-index", index_key) is None
    assert report.records[DEVICE_SEEN_RECORD_KIND] == 1
    assert report.records["context-index"] == 1
    # Bob keeps his, and the registry can still resolve his context by id.
    assert backend.read(DEVICE_SEEN_RECORD_KIND, bob_seen_key) is not None
    assert backend.read("context-index", bob_index_key) is not None
    assert bob.restarted().lookup_reader(bob.context.context_id) is not None
    assert alice.restarted().lookup_reader(alice.context.context_id) is None


def test_a_wipe_of_one_scope_does_not_touch_the_other_scope(riders):
    """The whole point of the two-installation fixture.

    Both riders share the local scope name ``rider``, so only the namespace
    separates them -- which is exactly the separation a wipe has to respect.
    """

    store, backend, alice, bob = riders

    wipe_scope(
        alice.namespace,
        alice.scope,
        irreversible=True,
        store=store,
        security_backend=backend,
    )

    bob_after = bob.restarted()
    assert [value.object_id for value in bob.readable_objects()] == ["ride-2"]
    assert store.get(bob.namespace, bob.scope, "ride-2").data["heartrate"] == 152
    assert store.revision(bob.namespace, bob.scope) == 3
    assert bob_after.resolve_writer(bob.writer.credential_id) is not None
    assert [
        device.credential_id
        for device in bob_after.list_devices_for_scope(bob.namespace, bob.scope)
    ] == [bob.device.credential_id]
    assert bob_after.device_last_seen(bob.device.credential_id) == 1_000.0
    assert bob_after.read_context_token(bob.context_token) is not None
    # Bob's redeemables are untouched and still redeemable.
    assert bob.enrollment.consume(bob.invitation) is not None


def test_a_wipe_is_refused_unless_it_is_asked_for_as_irreversible(riders):
    store, backend, alice, _bob = riders
    for flag in (False, None, 1, "yes"):
        with pytest.raises(ValueError, match="irreversible"):
            wipe_scope(
                alice.namespace,
                alice.scope,
                irreversible=flag,
                store=store,
                security_backend=backend,
            )
    # Nothing was removed by any of the refused calls.
    assert alice.readable_objects()
    assert alice.devices()


def test_a_wipe_rejects_an_invalid_scope_and_an_empty_call(riders):
    store, backend, alice, _bob = riders
    with pytest.raises(ValueError):
        wipe_scope("not-a-namespace", alice.scope, irreversible=True, store=store)
    with pytest.raises(ValueError):
        wipe_scope(alice.namespace, "", irreversible=True, store=store)
    with pytest.raises(ValueError):
        wipe_scope(alice.namespace, alice.scope, irreversible=True)
    assert alice.readable_objects()


# ---------------------------------------------------------------------------
# The kill switch, which must survive
# ---------------------------------------------------------------------------


def test_the_kill_switch_survives_a_wipe_and_the_deployment_stays_killed(riders):
    """Asserted on the *read*, not on the row.

    An absent kill-switch row reads as ENABLED, so "the row is still there"
    and "the deployment is still killed" fail differently once somebody
    changes the storage layout. The second one is the property.
    """

    store, backend, alice, _bob = riders
    set_kill_switch(
        backend,
        writes_enabled=False,
        public_enabled=False,
        reason="budget at 100%",
    )
    assert read_kill_switch(backend).public_enabled is False

    wipe_scope(
        alice.namespace,
        alice.scope,
        irreversible=True,
        store=store,
        security_backend=backend,
    )

    state = read_kill_switch(backend)
    assert state.writes_enabled is False
    assert state.public_enabled is False
    assert state.reason == "budget at 100%"


def test_wiping_one_rider_leaves_the_other_riders_deployment_killed(riders):
    """The two-installation half of the same constraint.

    A deployment killed while rider B was using it must still be killed after
    rider A's data is deleted. The switch is deployment-wide; a per-rider
    operation has no business clearing it.
    """

    store, backend, alice, bob = riders
    set_kill_switch(
        backend, writes_enabled=False, public_enabled=True, reason="budget at 80%"
    )

    wipe_scope(
        alice.namespace,
        alice.scope,
        irreversible=True,
        store=store,
        security_backend=backend,
    )

    assert read_kill_switch(backend).writes_enabled is False
    assert bob.credentials.resolve_writer(bob.writer.credential_id) is not None


def test_the_kill_switch_row_is_addressed_by_a_kind_the_wipe_refuses(riders):
    """Belt and braces on the row itself, as well as on the read above."""

    _store, backend, _alice, _bob = riders
    set_kill_switch(backend, writes_enabled=False, public_enabled=False)
    assert backend.read(KILL_SWITCH_RECORD_KIND, KILL_SWITCH_KEY) is not None
    assert KILL_SWITCH_RECORD_KIND in WIPE_PROTECTED_RECORD_KINDS
    assert KILL_SWITCH_RECORD_KIND not in WIPE_RECORD_KINDS


# ---------------------------------------------------------------------------
# The quota counters, which must also survive -- and why
# ---------------------------------------------------------------------------


def test_the_quota_counters_survive_a_wipe_of_their_own_scope(riders):
    """A wipe must not refund a rider's daily spend.

    Unlike the kill switch, a counter row *is* keyed by (namespace, scope), so
    a scope filter would find it. It is kept because an absent counter row
    reads as zero: wiping it turns wipe-then-re-enrol into an unlimited daily
    budget on a deployment whose only cost control is these rows. The row
    holds a byte count and a UTC date, so keeping it leaks nothing.
    """

    store, backend, alice, _bob = riders
    scope_row = counter_key(
        SCOPE_SUBJECT, alice.namespace, alice.scope, METRIC_UPLOAD_BYTES
    )
    installation_row = counter_key(
        INSTALLATION_SUBJECT, alice.namespace, "", METRIC_UPLOAD_BYTES
    )
    for key in (scope_row, installation_row):
        backend.charge_counter(
            QUOTA_RECORD_KIND,
            key,
            day="2026-09-15",
            amount=9_000,
            ceiling=10_000,
            expires_at=2_000_000_000.0,
            now=1_000.0,
        )

    wipe_scope(
        alice.namespace,
        alice.scope,
        irreversible=True,
        store=store,
        security_backend=backend,
    )

    assert backend.read(QUOTA_RECORD_KIND, scope_row)["value"] == 9_000
    assert backend.read(QUOTA_RECORD_KIND, installation_row)["value"] == 9_000
    assert QUOTA_RECORD_KIND in WIPE_PROTECTED_RECORD_KINDS


def test_the_protected_kinds_agree_with_the_sweepers_never_sweep_list():
    """The two denylists are separate by design, and must not drift apart.

    The sweeper may not remove credentials; the wipe must. So the lists differ
    on ``writer``, ``device`` and ``device-seen`` and that difference is the
    point. What they must agree on is the pair whose *absence* reads as a
    permissive value, plus the deployment's own probe row.
    """

    for kind in ("kill-switch", "quota-counter", "health"):
        assert kind in NEVER_SWEEP_RECORD_KINDS
        assert kind in WIPE_PROTECTED_RECORD_KINDS
    # Nothing the wipe walks is also protected, and nothing it walks is a kind
    # the sweeper already removes on expiry alone.
    assert not set(WIPE_RECORD_KINDS) & WIPE_PROTECTED_RECORD_KINDS
    assert "nonce" in SWEEPABLE_RECORD_KINDS
    assert "nonce" in WIPE_PROTECTED_RECORD_KINDS
    assert "nonce" not in WIPE_RECORD_KINDS


# ---------------------------------------------------------------------------
# Tombstones: the choice, made explicit
# ---------------------------------------------------------------------------


def test_a_wipe_bypasses_the_seven_day_recovery_retention(riders):
    """The documented, irreversible bypass of ``RECOVERY_RETENTION``.

    ``ride-1`` was tombstoned by an ordinary sync one revision before the wipe
    and is well inside the 7-day window, so before the wipe it is recoverable.
    Afterwards there is nothing to recover -- which is the whole request.
    """

    store, backend, alice, _bob = riders
    assert store.recover_deleted(alice.namespace, alice.scope, "ride-1") is not None
    # Put it back in the tombstoned state the wipe has to deal with.
    store.apply(alice.namespace, alice.scope, _batch("b4", 4, "ride-1", deleted=True))
    assert store.get(
        alice.namespace, alice.scope, "ride-1", include_deleted=True
    ) is not None

    wipe_scope(
        alice.namespace,
        alice.scope,
        irreversible=True,
        store=store,
        security_backend=backend,
    )

    assert store.recover_deleted(alice.namespace, alice.scope, "ride-1") is None
    assert store.get(
        alice.namespace, alice.scope, "ride-1", include_deleted=True
    ) is None


# ---------------------------------------------------------------------------
# Re-sync after a wipe
# ---------------------------------------------------------------------------


def test_sync_from_a_wiped_install_creates_a_clean_scope(riders):
    """Not a half-restore: the revision and the batch markers go too.

    A scope emptied of objects but left at revision 3 refuses a fresh
    install's first batch forever (``StaleRevision``), and a surviving batch
    marker replays an ``ApplyResult`` describing objects that no longer exist.
    """

    store, backend, alice, _bob = riders
    assert store.revision(alice.namespace, alice.scope) == 3

    wipe_scope(
        alice.namespace,
        alice.scope,
        irreversible=True,
        store=store,
        security_backend=backend,
    )
    assert store.revision(alice.namespace, alice.scope) == 0

    # A re-enrolled install starts over at revision 1 and reuses batch id b1.
    result = store.apply(alice.namespace, alice.scope, _batch("b1", 1, "ride-9", watts=99))
    assert result.replay is False
    assert result.revision == 1
    assert store.revision(alice.namespace, alice.scope) == 1
    fresh = store.list_objects(alice.namespace, alice.scope)
    assert [value.object_id for value in fresh] == ["ride-9"]
    assert fresh[0].data["watts"] == 99
    # Nothing from before the wipe came back with it.
    assert store.get(alice.namespace, alice.scope, "ride-2", include_deleted=True) is None


def test_without_a_wipe_a_reinstall_at_revision_one_is_still_refused(riders):
    """The control for the test above: this is the failure being prevented."""

    store, _backend, alice, _bob = riders
    with pytest.raises(StaleRevision):
        store.apply(alice.namespace, alice.scope, _batch("b9", 1, "ride-9"))


# ---------------------------------------------------------------------------
# Rows the wipe refuses to guess about
# ---------------------------------------------------------------------------


def test_an_unreadable_or_foreign_row_is_left_alone_and_reported(riders):
    store, backend, alice, _bob = riders
    backend.write("writer", "f" * 64, {"not": "a credential"})
    backend.write("device", "e" * 64, {"namespace": None, "local_user_scope": SCOPE})

    report = wipe_scope(
        alice.namespace,
        alice.scope,
        irreversible=True,
        store=store,
        security_backend=backend,
    )

    # Neither row named this rider, so neither was removed.
    assert backend.read("writer", "f" * 64) is not None
    assert backend.read("device", "e" * 64) is not None
    assert report.skipped == 0  # both decoded; they simply did not match


def test_a_non_ascii_scope_in_a_row_is_not_a_match_and_does_not_raise(riders):
    """``compare_digest`` refuses non-ASCII text.

    ``_require_local_scope`` cannot produce such a row, but an edited one
    could, and a wipe that raises partway through has already removed
    credentials. It is reported as "not this rider" instead.
    """

    store, backend, alice, _bob = riders
    backend.write(
        "writer",
        "a" * 64,
        {"namespace": alice.namespace, "local_user_scope": "rideré"},
    )

    report = wipe_scope(
        alice.namespace,
        alice.scope,
        irreversible=True,
        store=store,
        security_backend=backend,
    )

    assert backend.read("writer", "a" * 64) is not None
    assert report.complete
    assert report.records.get("writer") == 1  # only the real one


def test_a_row_whose_payload_will_not_decode_is_skipped_not_deleted(riders):
    store, backend, alice, _bob = riders

    class _Undecodable(_DurableMemoryBackend):
        def iter_records(self, kind, *, limit):
            rows = super().iter_records(kind, limit=limit)
            if kind == "writer":
                rows = list(rows) + [("c" * 64, None)]
            return rows

    hostile = _Undecodable()
    hostile._records = backend._records  # noqa: SLF001 - same rows, hostile reader
    report = wipe_scope(
        alice.namespace,
        alice.scope,
        irreversible=True,
        store=store,
        security_backend=hostile,
    )
    assert report.skipped == 1
    assert report.records.get("writer") == 1


def test_a_scope_prefix_is_not_a_scope_match():
    """``rider`` must not match ``rider2``, on either half of the pair."""

    backend = _DurableMemoryBackend()
    registry = CredentialRegistry(SECRET, backend=backend)
    installation = new_installation_id()
    namespace = derive_installation_namespace(SECRET, installation)
    near = registry.register_writer(
        installation, "rider2", signing_key=b"k" * 32, subscription_key=b"s" * 16
    )
    exact = registry.register_writer(
        installation, "rider", signing_key=b"k" * 32, subscription_key=b"s" * 16
    )

    report = wipe_scope(
        namespace, "rider", irreversible=True, security_backend=backend
    )

    assert report.records.get("writer") == 1
    assert registry.lookup_writer(exact.credential_id) is None
    assert registry.lookup_writer(near.credential_id) is not None


def test_a_wipe_reports_incompleteness_instead_of_silently_truncating(riders):
    store, backend, alice, _bob = riders
    report = wipe_scope(
        alice.namespace,
        alice.scope,
        irreversible=True,
        store=store,
        security_backend=backend,
        scan_limit=1,
    )
    assert report.complete is False
    with pytest.raises(ValueError):
        wipe_scope(
            alice.namespace, alice.scope, irreversible=True,
            security_backend=backend, scan_limit=0,
        )


# ---------------------------------------------------------------------------
# The Azure store, through doubles
# ---------------------------------------------------------------------------


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

    def delete_blob(self, **_kwargs):
        try:
            del self.container.blobs[self.name]
        except KeyError as exc:
            raise _StorageError(404) from exc


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

    def delete_entity(self, *, partition_key, row_key):
        try:
            del self.entities[(partition_key, row_key)]
        except KeyError as exc:
            raise _StorageError(404) from exc

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


def _azure_store():
    blobs = _FakeBlobService()
    tables = _FakeTableService()
    return AzureTenantStore(blobs, tables), blobs.container, tables.table


def test_azure_purge_removes_one_partitions_blobs_rows_and_lock(riders):
    store, container, table = _azure_store()
    namespace_a = "a" * 64
    namespace_b = "b" * 64
    for namespace in (namespace_a, namespace_b):
        store.apply(namespace, SCOPE, _batch("b1", 1, "ride-1"))
        store.apply(namespace, SCOPE, _batch("b2", 2, "ride-2"))
    partition_a = f"{namespace_a}:{SCOPE}"
    partition_b = f"{namespace_b}:{SCOPE}"
    assert any(name.startswith(partition_a) for name in container.blobs)

    result = store.purge_scope(namespace_a, SCOPE)

    assert result.objects == 2
    assert result.blobs == 2
    assert result.markers == 3  # two batch markers plus the scope row
    assert result.skipped == 0
    # Object blobs, the scope lease blob and every table row, all gone.
    assert not [name for name in container.blobs if name.startswith(partition_a)]
    assert f"{partition_a}/__lock" not in container.blobs
    assert not [key for key in table.entities if key[0] == partition_a]
    assert store.list_objects(namespace_a, SCOPE) == []
    assert store.revision(namespace_a, SCOPE) == 0
    # The other partition is whole, lease blob included.
    assert sorted(
        name for name in container.blobs if name.startswith(partition_b)
    ) == [
        f"{partition_b}/__lock",
        f"{partition_b}/object:ride-1.json",
        f"{partition_b}/object:ride-2.json",
    ]
    assert [value.object_id for value in store.list_objects(namespace_b, SCOPE)] == [
        "ride-1", "ride-2",
    ]
    assert store.revision(namespace_b, SCOPE) == 2
    # And the wiped partition takes a fresh install's revision 1 batch.
    assert store.apply(namespace_a, SCOPE, _batch("b1", 1, "ride-9")).replay is False


def test_azure_purge_will_not_follow_a_blobname_out_of_its_own_partition():
    """``BlobName`` is data in a row, so it is never the coordinate used.

    An edited row naming another partition's blob would, if followed, make a
    wipe of one rider delete a different rider's data. The expected name is
    recomputed from the partition and the object id instead, and a row that
    disagrees is skipped and counted.
    """

    store, container, table = _azure_store()
    victim = "b" * 64
    attacker = "a" * 64
    store.apply(victim, SCOPE, _batch("b1", 1, "ride-1"))
    store.apply(attacker, SCOPE, _batch("b1", 1, "ride-1"))
    victim_blob = f"{victim}:{SCOPE}/object:ride-1.json"
    assert victim_blob in container.blobs
    tampered = dict(table.entities[(f"{attacker}:{SCOPE}", "object:ride-1")])
    tampered["BlobName"] = victim_blob
    table.entities[(f"{attacker}:{SCOPE}", "object:ride-1")] = tampered

    result = store.purge_scope(attacker, SCOPE)

    assert result.skipped == 1
    assert result.blobs == 0
    assert victim_blob in container.blobs
    assert store.get(victim, SCOPE, "ride-1") is not None


def test_azure_and_memory_stores_agree_on_what_a_purge_leaves_behind():
    for store in (MemoryTenantStore(), _azure_store()[0]):
        namespace = "c" * 64
        store.apply(namespace, SCOPE, _batch("b1", 1, "ride-1"))
        store.purge_scope(namespace, SCOPE)
        assert store.list_objects(namespace, SCOPE) == []
        assert store.list_objects(namespace, SCOPE, include_deleted=True) == []
        assert store.revision(namespace, SCOPE) == 0
        assert store.usage(namespace, SCOPE) == 0
        assert store.usage_for_namespace(namespace) == 0


def test_purging_a_scope_that_never_existed_is_not_an_error():
    for store in (MemoryTenantStore(), _azure_store()[0]):
        result = store.purge_scope("d" * 64, SCOPE)
        assert (result.objects, result.blobs, result.skipped) == (0, 0, 0)


def test_the_wipe_entry_point_is_exported_and_carries_its_decision_in_source():
    """The tombstone choice has to be findable where the code is."""

    import wattracker.cloud as cloud
    from wattracker.cloud import wipe as wipe_module

    assert cloud.wipe_scope is wipe_scope
    source = wipe_module.__doc__ or ""
    assert "irreversible" in source
    assert "RECOVERY_RETENTION" in source
    # The kill-switch key is a hash of a fixed domain and b"deployment": it
    # belongs to no namespace, which is why a correct filter would already
    # miss it -- and why the exclusion is stated rather than relied upon.
    assert KILL_SWITCH_KEY == hashlib.sha256(
        b"wattracker-cloud-kill-switch-v1\x00deployment"
    ).hexdigest()
    assert json.loads(json.dumps(sorted(WIPE_RECORD_KINDS))) == [
        "context", "device", "device-pairing", "invitation", "writer",
    ]
    assert DEVICE_SEEN_RECORD_KIND not in WIPE_RECORD_KINDS
