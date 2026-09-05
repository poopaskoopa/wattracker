import base64
import hashlib
import hmac
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from wattracker.cloud.api import (
    CloudConfig,
    CloudState,
    _cursor_key,
    create_cloud_app,
)
from wattracker.cloud.models import (
    PUBLISHED_OBJECT_KINDS,
    CloudObject,
    SyncBatch,
)
from wattracker.cloud.security import (
    DEVICE_PAIRING_CODE_BITS,
    MAX_DEVICE_PAIRING_TTL_SECONDS,
    MIN_REPLAY_TTL_SECONDS,
    MemorySecurityStateBackend,
    canonical_request,
    digest_body,
    generate_p256_keypair,
    generate_pairing_code,
    generate_signing_keypair,
    new_installation_id,
    sign_request,
    sign_request_ecdsa_p256,
    sign_request_ed25519,
)
from wattracker.cloud.storage import MemoryTenantStore


SECRET = b"cloud-test-server-secret-32-bytes-long"
CLOUD_OBJECT_VECTOR = Path(__file__).parent / "vectors" / "cloud_objects_v1.json"


@pytest.fixture()
def cloud():
    config = CloudConfig(
        server_secret=SECRET,
        operator_token="operator-token",
        require_gateway_proof=False,
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


def _rider_batch(*, object_id, rider, revision):
    return json.dumps({
        "batch_id": f"{rider}-batch-{revision}",
        "revision": revision,
        "installation_id": f"{rider}-caller-installation",
        "local_user_scope": f"{rider}-caller-scope",
        "objects": [{
            "id": object_id,
            "kind": "activity",
            "revision": revision,
            "data": {
                "duration_s": 10,
                "rider_marker": rider,
            },
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


def _mobile_reader(state, *, scope="mobile-scope"):
    token, context = state.credentials.issue_reader_context(
        new_installation_id(), scope, "entra-user"
    )
    return token, context


def _mobile_headers(token):
    return {
        **READER_HEADERS,
        "Authorization": f"Bearer {token}",
    }


def _apply_mobile_batch(state, namespace, scope, batch_id, revision, objects):
    return state.store.apply(
        namespace,
        scope,
        SyncBatch(batch_id=batch_id, revision=revision, objects=tuple(objects)),
    )


def test_mobile_read_surface_routes_filter_kinds_and_expose_revision(cloud):
    _config, state, client = cloud
    token, context = _mobile_reader(state)
    _apply_mobile_batch(
        state,
        context.namespace,
        context.local_user_scope,
        "mobile-kinds",
        7,
        [
            CloudObject("profile", "profile", 1, {"ftp": 250}),
            CloudObject("state", "training_state", 2, {"ctl": 42}),
            CloudObject("load", "load_point", 3, {"date": "2026-08-31"}),
            CloudObject("curve", "curve", 4, {"points": [1, 2]}),
            CloudObject("week", "volume_week", 5, {"tss": 300}),
            CloudObject("activity", "activity", 6, {"duration_s": 60}),
        ],
    )
    headers = _mobile_headers(token)

    dashboard = client.get("/api/v1/context/dashboard", headers=headers)
    assert dashboard.status_code == 200, dashboard.text
    dashboard_body = dashboard.json()
    assert dashboard_body["revision"] == 7
    assert dashboard_body["next_cursor"] is None
    assert {item["kind"] for item in dashboard_body["items"]} == {
        "profile", "training_state", "load_point", "curve",
    }
    assert client.get("/api/v1/context/volume", headers=headers).json() == {
        "items": [{
            "id": "week", "kind": "volume_week", "revision": 5,
            "data": {"tss": 300},
        }],
        "revision": 7,
        "next_cursor": None,
    }
    assert client.get("/api/v1/context/curve", headers=headers).json()["items"] == [{
        "id": "curve", "kind": "curve", "revision": 4,
        "data": {"points": [1, 2]},
    }]
    context_response = client.get("/api/v1/context", headers=headers)
    assert context_response.status_code == 200
    capabilities = context_response.json()["capabilities"]
    assert all(capabilities[name] for name in ("dashboard", "volume", "curve"))
    assert dashboard.headers["cache-control"] == "private, no-store"
    assert dashboard.headers["etag"]


def test_activities_collection_pages_with_cursor_and_revision(cloud):
    _config, state, client = cloud
    token, context = _mobile_reader(state)
    _apply_mobile_batch(
        state,
        context.namespace,
        context.local_user_scope,
        "activity-pages",
        5,
        [
            CloudObject(f"activity-{index}", "activity", 5, {"index": index})
            for index in range(5)
        ],
    )
    headers = _mobile_headers(token)

    first = client.get(
        "/api/v1/context/activities?limit=2", headers=headers
    ).json()
    assert [item["id"] for item in first["items"]] == [
        "activity-0", "activity-1",
    ]
    assert first["revision"] == 5
    assert first["next_cursor"]

    second = client.get(
        "/api/v1/context/activities?limit=2&cursor=" + first["next_cursor"],
        headers=headers,
    ).json()
    assert [item["id"] for item in second["items"]] == [
        "activity-2", "activity-3",
    ]
    assert second["revision"] == 5
    assert second["next_cursor"]

    final = client.get(
        "/api/v1/context/activities?limit=2&cursor=" + second["next_cursor"],
        headers=headers,
    ).json()
    assert [item["id"] for item in final["items"]] == ["activity-4"]
    assert final["revision"] == 5
    assert final["next_cursor"] is None


def test_activities_collection_since_returns_tombstones(cloud):
    _config, state, client = cloud
    token, context = _mobile_reader(state)
    namespace = context.namespace
    scope = context.local_user_scope
    _apply_mobile_batch(
        state,
        namespace,
        scope,
        "activity-before",
        1,
        [CloudObject("activity-1", "activity", 1, {"duration_s": 60})],
    )
    _apply_mobile_batch(
        state,
        namespace,
        scope,
        "activity-delete",
        2,
        [CloudObject("activity-1", "activity", 2, {}, deleted=True)],
    )

    response = client.get(
        "/api/v1/context/activities?since=1", headers=_mobile_headers(token)
    )
    assert response.status_code == 200, response.text
    assert response.json() == {
        "items": [{
            "id": "activity-1",
            "kind": "activity",
            "revision": 2,
            "data": {},
            "deleted": True,
        }],
        "revision": 2,
        "next_cursor": None,
    }


def test_mobile_read_surface_since_returns_tombstones_and_is_scope_local(cloud):
    _config, state, client = cloud
    token, context = _mobile_reader(state, scope="first-scope")
    headers = _mobile_headers(token)
    _apply_mobile_batch(
        state,
        context.namespace,
        context.local_user_scope,
        "mobile-before",
        1,
        [
            CloudObject("profile", "profile", 1, {"ftp": 240}),
            CloudObject("state", "training_state", 1, {"ctl": 30}),
        ],
    )
    full_before = client.get("/api/v1/context/dashboard", headers=headers)
    assert full_before.json()["revision"] == 1
    _apply_mobile_batch(
        state,
        context.namespace,
        context.local_user_scope,
        "mobile-after",
        2,
        [
            CloudObject("profile", "profile", 2, {"ftp": 250}),
            CloudObject("state", "training_state", 2, {}, deleted=True),
        ],
    )

    delta = client.get(
        "/api/v1/context/dashboard?since=1", headers=headers
    )
    assert delta.status_code == 200, delta.text
    delta_body = delta.json()
    assert delta_body["revision"] == 2
    assert [(item["id"], item.get("deleted", False)) for item in delta_body["items"]] == [
        ("profile", False), ("state", True),
    ]
    assert delta_body["items"][0]["data"] == {"ftp": 250}

    # Replaying from an old checkpoint yields the current active state after
    # applying tombstones, exactly as a full fetch does.
    replay = client.get(
        "/api/v1/context/dashboard?since=0", headers=headers
    ).json()
    replayed = {}
    for item in replay["items"]:
        if item.get("deleted"):
            replayed.pop(item["id"], None)
        else:
            replayed[item["id"]] = item
    full_now = client.get("/api/v1/context/dashboard", headers=headers).json()
    assert replayed == {item["id"]: item for item in full_now["items"]}
    assert client.get(
        "/api/v1/context/dashboard?since=2", headers=headers
    ).json() == {"items": [], "revision": 2, "next_cursor": None}

    # A revision from another namespace is not a global clock and cannot
    # expose or suppress objects in this one.
    other_token, other_context = _mobile_reader(state, scope="other-scope")
    _apply_mobile_batch(
        state,
        other_context.namespace,
        other_context.local_user_scope,
        "other-mobile",
        99,
        [CloudObject("other", "profile", 99, {"ftp": 999})],
    )
    assert client.get(
        "/api/v1/context/dashboard?since=99", headers=headers
    ).json() == {"items": [], "revision": 2, "next_cursor": None}
    assert [item["id"] for item in client.get(
        "/api/v1/context/dashboard?since=0",
        headers=_mobile_headers(other_token),
    ).json()["items"]] == ["other"]


def test_mobile_read_surface_cursor_pagination_is_stable_and_bound_to_query(cloud):
    _config, state, client = cloud
    token, context = _mobile_reader(state)
    _apply_mobile_batch(
        state,
        context.namespace,
        context.local_user_scope,
        "mobile-pages",
        1,
        [
            CloudObject("a", "profile", 1, {"ftp": 240}),
            CloudObject("b", "profile", 1, {"ftp": 250}),
            CloudObject("c", "profile", 1, {"ftp": 260}),
        ],
    )
    headers = _mobile_headers(token)
    first = client.get(
        "/api/v1/context/dashboard?limit=1", headers=headers
    ).json()
    assert [item["id"] for item in first["items"]] == ["a"]
    assert first["next_cursor"]
    second = client.get(
        "/api/v1/context/dashboard?limit=1&cursor=" + first["next_cursor"],
        headers=headers,
    ).json()
    assert [item["id"] for item in second["items"]] == ["b"]
    assert second["next_cursor"]
    third = client.get(
        "/api/v1/context/dashboard?limit=1&cursor=" + second["next_cursor"],
        headers=headers,
    ).json()
    assert [item["id"] for item in third["items"]] == ["c"]
    assert third["next_cursor"] is None

    # The cursor is opaque and cannot be replayed against another route or
    # revision query, nor can malformed signed payloads crash the route.
    assert client.get(
        "/api/v1/context/curve?limit=1&cursor=" + first["next_cursor"],
        headers=headers,
    ).status_code == 400
    assert client.get(
        "/api/v1/context/dashboard?since=0&cursor=" + first["next_cursor"],
        headers=headers,
    ).status_code == 400
    assert client.get(
        "/api/v1/context/dashboard?cursor=not-a-cursor", headers=headers
    ).status_code == 400
    assert client.get(
        "/api/v1/context/dashboard?cursor=" + first["next_cursor"],
    ).status_code == 404


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
        require_gateway_proof=False,
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
        ("/api/v1/context", {"X-Gateway-Request-Proof": b"\xff"}),
        ("/api/v1/enrollment/start", {"X-Operator-Token": b"\xff"}),
        ("/api/v1/context", {"Authorization": "Bearer token", "X-Verified-Entra-Subject": b"\xff"}),
    ],
)
def test_malformed_non_ascii_auth_headers_fail_closed(path, headers):
    config = CloudConfig(
        server_secret=SECRET,
        operator_token="operator-token",
        require_gateway_proof="X-Gateway-Request-Proof" in headers,
        gateway_proof_value="private-proof" if "X-Gateway-Request-Proof" in headers else "",
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


def test_configured_gateway_proof_cannot_be_forged_with_boolean_marker():
    config = CloudConfig(
        server_secret=SECRET,
        operator_token="operator-token",
        gateway_proof_value="private-proof",
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
            headers={**headers, "X-Gateway-Request-Proof": "true"},
        ).status_code == 404
        assert client.get(
            "/api/v1/context",
            headers={**headers, "X-Gateway-Request-Proof": "private-proof"},
        ).status_code == 200


def test_empty_gateway_proof_configuration_never_accepts_boolean_marker():
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
                "X-Gateway-Request-Proof": "true",
            },
        )
    assert response.status_code == 404


@pytest.mark.parametrize("header", ["X-Gateway-Request-Proof", "X-Verified-Entra-Subject"])
def test_non_ascii_reader_auth_headers_fail_closed(header):
    config = CloudConfig(
        server_secret=SECRET,
        operator_token="operator-token",
        gateway_proof_value="private-proof",
    )
    state = CloudState.create(config)
    token, _ = state.credentials.issue_reader_context(
        new_installation_id(), "scope", "subject"
    )
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Verified-Entra-Subject": "subject",
        "X-Gateway-Request-Proof": "private-proof",
    }
    headers[header] = b"\xe9"
    with TestClient(create_cloud_app(config, state=state)) as client:
        response = client.get("/api/v1/context", headers=headers)
    assert response.status_code == 404


def test_non_ascii_operator_token_header_fails_closed():
    config = CloudConfig(
        server_secret=SECRET,
        operator_token="operator-token",
        require_gateway_proof=False,
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


def test_enrollment_works_without_a_gateway_and_ignores_subject_headers():
    config = CloudConfig(
        server_secret=SECRET,
        operator_token="operator-token",
        require_gateway_proof=False,
        require_verified_subject=False,
        clock=lambda: 1_000,
    )
    state = CloudState.create(config)
    with TestClient(create_cloud_app(config, state=state)) as client:
        started = client.post(
            "/api/v1/enrollment/start",
            headers={
                "X-Operator-Token": "operator-token",
                "X-Verified-Entra-Subject": "attacker-chosen-subject",
            },
        )
        assert started.status_code == 200, started.text
        completed = client.post(
            "/api/v1/enrollment/complete",
            headers={"X-Verified-Entra-Subject": "different-attacker-subject"},
            json={
                "invitation": started.json()["invitation"],
                "public_key": (b"e" * 32).hex(),
            },
        )
    assert completed.status_code == 200, completed.text


def test_read_enrollment_is_usable_by_restarted_sync_and_read_planes():
    pytest.importorskip("cryptography")
    backend = MemorySecurityStateBackend()
    read_config = CloudConfig(
        server_secret=SECRET,
        operator_token="operator-token",
        plane="read",
        require_gateway_proof=False,
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
        require_gateway_proof=False,
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


def _admin_headers(credential, signer, *, method, path, nonce, idem,
                   subscription=None, timestamp=1_000, revision=0):
    canonical = canonical_request(
        method, path, credential.namespace, timestamp, nonce,
        digest_body(b""), idem, str(revision),
    )
    return {
        "Ocp-Apim-Subscription-Key": (
            subscription
            if subscription is not None
            else credential.subscription_key.decode("ascii")
        ),
        "X-APIM-Client-Certificate-Verified": "true",
        "X-Writer-Credential": credential.credential_id,
        "X-Writer-Timestamp": str(timestamp),
        "X-Writer-Nonce": nonce,
        "X-Writer-Idempotency-Key": idem,
        "X-Writer-Revision": str(revision),
        "X-Writer-Signature": signer(canonical),
        "X-Verified-Entra-Subject": "entra-user",
    }


def _assert_not_found(response):
    assert response.status_code == 404, response.text
    assert response.json() == {"detail": "not found"}
    assert response.headers["cache-control"] == "no-store"


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
        require_gateway_proof=False,
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


# ---------------------------------------------------------------------------
# Desktop-minted device pairing (#152)
# ---------------------------------------------------------------------------

MINT_PATH = "/api/v1/devices/pairing-codes"
PAIR_PATH = "/api/v1/devices/pair"
PAIR_HEADERS = {
    "X-Verified-Entra-Subject": "entra-user",
    "X-APIM-Request-Verified": "true",
    "X-APIM-Client-Certificate-Verified": "true",
}


class _MovableClock:
    """A deployment clock the test can advance.

    The registries capture ``config.clock`` at construction, so reassigning
    the attribute afterwards would not move their clocks.
    """

    def __init__(self, value: float) -> None:
        self.value = float(value)

    def __call__(self) -> float:
        return self.value


def _mint_headers(credential, *, nonce="mint-1", timestamp=1_000,
                  idem="device-pairing-code", revision=0, namespace=None,
                  signer=None, subject="entra-user"):
    canonical = canonical_request(
        "POST", MINT_PATH, namespace or credential.namespace, timestamp, nonce,
        digest_body(b""), idem, str(revision),
    )
    sign = signer or (lambda material: sign_request(credential.signing_key, material))
    headers = {
        "Ocp-Apim-Subscription-Key": credential.subscription_key.decode(),
        "X-APIM-Client-Certificate-Verified": "true",
        "X-Writer-Credential": credential.credential_id,
        "X-Writer-Timestamp": str(timestamp),
        "X-Writer-Nonce": nonce,
        "X-Writer-Idempotency-Key": idem,
        "X-Writer-Revision": str(revision),
        "X-Writer-Signature": sign(canonical),
    }
    if subject is not None:
        headers["X-Verified-Entra-Subject"] = subject
    return headers


def _mint(client, credential, **kwargs):
    return client.post(MINT_PATH, headers=_mint_headers(credential, **kwargs))


def _pair(client, code, public_key, *, subject="entra-user", algorithm="ed25519",
          extra=None, headers=None):
    """Redeem a code.  ``subject=None`` sends no subject header at all."""
    payload = {"code": code, "public_key": public_key.hex(),
               "signature_algorithm": algorithm}
    payload.update(extra or {})
    sent = dict(PAIR_HEADERS if headers is None else headers)
    sent.pop("X-Verified-Entra-Subject", None)
    if subject is not None:
        sent["X-Verified-Entra-Subject"] = subject
    return client.post(PAIR_PATH, headers=sent, json=payload)


@pytest.fixture()
def attested_cloud():
    """A deployment with a real gateway: proof configured, subject attested.

    The plain ``cloud`` fixture turns the proof off, which is fine for routes
    that do not depend on the subject -- but production refuses to boot with a
    subject requirement and no gateway, so anything testing subject trust says
    so explicitly here.
    """
    config = CloudConfig(
        server_secret=SECRET,
        operator_token="operator-token",
        gateway_proof_value="proof-value",
        clock=lambda: 1_000,
    )
    assert config.gateway_attests_subject
    state = CloudState.create(config)
    return config, state, TestClient(create_cloud_app(config, state=state))


def _subject_writer(state, seed, *, scope="scope", subject="entra-user"):
    """A writer carrying a verified subject, as ``enrollment/complete`` mints."""
    writer = _writer(state, seed, scope=scope)
    assert state.credentials.set_writer_subject(writer.credential_id, subject)
    resolved = state.credentials.resolve_writer(writer.credential_id)
    assert resolved.subject == subject
    return resolved


def _paired_device(state, client, writer, *, nonce="mint-1", subject="entra-user"):
    """Mint a code as the desktop and redeem it as a phone."""
    private_key, public_key = generate_signing_keypair()
    minted = _mint(client, writer, nonce=nonce)
    assert minted.status_code == 200, minted.text
    paired = _pair(client, minted.json()["pairing_code"], public_key, subject=subject)
    assert paired.status_code == 200, paired.text
    device = state.credentials.resolve_device(paired.json()["device_credential"])
    assert device is not None
    return device, private_key, paired.json()


def test_two_riders_are_isolated_across_objects_revisions_headers_and_capabilities(
    cloud,
):
    """Exercise the whole client-side boundary with two independent riders."""
    pytest.importorskip("cryptography")
    _config, state, client = cloud
    rider_a = _writer(state, b"a", scope="rider-a")
    rider_b = _writer(state, b"b", scope="rider-b")
    object_a = "activity-rider-a"
    object_b = "activity-rider-b"
    body_a = _rider_batch(object_id=object_a, rider="rider-a", revision=7)
    body_b = _rider_batch(object_id=object_b, rider="rider-b", revision=3)

    assert rider_a.namespace != rider_b.namespace
    assert client.post(
        "/api/v1/sync/batches",
        headers=_headers(
            rider_a, body_a, nonce="upload-a", revision=7,
            idem="rider-a-batch-7",
        ),
        content=body_a,
    ).status_code == 200
    assert client.post(
        "/api/v1/sync/batches",
        headers=_headers(
            rider_b, body_b, nonce="upload-b", revision=3,
            idem="rider-b-batch-3",
        ),
        content=body_b,
    ).status_code == 200

    a_phone, a_phone_key, a_phone_body = _paired_device(
        state, client, rider_a, nonce="a-phone"
    )
    a_tablet, _a_tablet_key, a_tablet_body = _paired_device(
        state, client, rider_a, nonce="a-tablet"
    )
    b_phone, b_phone_key, b_phone_body = _paired_device(
        state, client, rider_b, nonce="b-phone"
    )
    b_tablet, _b_tablet_key, b_tablet_body = _paired_device(
        state, client, rider_b, nonce="b-tablet"
    )
    assert {
        a_phone.namespace, a_tablet.namespace,
        b_phone.namespace, b_tablet.namespace,
    } == {rider_a.namespace, rider_b.namespace}
    assert {
        a_phone.local_user_scope, a_tablet.local_user_scope,
        b_phone.local_user_scope, b_tablet.local_user_scope,
    } == {"rider-a", "rider-b"}
    assert len({
        a_phone.credential_id, a_tablet.credential_id,
        b_phone.credential_id, b_tablet.credential_id,
    }) == 4

    a_token = a_phone_body["reader_context"]
    b_token = b_phone_body["reader_context"]
    a_items = client.get(
        "/api/v1/context/activities?since=0",
        headers=_mobile_headers(a_token),
    )
    b_items = client.get(
        "/api/v1/context/activities?since=0",
        headers=_mobile_headers(b_token),
    )
    assert a_items.status_code == b_items.status_code == 200
    assert [item["id"] for item in a_items.json()["items"]] == [object_a]
    assert [item["id"] for item in b_items.json()["items"]] == [object_b]
    assert a_items.json()["items"][0]["data"]["rider_marker"] == "rider-a"
    assert b_items.json()["items"][0]["data"]["rider_marker"] == "rider-b"

    assert client.get(
        f"/api/v1/context/activities/{object_a}",
        headers=_mobile_headers(a_token),
    ).status_code == 200
    _assert_not_found(client.get(
        f"/api/v1/context/activities/{object_a}",
        headers=_mobile_headers(b_token),
    ))

    # A scope revision is not a global clock.  Asking rider B to replay past
    # rider A's checkpoint returns B's checkpoint and no object from A.
    a_context = client.get(
        "/api/v1/context", headers=_mobile_headers(a_token)
    )
    b_context = client.get(
        "/api/v1/context", headers=_mobile_headers(b_token)
    )
    assert a_context.json()["revision"] == 7
    assert b_context.json()["revision"] == 3
    cross_replay = client.get(
        f"/api/v1/context/activities?since={a_context.json()['revision']}",
        headers=_mobile_headers(b_token),
    )
    assert cross_replay.status_code == 200
    assert cross_replay.json() == {
        "items": [], "revision": 3, "next_cursor": None,
    }

    # A forward-looking guard, NOT evidence of header hardening. None of these
    # names is read by the reader plane today: _resolve_reader reads only
    # `authorization`, the gateway-proof header and the verified-subject
    # header, so this block passes just as well with plain valid headers and
    # proves nothing on its own. It is kept so that adding header-based scope
    # selection later fails here first. The headers the reader plane really
    # does read are covered by test_reader_context_requires_the_apim_verified_
    # subject_proof_by_default, test_non_ascii_reader_auth_headers_fail_closed
    # and test_configured_gateway_proof_cannot_be_forged_with_boolean_marker.
    forged_headers = {
        **_mobile_headers(b_token),
        "X-Namespace": rider_a.namespace,
        "X-Installation-ID": "a" * 32,
        "X-Local-User-Scope": rider_a.local_user_scope,
        "X-Device-Credential": a_phone.credential_id,
        "X-Writer-Credential": rider_a.credential_id,
        "Ocp-Apim-Subscription-Key": rider_a.subscription_key.decode("ascii"),
    }
    _assert_not_found(client.get(
        f"/api/v1/context/activities/{object_a}", headers=forged_headers
    ))
    forged_replay = client.get(
        "/api/v1/context/activities?since=0", headers=forged_headers
    )
    assert [item["id"] for item in forged_replay.json()["items"]] == [object_b]

    # A valid B signature carrying A's subscription key cannot reach B's
    # device listing, and in particular never turns into a cross-rider 403.
    wrong_subscription = client.get(
        "/api/v1/devices",
        headers=_admin_headers(
            b_phone,
            lambda material: _sign(b_phone, b_phone_key, material),
            method="GET",
            path="/api/v1/devices",
            nonce="b-list-wrong-key",
            idem="device-list",
            subscription=rider_a.subscription_key.decode("ascii"),
        ),
    )
    assert wrong_subscription.status_code == 401
    assert wrong_subscription.status_code != 403

    for writer, expected in (
        (rider_a, {a_phone.credential_id, a_tablet.credential_id}),
        (rider_b, {b_phone.credential_id, b_tablet.credential_id}),
    ):
        listing = client.get(
            "/api/v1/devices",
            headers=_admin_headers(
                writer,
                lambda material, writer=writer: sign_request(
                    writer.signing_key, material
                ),
                method="GET",
                path="/api/v1/devices",
                nonce=f"{writer.local_user_scope}-list",
                idem="device-list",
            ),
        )
        assert listing.status_code == 200, listing.text
        assert {
            entry["credential_id"] for entry in listing.json()["devices"]
        } == expected

    # A well-formed, correctly signed batch still cannot be written by a
    # read-only device credential.
    forbidden_body = _rider_batch(
        object_id="activity-forbidden", rider="rider-a", revision=8
    )
    forbidden = client.post(
        "/api/v1/sync/batches",
        headers=_device_sync_headers(
            a_phone, a_phone_key, forbidden_body,
            nonce="a-forbidden-write", revision=8,
            idem="rider-a-batch-8",
        ),
        content=forbidden_body,
    )
    assert forbidden.status_code == 401
    assert state.store.revision(rider_a.namespace, rider_a.local_user_scope) == 7
    assert state.store.get(
        rider_a.namespace, rider_a.local_user_scope, "activity-forbidden"
    ) is None

    # An authenticated B device may not revoke an A device by knowing its id.
    cross_revoke = client.post(
        f"/api/v1/devices/{a_phone.credential_id}/revoke",
        headers=_admin_headers(
            b_phone,
            lambda material: _sign(b_phone, b_phone_key, material),
            method="POST",
            path=f"/api/v1/devices/{a_phone.credential_id}/revoke",
            nonce="b-cross-revoke",
            idem="device-revoke",
        ),
    )
    _assert_not_found(cross_revoke)
    assert state.credentials.resolve_device(a_phone.credential_id) is not None
    assert a_tablet_body["device_credential"] != a_phone_body["device_credential"]
    assert b_tablet_body["device_credential"] != b_phone_body["device_credential"]


def test_revoked_device_loses_existing_context_immediately_and_after_restart():
    pytest.importorskip("cryptography")
    backend = _DurableMemoryBackend()
    config = CloudConfig(
        server_secret=SECRET,
        operator_token="operator-token",
        require_gateway_proof=False,
        clock=lambda: 1_000,
    )
    store = MemoryTenantStore()
    state = CloudState.create(config, security_backend=backend, store=store)

    with TestClient(create_cloud_app(config, state=state)) as client:
        writer = _writer(state, b"r", scope="revocation-rider")
        device, private_key, paired = _paired_device(
            state, client, writer, nonce="revocation-phone"
        )
        reader_headers = _mobile_headers(paired["reader_context"])
        assert client.get(
            "/api/v1/context/activities", headers=reader_headers
        ).status_code == 200

        revoked = client.post(
            f"/api/v1/devices/{device.credential_id}/revoke",
            headers=_admin_headers(
                writer,
                lambda material: sign_request(writer.signing_key, material),
                method="POST",
                path=f"/api/v1/devices/{device.credential_id}/revoke",
                nonce="revoke-phone",
                idem="device-revoke",
            ),
        )
        assert revoked.status_code == 200, revoked.text
        assert revoked.json() == {"revoked": True}
        _assert_not_found(client.get(
            "/api/v1/context/activities", headers=reader_headers
        ))

    restarted = CloudState.create(
        config, security_backend=backend, store=store
    )
    revoked_device = restarted.credentials.lookup_device(device.credential_id)
    assert revoked_device is not None
    assert revoked_device.revoked and not revoked_device.active

    with TestClient(create_cloud_app(config, state=restarted)) as client:
        _assert_not_found(client.get(
            "/api/v1/context/activities", headers=reader_headers
        ))
        _assert_not_found(client.post(
            "/api/v1/context/refresh",
            headers=_refresh_headers(
                revoked_device, private_key, nonce="revoked-after-restart"
            ),
        ))
        listing = client.get(
            "/api/v1/devices",
            headers=_admin_headers(
                writer,
                lambda material: sign_request(writer.signing_key, material),
                method="GET",
                path="/api/v1/devices",
                nonce="list-after-restart",
                idem="device-list",
            ),
        )
        assert listing.status_code == 200, listing.text
        entry = next(
            item for item in listing.json()["devices"]
            if item["credential_id"] == device.credential_id
        )
        assert entry["revoked"] is True


def test_shared_cloud_object_fixture_matches_python_published_kinds():
    fixture = json.loads(CLOUD_OBJECT_VECTOR.read_text(encoding="utf-8"))
    assert fixture["version"] == 1
    assert fixture["kinds"] == sorted(PUBLISHED_OBJECT_KINDS)
    assert len(fixture["items"]) == len(PUBLISHED_OBJECT_KINDS)
    assert {
        item["kind"] for item in fixture["items"]
    } == PUBLISHED_OBJECT_KINDS

    for raw in fixture["items"]:
        parsed = CloudObject(
            object_id=raw["id"],
            kind=raw["kind"],
            revision=raw["revision"],
            data=raw.get("data", {}),
            deleted=raw.get("deleted", False),
        )
        assert parsed.wire() == raw


def test_desktop_pairs_two_devices_into_its_own_single_namespace(cloud):
    """The iPhone and the iPad land in the same namespace and scope.

    This is the whole point of #152: before it, a second enrollment derived a
    fresh installation id and therefore a different storage partition, so the
    second device opened an empty account.
    """
    pytest.importorskip("cryptography")
    _config, state, client = cloud
    writer = _writer(state, b"a")
    body = _batch()
    assert client.post(
        "/api/v1/sync/batches", headers=_headers(writer, body), content=body
    ).status_code == 200

    phone, phone_key, phone_body = _paired_device(state, client, writer, nonce="phone")
    tablet, tablet_key, tablet_body = _paired_device(
        state, client, writer, nonce="tablet"
    )

    assert phone.credential_id != tablet.credential_id
    assert phone.subscription_key != tablet.subscription_key
    assert phone.namespace == tablet.namespace == writer.namespace
    assert phone.local_user_scope == tablet.local_user_scope == writer.local_user_scope
    assert phone_body["device_capabilities"] == ["read"]
    assert phone_body["signing_namespace"] == writer.namespace
    assert (
        phone_body["device_subscription_key"]
        != tablet_body["device_subscription_key"]
    )

    # Both devices read the desktop's data through their initial contexts...
    for issued in (phone_body, tablet_body):
        listed = client.get("/api/v1/context/activities", headers={
            **READER_HEADERS, "Authorization": "Bearer " + issued["reader_context"],
        })
        assert listed.status_code == 200
        assert [item["id"] for item in listed.json()["items"]] == ["activity-1"]

    # ...and each keeps reading after its own signed refresh.
    for device, key, nonce in (
        (phone, phone_key, "r-phone"), (tablet, tablet_key, "r-tablet"),
    ):
        refreshed = client.post(
            "/api/v1/context/refresh",
            headers=_refresh_headers(device, key, nonce=nonce),
        )
        assert refreshed.status_code == 200, refreshed.text
        listed = client.get("/api/v1/context/activities", headers={
            **READER_HEADERS,
            "Authorization": "Bearer " + refreshed.json()["reader_context"],
        })
        assert [item["id"] for item in listed.json()["items"]] == ["activity-1"]


def test_pairing_code_is_single_use(cloud):
    pytest.importorskip("cryptography")
    _config, state, client = cloud
    writer = _writer(state, b"a")
    _first_private, first_public = generate_signing_keypair()
    _second_private, second_public = generate_signing_keypair()

    code = _mint(client, writer, nonce="once").json()["pairing_code"]
    assert _pair(client, code, first_public).status_code == 200
    replayed = _pair(client, code, second_public)
    assert replayed.status_code == 404
    assert replayed.json() == {"detail": "not found"}
    # And the second device's key was never registered anywhere.
    assert all(
        credential.verification_key != second_public
        for credential in state.credentials._devices.values()
    )


def test_pairing_code_expires_against_the_deployment_clock():
    pytest.importorskip("cryptography")
    clock = _MovableClock(1_000)
    config = CloudConfig(
        server_secret=SECRET, operator_token="operator-token",
        require_gateway_proof=False, clock=clock,
    )
    state = CloudState.create(config)
    client = TestClient(create_cloud_app(config, state=state))
    writer = _writer(state, b"a")
    _private, public_key = generate_signing_keypair()

    code = _mint(client, writer, nonce="expiring").json()["pairing_code"]
    # One second before the 600-second default TTL elapses it still works;
    # the caller's own clock never enters the decision.
    clock.value = 1_000 + 599
    still_live = client.post(PAIR_PATH, headers=PAIR_HEADERS, json={
        "code": code, "public_key": public_key.hex()})
    assert still_live.status_code == 200, still_live.text

    later = _mint(client, writer, nonce="later", timestamp=int(clock.value))
    clock.value += 600
    expired = client.post(PAIR_PATH, headers=PAIR_HEADERS, json={
        "code": later.json()["pairing_code"], "public_key": public_key.hex()})
    assert expired.status_code == 404
    assert expired.json() == {"detail": "not found"}


def test_unknown_expired_and_consumed_pairing_codes_are_indistinguishable(cloud):
    pytest.importorskip("cryptography")
    _config, state, client = cloud
    writer = _writer(state, b"a")
    _private, public_key = generate_signing_keypair()

    subject_writer = _subject_writer(state, b"s")

    spent = _mint(client, writer, nonce="spent").json()["pairing_code"]
    assert _pair(client, spent, public_key).status_code == 200
    consumed = _pair(client, spent, public_key)
    unknown = _pair(client, generate_pairing_code(), public_key)
    malformed = _pair(client, "ZZZZ-ZZZZ", public_key)
    wrong_subject_code = _mint(
        client, subject_writer, nonce="subject", subject="entra-user"
    ).json()["pairing_code"]
    wrong_subject = _pair(
        client, wrong_subject_code, public_key, subject="someone-else"
    )
    missing_context = client.get(
        "/api/v1/context",
        headers={**READER_HEADERS, "Authorization": "Bearer " + "x" * 40},
    )

    responses = [consumed, unknown, malformed, wrong_subject, missing_context]
    assert {response.status_code for response in responses} == {404}
    assert {json.dumps(response.json()) for response in responses} == {
        '{"detail": "not found"}'
    }
    assert {response.headers["cache-control"] for response in responses} == {"no-store"}

    # The rejected-subject attempt did not spend the code.  A rider redeeming
    # on the wrong account must not lose it, and the response says nothing
    # either way.
    assert _pair(client, wrong_subject_code, public_key).status_code == 200


def test_where_a_gateway_attests_a_subject_the_code_carries_it(attested_cloud):
    """The subject is an additional binding on the code, never a substitute.

    Where a gateway attests one, the code inherits it and the redeeming device
    must present a match.  Crucially the *code* is what demands it, not the
    route: omitting the header cannot bypass a binding that exists.  Where
    nothing attests a subject the code carries none and no header is read at
    all -- see ``test_pairing_needs_no_identity_provider_at_all``.
    """
    pytest.importorskip("cryptography")
    _config, state, client = attested_cloud
    proof = {"X-Gateway-Request-Proof": "proof-value"}
    subject_writer = _subject_writer(state, b"s", subject="rider@example.invalid")
    _private, public_key = generate_signing_keypair()

    # The signed-in subject must match the one bound at enrollment.
    assert client.post(MINT_PATH, headers={
        **_mint_headers(subject_writer, nonce="hijack", subject="thief"), **proof,
    }).status_code == 401
    minted = client.post(MINT_PATH, headers={
        **_mint_headers(
            subject_writer, nonce="bound", subject="rider@example.invalid"),
        **proof,
    })
    assert minted.status_code == 200, minted.text
    code = minted.json()["pairing_code"]

    payload = {"code": code, "public_key": public_key.hex()}
    # Wrong subject, and no subject at all, are the same 404: a bound subject
    # is not bypassable by leaving the header off.
    wrong = client.post(PAIR_PATH, headers={
        **proof, "X-Verified-Entra-Subject": "thief"}, json=payload)
    omitted = client.post(PAIR_PATH, headers=proof, json=payload)
    assert wrong.status_code == omitted.status_code == 404
    assert wrong.json() == omitted.json() == {"detail": "not found"}
    # Neither attempt spent it.
    paired = client.post(PAIR_PATH, headers={
        **proof, "X-Verified-Entra-Subject": "rider@example.invalid"}, json=payload)
    assert paired.status_code == 200, paired.text
    device = state.credentials.resolve_device(paired.json()["device_credential"])
    assert device.subject == "rider@example.invalid"

    # A writer with no stored subject takes the attested one at mint time.
    plain_writer = _writer(state, b"p")
    plain = client.post(MINT_PATH, headers={
        **_mint_headers(plain_writer, nonce="plain", subject="whoever"), **proof,
    })
    _other_private, other_public = generate_signing_keypair()
    plain_payload = {
        "code": plain.json()["pairing_code"], "public_key": other_public.hex()}
    assert client.post(PAIR_PATH, headers={
        **proof, "X-Verified-Entra-Subject": "someone-else"},
        json=plain_payload).status_code == 404
    plain_paired = client.post(PAIR_PATH, headers={
        **proof, "X-Verified-Entra-Subject": "whoever"}, json=plain_payload)
    assert plain_paired.status_code == 200, plain_paired.text
    assert state.credentials.resolve_device(
        plain_paired.json()["device_credential"]
    ).subject == "whoever"


def test_pairing_code_cannot_be_redeemed_into_another_namespace(cloud):
    pytest.importorskip("cryptography")
    _config, state, client = cloud
    rider_a = _writer(state, b"a", scope="scope-a")
    rider_b = _writer(state, b"b", scope="scope-b")
    _private, public_key = generate_signing_keypair()

    code = _mint(client, rider_a, nonce="cross").json()["pairing_code"]
    # Every field a device could use to name a partition is supplied, and
    # every one is ignored -- the same treatment SyncBatch.from_wire gives a
    # client-supplied installation_id.
    paired = _pair(client, code, public_key, extra={
        "namespace": rider_b.namespace,
        "local_user_scope": rider_b.local_user_scope,
        "installation_id": new_installation_id(),
        "capabilities": ["read", "write"],
    })
    assert paired.status_code == 200, paired.text
    device = state.credentials.resolve_device(paired.json()["device_credential"])
    assert device.namespace == rider_a.namespace
    assert device.namespace != rider_b.namespace
    assert device.local_user_scope == "scope-a"
    assert device.capabilities == frozenset({"read"})
    assert paired.json()["device_capabilities"] == ["read"]
    assert paired.json()["signing_namespace"] == rider_a.namespace

    # Nor can rider A mint into rider B's namespace by claiming it in the
    # signed envelope: the server signs against the credential's namespace.
    forged = _mint(client, rider_a, nonce="forged", namespace=rider_b.namespace)
    assert forged.status_code == 401


def test_pairing_codes_cannot_be_brute_forced_inside_their_ttl(cloud):
    """The code space, not the endpoint, is what defeats guessing.

    APIM allows 60 requests/minute and 1,000/day per subscription key, so the
    most guesses one key can spend inside the 900-second TTL ceiling is
    15 * 60 == 900 -- under the daily cap, so the per-minute limit binds.
    Against 2**60 codes that is a 7.8e-16 chance.  The endpoint is hammered
    here only to prove a wrong guess is refused identically and never
    consumes the outstanding real code.
    """
    pytest.importorskip("cryptography")
    _config, state, client = cloud
    writer = _writer(state, b"a")
    _private, public_key = generate_signing_keypair()
    real = _mint(client, writer, nonce="brute").json()["pairing_code"]

    guesses_per_key = int(MAX_DEVICE_PAIRING_TTL_SECONDS / 60) * 60
    assert guesses_per_key == 900 and guesses_per_key <= 1_000
    assert guesses_per_key / 2 ** DEVICE_PAIRING_CODE_BITS < 2 ** -32
    # Even 1,000 subscription keys, each spending a full daily budget inside
    # one code's lifetime, stay far below a 2**-32 chance.
    assert (1_000 * guesses_per_key) / 2 ** DEVICE_PAIRING_CODE_BITS < 2 ** -32

    for _attempt in range(64):
        guess = generate_pairing_code()
        assert guess != real
        response = _pair(client, guess, public_key)
        assert response.status_code == 404
        assert response.json() == {"detail": "not found"}
    # None of that touched the live code.
    assert _pair(client, real, public_key).status_code == 200


def test_only_a_write_capable_credential_can_mint_a_pairing_code(cloud):
    """Pairing authority is installation authority.

    A read-only paired device holds a valid signing credential and a valid
    subscription key for the same namespace; the only thing it lacks is
    "write", and that is what stops it minting codes for further devices.
    """
    pytest.importorskip("cryptography")
    _config, state, client = cloud
    writer = _writer(state, b"a")
    device, device_key, _body = _paired_device(state, client, writer, nonce="authority")
    assert device.capabilities == frozenset({"read"})

    refused = client.post(MINT_PATH, headers=_mint_headers(
        device, nonce="device-mint",
        signer=lambda canonical: _sign(device, device_key, canonical),
    ))
    assert refused.status_code == 401
    assert refused.json() == {"detail": "writer authorization required"}

    granted, granted_key = _device(state, capabilities=("read", "write"))
    allowed = client.post(MINT_PATH, headers=_mint_headers(
        granted, nonce="granted-mint",
        signer=lambda canonical: _sign(granted, granted_key, canonical),
    ))
    assert allowed.status_code == 200, allowed.text


def test_minting_requires_a_fresh_untampered_signed_envelope(cloud):
    _config, state, client = cloud
    writer = _writer(state, b"a")

    assert client.post(MINT_PATH).status_code == 401
    tampered = _mint_headers(writer, nonce="tamper")
    tampered["X-Writer-Signature"] = "0" * 64
    assert client.post(MINT_PATH, headers=tampered).status_code == 401
    wrong_subscription = _mint_headers(writer, nonce="sub")
    wrong_subscription["Ocp-Apim-Subscription-Key"] = "not-the-key"
    assert client.post(MINT_PATH, headers=wrong_subscription).status_code == 401
    stale = _mint_headers(writer, nonce="stale", timestamp=1_000 - 301)
    assert client.post(MINT_PATH, headers=stale).status_code == 401
    # The envelope is fixed: neither field is the caller's to choose.
    assert client.post(
        MINT_PATH, headers=_mint_headers(writer, nonce="idem", idem="batch-1")
    ).status_code == 401
    assert client.post(
        MINT_PATH, headers=_mint_headers(writer, nonce="rev", revision=7)
    ).status_code == 401
    # A body is covered by the signed digest, so one cannot be smuggled in.
    assert client.post(
        MINT_PATH, headers=_mint_headers(writer, nonce="bodied"), content=b"{}"
    ).status_code == 401
    # Every read-plane route needs the gateway-verified subject; this one is
    # no exception, and the check sits behind proof of possession.
    assert client.post(
        MINT_PATH, headers=_mint_headers(writer, nonce="no-subject", subject=None)
    ).status_code == 401
    assert client.post(
        MINT_PATH, headers=_mint_headers(writer, nonce="long", subject="s" * 257)
    ).status_code == 401

    replayed = _mint_headers(writer, nonce="once")
    assert client.post(MINT_PATH, headers=replayed).status_code == 200
    assert client.post(MINT_PATH, headers=replayed).status_code == 401


def test_pairing_rejects_unusable_keys_without_spending_the_code(cloud):
    pytest.importorskip("cryptography")
    _config, state, client = cloud
    writer = _writer(state, b"a")
    code = _mint(client, writer, nonce="keys").json()["pairing_code"]
    _private, ed_public = generate_signing_keypair()
    _p256_private, p256_public = generate_p256_keypair()

    # An Ed25519 key declared as a P-256 point.
    assert _pair(
        client, code, ed_public, algorithm="ecdsa-p256-sha256"
    ).status_code == 400
    # A symmetric algorithm is not a device algorithm at all: a device keeps
    # its private half in hardware.
    assert _pair(client, code, ed_public, algorithm="hmac-sha256").status_code == 400
    assert client.post(
        PAIR_PATH, headers=PAIR_HEADERS, content=b"not json"
    ).status_code == 400
    assert client.post(PAIR_PATH, headers=PAIR_HEADERS, json={
        "code": code, "public_key": "zz"}).status_code == 400
    assert client.post(PAIR_PATH, headers=PAIR_HEADERS, json={
        "public_key": ed_public.hex()}).status_code == 400
    assert client.post(PAIR_PATH, headers=PAIR_HEADERS, json=[
        "code", ed_public.hex()]).status_code == 400

    # None of those spent the code.
    accepted = _pair(client, code, p256_public, algorithm="ecdsa-p256-sha256")
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["device_signature_algorithm"] == "ecdsa-p256-sha256"


def test_a_deployment_without_the_crypto_extra_refuses_without_spending(
    cloud, monkeypatch
):
    """A P-256 pairing on a crypto-less deployment must not burn the code."""
    pytest.importorskip("cryptography")
    from wattracker.cloud import security

    _config, state, client = cloud
    writer = _writer(state, b"a")
    code = _mint(client, writer, nonce="unavailable").json()["pairing_code"]
    _private, public_key = generate_p256_keypair()

    def _unavailable(algorithm, key):
        raise security.PublicKeyUnavailable("cloud extra is not installed")

    monkeypatch.setattr("wattracker.cloud.api.validate_public_key", _unavailable)
    stranded = _pair(client, code, public_key, algorithm="ecdsa-p256-sha256")
    assert stranded.status_code == 404
    assert stranded.json() == {"detail": "not found"}

    monkeypatch.undo()
    retried = _pair(client, code, public_key, algorithm="ecdsa-p256-sha256")
    assert retried.status_code == 200, retried.text


def test_pairing_still_requires_the_gateway_proof_where_one_is_configured():
    pytest.importorskip("cryptography")
    config = CloudConfig(
        server_secret=SECRET,
        operator_token="operator-token",
        gateway_proof_value="proof-value",
        clock=lambda: 1_000,
    )
    state = CloudState.create(config)
    client = TestClient(create_cloud_app(config, state=state))
    writer = _writer(state, b"a")
    _private, public_key = generate_signing_keypair()
    proof = {"X-Gateway-Request-Proof": "proof-value"}

    minted = client.post(
        MINT_PATH, headers={**_mint_headers(writer, nonce="proofed"), **proof}
    )
    assert minted.status_code == 200, minted.text
    payload = {"code": minted.json()["pairing_code"], "public_key": public_key.hex()}

    unproofed = client.post(
        PAIR_PATH, headers={"X-Verified-Entra-Subject": "entra-user"}, json=payload
    )
    assert unproofed.status_code == 404
    # It did not spend the code.
    accepted = client.post(PAIR_PATH, headers={
        **proof, "X-Verified-Entra-Subject": "entra-user"}, json=payload)
    assert accepted.status_code == 200, accepted.text
    # Minting without the proof header is refused too.
    assert client.post(
        MINT_PATH, headers=_mint_headers(writer, nonce="unproofed")
    ).status_code == 401


def test_pairing_routes_are_absent_from_the_sync_plane():
    """Both routes live on the read plane.

    Minting persists a record in ``CloudAuth`` and only the read plane's
    managed identity may write that table (``infra/azure/main.bicep``), so
    the sync plane could not honour a mint even if it exposed one.
    """
    sync_app = create_cloud_app(CloudConfig(
        server_secret=SECRET, operator_token="operator-token", plane="sync"
    ))
    read_app = create_cloud_app(CloudConfig(
        server_secret=SECRET, operator_token="operator-token", plane="read"
    ))
    sync_paths = {route.path for route in sync_app.routes}
    read_paths = {route.path for route in read_app.routes}
    assert not {MINT_PATH, PAIR_PATH} & sync_paths
    assert {MINT_PATH, PAIR_PATH} <= read_paths


def test_pairing_survives_a_read_plane_restart():
    """The phone can be paired by one replica and refresh against another."""
    pytest.importorskip("cryptography")
    backend = MemorySecurityStateBackend()
    config = CloudConfig(
        server_secret=SECRET,
        operator_token="operator-token",
        plane="read",
        require_gateway_proof=False,
        clock=lambda: 1_000,
    )
    state = CloudState.create(config, security_backend=backend)
    writer = _writer(state, b"a")
    device_private, device_public = generate_signing_keypair()

    with TestClient(create_cloud_app(config, state=state)) as client:
        code = _mint(client, writer, nonce="restart").json()["pairing_code"]

    restarted = CloudState.create(config, security_backend=backend)
    with TestClient(create_cloud_app(config, state=restarted)) as client:
        paired = _pair(client, code, device_public)
        assert paired.status_code == 200, paired.text
        # Single-use holds across the restart boundary too.
        assert _pair(client, code, device_public).status_code == 404

    device = restarted.credentials.resolve_device(paired.json()["device_credential"])
    assert device is not None
    assert device.namespace == writer.namespace
    with TestClient(create_cloud_app(config, state=restarted)) as client:
        refreshed = client.post(
            "/api/v1/context/refresh",
            headers=_refresh_headers(device, device_private, nonce="post-restart"),
        )
    assert refreshed.status_code == 200, refreshed.text


def test_the_kill_switch_disables_pairing(cloud):
    pytest.importorskip("cryptography")
    _config, state, client = cloud
    writer = _writer(state, b"a")
    _private, public_key = generate_signing_keypair()
    code = _mint(client, writer, nonce="kill").json()["pairing_code"]
    state.quotas.set_public_enabled(False)
    assert _pair(client, code, public_key).status_code == 404
    assert client.post(
        MINT_PATH, headers=_mint_headers(writer, nonce="kill-mint")
    ).status_code == 403
    state.quotas.set_public_enabled(True)
    assert _pair(client, code, public_key).status_code == 200


def test_the_kill_switch_disables_enrollment_start_before_auth_or_write(cloud):
    _config, state, client = cloud
    before = dict(state.enrollments._records)
    state.quotas.set_public_enabled(False)

    refused = client.post(
        "/api/v1/enrollment/start",
        headers={"X-Operator-Token": "wrong-token"},
    )

    assert refused.status_code == 404
    assert state.enrollments._records == before


def test_enrollment_start_does_not_reveal_public_kill_state(cloud):
    _config, state, client = cloud
    invalid_token = client.post(
        "/api/v1/enrollment/start",
        headers={"X-Operator-Token": "wrong-token"},
    )

    state.quotas.set_public_enabled(False)
    disabled = client.post(
        "/api/v1/enrollment/start",
        headers={"X-Operator-Token": "operator-token"},
    )

    assert disabled.status_code == invalid_token.status_code == 404
    assert disabled.content == invalid_token.content
    assert dict(disabled.headers) == dict(invalid_token.headers)


@pytest.mark.parametrize("public_enabled", [True, False])
def test_enrollment_complete_invalid_gateway_proof_matches_start(public_enabled):
    config = CloudConfig(
        server_secret=SECRET,
        operator_token="operator-token",
        gateway_proof_value="proof-value",
        clock=lambda: 1_000,
    )
    state = CloudState.create(config)
    if not public_enabled:
        state.quotas.set_public_enabled(False)

    with TestClient(create_cloud_app(config, state=state)) as client:
        start = client.post(
            "/api/v1/enrollment/start",
            headers={
                "X-Operator-Token": "operator-token",
                "X-Gateway-Request-Proof": "invalid-proof",
            },
        )
        complete = client.post(
            "/api/v1/enrollment/complete",
            headers={"X-Gateway-Request-Proof": "invalid-proof"},
            json={"invitation": "unused", "public_key": (b"e" * 32).hex()},
        )

    assert complete.status_code == start.status_code == 404
    assert complete.content == start.content
    assert complete.headers["cache-control"] == start.headers["cache-control"] == "no-store"
    assert complete.headers["pragma"] == start.headers["pragma"] == "no-cache"


def test_the_kill_switch_disables_enrollment_complete_before_auth_or_write(cloud):
    _config, state, client = cloud
    started = client.post(
        "/api/v1/enrollment/start",
        headers={
            "X-Operator-Token": "operator-token",
            "X-Verified-Entra-Subject": "entra-user",
            "X-APIM-Client-Certificate-Verified": "true",
        },
    )
    assert started.status_code == 200
    before_invitations = dict(state.enrollments._records)
    before_writers = dict(state.credentials._writers)
    before_contexts = dict(state.credentials._contexts)
    before_context_ids = dict(state.credentials._contexts_by_id)
    state.quotas.set_public_enabled(False)

    refused = client.post(
        "/api/v1/enrollment/complete",
        headers={
            "X-Verified-Entra-Subject": "entra-user",
            "X-APIM-Request-Verified": "true",
            "X-APIM-Client-Certificate-Verified": "true",
        },
        json={
            "invitation": started.json()["invitation"],
            "public_key": (b"e" * 32).hex(),
        },
    )

    assert refused.status_code == 404
    assert state.enrollments._records == before_invitations
    assert state.credentials._writers == before_writers
    assert state.credentials._contexts == before_contexts
    assert state.credentials._contexts_by_id == before_context_ids


class _DurableMemoryBackend(MemorySecurityStateBackend):
    """A shared-process backend that claims durability, for boot-check tests."""

    durable = True


def test_production_refuses_to_trust_a_subject_header_with_no_gateway():
    """Removing the gateway must be a config change, not a silent downgrade.

    A verified-subject header is worth exactly as much as the gateway that
    overwrites it.  The failure mode being designed out is a header that is
    trustworthy in one deployment and forgeable in another, with nothing at
    startup telling the two apart -- so production refuses to boot in the
    combination that would serve the second while looking like the first.
    """
    backend = _DurableMemoryBackend()

    def _config(**overrides):
        return CloudConfig(
            server_secret=SECRET, operator_token="operator-token", **overrides
        )

    ungated = _config(require_gateway_proof=False)
    assert ungated.require_verified_subject
    assert not ungated.gateway_attests_subject
    with pytest.raises(RuntimeError, match="verified-subject header requires a gateway"):
        CloudState.create(
            ungated, security_backend=backend, require_persistent_security=True
        )

    # Demanding the proof header without configuring a value is not a gateway
    # either: `_gateway_proof_valid` fails closed on an empty value, so nothing
    # would ever have overwritten the subject.
    hollow = _config(gateway_proof_value="")
    assert not hollow.gateway_attests_subject
    with pytest.raises(RuntimeError, match="verified-subject header requires a gateway"):
        CloudState.create(
            hollow, security_backend=backend, require_persistent_security=True
        )

    # Declaring the truth boots, and so does keeping a real gateway.
    declared = _config(require_gateway_proof=False, require_verified_subject=False)
    assert CloudState.create(
        declared, security_backend=backend, require_persistent_security=True
    ) is not None
    gated = _config(gateway_proof_value="proof-value")
    assert gated.gateway_attests_subject
    assert CloudState.create(
        gated, security_backend=backend, require_persistent_security=True
    ) is not None


def test_pairing_needs_no_identity_provider_at_all():
    """The configuration #164 produces: no gateway, no Entra, no subject header.

    The pairing code *is* the authorization -- 60 bits, single use, at most
    900 seconds, and mintable only by a writer-signed request from the rider's
    own desktop.  Nothing in this test sends a subject header, because on a
    phone there is nobody to get one from, and nothing would vouch for it if
    there were.
    """
    pytest.importorskip("cryptography")
    config = CloudConfig(
        server_secret=SECRET,
        operator_token="operator-token",
        require_gateway_proof=False,
        require_verified_subject=False,
        clock=lambda: 1_000,
    )
    state = CloudState.create(config)
    client = TestClient(create_cloud_app(config, state=state))
    # The writer still carries a subject left over from an enrollment that ran
    # while a gateway existed.  It must not be bound into the code: nothing
    # can check it later, and a device pinned to a string it cannot obtain is
    # a device that cannot read.
    writer = _subject_writer(state, b"a", subject="left-over-from-entra")
    body = _batch()
    assert client.post(
        "/api/v1/sync/batches", headers=_headers(writer, body), content=body
    ).status_code == 200

    minted = client.post(
        MINT_PATH, headers=_mint_headers(writer, nonce="no-idp", subject=None)
    )
    assert minted.status_code == 200, minted.text

    device_private, device_public = generate_signing_keypair()
    paired = _pair(
        client, minted.json()["pairing_code"], device_public, subject=None, headers={}
    )
    assert paired.status_code == 200, paired.text
    device = state.credentials.resolve_device(paired.json()["device_credential"])
    assert device.namespace == writer.namespace
    assert device.local_user_scope == writer.local_user_scope
    assert device.subject is None

    # The context it was handed reads, with no subject header.
    listed = client.get("/api/v1/context/activities", headers={
        "Authorization": "Bearer " + paired.json()["reader_context"]})
    assert listed.status_code == 200, listed.text
    assert [item["id"] for item in listed.json()["items"]] == ["activity-1"]

    # And the device refreshes its own context, still with no subject header.
    refresh_headers = {
        name: value
        for name, value in _refresh_headers(
            device, device_private, nonce="no-idp-refresh"
        ).items()
        if name != "X-Verified-Entra-Subject"
    }
    refreshed = client.post("/api/v1/context/refresh", headers=refresh_headers)
    assert refreshed.status_code == 200, refreshed.text
    second = client.get("/api/v1/context/activities", headers={
        "Authorization": "Bearer " + refreshed.json()["reader_context"]})
    assert [item["id"] for item in second.json()["items"]] == ["activity-1"]


def test_an_ungated_deployment_never_reads_the_subject_header():
    """Where nothing attests a subject, a supplied one changes nothing.

    Otherwise the header would be a control in appearance only: present it and
    something happens, forge it and the same thing happens.
    """
    pytest.importorskip("cryptography")
    config = CloudConfig(
        server_secret=SECRET,
        operator_token="operator-token",
        require_gateway_proof=False,
        require_verified_subject=False,
        clock=lambda: 1_000,
    )
    state = CloudState.create(config)
    client = TestClient(create_cloud_app(config, state=state))
    writer = _subject_writer(state, b"a", subject="left-over-from-entra")
    _private, public_key = generate_signing_keypair()

    # Minting while claiming somebody else's identity is neither refused nor
    # believed -- the header is simply not read.
    minted = client.post(
        MINT_PATH, headers=_mint_headers(writer, nonce="ignored", subject="thief")
    )
    assert minted.status_code == 200, minted.text
    paired = _pair(
        client, minted.json()["pairing_code"], public_key, subject="someone-else"
    )
    assert paired.status_code == 200, paired.text
    device = state.credentials.resolve_device(paired.json()["device_credential"])
    assert device.subject is None
    assert device.namespace == writer.namespace


def test_the_code_demands_a_subject_not_the_route(attested_cloud):
    """Even where one is attested, the route itself never demands a subject.

    Mint always binds one in an attested deployment, so this constructs the
    case the API cannot currently reach -- a subject-less code where a gateway
    exists -- to pin the property directly.  It matters because the route must
    stay correct if anything ever mints a code without a subject: the demand
    has to come from the stored record, so that it exists exactly when there
    is something to enforce.
    """
    pytest.importorskip("cryptography")
    _config, state, client = attested_cloud
    writer = _writer(state, b"a")
    _private, public_key = generate_signing_keypair()
    minted = state.pairings.create(writer.namespace, writer.local_user_scope)
    assert minted.subject is None

    paired = client.post(PAIR_PATH, headers={"X-Gateway-Request-Proof": "proof-value"},
                         json={"code": minted.code, "public_key": public_key.hex()})
    assert paired.status_code == 200, paired.text
    device = state.credentials.resolve_device(paired.json()["device_credential"])
    assert device.namespace == writer.namespace
    assert device.subject is None


def test_credentials_outliving_their_gateway_still_resolve():
    """A subject bound while a gateway existed must not lock a rider out.

    When the gateway goes, contexts and devices issued before it went still
    carry a subject.  Nothing can attest one any more, so the check is
    skipped rather than failed -- the bearer context token and the device's
    private key remain the whole authorization, which is what they always
    actually were.
    """
    pytest.importorskip("cryptography")
    config = CloudConfig(
        server_secret=SECRET,
        operator_token="operator-token",
        require_gateway_proof=False,
        require_verified_subject=False,
        clock=lambda: 1_000,
    )
    state = CloudState.create(config)
    client = TestClient(create_cloud_app(config, state=state))
    writer = _writer(state, b"a")
    body = _batch()
    assert client.post(
        "/api/v1/sync/batches", headers=_headers(writer, body), content=body
    ).status_code == 200

    # Issued back when a gateway attested "old-rider".
    token, context = state.credentials.issue_reader_context_for_scope(
        writer.namespace, writer.local_user_scope, "old-rider"
    )
    assert context.subject == "old-rider"
    listed = client.get(
        "/api/v1/context/activities", headers={"Authorization": f"Bearer {token}"}
    )
    assert listed.status_code == 200, listed.text
    assert [item["id"] for item in listed.json()["items"]] == ["activity-1"]

    device, device_key = _device(
        state, scope=writer.local_user_scope, subject="old-rider",
    )
    refresh_headers = {
        name: value
        for name, value in _refresh_headers(
            device, device_key, nonce="outlived"
        ).items()
        if name != "X-Verified-Entra-Subject"
    }
    refreshed = client.post("/api/v1/context/refresh", headers=refresh_headers)
    assert refreshed.status_code == 200, refreshed.text


def test_a_code_bound_to_an_identity_is_refused_once_nothing_attests_it():
    """Fail closed on a code that outlived its issuer.

    A code minted seconds before the gateway was removed still carries the
    subject that gateway attested.  Afterwards nothing can verify that
    subject, so the code is refused rather than redeemed on the strength of a
    header the caller wrote -- otherwise anyone who knows the rider's account
    name could spend it.  The rider mints another; it costs 15 seconds.
    """
    pytest.importorskip("cryptography")
    config = CloudConfig(
        server_secret=SECRET,
        operator_token="operator-token",
        require_gateway_proof=False,
        require_verified_subject=False,
        clock=lambda: 1_000,
    )
    state = CloudState.create(config)
    client = TestClient(create_cloud_app(config, state=state))
    writer = _writer(state, b"a")
    _private, public_key = generate_signing_keypair()
    stale = state.pairings.create(
        writer.namespace, writer.local_user_scope, subject="old-rider"
    )

    # Even presenting exactly the right subject does not redeem it: the header
    # is not read, because nothing vouches for it.
    for subject in ("old-rider", "anyone-else", None):
        refused = _pair(client, stale.code, public_key, subject=subject, headers={})
        assert refused.status_code == 404, refused.text
        assert refused.json() == {"detail": "not found"}

    # A code minted now, under the deployment that actually exists, works.
    fresh = state.pairings.create(writer.namespace, writer.local_user_scope)
    accepted = _pair(client, fresh.code, public_key, subject=None, headers={})
    assert accepted.status_code == 200, accepted.text


def test_a_placeholder_proof_value_cannot_claim_a_gateway():
    """`gateway_attests_subject` must not be satisfiable by a placeholder.

    The flag is what licenses every route to trust the verified-subject
    header, so a whitespace or trivially short proof value would let a
    deployment claim a gateway vouches for the subject while the "secret"
    guarding it is guessable -- and whoever guessed it could then dictate
    the subject.  An empty value is still allowed and still means exactly
    "no gateway"; it is the non-empty placeholder that is refused.
    """

    def _cfg(**overrides):
        return CloudConfig(
            server_secret=SECRET, operator_token="operator-token", **overrides
        )

    for placeholder in (" ", "\t", "  \n ", "0", "proof", "1234567"):
        with pytest.raises(ValueError, match="must be a secret"):
            _cfg(gateway_proof_value=placeholder)

    # An empty value remains legal: it is how a gateway-less deployment
    # declares itself, and CloudState.create refuses to serve a verified
    # subject on top of it.
    assert not _cfg(gateway_proof_value="").gateway_attests_subject
    assert _cfg(gateway_proof_value="proof-value").gateway_attests_subject


def _load_points(revision, ids):
    return [CloudObject(oid, "load_point", revision, {"tss": 10}) for oid in ids]


def test_mobile_delta_checkpoint_is_pinned_at_page_one_so_midpagination_edits_survive(cloud):
    """A mid-pagination mutation must never be skipped by the next poll.

    ``docs/cloud-sync.md`` tells the client to checkpoint the envelope
    ``revision`` only after consuming every page.  If each page recomputed
    that revision, an object delivered on an early page and then mutated
    before the last page would sit *behind* the cursor (excluded by
    ``object_id > after``) while the checkpoint advanced *past* its new
    revision -- the client would checkpoint a revision it never actually saw
    that object at, and never be offered it again.
    """
    _config, state, client = cloud
    token, context = _mobile_reader(state)
    namespace, scope = context.namespace, context.local_user_scope
    ids = ["c000", "c001", "c002", "c003", "c004", "c005"]
    # The phone is caught up at scope revision 10.
    _apply_mobile_batch(state, namespace, scope, "seed", 10, _load_points(10, ids))
    headers = _mobile_headers(token)
    assert client.get(
        "/api/v1/context/dashboard?since=10", headers=headers
    ).json()["items"] == []

    # Everything is bumped to 11; the phone starts paging the delta.
    _apply_mobile_batch(state, namespace, scope, "bump", 11, _load_points(11, ids))
    page = client.get(
        "/api/v1/context/dashboard?since=10&limit=2", headers=headers
    ).json()
    assert [item["id"] for item in page["items"]] == ["c000", "c001"]
    pinned = page["revision"]
    assert pinned == 11

    # c000 is mutated after it was already delivered on page 1.  It now sorts
    # BEFORE the cursor, so no later page can carry it.
    _apply_mobile_batch(state, namespace, scope, "mutate", 12,
                        _load_points(12, ["c000"]))

    delivered = [item["id"] for item in page["items"]]
    while page["next_cursor"]:
        page = client.get(
            "/api/v1/context/dashboard?since=10&limit=2&cursor=" + page["next_cursor"],
            headers=headers,
        ).json()
        delivered.extend(item["id"] for item in page["items"])
        # Every page carries the checkpoint pinned when page 1 was read.
        assert page["revision"] == pinned, (
            "checkpoint advanced mid-pagination past an object already delivered"
        )
    assert delivered == ids

    # The client checkpoints the last page's revision and polls again.  The
    # mid-pagination edit to c000 must still be delivered.
    checkpoint = page["revision"]
    follow_up = client.get(
        f"/api/v1/context/dashboard?since={checkpoint}", headers=headers
    ).json()
    assert [item["id"] for item in follow_up["items"]] == ["c000"], (
        "c000 was mutated to revision 12 mid-pagination and is now unreachable"
    )


def _mint_cursor(state, namespace, scope, payload):
    """Sign an arbitrary cursor payload with the server's real cursor key.

    This is the strongest attacker a cursor faces short of the server secret:
    someone who can present any *validly signed* payload.  The pinned revision
    must be enforced by the decoder, not merely by what the encoder happens to
    emit.
    """
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    mac = hmac.new(
        _cursor_key(state.config.server_secret, namespace, scope),
        body, hashlib.sha256,
    ).digest()
    return base64.urlsafe_b64encode(body + mac).decode().rstrip("=")


def _paged_scope(state, client, scope="mobile-scope"):
    token, context = _mobile_reader(state, scope=scope)
    _apply_mobile_batch(
        state, context.namespace, context.local_user_scope, "pages", 4,
        _load_points(4, ["p0", "p1", "p2"]),
    )
    headers = _mobile_headers(token)
    first = client.get(
        "/api/v1/context/dashboard?limit=1", headers=headers
    ).json()
    assert first["next_cursor"]
    return context, headers, first


def test_mobile_cursor_without_a_pinned_revision_is_rejected(cloud):
    """A fieldless cursor is invalid, not silently defaulted.

    Defaulting a missing pin to the freshly read scope revision would restore
    exactly the per-page checkpoint recomputation that loses objects mutated
    mid-pagination.  No mobile client has shipped, so there is nothing to keep
    compatible.
    """
    _config, state, client = cloud
    context, headers, first = _paged_scope(state, client)
    legacy = _mint_cursor(
        state, context.namespace, context.local_user_scope,
        {"route": "dashboard", "since": None, "after": "p0"},
    )
    response = client.get(
        "/api/v1/context/dashboard?limit=1&cursor=" + legacy, headers=headers
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "invalid cursor"

    # Neither a null, a bool (an ``int`` subclass in Python), a float, a
    # string, nor a negative revision may stand in for the pin.
    for bad in (None, True, False, 1.5, "4", -1):
        forged = _mint_cursor(
            state, context.namespace, context.local_user_scope,
            {"route": "dashboard", "since": None, "after": "p0", "revision": bad},
        )
        assert client.get(
            "/api/v1/context/dashboard?limit=1&cursor=" + forged, headers=headers
        ).status_code == 400, bad

    # The genuine cursor still works, so the rejection is about the field and
    # not about the minting helper.
    assert client.get(
        "/api/v1/context/dashboard?limit=1&cursor=" + first["next_cursor"],
        headers=headers,
    ).status_code == 200


def test_mobile_cursor_with_a_tampered_pinned_revision_fails_the_mac(cloud):
    """The pin is inside the signed payload, so editing it breaks the MAC."""
    _config, state, client = cloud
    context, headers, first = _paged_scope(state, client)
    raw = first["next_cursor"]
    encoded = raw.encode("ascii")
    encoded += b"=" * (-len(encoded) % 4)
    value = base64.urlsafe_b64decode(encoded)
    payload, mac = value[:-32], value[-32:]
    decoded = json.loads(payload.decode())
    assert decoded["revision"] == 4
    # Re-encode with a bumped pin but the original MAC.
    decoded["revision"] = 9_999
    forged = json.dumps(decoded, separators=(",", ":"), sort_keys=True).encode()
    tampered = base64.urlsafe_b64encode(forged + mac).decode().rstrip("=")
    assert tampered != raw
    response = client.get(
        "/api/v1/context/dashboard?limit=1&cursor=" + tampered, headers=headers
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "invalid cursor"


def test_mobile_cursor_cannot_be_replayed_across_scopes(cloud):
    """Scope binding lives in the HMAC key, so another scope cannot verify it."""
    _config, state, client = cloud
    context_a, headers_a, first_a = _paged_scope(state, client, scope="scope-a")
    context_b, headers_b, _first_b = _paged_scope(state, client, scope="scope-b")
    assert context_a.local_user_scope != context_b.local_user_scope
    assert client.get(
        "/api/v1/context/dashboard?limit=1&cursor=" + first_a["next_cursor"],
        headers=headers_b,
    ).status_code == 400
    # A cursor forged against scope B's key still cannot be used in scope A.
    cross = _mint_cursor(
        state, context_b.namespace, context_b.local_user_scope,
        {"route": "dashboard", "since": None, "after": "p0", "revision": 4},
    )
    assert client.get(
        "/api/v1/context/dashboard?limit=1&cursor=" + cross, headers=headers_a
    ).status_code == 400
    # ...and the owning scope is unaffected.
    assert client.get(
        "/api/v1/context/dashboard?limit=1&cursor=" + first_a["next_cursor"],
        headers=headers_a,
    ).status_code == 200


def test_mobile_pinned_checkpoint_never_exceeds_the_scope_revision(cloud):
    """The pin only ever holds the checkpoint back, never pushes it forward.

    A cursor is signed, so its pin cannot be attacker-chosen, but it can be
    genuinely stale (an abandoned walk resumed later).  A stale pin must still
    be a floor: it is reported verbatim, so the client re-reads rather than
    skipping anything.
    """
    _config, state, client = cloud
    context, headers, first = _paged_scope(state, client)
    assert first["revision"] == 4
    _apply_mobile_batch(
        state, context.namespace, context.local_user_scope, "later", 40,
        _load_points(40, ["p1"]),
    )
    later = client.get(
        "/api/v1/context/dashboard?limit=1&cursor=" + first["next_cursor"],
        headers=headers,
    ).json()
    assert later["revision"] == 4 < state.store.revision(
        context.namespace, context.local_user_scope
    )
    # A fresh walk with no cursor sees the current scope revision.
    assert client.get(
        "/api/v1/context/dashboard?limit=1", headers=headers
    ).json()["revision"] == 40


def test_mobile_concurrent_reads_do_not_conflict_on_the_writer_lease(cloud):
    """Overlapping phone reads must both succeed.

    The read path used to take the writer's exclusive blob lease, and
    ``collection`` catches only ``QuotaExceeded``, so a lease conflict
    surfaced as a bare 500.  This drives real concurrent requests through the
    route and asserts none of them fails.
    """
    import threading

    _config, state, client = cloud
    token, context = _mobile_reader(state)
    _apply_mobile_batch(
        state, context.namespace, context.local_user_scope, "concurrent", 3,
        _load_points(3, [f"k{index}" for index in range(8)]),
    )
    headers = _mobile_headers(token)
    barrier = threading.Barrier(6)
    results = []
    lock = threading.Lock()

    def _read():
        barrier.wait(timeout=10)
        response = client.get("/api/v1/context/dashboard?limit=2", headers=headers)
        with lock:
            results.append((response.status_code, response.json()))

    # daemon=True so a thread that wedges inside TestClient cannot outlive
    # this test.  A non-daemon thread abandoned by a join timeout stays alive
    # for the rest of the pytest process, competing with every test that runs
    # after it -- including the browser suite, whose Playwright waits are on a
    # real-time budget.
    threads = [threading.Thread(target=_read, daemon=True) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
    # Assert the joins actually completed.  Without this a wedged thread is
    # silently abandoned and the failure surfaces somewhere unrelated later.
    stuck = [thread for thread in threads if thread.is_alive()]
    assert not stuck, f"{len(stuck)} reader thread(s) did not finish"
    assert [status for status, _ in results] == [200] * 6
    for _status, body in results:
        assert [item["id"] for item in body["items"]] == ["k0", "k1"]
        assert body["revision"] == 3
