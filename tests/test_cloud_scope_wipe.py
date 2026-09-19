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
    ScopePurge,
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


def _consume_pairing(registry, code):
    binding = registry.peek(code)
    if binding is None:
        return None
    return registry.consume_binding(binding)


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
    assert _consume_pairing(alice.pairing, alice.pairing_code.code) is None


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


def test_credentials_are_gone_before_the_store_is_purged(riders):
    """N1: "credentials first, data last" is the design, so it is pinned.

    Moving the ``purge_scope`` call above the record loop is the exact
    inversion of the ordering the module docstring argues for, and it left the
    suite green. It is not cosmetic: a sync in flight under a credential that
    still resolves can write a fresh object into a scope that has just been
    emptied, and an invitation can still be redeemed for it.

    Asserted from inside ``purge_scope`` itself -- the only place that can
    observe the ordering -- rather than from the report, which looks identical
    either way.
    """

    store, backend, alice, _bob = riders
    writer_key = hashlib.sha256(
        alice.writer.credential_id.encode("utf-8")
    ).hexdigest()
    device_key = hashlib.sha256(
        alice.device.credential_id.encode("utf-8")
    ).hexdigest()
    observed = {}

    class _ObservingStore:
        def purge_scope(self, namespace, local_user_scope):
            observed["writer"] = backend.read("writer", writer_key)
            observed["device"] = backend.read("device", device_key)
            observed["invitation"] = alice.enrollment.consume(alice.invitation)
            observed["pairing"] = _consume_pairing(
                alice.pairing, alice.pairing_code.code
            )
            return store.purge_scope(namespace, local_user_scope)

    assert backend.read("writer", writer_key) is not None

    wipe_scope(
        alice.namespace,
        alice.scope,
        irreversible=True,
        store=_ObservingStore(),
        security_backend=backend,
    )

    assert observed["writer"] is None
    assert observed["device"] is None
    # And nothing redeemable into a fresh credential was left either.
    assert observed["invitation"] is None
    assert observed["pairing"] is None


def test_the_report_adds_the_stores_skipped_rows_to_its_own(riders):
    """N2: ``purge.skipped`` must reach the caller, and so must ``orphans``.

    Dropping it left the suite green while silently discarding the one signal
    that says "a row in your store names a blob this purge would not follow".
    The two sources are summed, so the assertion is on a total neither half
    could produce alone.

    ``orphans`` is the same class of signal for C1 -- blobs found under the
    scope's prefix with no row naming them -- and has only one source, so it
    is asserted to arrive unchanged and not folded into ``skipped``.
    """

    _store, backend, alice, _bob = riders

    class _SkippingStore:
        def purge_scope(self, _namespace, _scope):
            return ScopePurge(
                objects=1, blobs=1, markers=2, skipped=3, orphans=5
            )

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
        store=_SkippingStore(),
        security_backend=hostile,
    )

    assert report.skipped == 4  # 3 from the store, 1 unreadable row
    assert report.objects == 1
    assert report.blobs == 1
    assert report.markers == 2
    assert report.orphans == 5  # its own count, not folded into skipped


def test_an_invalid_scan_limit_is_refused_by_the_wipe_itself(riders):
    """N2: the bound is validated here, not borrowed from the backend.

    ``MemorySecurityStateBackend.iter_records`` raises on a non-positive
    limit, so a wipe driven by *that* backend appears to validate even with
    its own check removed. A store-only wipe never calls ``iter_records`` at
    all, which is what makes this test pin ``wipe_scope``'s own guard.
    """

    store, _backend, alice, _bob = riders

    for limit in (0, -1, True, False, 1.5, "10", None):
        with pytest.raises(ValueError, match="scan_limit"):
            wipe_scope(
                alice.namespace,
                alice.scope,
                irreversible=True,
                store=store,
                scan_limit=limit,
            )
    # Refused before anything was removed: the check precedes the purge.
    assert [value.object_id for value in alice.readable_objects()] == ["ride-2"]
    assert store.revision(alice.namespace, alice.scope) == 3


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


def test_a_tampered_device_payload_cannot_delete_another_riders_device_seen_row(
    riders,
):
    """B1, the ``device-seen`` vector.

    The threat model includes a compromised read plane, which holds
    ``entities/write`` on ``authTable`` and can therefore rewrite any row's
    payload. A ``device`` row is *addressed* by ``sha256(credential_id)``, so
    a payload whose ``credential_id`` names a different device describes a row
    that is not the one it sits in. Deriving the companion key from that field
    alone made wiping Alice delete Bob's ``device-seen`` row -- a row that
    carries no namespace, so nothing downstream could catch it.

    The parent row is still deleted: ``_find_device_locked`` resolves a device
    from the *supplied* id and the row at its digest, ignoring the payload's
    ``credential_id``, so a tampered row still authenticates Alice's device.
    Leaving it would leave a live credential behind a wipe.
    """

    store, backend, alice, bob = riders
    alice_device_key = hashlib.sha256(
        alice.device.credential_id.encode("utf-8")
    ).hexdigest()
    bob_seen_key = hashlib.sha256(
        bob.device.credential_id.encode("utf-8")
    ).hexdigest()
    tampered = dict(backend.read("device", alice_device_key))
    tampered["credential_id"] = bob.device.credential_id
    backend.write("device", alice_device_key, tampered)
    assert backend.read(DEVICE_SEEN_RECORD_KIND, bob_seen_key) is not None

    report = wipe_scope(
        alice.namespace,
        alice.scope,
        irreversible=True,
        store=store,
        security_backend=backend,
    )

    # Bob keeps the row, and keeps reading his own "last seen" through it.
    assert backend.read(DEVICE_SEEN_RECORD_KIND, bob_seen_key) is not None
    assert bob.restarted().device_last_seen(bob.device.credential_id) == 1_000.0
    assert report.records.get(DEVICE_SEEN_RECORD_KIND, 0) == 0
    # Alice's device row is gone anyway: it still authenticated her device.
    assert backend.read("device", alice_device_key) is None
    assert alice.restarted().lookup_device(alice.device.credential_id) is None
    # And the refusal is reported rather than silent.
    assert report.skipped >= 1


def test_a_tampered_context_payload_cannot_delete_another_riders_context_index(
    riders,
):
    """B1, the ``context-index`` vector.

    A ``context`` row is addressed by ``sha256(token)`` and its payload's
    ``context_id`` addresses the ``context-index`` row -- two different
    digests, so the row cannot prove its own ``context_id`` the way a device
    row can. The index is what binds them, and it is read back here: its
    ``token_digest`` must name this very context row. A payload edited to
    Bob's context id points at an index that names Bob's context, so it is
    skipped; without the check, Bob's reader-context lookups broke.
    """

    store, backend, alice, bob = riders
    alice_context_key = hashlib.sha256(
        alice.context_token.encode("utf-8")
    ).hexdigest()
    bob_index_key = hashlib.sha256(
        bob.context.context_id.encode("utf-8")
    ).hexdigest()
    assert backend.read("context", alice_context_key) is not None
    tampered = dict(backend.read("context", alice_context_key))
    tampered["context_id"] = bob.context.context_id
    backend.write("context", alice_context_key, tampered)

    report = wipe_scope(
        alice.namespace,
        alice.scope,
        irreversible=True,
        store=store,
        security_backend=backend,
    )

    # Bob's index survives and still resolves his context by id.
    assert backend.read("context-index", bob_index_key) is not None
    assert bob.restarted().lookup_reader(bob.context.context_id) is not None
    assert bob.restarted().read_context_token(bob.context_token) is not None
    assert report.records.get("context-index", 0) == 0
    # Alice's context row is gone anyway: it still granted reads on her scope.
    assert backend.read("context", alice_context_key) is None
    assert alice.restarted().read_context_token(alice.context_token) is None
    assert report.skipped >= 1


def test_a_wipe_still_removes_the_companion_rows_it_can_prove(riders):
    """The control for the two tests above: proof is not an excuse to stop.

    Untampered rows prove out, so both companions go and nothing is reported
    as skipped. Without this, the fix for B1 could be "never delete a
    companion" and the suite would not notice.
    """

    store, backend, alice, _bob = riders
    seen_key = hashlib.sha256(
        alice.device.credential_id.encode("utf-8")
    ).hexdigest()
    index_key = hashlib.sha256(
        alice.context.context_id.encode("utf-8")
    ).hexdigest()

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
    assert report.skipped == 0


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


class _FakeBlobProperties:
    """What ``ContainerClient.list_blobs`` yields: a name-carrying object."""

    def __init__(self, name):
        self.name = name


class _FakeContainer:
    def __init__(self):
        self.blobs = {}

    def get_blob_client(self, name):
        return _FakeBlob(self, name)

    def list_blobs(self, *, name_starts_with):
        return [
            _FakeBlobProperties(name)
            for name in sorted(self.blobs)
            if name.startswith(name_starts_with)
        ]


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
    recomputed from the partition and the object id instead, and *that* is the
    name deleted; a row that disagrees is counted in ``skipped``.

    The ``blobs`` count here used to be 0, which is what the defect looked
    like from the report: the disagreement stopped the deletion outright
    rather than only stopping the stored name being followed, and the wiped
    rider's own blob survived. See
    ``test_azure_purge_leaves_no_readable_blob_when_a_row_names_a_foreign_one``
    for that half; what this test still owns is the other rider's blob, and
    that assertion is unchanged.
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
    # One blob deleted, and it is the wiped rider's own -- at the derived
    # name, never at the name the row stored.
    assert result.blobs == 1
    assert f"{attacker}:{SCOPE}/object:ride-1.json" not in container.blobs
    assert victim_blob in container.blobs
    assert store.get(victim, SCOPE, "ride-1") is not None


def test_azure_purge_leaves_no_readable_blob_when_a_row_names_a_foreign_one():
    """B2: refusing to follow a stored name must not orphan the rider's data.

    Skipping the *stored* name is right -- it may be another rider's blob.
    Skipping the deletion entirely was not: the row was deleted anyway, so the
    rider's own blob at the derived name survived holding heart rate, watts
    and body weight with nothing left pointing at it, and no later wipe could
    ever find it again. #170's promise is "a wipe leaves no readable object".

    The derived name is provably inside this partition -- ``_partition``
    validates the namespace as 64 hex characters and the scope against a
    charset with no quote or slash in it, and ``_row_key`` validates the
    object id the same way -- so deleting it can reach nothing but this
    rider's own data. The disagreement is still counted in ``skipped``.
    """

    store, container, table = _azure_store()
    victim = "b" * 64
    attacker = "a" * 64
    store.apply(victim, SCOPE, _batch("b1", 1, "ride-1"))
    store.apply(attacker, SCOPE, _batch("b1", 1, "ride-1"))
    victim_blob = f"{victim}:{SCOPE}/object:ride-1.json"
    attacker_partition = f"{attacker}:{SCOPE}"
    attacker_blob = f"{attacker_partition}/object:ride-1.json"
    # The rider's own blob really does hold the rider's own data.
    assert b"heartrate" in container.blobs[attacker_blob]
    tampered = dict(table.entities[(attacker_partition, "object:ride-1")])
    tampered["BlobName"] = victim_blob
    table.entities[(attacker_partition, "object:ride-1")] = tampered

    result = store.purge_scope(attacker, SCOPE)

    # Nothing of the wiped rider's survives, at any name in the partition.
    assert attacker_blob not in container.blobs
    assert not [name for name in container.blobs if name.startswith(attacker_partition)]
    assert not [key for key in table.entities if key[0] == attacker_partition]
    assert store.get(attacker, SCOPE, "ride-1", include_deleted=True) is None
    # The counts describe what was actually deleted, and still report the
    # disagreement rather than hiding it.
    assert (result.objects, result.blobs, result.markers, result.skipped) == (
        1, 1, 2, 1,
    )
    # The other rider's blob was never a coordinate this purge could use.
    assert victim_blob in container.blobs
    assert store.get(victim, SCOPE, "ride-1") is not None


def test_azure_purge_removes_a_blob_no_table_row_accounts_for():
    """C1: the wipe is defined by the container, not only by the table.

    ``_put_object`` uploads the blob and *then* upserts the row. A crash, a
    dropped connection or a 500 between those two writes leaves a blob holding
    heart rate, watts and body weight with no row naming it. ``get`` never
    returns it, so nobody notices -- and a row-driven purge never enumerates
    it, so it sits in the container after the rider asked for deletion. This
    needs no attacker; it is ordinary failure.

    Before the prefix pass the receipt said ``skipped=0`` while the blob
    survived, which is the worst version of the bug: the wipe reported a
    clean sweep it had not performed.
    """

    store, container, table = _azure_store()
    namespace = "a" * 64
    store.apply(namespace, SCOPE, _batch("b1", 1, "ride-1"))
    partition = f"{namespace}:{SCOPE}"
    # Exactly the state a crash between the two writes of ``_put_object``
    # leaves: the blob is there, the row never landed.
    orphan = f"{partition}/object:ride-9.json"
    container.blobs[orphan] = json.dumps(
        {"object_id": "ride-9", "heartrate": 152, "weight_kg": 71.4}
    ).encode("utf-8")
    assert (partition, "object:ride-9") not in table.entities

    result = store.purge_scope(namespace, SCOPE)

    # Nothing of this rider's is left in the container at any name.
    assert orphan not in container.blobs
    assert not [name for name in container.blobs if name.startswith(partition)]
    assert not [key for key in table.entities if key[0] == partition]
    # And the receipt says so: one row-driven blob, one the prefix pass found.
    assert (result.objects, result.blobs, result.orphans) == (1, 1, 1)
    assert result.skipped == 0


def test_azure_purge_finishes_the_partition_when_a_row_is_already_gone():
    """C2: a 404 from ``delete_entity`` is the desired end state, not an error.

    ``_delete_blob`` already swallows not-found; the row deletion called the
    table raw, so one 404 raised out of ``purge_scope`` mid-partition and left
    the remaining rows and blobs behind -- with the rider's credentials
    already deleted by the wipe's earlier phase, so nothing could reach the
    data and no later call could find it either.

    Genuine failures must still propagate: the second half asserts a 403 out
    of the same call is not swallowed.
    """

    store, container, table = _azure_store()
    namespace = "a" * 64
    store.apply(namespace, SCOPE, _batch("b1", 1, "ride-1"))
    store.apply(namespace, SCOPE, _batch("b2", 2, "ride-2"))
    partition = f"{namespace}:{SCOPE}"
    raw_delete = table.delete_entity
    vanished = {"object:ride-1"}

    def racing_delete(*, partition_key, row_key):
        if row_key in vanished:
            vanished.discard(row_key)
            del table.entities[(partition_key, row_key)]
            raise _StorageError(404)
        return raw_delete(partition_key=partition_key, row_key=row_key)

    table.delete_entity = racing_delete

    result = store.purge_scope(namespace, SCOPE)

    assert not [key for key in table.entities if key[0] == partition]
    assert not [name for name in container.blobs if name.startswith(partition)]
    # The row that was already gone is not counted as one this call removed.
    assert (result.objects, result.blobs, result.markers) == (1, 2, 3)
    assert store.revision(namespace, SCOPE) == 0

    # A refusal is not an absence. 403 still propagates.
    store.apply(namespace, SCOPE, _batch("b3", 3, "ride-3"))

    def refusing_delete(*, partition_key, row_key):
        raise _StorageError(403)

    table.delete_entity = refusing_delete
    with pytest.raises(_StorageError):
        store.purge_scope(namespace, SCOPE)


def test_azure_purge_keeps_an_object_row_whose_id_cannot_be_derived():
    """C3: an underivable RowKey must not be deleted into invisibility.

    ``_row_key`` validates on write, so this is not reachable through the app:
    it needs ``entities/write`` on ``CloudObjects`` -- the same compromised
    sync identity as the ``BlobName`` tampering. The row key ``object:.bad``
    yields no object id, so no blob name can be derived from it. Deleting the
    row anyway destroyed the only record that the pair was inconsistent while
    leaving the blob, so no later wipe could ever find it.

    The prefix pass of C1 does remove that blob -- the rider's data is gone
    either way, which is the promise that matters. The row is still kept,
    because it is the only remaining evidence that something wrote an
    unvalidated key into this table, and a row with no data behind it is
    cheap. It is counted in ``skipped``, never in ``objects``: ``objects`` is
    a receipt of rows actually removed.
    """

    store, container, table = _azure_store()
    namespace = "a" * 64
    store.apply(namespace, SCOPE, _batch("b1", 1, "ride-1"))
    partition = f"{namespace}:{SCOPE}"
    real_blob = f"{partition}/object:ride-1.json"
    assert b"heartrate" in container.blobs[real_blob]
    tampered = dict(table.entities.pop((partition, "object:ride-1")))
    tampered["RowKey"] = "object:.bad"
    table.entities[(partition, "object:.bad")] = tampered

    result = store.purge_scope(namespace, SCOPE)

    # The row survives, and it is the only thing in the partition that does.
    assert [key[1] for key in table.entities if key[0] == partition] == [
        "object:.bad"
    ]
    # The rider's data does not survive: the prefix pass reached the blob the
    # row key could no longer name.
    assert not [name for name in container.blobs if name.startswith(partition)]
    assert (result.objects, result.blobs, result.orphans, result.skipped) == (
        0, 0, 1, 1,
    )


def test_azure_purge_leaves_a_sibling_scope_whose_name_it_prefixes():
    """The prefix is a path prefix, not a string prefix.

    ``rider`` and ``rider2`` share a namespace and the first spells the start
    of the second, so a pass that listed ``f"{partition}"`` rather than
    ``f"{partition}/"`` would empty both and report nothing wrong. The
    trailing separator is the whole isolation guarantee for the prefix pass,
    and this is the case that can tell.
    """

    store, container, table = _azure_store()
    namespace = "a" * 64
    store.apply(namespace, "rider", _batch("b1", 1, "ride-1"))
    store.apply(namespace, "rider2", _batch("b1", 1, "ride-2"))
    sibling_blob = f"{namespace}:rider2/object:ride-2.json"
    orphan = f"{namespace}:rider2/object:ride-9.json"
    container.blobs[orphan] = b'{"heartrate": 152}'

    result = store.purge_scope(namespace, "rider")

    assert sibling_blob in container.blobs
    assert orphan in container.blobs
    assert store.get(namespace, "rider2", "ride-2") is not None
    assert store.revision(namespace, "rider2") == 1
    assert not [n for n in container.blobs if n.startswith(f"{namespace}:rider/")]
    assert result.orphans == 0


def test_azure_purge_will_not_follow_a_listed_name_back_out_of_the_prefix():
    """``startswith`` is not containment: the service resolves ``..``.

    A listed name can sit inside the prefix as text and still address another
    rider's blob once the service walks it, which is the one way past the
    check above. The rider's own names never carry a ``..`` segment --
    ``_blob_name`` builds them from a validated object id -- so refusing them
    costs nothing and is counted like any other listing anomaly.
    """

    store, container, table = _azure_store()
    victim = "b" * 64
    attacker = "a" * 64
    store.apply(victim, SCOPE, _batch("b1", 1, "ride-1"))
    store.apply(attacker, SCOPE, _batch("b1", 1, "ride-1"))
    victim_blob = f"{victim}:{SCOPE}/object:ride-1.json"
    prefix = f"{attacker}:{SCOPE}/"
    climbing = f"{prefix}../{victim}:{SCOPE}/object:ride-1.json"
    container.blobs[climbing] = b"placeholder"
    honest_list = container.list_blobs

    def lying_list(*, name_starts_with):
        listed = honest_list(name_starts_with=name_starts_with)
        if name_starts_with.startswith(attacker):
            listed.append(_FakeBlobProperties(climbing))
        return listed

    container.list_blobs = lying_list

    result = store.purge_scope(attacker, SCOPE)

    assert victim_blob in container.blobs
    assert store.get(victim, SCOPE, "ride-1") is not None
    assert result.skipped >= 1


def test_azure_purge_will_not_delete_a_listed_name_outside_the_partition():
    """The prefix listing is a coordinate source, so it is bounded too.

    Every other name this purge deletes is derived locally. These come back
    from the service, so a name outside ``f"{partition}/"`` is refused rather
    than followed -- the same rule as ``BlobName`` -- and counted in
    ``skipped`` so the anomaly is reported rather than swallowed.
    """

    store, container, table = _azure_store()
    victim = "b" * 64
    attacker = "a" * 64
    store.apply(victim, SCOPE, _batch("b1", 1, "ride-1"))
    store.apply(attacker, SCOPE, _batch("b1", 1, "ride-1"))
    victim_blob = f"{victim}:{SCOPE}/object:ride-1.json"
    partition = f"{attacker}:{SCOPE}"
    honest_list = container.list_blobs

    def lying_list(*, name_starts_with):
        listed = honest_list(name_starts_with=name_starts_with)
        if name_starts_with.startswith(attacker):
            listed.append(_FakeBlobProperties(victim_blob))
        return listed

    container.list_blobs = lying_list

    result = store.purge_scope(attacker, SCOPE)

    assert victim_blob in container.blobs
    assert store.get(victim, SCOPE, "ride-1") is not None
    assert not [name for name in container.blobs if name.startswith(partition)]
    assert result.skipped == 1


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
