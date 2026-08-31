import hashlib
import json

import pytest
from fastapi.testclient import TestClient

from wattracker.cloud.api import CloudConfig, CloudState, create_cloud_app
from wattracker.cloud.models import CloudObject, SyncBatch
from wattracker.cloud.security import (
    MIN_REPLAY_TTL_SECONDS,
    MemorySecurityStateBackend,
    canonical_request,
    digest_body,
    generate_p256_keypair,
    generate_signing_keypair,
    new_installation_id,
    sign_request,
    sign_request_ecdsa_p256,
    sign_request_ed25519,
)
from wattracker.cloud.storage import MemoryTenantStore


SECRET = b"cloud-test-server-secret-32-bytes-long"


@pytest.fixture()
def cloud():
    config = CloudConfig(
        server_secret=SECRET,
        operator_token="operator-token",
        require_apim_proof=False,
        clock=lambda: 1_000,
    )
    state = CloudState.create(config)
    return config, state, TestClient(create_cloud_app(config, state=state))


def _writer(state, seed: bytes, scope: str = "scope"):
    return state.credentials.register_writer(
        new_installation_id(), scope, seed * 32, seed[::-1] * 32
    )


def _headers(
    writer, body: bytes, *, nonce="nonce", revision=1, idem="batch-1", timestamp=1_000
):
    namespace = writer.namespace
    canonical = canonical_request(
        "POST", "/api/v1/sync/batches", namespace, timestamp, nonce,
        digest_body(body), idem, str(revision),
    )
    return {
        "Authorization": "Writer " + writer.credential_id,
        "Ocp-Apim-Subscription-Key": writer.subscription_key.decode(),
        "X-APIM-Client-Certificate-Verified": "true",
        "X-Writer-Credential": writer.credential_id,
        "X-Writer-Timestamp": str(timestamp),
        "X-Writer-Nonce": nonce,
        "X-Writer-Idempotency-Key": idem,
        "X-Writer-Revision": str(revision),
        "X-Writer-Signature": sign_request(writer.signing_key, canonical),
    }


def _status_headers(writer, *, nonce="status-nonce", revision=0,
                    idem="status-1", timestamp=1_000):
    canonical = canonical_request(
        "GET", "/api/v1/sync/status", writer.namespace, timestamp, nonce,
        digest_body(b""), idem, str(revision),
    )
    return {
        "Ocp-Apim-Subscription-Key": writer.subscription_key.decode(),
        "X-APIM-Client-Certificate-Verified": "true",
        "X-Writer-Credential": writer.credential_id,
        "X-Writer-Timestamp": str(timestamp),
        "X-Writer-Nonce": nonce,
        "X-Writer-Idempotency-Key": idem,
        "X-Writer-Revision": str(revision),
        "X-Writer-Signature": sign_request(writer.signing_key, canonical),
    }


def _batch(*, installation_id="caller-selected", scope="caller-selected", revision=1,
           batch_id=None):
    return json.dumps({
        "batch_id": batch_id or f"batch-{revision}",
        "revision": revision,
        "installation_id": installation_id,
        "local_user_scope": scope,
        "partition_key": "other-tenant",
        "objects": [{
            "id": "activity-1",
            "kind": "activity",
            "revision": revision,
            "data": {"duration_s": 10, "watts": 250},
        }],
    }, separators=(",", ":")).encode()


def test_anonymous_and_reader_credentials_cannot_write(cloud):
    _config, state, client = cloud
    writer = _writer(state, b"a")
    body = _batch()
    assert client.post("/api/v1/sync/batches", content=body).status_code == 401
    context_token, _ = state.credentials.issue_reader_context(
        new_installation_id(), "reader-scope", "entra-user"
    )
    reader_headers = {
        "Authorization": f"Bearer {context_token}",
        "X-Verified-Entra-Subject": "entra-user",
        "X-APIM-Request-Verified": "true",
        "X-APIM-Client-Certificate-Verified": "true",
    }
    assert client.post("/api/v1/sync/batches", headers=reader_headers, content=body).status_code == 401
    assert client.get("/api/v1/context", headers=reader_headers).status_code == 200
    assert writer is not None


def test_writer_scope_ignores_caller_installation_and_partition_fields(cloud):
    _config, state, client = cloud
    writer_a = _writer(state, b"a")
    writer_b = _writer(state, b"b")
    body = _batch(installation_id="installation-b", scope="scope-b")
    response = client.post(
        "/api/v1/sync/batches", headers=_headers(writer_a, body), content=body
    )
    assert response.status_code == 200
    # A reader context is explicitly issued for the same derived scope.
    context_a, _ = state.credentials.issue_reader_context_for_scope(
        writer_a.namespace, writer_a.local_user_scope, "reader-a"
    )
    assert client.get(
        "/api/v1/context/activities", headers={
            "Authorization": f"Bearer {context_a}",
            "X-Verified-Entra-Subject": "reader-a",
            "X-APIM-Request-Verified": "true",
            "X-APIM-Client-Certificate-Verified": "true",
        }
    ).json()["items"][0]["id"] == "activity-1"
    context_b, _ = state.credentials.issue_reader_context_for_scope(
        writer_b.namespace, writer_b.local_user_scope, "reader-b"
    )
    assert client.get(
        "/api/v1/context/activities", headers={
            "Authorization": f"Bearer {context_b}",
            "X-Verified-Entra-Subject": "reader-b",
            "X-APIM-Request-Verified": "true",
            "X-APIM-Client-Certificate-Verified": "true",
        }
    ).json()["items"] == []


def test_signature_tampering_replay_and_stale_revision_are_rejected(cloud):
    _config, state, client = cloud
    writer = _writer(state, b"c")
    body = _batch(revision=1)
    headers = _headers(writer, body, nonce="one", revision=1, idem="batch-1")
    assert client.post("/api/v1/sync/batches", headers=headers, content=body).status_code == 200
    tampered = dict(headers)
    tampered["X-Writer-Signature"] = "0" * 64
    assert client.post("/api/v1/sync/batches", headers=tampered, content=body).status_code == 401
    assert client.post("/api/v1/sync/batches", headers=headers, content=body).status_code == 401
    replay = _headers(writer, body, nonce="two", revision=1, idem="batch-1")
    assert client.post("/api/v1/sync/batches", headers=replay, content=body).json()["replayed"] is True
    stale_body = _batch(revision=1, batch_id="batch-stale")
    stale = _headers(writer, stale_body, nonce="three", revision=1, idem="batch-stale")
    assert client.post("/api/v1/sync/batches", headers=stale, content=stale_body).status_code == 409


def test_status_requires_a_signed_writer_request_and_rejects_replay(cloud):
    _config, state, client = cloud
    writer = _writer(state, b"s")
    unsigned = {
        "X-Writer-Credential": writer.credential_id,
        "Ocp-Apim-Subscription-Key": writer.subscription_key.decode(),
    }
    assert client.get("/api/v1/sync/status", headers=unsigned).status_code == 401

    headers = _status_headers(writer)
    assert client.get("/api/v1/sync/status", headers=headers).status_code == 200
    tampered = dict(headers)
    tampered["X-Writer-Signature"] = "0" * 64
    assert client.get("/api/v1/sync/status", headers=tampered).status_code == 401
    assert client.get("/api/v1/sync/status", headers=headers).status_code == 401


def test_replay_pruning_uses_server_clock_for_writers_at_window_edges(cloud):
    _config, state, client = cloud
    writer = _writer(state, b"e")
    body = _batch()
    first = _headers(writer, body, nonce="same", timestamp=700)
    second = _headers(writer, body, nonce="same", timestamp=1_300)
    assert client.post("/api/v1/sync/batches", headers=first, content=body).status_code == 200
    assert client.post("/api/v1/sync/batches", headers=second, content=body).status_code == 401


def test_future_skewed_nonce_is_not_pruned_early():
    # A request bearing a far-future timestamp must not become the guard's
    # notion of "now": doing so would evict an earlier entry that is still
    # inside its TTL and re-open that nonce for replay.
    current = [1_000.0]
    config = CloudConfig(
        server_secret=SECRET,
        operator_token="operator-token",
        require_apim_proof=False,
        clock=lambda: current[0],
    )
    state = CloudState.create(config)
    writer = _writer(state, b"f")
    victim_body = _batch(revision=1, batch_id="victim-1")
    skewed_body = _batch(revision=2, batch_id="skewed-1")

    with TestClient(create_cloud_app(config, state=state)) as client:
        victim = _headers(writer, victim_body, nonce="victim-nonce",
                          idem="victim-1", timestamp=1_000)
        assert client.post(
            "/api/v1/sync/batches", headers=victim, content=victim_body
        ).status_code == 200
        # The victim entry expires at 1_000 + MIN_REPLAY_TTL_SECONDS == 1_600.
        current[0] = 1_301.0
        skewed = _headers(writer, skewed_body, nonce="skewed-nonce",
                          idem="skewed-1", revision=2, timestamp=1_600)
        assert client.post(
            "/api/v1/sync/batches", headers=skewed, content=skewed_body
        ).status_code == 200
        replay = _headers(writer, victim_body, nonce="victim-nonce",
                          idem="victim-1", timestamp=1_301)
        assert client.post(
            "/api/v1/sync/batches", headers=replay, content=victim_body
        ).status_code == 401


@pytest.mark.parametrize(
    "path,headers",
    [
        ("/api/v1/context", {"X-APIM-Request-Proof": b"\xff"}),
        ("/api/v1/enrollment/start", {"X-Operator-Token": b"\xff"}),
        ("/api/v1/context", {"Authorization": "Bearer token", "X-Verified-Entra-Subject": b"\xff"}),
    ],
)
def test_malformed_non_ascii_auth_headers_fail_closed(path, headers):
    config = CloudConfig(
        server_secret=SECRET,
        operator_token="operator-token",
        require_apim_proof="X-APIM-Request-Proof" in headers,
        apim_proof_value="private-proof" if "X-APIM-Request-Proof" in headers else "",
    )
    state = CloudState.create(config)
    token, _ = state.credentials.issue_reader_context(
        new_installation_id(), "scope", "subject"
    )
    if path == "/api/v1/context" and "Authorization" in headers:
        headers = {**headers, "Authorization": f"Bearer {token}"}
    with TestClient(create_cloud_app(config, state=state)) as client:
        response = client.post(path, headers=headers) if "enrollment" in path else client.get(path, headers=headers)
    assert response.status_code in {401, 404}


def test_revoked_context_is_indistinguishable_from_unknown(cloud):
    _config, state, client = cloud
    token, context = state.credentials.issue_reader_context(
        new_installation_id(), "scope", "subject"
    )
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Verified-Entra-Subject": "subject",
        "X-APIM-Request-Verified": "true",
        "X-APIM-Client-Certificate-Verified": "true",
    }
    assert client.get("/api/v1/context", headers=headers).status_code == 200
    state.credentials.revoke_reader(context.context_id)
    revoked = client.get("/api/v1/context", headers=headers)
    missing = client.get(
        "/api/v1/context", headers={"Authorization": "Bearer " + "x" * 40}
    )
    assert revoked.status_code == missing.status_code == 404
    assert revoked.json() == missing.json() == {"detail": "not found"}
    assert revoked.headers["cache-control"] == missing.headers["cache-control"] == "no-store"


def test_reader_context_requires_the_apim_verified_subject_proof_by_default():
    config = CloudConfig(server_secret=SECRET, operator_token="operator-token")
    state = CloudState.create(config)
    token, _ = state.credentials.issue_reader_context(
        new_installation_id(), "scope", "subject"
    )
    with TestClient(create_cloud_app(config, state=state)) as client:
        response = client.get(
            "/api/v1/context",
            headers={
                "Authorization": f"Bearer {token}",
                "X-Verified-Entra-Subject": "subject",
            },
        )
    assert response.status_code == 404


def test_configured_apim_proof_cannot_be_forged_with_boolean_marker():
    config = CloudConfig(
        server_secret=SECRET,
        operator_token="operator-token",
        apim_proof_value="private-proof",
    )
    state = CloudState.create(config)
    token, _ = state.credentials.issue_reader_context(
        new_installation_id(), "scope", "subject"
    )
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Verified-Entra-Subject": "subject",
        "X-APIM-Client-Certificate-Verified": "true",
    }
    with TestClient(create_cloud_app(config, state=state)) as client:
        assert client.get(
            "/api/v1/context",
            headers={**headers, "X-APIM-Request-Proof": "true"},
        ).status_code == 404
        assert client.get(
            "/api/v1/context",
            headers={**headers, "X-APIM-Request-Proof": "private-proof"},
        ).status_code == 200


def test_empty_apim_proof_configuration_never_accepts_boolean_marker():
    config = CloudConfig(server_secret=SECRET, operator_token="operator-token")
    state = CloudState.create(config)
    token, _ = state.credentials.issue_reader_context(
        new_installation_id(), "scope", "subject"
    )
    with TestClient(create_cloud_app(config, state=state)) as client:
        response = client.get(
            "/api/v1/context",
            headers={
                "Authorization": f"Bearer {token}",
                "X-Verified-Entra-Subject": "subject",
                "X-APIM-Request-Proof": "true",
            },
        )
    assert response.status_code == 404


@pytest.mark.parametrize("header", ["X-APIM-Request-Proof", "X-Verified-Entra-Subject"])
def test_non_ascii_reader_auth_headers_fail_closed(header):
    config = CloudConfig(
        server_secret=SECRET,
        operator_token="operator-token",
        apim_proof_value="private-proof",
    )
    state = CloudState.create(config)
    token, _ = state.credentials.issue_reader_context(
        new_installation_id(), "scope", "subject"
    )
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Verified-Entra-Subject": "subject",
        "X-APIM-Request-Proof": "private-proof",
    }
    headers[header] = b"\xe9"
    with TestClient(create_cloud_app(config, state=state)) as client:
        response = client.get("/api/v1/context", headers=headers)
    assert response.status_code == 404


def test_non_ascii_operator_token_header_fails_closed():
    config = CloudConfig(
        server_secret=SECRET,
        operator_token="operator-token",
        require_apim_proof=False,
    )
    with TestClient(create_cloud_app(config)) as client:
        response = client.post(
            "/api/v1/enrollment/start",
            headers={"X-Operator-Token": b"\xe9"},
        )
    assert response.status_code == 404


def test_limits_and_read_plane_surface(cloud):
    config, state, client = cloud
    writer = _writer(state, b"d")
    state.quotas.set_writes_enabled(False)
    body = _batch()
    assert client.post("/api/v1/sync/batches", headers=_headers(writer, body), content=body).status_code == 403

    read_config = CloudConfig(
        server_secret=SECRET, operator_token="operator-token", plane="read"
    )
    read_app = create_cloud_app(read_config)
    assert not any(route.path == "/api/v1/sync/batches" for route in read_app.routes)
    assert not any(route.path == "/api/v1/sync/status" for route in read_app.routes)
    assert config.plane == "all"


def test_local_app_is_not_modified_or_cloud_dependent():
    from wattracker.server import create_app

    app = create_app()
    assert not any(route.path.startswith("/api/v1/") for route in app.routes)


def test_enrollment_is_operator_only_one_time_and_returns_opaque_credentials(cloud):
    _config, state, client = cloud
    assert client.post("/api/v1/enrollment/start").status_code == 404
    started = client.post(
        "/api/v1/enrollment/start",
        headers={
            "X-Operator-Token": "operator-token",
            "X-Verified-Entra-Subject": "entra-user",
            "X-APIM-Client-Certificate-Verified": "true",
        },
    )
    assert started.status_code == 200
    invitation = started.json()["invitation"]
    assert client.post(
        "/api/v1/enrollment/complete",
        headers={
            "X-Verified-Entra-Subject": "other-user",
            "X-APIM-Request-Verified": "true",
            "X-APIM-Client-Certificate-Verified": "true",
        },
        json={"invitation": invitation, "public_key": (b"e" * 32).hex()},
    ).status_code == 404
    completed = client.post(
        "/api/v1/enrollment/complete",
        headers={
            "X-Verified-Entra-Subject": "entra-user",
            "X-APIM-Request-Verified": "true",
            "X-APIM-Client-Certificate-Verified": "true",
        },
        json={"invitation": invitation, "public_key": (b"e" * 32).hex()},
    )
    assert completed.status_code == 200
    payload = completed.json()
    assert payload["credential"] and payload["subscription_key"] and payload["reader_context"]
    assert client.post(
        "/api/v1/enrollment/complete",
        headers={
            "X-Verified-Entra-Subject": "entra-user",
            "X-APIM-Request-Verified": "true",
            "X-APIM-Client-Certificate-Verified": "true",
        },
        json={"invitation": invitation, "public_key": (b"e" * 32).hex()},
    ).status_code == 404


def test_read_enrollment_is_usable_by_restarted_sync_and_read_planes():
    pytest.importorskip("cryptography")
    backend = MemorySecurityStateBackend()
    read_config = CloudConfig(
        server_secret=SECRET,
        operator_token="operator-token",
        plane="read",
        require_apim_proof=False,
        clock=lambda: 1_000,
    )
    read_state = CloudState.create(read_config, security_backend=backend)
    private_key, public_key = generate_signing_keypair()
    with TestClient(create_cloud_app(read_config, state=read_state)) as client:
        started = client.post(
            "/api/v1/enrollment/start",
            headers={
                "X-Operator-Token": "operator-token",
                "X-Verified-Entra-Subject": "entra-user",
                "X-APIM-Client-Certificate-Verified": "true",
            },
        )
        completed = client.post(
            "/api/v1/enrollment/complete",
            headers={
                "X-Verified-Entra-Subject": "entra-user",
                "Ocp-Apim-Subscription-Key": "apim-subscription",
                "X-APIM-Client-Certificate-Verified": "true",
            },
            json={
                "invitation": started.json()["invitation"],
                "public_key": public_key.hex(),
            },
        )
    assert completed.status_code == 200
    enrolled = completed.json()
    assert enrolled["subscription_key"]
    assert enrolled["subscription_key"] != "apim-subscription"

    sync_config = CloudConfig(
        server_secret=SECRET,
        operator_token="operator-token",
        plane="sync",
        require_apim_proof=False,
        clock=lambda: 1_000,
    )
    sync_state = CloudState.create(sync_config, security_backend=backend)
    writer = sync_state.credentials.authenticate_writer(
        enrolled["credential"], enrolled["subscription_key"]
    )
    assert writer is not None
    assert writer.signature_algorithm == "ed25519"
    body = _batch(revision=1, batch_id="restart-batch")
    canonical = canonical_request(
        "POST", "/api/v1/sync/batches", writer.namespace, 1_000, "restart",
        digest_body(body), "restart-batch", "1",
    )
    headers = {
        "Ocp-Apim-Subscription-Key": enrolled["subscription_key"],
        "X-APIM-Client-Certificate-Verified": "true",
        "X-Writer-Credential": enrolled["credential"],
        "X-Writer-Timestamp": "1000",
        "X-Writer-Nonce": "restart",
        "X-Writer-Idempotency-Key": "restart-batch",
        "X-Writer-Revision": "1",
        "X-Writer-Signature": sign_request_ed25519(private_key, canonical),
    }
    with TestClient(create_cloud_app(sync_config, state=sync_state)) as sync_client:
        uploaded = sync_client.post(
            "/api/v1/sync/batches", headers=headers, content=body
        )
    assert uploaded.status_code == 200, uploaded.text
    assert uploaded.json() == {"accepted": 1, "revision": 1, "replayed": False}

    restarted_read = CloudState.create(read_config, security_backend=backend)
    assert restarted_read.credentials.resolve_reader(
        enrolled["reader_context"], now=1_001
    ) is not None


def test_production_state_requires_durable_security_backend():
    config = CloudConfig(server_secret=SECRET, operator_token="operator-token")
    with pytest.raises(RuntimeError, match="durable auth state"):
        CloudState.create(config, require_persistent_security=True)


def test_cloud_config_rejects_replay_ttl_shorter_than_freshness_window():
    with pytest.raises(ValueError, match="replay TTL"):
        CloudConfig(
            server_secret=SECRET,
            operator_token="operator-token",
            replay_ttl_seconds=MIN_REPLAY_TTL_SECONDS - 1,
        )


# ---------------------------------------------------------------------------
# Paired device credentials and POST /api/v1/context/refresh
# ---------------------------------------------------------------------------


READER_HEADERS = {
    "X-Verified-Entra-Subject": "entra-user",
    "X-APIM-Request-Verified": "true",
    "X-APIM-Client-Certificate-Verified": "true",
}


def _device(state, *, scope="scope", algorithm="ed25519", capabilities=("read",),
            subject="entra-user", installation_id=None):
    pytest.importorskip("cryptography")
    if algorithm == "ed25519":
        private_key, public_key = generate_signing_keypair()
    else:
        private_key, public_key = generate_p256_keypair()
    device = state.credentials.register_device(
        installation_id or new_installation_id(), scope, public_key,
        signature_algorithm=algorithm, capabilities=capabilities, subject=subject,
    )
    return device, private_key


def _sign(device, private_key, canonical):
    if device.signature_algorithm == "ed25519":
        return sign_request_ed25519(private_key, canonical)
    return sign_request_ecdsa_p256(private_key, canonical)


def _refresh_headers(device, private_key, *, nonce="refresh-1", timestamp=1_000,
                     namespace=None, subject="entra-user", body=b""):
    canonical = canonical_request(
        "POST", "/api/v1/context/refresh", namespace or device.namespace,
        timestamp, nonce, digest_body(body), "context-refresh", "",
    )
    return {
        "X-Device-Credential": device.credential_id,
        "X-Device-Timestamp": str(timestamp),
        "X-Device-Nonce": nonce,
        "X-Device-Signature": _sign(device, private_key, canonical),
        "X-Verified-Entra-Subject": subject,
        "X-APIM-Request-Verified": "true",
        "X-APIM-Client-Certificate-Verified": "true",
    }


def _device_sync_headers(device, private_key, body, *, nonce="device-nonce",
                         revision=1, idem="batch-1", timestamp=1_000):
    canonical = canonical_request(
        "POST", "/api/v1/sync/batches", device.namespace, timestamp, nonce,
        digest_body(body), idem, str(revision),
    )
    return {
        "Ocp-Apim-Subscription-Key": device.subscription_key.decode("ascii"),
        "X-APIM-Client-Certificate-Verified": "true",
        "X-Writer-Credential": device.credential_id,
        "X-Writer-Timestamp": str(timestamp),
        "X-Writer-Nonce": nonce,
        "X-Writer-Idempotency-Key": idem,
        "X-Writer-Revision": str(revision),
        "X-Writer-Signature": _sign(device, private_key, canonical),
    }


@pytest.mark.parametrize("algorithm", ["ed25519", "ecdsa-p256-sha256"])
def test_device_refresh_returns_a_usable_reader_context(cloud, algorithm):
    _config, state, client = cloud
    device, private_key = _device(state, algorithm=algorithm)
    response = client.post(
        "/api/v1/context/refresh",
        headers=_refresh_headers(device, private_key),
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["expires_in"] == 300.0
    assert payload["capabilities"] == ["read"]
    assert response.headers["cache-control"] == "no-store"

    token = payload["reader_context"]
    assert client.get(
        "/api/v1/context",
        headers={**READER_HEADERS, "Authorization": f"Bearer {token}"},
    ).status_code == 200

    # A second refresh with a fresh nonce mints a distinct context, which is
    # the whole point: the phone can keep going without the operator token.
    again = client.post(
        "/api/v1/context/refresh",
        headers=_refresh_headers(device, private_key, nonce="refresh-2"),
    )
    assert again.status_code == 200
    assert again.json()["reader_context"] != token


def test_device_refresh_context_is_scoped_to_that_device(cloud):
    _config, state, client = cloud
    pytest.importorskip("cryptography")
    writer = _writer(state, b"r", scope="rider-scope")
    # The device is paired into the writer's already server-derived scope, so
    # the phone reads exactly what the desktop uploaded.
    private_key, public_key = generate_signing_keypair()
    device = state.credentials.register_device_for_scope(
        writer.namespace, writer.local_user_scope, public_key,
        subject="entra-user",
    )
    body = _batch()
    assert client.post(
        "/api/v1/sync/batches", headers=_headers(writer, body), content=body
    ).status_code == 200

    refreshed = client.post(
        "/api/v1/context/refresh", headers=_refresh_headers(device, private_key)
    )
    assert refreshed.status_code == 200
    token = refreshed.json()["reader_context"]
    items = client.get(
        "/api/v1/context/activities",
        headers={**READER_HEADERS, "Authorization": f"Bearer {token}"},
    ).json()["items"]
    assert [item["id"] for item in items] == ["activity-1"]

    # A device paired into a different namespace never sees that scope.
    other_device, other_key = _device(state, scope="other-scope")
    other = client.post(
        "/api/v1/context/refresh",
        headers=_refresh_headers(other_device, other_key, nonce="other-nonce"),
    )
    assert other.status_code == 200
    assert client.get(
        "/api/v1/context/activities",
        headers={
            **READER_HEADERS,
            "Authorization": f"Bearer {other.json()['reader_context']}",
        },
    ).json()["items"] == []


def test_device_refresh_rejects_tampered_stale_and_replayed_envelopes(cloud):
    _config, state, client = cloud
    device, private_key = _device(state)

    tampered = _refresh_headers(device, private_key, nonce="tamper")
    tampered["X-Device-Signature"] = "0" * 128
    assert client.post("/api/v1/context/refresh", headers=tampered).status_code == 404

    # A valid signature over a different namespace than the one the server
    # takes from stored credential state.
    forged_scope = _refresh_headers(
        device, private_key, nonce="forged", namespace="f" * 64
    )
    assert client.post(
        "/api/v1/context/refresh", headers=forged_scope
    ).status_code == 404

    # The signature covers the body digest, so a body added after signing fails.
    signed_empty = _refresh_headers(device, private_key, nonce="bodied")
    assert client.post(
        "/api/v1/context/refresh", headers=signed_empty, content=b"{}"
    ).status_code == 404

    for field in ("X-Device-Credential", "X-Device-Timestamp", "X-Device-Nonce",
                  "X-Device-Signature", "X-Verified-Entra-Subject"):
        incomplete = _refresh_headers(device, private_key, nonce="missing")
        del incomplete[field]
        assert client.post(
            "/api/v1/context/refresh", headers=incomplete
        ).status_code == 404

    # Stale and future-skewed timestamps outside the 300 s freshness window.
    for timestamp in (1_000 - 301, 1_000 + 301):
        stale = _refresh_headers(device, private_key, nonce=f"stale-{timestamp}",
                                 timestamp=timestamp)
        assert client.post(
            "/api/v1/context/refresh", headers=stale
        ).status_code == 404
    edge = _refresh_headers(device, private_key, nonce="edge", timestamp=1_000 - 300)
    assert client.post("/api/v1/context/refresh", headers=edge).status_code == 200

    # A replayed nonce is refused even though the signature is still valid.
    replayed = _refresh_headers(device, private_key, nonce="once")
    assert client.post("/api/v1/context/refresh", headers=replayed).status_code == 200
    assert client.post("/api/v1/context/refresh", headers=replayed).status_code == 404


def test_device_without_the_read_capability_cannot_refresh(cloud):
    """The refresh route's capability assertion must be a live control.

    Nothing issues a device without "read" today, so this constructs one
    directly through the registry.  Without it the assertion at the route is
    unreachable and would read as a control while enforcing nothing -- the
    same standard the write-side assertion is already held to.
    """
    _config, state, client = cloud
    device, private_key = _device(state, capabilities=("write",))
    assert not device.has_capability("read")
    refused = client.post(
        "/api/v1/context/refresh",
        headers=_refresh_headers(device, private_key, nonce="no-read"),
    )
    assert refused.status_code == 404
    assert refused.json() == {"detail": "not found"}

    # The same device, granted "read", is accepted by the same code path.
    allowed_device, allowed_key = _device(state, capabilities=("read", "write"))
    assert client.post(
        "/api/v1/context/refresh",
        headers=_refresh_headers(allowed_device, allowed_key, nonce="with-read"),
    ).status_code == 200


def test_malleated_signature_cannot_replay_a_spent_nonce(cloud):
    """Pin the reason ECDSA malleability is safe here, not the malleability.

    Low-s is deliberately not enforced, because CryptoKit and the Secure
    Enclave emit high-s about half the time.  That is only safe because
    freshness keys on the nonce and never on signature bytes.  If someone
    later makes a signature an identity, this test is what should break.
    """
    pytest.importorskip("cryptography")
    _config, state, client = cloud
    device, private_key = _device(state, algorithm="ecdsa-p256-sha256")
    headers = _refresh_headers(device, private_key, nonce="malleable")
    assert client.post("/api/v1/context/refresh", headers=headers).status_code == 200

    order = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
    signature = headers["X-Device-Signature"]
    r, s = int(signature[:64], 16), int(signature[64:], 16)
    malleated = f"{r:064x}{order - s:064x}"
    assert malleated != signature
    replayed = {**headers, "X-Device-Signature": malleated}
    # A distinct, still-valid signature over the same canonical request --
    # and it buys nothing, because the nonce is already spent.
    assert client.post(
        "/api/v1/context/refresh", headers=replayed
    ).status_code == 404


def test_device_refresh_binds_the_apim_verified_subject(cloud):
    _config, state, client = cloud
    device, private_key = _device(state, subject="entra-user")
    wrong_subject = _refresh_headers(
        device, private_key, nonce="subject", subject="someone-else"
    )
    assert client.post(
        "/api/v1/context/refresh", headers=wrong_subject
    ).status_code == 404


def test_revoked_and_unknown_devices_are_indistinguishable(cloud):
    _config, state, client = cloud
    device, private_key = _device(state)
    assert client.post(
        "/api/v1/context/refresh",
        headers=_refresh_headers(device, private_key, nonce="before"),
    ).status_code == 200

    assert state.credentials.revoke_device(device.credential_id)
    revoked = client.post(
        "/api/v1/context/refresh",
        headers=_refresh_headers(device, private_key, nonce="after"),
    )
    unknown_headers = _refresh_headers(device, private_key, nonce="unknown")
    unknown_headers["X-Device-Credential"] = "f" * 64
    unknown = client.post("/api/v1/context/refresh", headers=unknown_headers)
    missing_context = client.get(
        "/api/v1/context",
        headers={**READER_HEADERS, "Authorization": "Bearer " + "x" * 40},
    )
    assert revoked.status_code == unknown.status_code == 404
    assert missing_context.status_code == 404
    assert revoked.json() == unknown.json() == missing_context.json()
    assert revoked.json() == {"detail": "not found"}
    assert (
        revoked.headers["cache-control"]
        == unknown.headers["cache-control"]
        == missing_context.headers["cache-control"]
        == "no-store"
    )


def test_read_only_device_cannot_write_but_a_granted_one_can(cloud):
    _config, state, client = cloud
    reader_device, reader_key = _device(state)
    body = _batch()
    # Correct subscription key, correct signature, correct envelope: the only
    # thing missing is the "write" capability.
    rejected = client.post(
        "/api/v1/sync/batches",
        headers=_device_sync_headers(reader_device, reader_key, body),
        content=body,
    )
    assert rejected.status_code == 401
    assert rejected.json() == {"detail": "writer authorization required"}

    # The same device with "write" granted is accepted by the same route and
    # the same code path.  Mobile writes are a capability grant, not a rewrite.
    writer_device, writer_key = _device(state, capabilities=("read", "write"))
    accepted = client.post(
        "/api/v1/sync/batches",
        headers=_device_sync_headers(writer_device, writer_key, body,
                                     nonce="granted"),
        content=body,
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json() == {"accepted": 1, "revision": 1, "replayed": False}


def test_device_sync_request_still_needs_its_subscription_and_signature(cloud):
    _config, state, client = cloud
    device, private_key = _device(state, capabilities=("read", "write"))
    body = _batch()
    no_subscription = _device_sync_headers(device, private_key, body)
    del no_subscription["Ocp-Apim-Subscription-Key"]
    assert client.post(
        "/api/v1/sync/batches", headers=no_subscription, content=body
    ).status_code == 401
    wrong_subscription = _device_sync_headers(device, private_key, body,
                                              nonce="wrong-sub")
    wrong_subscription["Ocp-Apim-Subscription-Key"] = "not-the-key"
    assert client.post(
        "/api/v1/sync/batches", headers=wrong_subscription, content=body
    ).status_code == 401
    tampered = _device_sync_headers(device, private_key, body, nonce="tampered")
    tampered["X-Writer-Signature"] = "0" * 128
    assert client.post(
        "/api/v1/sync/batches", headers=tampered, content=body
    ).status_code == 401


def test_reader_context_token_cannot_be_used_to_refresh_itself(cloud):
    _config, state, client = cloud
    token, context = state.credentials.issue_reader_context(
        new_installation_id(), "scope", "entra-user"
    )
    assert client.post(
        "/api/v1/context/refresh",
        headers={**READER_HEADERS, "Authorization": f"Bearer {token}"},
    ).status_code == 404
    # A writer credential is not a device credential either.
    writer = _writer(state, b"w")
    assert client.post(
        "/api/v1/context/refresh",
        headers={**READER_HEADERS, "X-Device-Credential": writer.credential_id},
    ).status_code == 404
    assert context is not None


def test_refresh_is_absent_from_the_sync_plane():
    sync_config = CloudConfig(
        server_secret=SECRET, operator_token="operator-token", plane="sync"
    )
    sync_app = create_cloud_app(sync_config)
    assert not any(
        route.path == "/api/v1/context/refresh" for route in sync_app.routes
    )
    read_config = CloudConfig(
        server_secret=SECRET, operator_token="operator-token", plane="read"
    )
    read_app = create_cloud_app(read_config)
    assert any(
        route.path == "/api/v1/context/refresh" for route in read_app.routes
    )


@pytest.mark.parametrize("algorithm", ["ed25519", "ecdsa-p256-sha256"])
def test_enrollment_pairs_a_device_and_the_device_refreshes_after_restart(algorithm):
    pytest.importorskip("cryptography")
    backend = MemorySecurityStateBackend()
    read_config = CloudConfig(
        server_secret=SECRET,
        operator_token="operator-token",
        plane="read",
        require_apim_proof=False,
        clock=lambda: 1_000,
    )
    read_state = CloudState.create(read_config, security_backend=backend)
    _writer_private, writer_public = generate_signing_keypair()
    if algorithm == "ed25519":
        device_private, device_public = generate_signing_keypair()
    else:
        device_private, device_public = generate_p256_keypair()

    with TestClient(create_cloud_app(read_config, state=read_state)) as client:
        started = client.post(
            "/api/v1/enrollment/start",
            headers={
                "X-Operator-Token": "operator-token",
                "X-Verified-Entra-Subject": "entra-user",
                "X-APIM-Client-Certificate-Verified": "true",
            },
        )
        completed = client.post(
            "/api/v1/enrollment/complete",
            headers={
                "X-Verified-Entra-Subject": "entra-user",
                "X-APIM-Client-Certificate-Verified": "true",
            },
            json={
                "invitation": started.json()["invitation"],
                "public_key": writer_public.hex(),
                "device_public_key": device_public.hex(),
                "device_signature_algorithm": algorithm,
            },
        )
    assert completed.status_code == 200, completed.text
    enrolled = completed.json()
    assert enrolled["device_credential"]
    assert enrolled["device_capabilities"] == ["read"]
    assert enrolled["device_signature_algorithm"] == algorithm
    assert enrolled["device_subscription_key"] != enrolled["subscription_key"]
    assert enrolled["device_credential"] != enrolled["credential"]

    # A restarted read plane, sharing only the durable auth table, accepts the
    # device's signed refresh -- the phone survives a container recycle.
    restarted = CloudState.create(read_config, security_backend=backend)
    device = restarted.credentials.resolve_device(enrolled["device_credential"])
    assert device is not None
    with TestClient(create_cloud_app(read_config, state=restarted)) as client:
        refreshed = client.post(
            "/api/v1/context/refresh",
            headers=_refresh_headers(device, device_private, nonce="restart"),
        )
    assert refreshed.status_code == 200, refreshed.text
    assert restarted.credentials.resolve_reader(
        refreshed.json()["reader_context"], now=1_001
    ) is not None


def test_unpairable_device_key_never_spends_the_invitation(cloud, monkeypatch):
    """A rejected device key must not strand the rider mid-enrollment.

    Device-key validation runs before the one-time invitation is consumed.  If
    it ran after, a deployment missing the crypto extra would spend the
    invitation, create a writer, fail on the device, and leave the rider with
    no way to retry short of a new operator-issued token.
    """
    pytest.importorskip("cryptography")
    from wattracker.cloud import security

    _config, state, client = cloud
    _writer_private, writer_public = generate_signing_keypair()
    _device_private, device_public = generate_p256_keypair()
    started = client.post(
        "/api/v1/enrollment/start",
        headers={
            "X-Operator-Token": "operator-token",
            "X-Verified-Entra-Subject": "entra-user",
            "X-APIM-Client-Certificate-Verified": "true",
        },
    )
    invitation = started.json()["invitation"]

    def _unavailable(algorithm, key):
        raise security.PublicKeyUnavailable("cloud extra is not installed")

    complete_headers = {
        "X-Verified-Entra-Subject": "entra-user",
        "X-APIM-Client-Certificate-Verified": "true",
    }
    payload = {
        "invitation": invitation,
        "public_key": writer_public.hex(),
        "device_public_key": device_public.hex(),
        "device_signature_algorithm": "ecdsa-p256-sha256",
    }
    monkeypatch.setattr(
        "wattracker.cloud.api.validate_public_key", _unavailable
    )
    stranded = client.post(
        "/api/v1/enrollment/complete", headers=complete_headers, json=payload
    )
    assert stranded.status_code == 404
    assert stranded.json() == {"detail": "not found"}

    # Nothing was created and nothing was spent: the same invitation still
    # works once the deployment is fixed.
    monkeypatch.undo()
    retried = client.post(
        "/api/v1/enrollment/complete", headers=complete_headers, json=payload
    )
    assert retried.status_code == 200, retried.text
    assert retried.json()["device_credential"]


def test_enrollment_rejects_a_device_key_that_does_not_match_its_algorithm(cloud):
    pytest.importorskip("cryptography")
    _config, _state, client = cloud
    _writer_private, writer_public = generate_signing_keypair()
    _device_private, ed_device_public = generate_signing_keypair()
    started = client.post(
        "/api/v1/enrollment/start",
        headers={
            "X-Operator-Token": "operator-token",
            "X-Verified-Entra-Subject": "entra-user",
            "X-APIM-Client-Certificate-Verified": "true",
        },
    )
    invitation = started.json()["invitation"]
    mismatched = client.post(
        "/api/v1/enrollment/complete",
        headers={
            "X-Verified-Entra-Subject": "entra-user",
            "X-APIM-Client-Certificate-Verified": "true",
        },
        json={
            "invitation": invitation,
            "public_key": writer_public.hex(),
            # An Ed25519 key declared as a P-256 point.
            "device_public_key": ed_device_public.hex(),
            "device_signature_algorithm": "ecdsa-p256-sha256",
        },
    )
    # Rejected on shape before the one-time invitation is spent, so the same
    # invitation is still usable with a correctly encoded key.
    assert mismatched.status_code == 400
    unsupported = client.post(
        "/api/v1/enrollment/complete",
        headers={
            "X-Verified-Entra-Subject": "entra-user",
            "X-APIM-Client-Certificate-Verified": "true",
        },
        json={
            "invitation": invitation,
            "public_key": writer_public.hex(),
            "device_public_key": ed_device_public.hex(),
            "device_signature_algorithm": "hmac-sha256",
        },
    )
    assert unsupported.status_code == 400

    # Neither rejection consumed the invitation.
    _p256_private, p256_public = generate_p256_keypair()
    accepted = client.post(
        "/api/v1/enrollment/complete",
        headers={
            "X-Verified-Entra-Subject": "entra-user",
            "X-APIM-Client-Certificate-Verified": "true",
        },
        json={
            "invitation": invitation,
            "public_key": writer_public.hex(),
            "device_public_key": p256_public.hex(),
            "device_signature_algorithm": "ecdsa-p256-sha256",
        },
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["device_signature_algorithm"] == "ecdsa-p256-sha256"
