"""Revoking a lost device over HTTP, and making the revocation stick (#153).

Two riders, two namespaces, and one rule underneath every test here: a
credential can only ever act inside the ``(namespace, local_user_scope)`` its
own stored record names, and an error must never confirm that a credential
exists in a namespace the caller cannot see.

There is no two-installation fixture in this repository to reuse, so a second
namespace is built in-test: ``_rider`` mints an independent installation, which
is exactly what a second rider is.
"""

import json

import pytest
from fastapi.testclient import TestClient

from wattracker.cloud.api import CloudConfig, CloudState, create_cloud_app
from wattracker.cloud.limits import (
    QuotaManager,
    QuotaPolicy,
    KILL_SWITCH_KEY,
    KILL_SWITCH_RECORD_KIND,
    QUOTA_RECORD_KIND,
    read_kill_switch,
)
from wattracker.cloud.security import (
    ExpiredRecordSweeper,
    MemorySecurityStateBackend,
    canonical_request,
    digest_body,
    generate_signing_keypair,
    new_installation_id,
    sign_request,
    sign_request_ed25519,
)


SECRET = b"cloud-test-server-secret-32-bytes-long"
LIST_PATH = "/api/v1/devices"
NOT_FOUND = {"detail": "not found"}


class _DurableMemoryBackend(MemorySecurityStateBackend):
    """Shared-process state that claims durability, as a real table would.

    Revocation has to outlive the process that performed it, so every test
    here that restarts a replica builds a fresh ``CloudState`` over this same
    backend -- the pattern ``test_pairing_survives_a_read_plane_restart``
    already uses for pairing.
    """

    durable = True


class _Clock:
    def __init__(self, value: float) -> None:
        self.value = float(value)

    def __call__(self) -> float:
        return self.value


@pytest.fixture()
def deployment():
    """A read plane over durable state, restartable in place."""

    pytest.importorskip("cryptography")
    backend = _DurableMemoryBackend()
    clock = _Clock(1_000.0)
    config = CloudConfig(
        server_secret=SECRET,
        operator_token="operator-token",
        plane="read",
        require_apim_proof=False,
        clock=clock,
    )
    state = CloudState.create(config, security_backend=backend)
    return _Deployment(config, backend, clock, state)


class _Deployment:
    def __init__(self, config, backend, clock, state) -> None:
        self.config = config
        self.backend = backend
        self.clock = clock
        self.state = state

    @property
    def client(self):
        return TestClient(create_cloud_app(self.config, state=self.state))

    def restart(self):
        """Drop every process-local cache; keep only the durable rows."""

        self.state = CloudState.create(self.config, security_backend=self.backend)
        return self


def _rider(deployment, *, seed=b"a", scope="scope"):
    """A desktop writer in its own installation, i.e. its own namespace."""

    return deployment.state.credentials.register_writer(
        new_installation_id(), scope, seed * 32, seed[::-1] * 32
    )


def _device(deployment, writer, *, label="phone"):
    """A device paired into the writer's namespace and scope."""

    private_key, public_key = generate_signing_keypair()
    device = deployment.state.credentials.register_device_for_scope(
        writer.namespace, writer.local_user_scope, public_key, label=label,
    )
    return device, private_key


def _envelope(credential, method, path, *, nonce, idem, revision=0,
              body=b"", timestamp=1_000, namespace=None, signer=None):
    canonical = canonical_request(
        method, path, namespace or credential.namespace, timestamp, nonce,
        digest_body(body), idem, str(revision),
    )
    if signer is None:
        def signer(material):
            return sign_request(credential.signing_key, material)
    return {
        "Ocp-Apim-Subscription-Key": credential.subscription_key.decode("ascii"),
        "X-Writer-Credential": credential.credential_id,
        "X-Writer-Timestamp": str(timestamp),
        "X-Writer-Nonce": nonce,
        "X-Writer-Idempotency-Key": idem,
        "X-Writer-Revision": str(revision),
        "X-Writer-Signature": signer(canonical),
        "X-Verified-Entra-Subject": "entra-user",
    }


def _device_signer(private_key):
    return lambda material: sign_request_ed25519(private_key, material)


def _list_headers(credential, *, nonce="list-1", signer=None, **kwargs):
    return _envelope(
        credential, "GET", LIST_PATH, nonce=nonce, idem="device-list",
        signer=signer, **kwargs
    )


def _revoke_path(credential_id):
    return f"/api/v1/devices/{credential_id}/revoke"


def _revoke_headers(credential, target_id, *, nonce="revoke-1", signer=None,
                    path=None, **kwargs):
    return _envelope(
        credential, "POST", path or _revoke_path(target_id), nonce=nonce,
        idem="device-revoke", signer=signer, **kwargs
    )


def _refresh_headers(device, private_key, *, nonce="refresh-1", timestamp=1_000):
    canonical = canonical_request(
        "POST", "/api/v1/context/refresh", device.namespace, timestamp, nonce,
        digest_body(b""), "context-refresh", "",
    )
    return {
        "X-Device-Credential": device.credential_id,
        "X-Device-Timestamp": str(timestamp),
        "X-Device-Nonce": nonce,
        "X-Device-Signature": sign_request_ed25519(private_key, canonical),
        "X-Verified-Entra-Subject": "entra-user",
    }


def _refresh(client, device, private_key, **kwargs):
    return client.post(
        "/api/v1/context/refresh",
        headers=_refresh_headers(device, private_key, **kwargs),
    )


def _reader_headers(token):
    return {
        "Authorization": f"Bearer {token}",
        "X-Verified-Entra-Subject": "entra-user",
    }


def _assert_not_found(response):
    """The one shape every refusal that must say nothing takes."""

    assert response.status_code == 404, response.text
    assert response.json() == NOT_FOUND
    assert response.headers["cache-control"] == "no-store"


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------


def test_the_listing_never_returns_key_material(deployment):
    """Asserted on the response body's keys, not on a sample of values.

    A stored device carries a verification key, a subscription-key digest and
    a signature algorithm.  None of it helps a rider decide which phone to
    revoke, and this response is the one place device state leaves the
    deployment, so the contract is the exact key set -- a field added to
    ``DeviceCredential`` later must fail here rather than ship.
    """

    writer = _rider(deployment)
    device, _private = _device(deployment, writer, label="Ryu's iPhone")
    with deployment.client as client:
        response = client.get(LIST_PATH, headers=_list_headers(writer))
    assert response.status_code == 200, response.text
    payload = response.json()
    assert list(payload) == ["devices"]
    assert len(payload["devices"]) == 1
    entry = payload["devices"][0]
    assert set(entry) == {
        "credential_id", "label", "capabilities", "created_at",
        "last_seen_at", "revoked", "self",
    }
    assert entry["credential_id"] == device.credential_id
    assert entry["label"] == "Ryu's iPhone"
    assert entry["capabilities"] == ["read"]
    assert entry["revoked"] is False
    assert entry["self"] is False

    # And nothing key-shaped anywhere in the serialized body, by value.
    body = response.text
    assert device.verification_key.hex() not in body
    assert device.subscription_key.decode("ascii") not in body
    assert device.subscription_verifier.hex() not in body
    assert device.namespace not in body
    assert "verification_key" not in body
    assert "subscription" not in body
    assert "signature_algorithm" not in body
    assert "namespace" not in body
    assert response.headers["cache-control"] == "no-store"


def test_the_listing_shows_only_the_callers_own_scope(deployment):
    """Two riders, two namespaces, and no way to name the other one."""

    mine = _rider(deployment, seed=b"a")
    theirs = _rider(deployment, seed=b"b")
    my_device, _mine_key = _device(deployment, mine, label="mine")
    their_device, _their_key = _device(deployment, theirs, label="theirs")
    other_scope = deployment.state.credentials.register_device_for_scope(
        mine.namespace, "another-local-user", generate_signing_keypair()[1],
        label="same rider, other local user",
    )

    with deployment.client as client:
        mine_listing = client.get(LIST_PATH, headers=_list_headers(mine))
        theirs_listing = client.get(
            LIST_PATH, headers=_list_headers(theirs, nonce="list-2")
        )
    listed = [entry["credential_id"] for entry in mine_listing.json()["devices"]]
    assert listed == [my_device.credential_id]
    assert other_scope.credential_id not in listed
    assert [
        entry["credential_id"] for entry in theirs_listing.json()["devices"]
    ] == [their_device.credential_id]


def test_a_device_can_list_and_sees_itself_flagged(deployment):
    writer = _rider(deployment)
    device, private_key = _device(deployment, writer)
    sibling, _sibling_key = _device(deployment, writer, label="iPad")
    with deployment.client as client:
        response = client.get(
            LIST_PATH,
            headers=_list_headers(device, signer=_device_signer(private_key)),
        )
    assert response.status_code == 200, response.text
    flags = {
        entry["credential_id"]: entry["self"]
        for entry in response.json()["devices"]
    }
    assert flags == {device.credential_id: True, sibling.credential_id: False}


def test_the_listing_reports_last_seen_after_a_refresh(deployment):
    writer = _rider(deployment)
    device, private_key = _device(deployment, writer)
    with deployment.client as client:
        before = client.get(LIST_PATH, headers=_list_headers(writer))
        assert before.json()["devices"][0]["last_seen_at"] is None
        deployment.clock.value = 2_000.0
        assert _refresh(
            client, device, private_key, timestamp=2_000
        ).status_code == 200
        after = client.get(LIST_PATH, headers=_list_headers(writer, nonce="l2",
                                                           timestamp=2_000))
    assert after.json()["devices"][0]["last_seen_at"] == 2_000.0


def test_the_listing_needs_a_fresh_untampered_signed_envelope(deployment):
    writer = _rider(deployment)
    _device(deployment, writer)
    with deployment.client as client:
        assert client.get(LIST_PATH).status_code == 401
        # A replayed nonce is refused as it is on every other signed route.
        assert client.get(LIST_PATH, headers=_list_headers(writer)).status_code == 200
        assert client.get(LIST_PATH, headers=_list_headers(writer)).status_code == 401
        # The envelope is fixed; a caller-chosen idempotency key is not signed
        # into a different meaning, it is refused.
        assert client.get(LIST_PATH, headers=_envelope(
            writer, "GET", LIST_PATH, nonce="n2", idem="device-revoke",
        )).status_code == 401
        # A signature over another namespace does not verify under this key.
        assert client.get(LIST_PATH, headers=_list_headers(
            writer, nonce="n3", namespace=_rider(deployment, seed=b"z").namespace,
        )).status_code == 401


# ---------------------------------------------------------------------------
# Revocation
# ---------------------------------------------------------------------------


def test_revocation_survives_a_process_restart(deployment):
    """The whole point: a revocation a cold replica still honours.

    The desktop revokes, every process-local cache is dropped, and the phone
    tries again against state that only the durable backend carried across.
    """

    writer = _rider(deployment)
    device, private_key = _device(deployment, writer)
    with deployment.client as client:
        assert _refresh(client, device, private_key).status_code == 200
        revoked = client.post(
            _revoke_path(device.credential_id),
            headers=_revoke_headers(writer, device.credential_id),
        )
    assert revoked.status_code == 200, revoked.text
    assert revoked.json() == {"revoked": True}

    deployment.restart()
    with deployment.client as client:
        _assert_not_found(
            _refresh(client, device, private_key, nonce="after-restart")
        )
        listing = client.get(LIST_PATH, headers=_list_headers(writer, nonce="l2"))
    assert listing.json()["devices"][0]["revoked"] is True


def test_a_revoked_device_fails_exactly_as_an_unknown_one_does(deployment):
    """Refresh *and* reads, byte for byte identical to a stranger's."""

    writer = _rider(deployment)
    device, private_key = _device(deployment, writer)
    unknown, unknown_key = _device(deployment, writer)
    assert deployment.state.credentials.revoke_device(unknown.credential_id)

    with deployment.client as client:
        refreshed = _refresh(client, device, private_key)
        assert refreshed.status_code == 200
        token = refreshed.json()["reader_context"]
        # The context works right up until the revocation lands.
        assert client.get(
            "/api/v1/context", headers=_reader_headers(token)
        ).status_code == 200

        assert client.post(
            _revoke_path(device.credential_id),
            headers=_revoke_headers(writer, device.credential_id),
        ).status_code == 200

        revoked_refresh = _refresh(client, device, private_key, nonce="r2")
        unknown_refresh = _refresh(client, unknown, unknown_key, nonce="r3")
        # A reader context minted before the revocation dies with the device;
        # five more minutes of reads would be a delay, not a revocation.
        revoked_read = client.get(
            "/api/v1/context", headers=_reader_headers(token)
        )
        unknown_read = client.get(
            "/api/v1/context", headers=_reader_headers("no-such-context-token")
        )

    for response in (revoked_refresh, unknown_refresh, revoked_read, unknown_read):
        _assert_not_found(response)
    assert revoked_refresh.text == unknown_refresh.text
    assert revoked_read.text == unknown_read.text
    assert dict(revoked_refresh.headers) == dict(unknown_refresh.headers)


def test_a_device_may_revoke_itself_and_a_sibling(deployment):
    """The iPad revokes the lost iPhone without the desktop in the room."""

    writer = _rider(deployment)
    lost, lost_key = _device(deployment, writer, label="lost iPhone")
    ipad, ipad_key = _device(deployment, writer, label="iPad")
    with deployment.client as client:
        sibling = client.post(
            _revoke_path(lost.credential_id),
            headers=_revoke_headers(
                ipad, lost.credential_id, signer=_device_signer(ipad_key)
            ),
        )
        assert sibling.status_code == 200, sibling.text
        _assert_not_found(_refresh(client, lost, lost_key))
        itself = client.post(
            _revoke_path(ipad.credential_id),
            headers=_revoke_headers(
                ipad, ipad.credential_id, nonce="self-1",
                signer=_device_signer(ipad_key),
            ),
        )
        assert itself.status_code == 200, itself.text
        _assert_not_found(_refresh(client, ipad, ipad_key, nonce="after-self"))


def test_revocation_is_idempotent_and_says_nothing_extra_the_second_time(deployment):
    writer = _rider(deployment)
    device, _private = _device(deployment, writer)
    with deployment.client as client:
        first = client.post(
            _revoke_path(device.credential_id),
            headers=_revoke_headers(writer, device.credential_id),
        )
        second = client.post(
            _revoke_path(device.credential_id),
            headers=_revoke_headers(writer, device.credential_id, nonce="again"),
        )
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json() == {"revoked": True}


def test_a_writer_credential_is_not_revocable_through_the_device_route(deployment):
    """A stolen phone must never be able to cut the desktop off from sync."""

    writer = _rider(deployment)
    device, private_key = _device(deployment, writer)
    with deployment.client as client:
        attempt = client.post(
            _revoke_path(writer.credential_id),
            headers=_revoke_headers(
                device, writer.credential_id, signer=_device_signer(private_key)
            ),
        )
    _assert_not_found(attempt)
    assert deployment.state.credentials.resolve_writer(
        writer.credential_id
    ) is not None


# ---------------------------------------------------------------------------
# Cross-namespace: 404, never 403
# ---------------------------------------------------------------------------


def test_a_cross_namespace_revoke_is_404_and_leaves_the_device_alone(deployment):
    """403 would confirm the credential exists.  404 says nothing at all."""

    mine = _rider(deployment, seed=b"a")
    theirs = _rider(deployment, seed=b"b")
    their_device, their_key = _device(deployment, theirs, label="their phone")

    with deployment.client as client:
        attempt = client.post(
            _revoke_path(their_device.credential_id),
            headers=_revoke_headers(mine, their_device.credential_id),
        )
        unknown = client.post(
            _revoke_path("f" * 64),
            headers=_revoke_headers(mine, "f" * 64, nonce="unknown"),
        )
    _assert_not_found(attempt)
    _assert_not_found(unknown)
    assert attempt.text == unknown.text
    assert dict(attempt.headers) == dict(unknown.headers)
    # And it really is untouched.
    assert deployment.state.credentials.resolve_device(
        their_device.credential_id
    ) is not None
    with deployment.client as client:
        assert _refresh(client, their_device, their_key).status_code == 200


def test_no_id_guess_or_header_reaches_another_namespace(deployment):
    """Nothing a caller can send names a scope; the credential decides.

    Every one of these is a way somebody might try to point the route
    somewhere else: a signature over the target's namespace, headers naming
    another credential or subject, and a body carrying the fields the wire
    format elsewhere in this API deliberately parses and discards.
    """

    mine = _rider(deployment, seed=b"a")
    theirs = _rider(deployment, seed=b"b")
    my_device, my_key = _device(deployment, mine)
    their_device, their_key = _device(deployment, theirs)
    path = _revoke_path(their_device.credential_id)

    attempts = {
        # Sign the canonical request over the victim's namespace.
        "foreign-namespace": _revoke_headers(
            mine, their_device.credential_id, nonce="x1",
            namespace=theirs.namespace,
        ),
        # Claim the victim's credential id in the header, sign with our key.
        "borrowed-credential-header": {
            **_revoke_headers(mine, their_device.credential_id, nonce="x2"),
            "X-Writer-Credential": their_device.credential_id,
        },
        # Present the victim's subscription key alongside our own credential.
        "borrowed-subscription": {
            **_revoke_headers(mine, their_device.credential_id, nonce="x3"),
            "Ocp-Apim-Subscription-Key": their_device.subscription_key.decode(),
        },
        # Invent scope headers the app has never read.
        "invented-scope-headers": {
            **_revoke_headers(mine, their_device.credential_id, nonce="x4"),
            "X-Namespace": theirs.namespace,
            "X-Local-User-Scope": theirs.local_user_scope,
            "X-Installation-Id": new_installation_id(),
            "X-Verified-Entra-Subject": "somebody-else",
        },
    }
    with deployment.client as client:
        for name, headers in attempts.items():
            response = client.post(path, headers=headers)
            assert response.status_code in (401, 404), f"{name}: {response.text}"
            if response.status_code == 404:
                _assert_not_found(response)
        # A body naming the victim is parsed by nothing.
        body = json.dumps({
            "namespace": theirs.namespace,
            "local_user_scope": theirs.local_user_scope,
            "installation_id": new_installation_id(),
            "credential_id": their_device.credential_id,
        }).encode()
        _assert_not_found(client.post(
            path,
            headers=_revoke_headers(
                mine, their_device.credential_id, nonce="x5", body=body
            ),
            content=body,
        ))
        # Untouched throughout.
        assert _refresh(client, their_device, their_key).status_code == 200
        assert _refresh(client, my_device, my_key, nonce="mine-ok").status_code == 200


def test_a_revoked_credential_cannot_revoke_anything(deployment):
    writer = _rider(deployment)
    attacker, attacker_key = _device(deployment, writer, label="stolen")
    victim, victim_key = _device(deployment, writer, label="still mine")
    assert deployment.state.credentials.revoke_device(attacker.credential_id)

    with deployment.client as client:
        response = client.post(
            _revoke_path(victim.credential_id),
            headers=_revoke_headers(
                attacker, victim.credential_id,
                signer=_device_signer(attacker_key),
            ),
        )
        assert response.status_code == 401, response.text
        listing = client.get(
            LIST_PATH,
            headers=_list_headers(attacker, signer=_device_signer(attacker_key)),
        )
        assert listing.status_code == 401, listing.text
        assert _refresh(client, victim, victim_key).status_code == 200


def test_a_revoke_request_cannot_be_replayed_or_re_aimed(deployment):
    """The nonce stops the repeat; the path stops the redirection.

    The target credential id is part of the canonical request because it is
    part of the path, so a captured revoke for one device does not verify
    against another's route.
    """

    writer = _rider(deployment)
    first, first_key = _device(deployment, writer, label="first")
    second, second_key = _device(deployment, writer, label="second")

    headers = _revoke_headers(writer, first.credential_id, nonce="once")
    with deployment.client as client:
        assert client.post(
            _revoke_path(first.credential_id), headers=headers
        ).status_code == 200
        # Byte-identical replay of a request that already succeeded.
        assert client.post(
            _revoke_path(first.credential_id), headers=headers
        ).status_code == 401
        # The same signed envelope aimed at the sibling.
        assert client.post(
            _revoke_path(second.credential_id), headers=headers
        ).status_code == 401
        # ... and a signature made for the first device's path, replayed with
        # a fresh nonce against the second, still does not verify.
        re_aimed = _revoke_headers(
            writer, second.credential_id, nonce="re-aimed",
            path=_revoke_path(first.credential_id),
        )
        assert client.post(
            _revoke_path(second.credential_id), headers=re_aimed
        ).status_code == 401
        assert _refresh(client, second, second_key).status_code == 200


def test_the_kill_switch_row_is_not_addressable_as_a_device(deployment):
    """A revoke aimed at the kill switch is a 404 and touches nothing.

    Row kinds are separate address spaces: ``lookup_device`` hashes whatever
    it is given and reads the ``device`` kind, so the kill switch's own key
    cannot name a credential.  Asserted on the *reading* of the switch.
    """

    writer = _rider(deployment)
    deployment.state.quotas.set_public_enabled(False, reason="drill")
    assert read_kill_switch(deployment.backend).public_enabled is False
    deployment.state.quotas.set_public_enabled(True, reason="drill over")

    with deployment.client as client:
        for target in (KILL_SWITCH_KEY, QUOTA_RECORD_KIND, "0" * 64, "nope"):
            response = client.post(
                _revoke_path(target),
                headers=_revoke_headers(
                    writer, target, nonce=f"kill-{target[:8]}"
                ),
            )
            assert response.status_code == 404, response.text
    assert deployment.backend.read(
        KILL_SWITCH_RECORD_KIND, KILL_SWITCH_KEY
    ) is not None
    assert read_kill_switch(deployment.backend).public_enabled is True


# ---------------------------------------------------------------------------
# The sweep, seen from the routes
# ---------------------------------------------------------------------------


def test_the_sweep_runs_on_the_read_plane_and_never_on_the_sync_plane():
    """Only the read identity holds a table delete; only it may sweep."""

    backend = _DurableMemoryBackend()
    read_config = CloudConfig(
        server_secret=SECRET, operator_token="operator-token", plane="read",
        require_apim_proof=False, clock=lambda: 1_000,
    )
    sync_config = CloudConfig(
        server_secret=SECRET, operator_token="operator-token", plane="sync",
        require_apim_proof=False, clock=lambda: 1_000,
    )
    read_state = CloudState.create(read_config, security_backend=backend)
    sync_state = CloudState.create(sync_config, security_backend=backend)
    assert isinstance(read_state.sweeper, ExpiredRecordSweeper)
    assert sync_state.sweeper is None
    assert "device" not in read_state.sweeper.kinds
    assert "writer" not in read_state.sweeper.kinds


def test_sweeping_never_removes_a_live_credential_or_the_kill_switch(deployment):
    """A full pass over a table that has been used, with everything checked.

    The sweep is wired to the refresh path, so this drives it the way the
    deployment does rather than calling it directly.
    """

    writer = _rider(deployment)
    device, private_key = _device(deployment, writer)
    deployment.state.quotas.set_public_enabled(False, reason="budget-100")
    assert read_kill_switch(deployment.backend).public_enabled is False
    deployment.state.quotas.set_public_enabled(True, reason="")

    with deployment.client as client:
        assert _refresh(client, device, private_key).status_code == 200
        stale = deployment.backend.iter_records("context", limit=100)
        assert stale, "the refresh should have written a context to sweep"
        # Move the deployment clock far past every expiry written so far, and
        # past the sweeper's own interval, then take another refresh.
        deployment.clock.value = 1_000_000.0
        assert _refresh(
            client, device, private_key, nonce="post-sweep", timestamp=1_000_000
        ).status_code == 200
        listing = client.get(
            LIST_PATH, headers=_list_headers(writer, nonce="l9", timestamp=1_000_000)
        )

    # The pass really ran: every context and index row written before the
    # clock moved is gone, and only the one the second refresh minted remains.
    survivors = {key for key, _value in deployment.backend.iter_records(
        "context", limit=100
    )}
    assert survivors and not survivors & {key for key, _v in stale}
    assert deployment.backend.iter_records("nonce", limit=100)

    assert listing.status_code == 200, listing.text
    assert [
        entry["credential_id"] for entry in listing.json()["devices"]
    ] == [device.credential_id]
    assert deployment.state.credentials.resolve_writer(writer.credential_id)
    assert deployment.state.credentials.resolve_device(device.credential_id)
    # Asserted on the read, not on the row.
    assert read_kill_switch(deployment.backend).public_enabled is True
    assert read_kill_switch(deployment.backend).writes_enabled is True
    assert deployment.backend.read(
        KILL_SWITCH_RECORD_KIND, KILL_SWITCH_KEY
    ) is not None


def test_the_device_routes_are_absent_from_the_sync_plane():
    config = CloudConfig(
        server_secret=SECRET, operator_token="operator-token", plane="sync",
        require_apim_proof=False, clock=lambda: 1_000,
    )
    state = CloudState.create(config)
    with TestClient(create_cloud_app(config, state=state)) as client:
        assert client.get(LIST_PATH).status_code == 404
        assert client.post(_revoke_path("a" * 64)).status_code == 404


def test_an_exhausted_read_quota_still_lets_a_rider_revoke():
    """Refusing a revocation for quota would be a security failure.

    The daily read counters exist to bound a bill. A rider whose phone is
    gone has already lost the argument if the answer is "come back tomorrow",
    so the revoke route is metered after the fact rather than admitted before
    it -- the same order pairing uses, and for a stronger reason. Listing is
    an ordinary read and is refused normally; revocation by id does not need
    it.
    """

    pytest.importorskip("cryptography")
    backend = _DurableMemoryBackend()
    config = CloudConfig(
        server_secret=SECRET, operator_token="operator-token", plane="read",
        require_apim_proof=False, clock=_Clock(1_000.0),
    )
    state = CloudState.create(
        config,
        security_backend=backend,
        quotas=QuotaManager(QuotaPolicy(max_read_requests_per_day=1)),
    )
    deployment = _Deployment(config, backend, config.clock, state)
    writer = _rider(deployment)
    device, private_key = _device(deployment, writer)

    with deployment.client as client:
        # Burn the day's single read request.
        assert client.get(LIST_PATH, headers=_list_headers(writer)).status_code == 200
        exhausted = client.get(LIST_PATH, headers=_list_headers(writer, nonce="l2"))
        assert exhausted.status_code == 429, exhausted.text
        # The revocation still lands, and it really took effect.
        revoked = client.post(
            _revoke_path(device.credential_id),
            headers=_revoke_headers(writer, device.credential_id),
        )
        assert revoked.status_code == 200, revoked.text
        _assert_not_found(_refresh(client, device, private_key))
