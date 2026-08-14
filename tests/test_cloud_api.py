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
    generate_signing_keypair,
    new_installation_id,
    sign_request,
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
