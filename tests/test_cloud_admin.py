import hashlib
import json
import urllib.error

import pytest
from fastapi.testclient import TestClient

from wattracker.cloud import admin
from wattracker.cloud.api import CloudConfig, CloudState, create_cloud_app
from wattracker.cloud.limits import (
    PUBLIC_UNAVAILABLE_DETAIL,
    PUBLIC_UNAVAILABLE_RETRY_AFTER,
    QuotaExceeded,
)
from wattracker.cloud.security import (
    MemorySecurityStateBackend,
    SecurityStateUnavailable,
    canonical_request,
    digest_body,
    new_installation_id,
    sign_request,
)


SECRET = b"cloud-admin-test-server-secret-32-bytes"
TOKEN = "operator-token-for-admin-tests"
GATEWAY_PROOF = "gateway-proof-for-admin-tests"


def _raise_public_unavailable():
    raise QuotaExceeded(
        PUBLIC_UNAVAILABLE_DETAIL,
        status_code=503,
        retry_after=PUBLIC_UNAVAILABLE_RETRY_AFTER,
    )


def _raise_security_state_unavailable():
    raise SecurityStateUnavailable("security backend unavailable")


def _admin_response_contract(response):
    return (
        response.status_code,
        response.content,
        response.headers.get("Retry-After"),
    )


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://cloud.example",
        "https://cloud.example/",
        "http://localhost:8000",
        "http://127.0.0.1:8000",
        "http://[::1]:8000",
    ],
)
def test_validate_endpoint_accepts_https_and_exact_loopback_http(endpoint):
    assert admin._validate_endpoint(endpoint).startswith(endpoint.rstrip("/"))


@pytest.mark.parametrize(
    "endpoint",
    [
        "cloud.example",
        "/api",
        "http://example.com",
        "http://127.0.0.1.example",
        "http://localhost.evil",
        "https://user:password@cloud.example",
        "https://cloud.example/?token=secret",
        "https://cloud.example/#fragment",
    ],
)
def test_validate_endpoint_rejects_non_absolute_or_unsafe_urls(endpoint):
    with pytest.raises(admin.AdminError):
        admin._validate_endpoint(endpoint)


@pytest.mark.parametrize(
    "argv",
    [
        ["invite", "operator-token-that-must-not-appear"],
        ["--operator-token", "operator-token-that-must-not-appear", "invite"],
    ],
)
def test_invalid_token_arguments_are_rejected_without_echo(argv, capsys):
    assert admin.main(argv) == 2
    captured = capsys.readouterr()
    assert "operator-token-that-must-not-appear" not in captured.err
    assert "operator-token-that-must-not-appear" not in captured.out


def test_admin_routes_list_and_revoke_durable_writer_installation():
    backend = MemorySecurityStateBackend()
    config = CloudConfig(
        server_secret=SECRET,
        operator_token=TOKEN,
        gateway_proof_value=GATEWAY_PROOF,
    )
    state = CloudState.create(config, security_backend=backend)
    writer = state.credentials.register_writer(
        new_installation_id(), "rider-scope", b"w" * 32, b"s" * 32
    )
    row_key = hashlib.sha256(writer.credential_id.encode("ascii")).hexdigest()
    persisted = backend.read("writer", row_key)
    assert persisted is not None
    assert "credential_id" not in persisted

    # Recreate the registries around the same backend to exercise the durable
    # row rather than the first process's in-memory writer cache.
    restarted = CloudState.create(config, security_backend=backend)
    assert restarted.credentials.authenticate_writer(
        writer.credential_id, b"s" * 32
    ) is not None
    with TestClient(create_cloud_app(config, state=restarted)) as client:
        unauthorized = client.get(
            "/api/v1/admin/installations",
            headers={
                "X-Operator-Token": "wrong-token",
                "X-Gateway-Request-Proof": GATEWAY_PROOF,
            },
        )
        assert unauthorized.status_code == 404

        listed = client.get(
            "/api/v1/admin/installations",
            headers={
                "X-Operator-Token": TOKEN,
                "X-Gateway-Request-Proof": GATEWAY_PROOF,
            },
        )
        assert listed.status_code == 200
        assert listed.json() == {
            "installations": [{
                "operator_handle": row_key,
                "status": "active",
                "capabilities": ["read", "write"],
                "signature_algorithm": "hmac-sha256",
            }]
        }
        assert "namespace" not in json.dumps(listed.json())
        assert "local_user_scope" not in json.dumps(listed.json())

        revoked = client.post(
            f"/api/v1/admin/installations/{row_key}/revoke",
            headers={
                "X-Operator-Token": TOKEN,
                "X-Gateway-Request-Proof": GATEWAY_PROOF,
            },
        )
        assert revoked.status_code == 200
        assert revoked.json() == {
            "operator_handle": row_key,
            "status": "revoked",
        }

        listed_again = client.get(
            "/api/v1/admin/installations",
            headers={
                "X-Operator-Token": TOKEN,
                "X-Gateway-Request-Proof": GATEWAY_PROOF,
            },
        )
        assert listed_again.json()["installations"][0]["status"] == "revoked"


def test_admin_routes_list_and_revoke_legacy_durable_writer_row_in_place():
    backend = MemorySecurityStateBackend()
    config = CloudConfig(
        server_secret=SECRET,
        operator_token=TOKEN,
        gateway_proof_value=GATEWAY_PROOF,
    )
    state = CloudState.create(config, security_backend=backend)
    writer = state.credentials.register_writer(
        new_installation_id(), "rider-scope", b"w" * 32, b"s" * 32
    )
    row_key = hashlib.sha256(writer.credential_id.encode("ascii")).hexdigest()
    legacy_value = backend.read("writer", row_key)
    assert legacy_value is not None
    legacy_value["credential_id"] = writer.credential_id
    backend.write("writer", row_key, legacy_value)

    restarted = CloudState.create(config, security_backend=backend)
    assert restarted.credentials.authenticate_writer(
        writer.credential_id, b"s" * 32
    ) is not None
    with TestClient(create_cloud_app(config, state=restarted)) as client:
        listed = client.get(
            "/api/v1/admin/installations",
            headers={
                "X-Operator-Token": TOKEN,
                "X-Gateway-Request-Proof": GATEWAY_PROOF,
            },
        )
        assert listed.status_code == 200
        assert listed.json() == {
            "installations": [{
                "operator_handle": row_key,
                "status": "active",
                "capabilities": ["read", "write"],
                "signature_algorithm": "hmac-sha256",
            }]
        }
        assert "namespace" not in json.dumps(listed.json())
        assert "local_user_scope" not in json.dumps(listed.json())
        assert "verification_key" not in json.dumps(listed.json())
        assert "subscription_verifier" not in json.dumps(listed.json())

        revoked = client.post(
            f"/api/v1/admin/installations/{row_key}/revoke",
            headers={
                "X-Operator-Token": TOKEN,
                "X-Gateway-Request-Proof": GATEWAY_PROOF,
            },
        )
        assert revoked.status_code == 200
        assert revoked.json() == {
            "operator_handle": row_key,
            "status": "revoked",
        }

    persisted = backend.read("writer", row_key)
    assert persisted is not None
    assert "credential_id" not in persisted
    assert persisted["active"] is False
    assert persisted["revoked"] is True
    assert backend.read("writer", hashlib.sha256(row_key.encode("ascii")).hexdigest()) is None
    assert restarted.credentials.authenticate_writer(
        writer.credential_id, b"s" * 32
    ) is None


def test_admin_revoke_rejects_unknown_and_malformed_handles():
    config = CloudConfig(
        server_secret=SECRET,
        operator_token=TOKEN,
        gateway_proof_value=GATEWAY_PROOF,
    )
    state = CloudState.create(config)
    with TestClient(create_cloud_app(config, state=state)) as client:
        for handle in ("not-a-handle", "f" * 63, "f" * 65):
            response = client.post(
                f"/api/v1/admin/installations/{handle}/revoke",
                headers={
                    "X-Operator-Token": TOKEN,
                    "X-Gateway-Request-Proof": GATEWAY_PROOF,
                },
            )
            assert response.status_code == 404


def test_list_installations_rejects_old_shape_without_echoing_response_or_token(monkeypatch):
    old_installation_id = "legacy-installation-value-that-must-not-appear"
    payload = {
        "installations": [{
            "installation_id": old_installation_id,
            "status": "active",
            "capabilities": [TOKEN],
            "signature_algorithm": "hmac-sha256",
        }]
    }
    monkeypatch.setattr(admin, "_request_json", lambda *args, **kwargs: payload)

    with pytest.raises(admin.AdminError) as raised:
        admin._list_installations("https://cloud.example", TOKEN)

    message = str(raised.value)
    assert message == (
        "cloud admin response uses the old installation_id field; "
        "the deployed image predates this CLI; readImage/syncImage must be "
        "re-pinned to the current digest and redeployed"
    )
    assert old_installation_id not in message
    assert TOKEN not in message


def test_list_installations_preserves_generic_error_for_other_malformed_payload(monkeypatch):
    payload = {
        "installations": [{
            "operator_handle": None,
            "status": "active",
            "capabilities": [],
            "signature_algorithm": "hmac-sha256",
        }]
    }
    monkeypatch.setattr(admin, "_request_json", lambda *args, **kwargs: payload)

    with pytest.raises(admin.AdminError, match="^cloud admin response was invalid$"):
        admin._list_installations("https://cloud.example", TOKEN)


def test_admin_requires_gateway_proof_in_addition_to_operator_token():
    # Gateway-proof-first is a deliberate security exception to #320 criterion
    # 2: reordering would expose outage state to unauthenticated callers, so
    # this oracle remains accepted by review.
    config = CloudConfig(
        server_secret=SECRET,
        operator_token=TOKEN,
        gateway_proof_value=GATEWAY_PROOF,
    )
    state = CloudState.create(config)
    writer = state.credentials.register_writer(
        new_installation_id(), "rider-scope", b"w" * 32, b"s" * 32
    )
    with TestClient(create_cloud_app(config, state=state)) as client:
        missing = client.get(
            "/api/v1/admin/installations",
            headers={"X-Operator-Token": TOKEN},
        )
        wrong = client.get(
            "/api/v1/admin/installations",
            headers={
                "X-Operator-Token": TOKEN,
                "X-Gateway-Request-Proof": "wrong-proof",
            },
        )
        correct = client.get(
            "/api/v1/admin/installations",
            headers={
                "X-Operator-Token": TOKEN,
                "X-Gateway-Request-Proof": GATEWAY_PROOF,
            },
        )
        missing_revoke = client.post(
            f"/api/v1/admin/installations/{writer.credential_id}/revoke",
            headers={"X-Operator-Token": TOKEN},
        )
        correct_revoke = client.post(
            f"/api/v1/admin/installations/{writer.credential_id}/revoke",
            headers={
                "X-Operator-Token": TOKEN,
                "X-Gateway-Request-Proof": GATEWAY_PROOF,
            },
        )
    assert missing.status_code == 404
    assert wrong.status_code == 404
    assert correct.status_code == 200
    assert missing_revoke.status_code == 404
    assert correct_revoke.status_code == 200
    assert correct_revoke.json() == {
        "operator_handle": hashlib.sha256(writer.credential_id.encode("ascii")).hexdigest(),
        "status": "revoked",
    }


def test_admin_operator_handle_round_trips_without_durable_backend():
    config = CloudConfig(
        server_secret=SECRET,
        operator_token=TOKEN,
        gateway_proof_value=GATEWAY_PROOF,
    )
    state = CloudState.create(config)
    writer = state.credentials.register_writer(
        new_installation_id(), "rider-scope", b"w" * 32, b"s" * 32
    )
    headers = {
        "X-Operator-Token": TOKEN,
        "X-Gateway-Request-Proof": GATEWAY_PROOF,
    }
    with TestClient(create_cloud_app(config, state=state)) as client:
        listed = client.get("/api/v1/admin/installations", headers=headers)
        handle = listed.json()["installations"][0]["operator_handle"]
        revoked = client.post(
            f"/api/v1/admin/installations/{handle}/revoke", headers=headers
        )

    assert handle == hashlib.sha256(writer.credential_id.encode("ascii")).hexdigest()
    assert revoked.status_code == 200
    assert revoked.json() == {
        "operator_handle": handle,
        "status": "revoked",
    }


def test_admin_list_preserves_kill_switch_503_for_any_operator_token(monkeypatch):
    config = CloudConfig(
        server_secret=SECRET,
        operator_token=TOKEN,
        gateway_proof_value=GATEWAY_PROOF,
    )
    state = CloudState.create(config)
    monkeypatch.setattr(state.quotas, "kill_state", _raise_public_unavailable)

    with TestClient(create_cloud_app(config, state=state)) as client:
        responses = [
            client.get(
                "/api/v1/admin/installations",
                headers={
                    "X-Operator-Token": token,
                    "X-Gateway-Request-Proof": GATEWAY_PROOF,
                },
            )
            for token in (TOKEN, "wrong-token")
        ]

    assert [_admin_response_contract(response) for response in responses] == [
        (
            503,
            b'{"detail":"public API unavailable"}',
            str(PUBLIC_UNAVAILABLE_RETRY_AFTER),
        ),
    ] * 2
    assert all(response.json() == {"detail": PUBLIC_UNAVAILABLE_DETAIL} for response in responses)


def test_admin_revoke_preserves_kill_switch_503_without_an_installation_or_token_oracle(
    monkeypatch,
):
    config = CloudConfig(
        server_secret=SECRET,
        operator_token=TOKEN,
        gateway_proof_value=GATEWAY_PROOF,
    )
    state = CloudState.create(config)
    writer = state.credentials.register_writer(
        new_installation_id(), "rider-scope", b"w" * 32, b"s" * 32
    )
    fake_installation_id = "f" * 64
    monkeypatch.setattr(state.quotas, "kill_state", _raise_public_unavailable)

    with TestClient(create_cloud_app(config, state=state)) as client:
        responses = [
            client.post(
                f"/api/v1/admin/installations/{installation_id}/revoke",
                headers={
                    "X-Operator-Token": token,
                    "X-Gateway-Request-Proof": GATEWAY_PROOF,
                },
            )
            for token in (TOKEN, "wrong-token")
            for installation_id in (writer.credential_id, fake_installation_id)
        ]

    assert [_admin_response_contract(response) for response in responses] == [
        (
            503,
            b'{"detail":"public API unavailable"}',
            str(PUBLIC_UNAVAILABLE_RETRY_AFTER),
        ),
    ] * 4
    assert all(response.json() == {"detail": PUBLIC_UNAVAILABLE_DETAIL} for response in responses)


def test_admin_security_state_unavailable_is_neutral_and_retryable_without_oracles(
    monkeypatch,
):
    config = CloudConfig(
        server_secret=SECRET,
        operator_token=TOKEN,
        gateway_proof_value=GATEWAY_PROOF,
    )
    state = CloudState.create(config)
    writer = state.credentials.register_writer(
        new_installation_id(), "rider-scope", b"w" * 32, b"s" * 32
    )
    fake_installation_id = "f" * 64
    monkeypatch.setattr(state.quotas, "kill_state", _raise_security_state_unavailable)

    with TestClient(create_cloud_app(config, state=state)) as client:
        listed = [
            client.get(
                "/api/v1/admin/installations",
                headers={
                    "X-Operator-Token": token,
                    "X-Gateway-Request-Proof": GATEWAY_PROOF,
                },
            )
            for token in (TOKEN, "wrong-token")
        ]
        revoked = [
            client.post(
                f"/api/v1/admin/installations/{installation_id}/revoke",
                headers={
                    "X-Operator-Token": token,
                    "X-Gateway-Request-Proof": GATEWAY_PROOF,
                },
            )
            for token in (TOKEN, "wrong-token")
            for installation_id in (writer.credential_id, fake_installation_id)
        ]
        gateway_refused = client.get(
            "/api/v1/admin/installations",
            headers={
                "X-Operator-Token": TOKEN,
                "X-Gateway-Request-Proof": "wrong-proof",
            },
        )

    expected = (
        503,
        b'{"detail":"public API unavailable"}',
        str(PUBLIC_UNAVAILABLE_RETRY_AFTER),
    )
    assert [_admin_response_contract(response) for response in listed] == [expected] * 2
    assert [_admin_response_contract(response) for response in revoked] == [expected] * 4
    assert gateway_refused.status_code == 404
    assert gateway_refused.json() == {"detail": "not found"}


def test_admin_revoke_guard_contention_is_neutral_for_known_installation():
    """Known-installation contention uses the in-memory Azure-style backend."""
    backend = MemorySecurityStateBackend()
    config = CloudConfig(
        server_secret=SECRET,
        operator_token=TOKEN,
        gateway_proof_value=GATEWAY_PROOF,
    )
    holder = CloudState.create(config, security_backend=backend)
    contender = CloudState.create(config, security_backend=backend)
    writer = holder.credentials.register_writer(
        new_installation_id(), "rider-scope", b"w" * 32, b"s" * 32
    )
    headers = {
        "X-Operator-Token": TOKEN,
        "X-Gateway-Request-Proof": GATEWAY_PROOF,
    }

    # This is the real scope-guard implementation on the in-memory backend,
    # not a live Azure test or a monkeypatched exception.
    with holder.credentials._lock, holder.credentials._pairing_scope_guard_locked(
        writer.namespace, writer.local_user_scope
    ):
        with TestClient(create_cloud_app(config, state=contender)) as client:
            response = client.post(
                f"/api/v1/admin/installations/{writer.credential_id}/revoke",
                headers=headers,
            )

    expected = (
        503,
        b'{"detail":"public API unavailable"}',
        str(PUBLIC_UNAVAILABLE_RETRY_AFTER),
    )
    assert _admin_response_contract(response) == expected
    assert holder.credentials.authenticate_writer(
        writer.credential_id, writer.subscription_key
    ) is not None


def test_admin_revoke_cascades_to_same_scope_device_and_is_idempotent():
    backend = MemorySecurityStateBackend()
    config = CloudConfig(
        server_secret=SECRET,
        operator_token=TOKEN,
        gateway_proof_value=GATEWAY_PROOF,
    )
    state = CloudState.create(config, security_backend=backend)
    writer = state.credentials.register_writer(
        new_installation_id(), "rider-scope", b"w" * 32, b"s" * 32
    )
    device = state.credentials.register_device_for_scope(
        writer.namespace,
        writer.local_user_scope,
        b"d" * 32,
        subscription_key=b"device-subscription",
    )
    assert state.credentials.authenticate_writer(
        writer.credential_id, writer.subscription_key
    ) is not None
    assert state.credentials.authenticate_device(
        device.credential_id, device.subscription_key
    ) is not None
    row_key = hashlib.sha256(writer.credential_id.encode("ascii")).hexdigest()
    restarted = CloudState.create(config, security_backend=backend)
    headers = {
        "X-Operator-Token": TOKEN,
        "X-Gateway-Request-Proof": GATEWAY_PROOF,
    }

    with TestClient(create_cloud_app(config, state=restarted)) as client:
        first = client.post(
            f"/api/v1/admin/installations/{row_key}/revoke",
            headers=headers,
        )
        retry = client.post(
            f"/api/v1/admin/installations/{row_key}/revoke",
            headers=headers,
        )

    assert first.status_code == 200
    assert retry.status_code == 200
    assert first.json() == retry.json() == {
        "operator_handle": row_key,
        "status": "revoked",
    }
    assert state.credentials.authenticate_writer(
        writer.credential_id, writer.subscription_key
    ) is None
    assert state.credentials.authenticate_device(
        device.credential_id, device.subscription_key
    ) is None


def test_admin_revoke_invalidates_preexisting_pairing_code_without_collateral():
    backend = MemorySecurityStateBackend()
    config = CloudConfig(
        server_secret=SECRET,
        operator_token=TOKEN,
        gateway_proof_value=GATEWAY_PROOF,
        clock=lambda: 1_000,
    )
    state = CloudState.create(config, security_backend=backend)
    revoked_writer = state.credentials.register_writer(
        new_installation_id(), "revoked-scope", b"a" * 32, b"s" * 32
    )
    other_writer = state.credentials.register_writer(
        new_installation_id(), "other-scope", b"b" * 32, b"t" * 32
    )

    def mint(client, writer, nonce):
        path = "/api/v1/devices/pairing-codes"
        canonical = canonical_request(
            "POST",
            path,
            writer.namespace,
            1_000,
            nonce,
            digest_body(b""),
            "device-pairing-code",
            "0",
        )
        response = client.post(
            path,
            headers={
                "Ocp-Apim-Subscription-Key": writer.subscription_key.decode(),
                "X-Gateway-Request-Proof": GATEWAY_PROOF,
                "X-Verified-Entra-Subject": "rider",
                "X-Writer-Credential": writer.credential_id,
                "X-Writer-Timestamp": "1000",
                "X-Writer-Nonce": nonce,
                "X-Writer-Idempotency-Key": "device-pairing-code",
                "X-Writer-Revision": "0",
                "X-Writer-Signature": sign_request(
                    writer.signing_key, canonical
                ),
            },
        )
        assert response.status_code == 200, response.text
        return response.json()["pairing_code"]

    with TestClient(create_cloud_app(config, state=state)) as client:
        revoked_code = mint(client, revoked_writer, "mint-revoked")
        other_code = mint(client, other_writer, "mint-other")

    # Rebuild around the durable backend so both invalidation and owner checks
    # run without relying on the first process's caches.
    restarted = CloudState.create(config, security_backend=backend)
    common_headers = {
        "X-Gateway-Request-Proof": GATEWAY_PROOF,
        "X-Verified-Entra-Subject": "rider",
    }
    with TestClient(create_cloud_app(config, state=restarted)) as client:
        revoked = client.post(
            f"/api/v1/admin/installations/{revoked_writer.credential_id}/revoke",
            headers={
                "X-Operator-Token": TOKEN,
                "X-Gateway-Request-Proof": GATEWAY_PROOF,
            },
        )
        blocked = client.post(
            "/api/v1/devices/pair",
            headers=common_headers,
            json={"code": revoked_code, "public_key": (b"x" * 32).hex()},
        )
        unaffected = client.post(
            "/api/v1/devices/pair",
            headers=common_headers,
            json={"code": other_code, "public_key": (b"y" * 32).hex()},
        )

    assert revoked.status_code == 200
    assert blocked.status_code == 404
    assert blocked.json() == {"detail": "not found"}
    assert unaffected.status_code == 200, unaffected.text
    unaffected_device = restarted.credentials.resolve_device(
        unaffected.json()["device_credential"]
    )
    assert unaffected_device is not None
    assert unaffected_device.namespace == other_writer.namespace


@pytest.mark.parametrize("installation_id", [
    TOKEN,
    "g" * 64,
    "A" * 64,
    "a" * 63,
    "a" * 65,
])
def test_revoke_installation_rejects_non_hex_handle_before_transport(
    monkeypatch, installation_id
):
    calls = []

    def unexpected_transport(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("transport must not be called")

    monkeypatch.setattr(admin, "_request_json", unexpected_transport)
    with pytest.raises(admin.AdminError, match="invalid operator handle or credential id"):
        admin._revoke_installation(
            "https://cloud.example", TOKEN, installation_id
        )
    assert calls == []


def test_commands_dispatch_and_print_json(monkeypatch, capsys):
    installation_id = "a" * 64
    calls = []

    def fake_request(endpoint, path, token, *, method, **kwargs):
        calls.append((endpoint, path, token, method))
        assert token == TOKEN
        if path == "/api/v1/enrollment/start":
            return {"invitation": "invitation-value", "expires_at": 1900}
        if path == "/api/v1/admin/installations":
            return {
                "installations": [{
                    "operator_handle": installation_id,
                    "status": "active",
                    "capabilities": ["read", "write"],
                    "signature_algorithm": "hmac-sha256",
                }]
            }
        if path == "/api/v1/admin/version":
            return {"commit": "a" * 40}
        assert path == f"/api/v1/admin/installations/{installation_id}/revoke"
        return {"operator_handle": installation_id, "status": "revoked"}

    monkeypatch.setattr(admin, "_request_json", fake_request)
    monkeypatch.setenv("WATTRACKER_CLOUD_ENDPOINT", "http://127.0.0.1:8765")
    monkeypatch.setenv("WATTRACKER_CLOUD_OPERATOR_TOKEN", TOKEN)

    assert admin.main(["invite"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "invitation": "invitation-value",
        "expires_at": 1900,
    }

    assert admin.main(["version"]) == 0
    assert json.loads(capsys.readouterr().out) == {"commit": "a" * 40}

    assert admin.main(["list-installations"]) == 0
    assert json.loads(capsys.readouterr().out)["installations"][0][
        "operator_handle"
    ] == installation_id

    assert admin.main(["revoke-installation", installation_id]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "operator_handle": installation_id,
        "status": "revoked",
    }
    assert [(path, method) for _, path, _, method in calls] == [
        ("/api/v1/enrollment/start", "POST"),
        ("/api/v1/admin/version", "GET"),
        ("/api/v1/admin/installations", "GET"),
        (f"/api/v1/admin/installations/{installation_id}/revoke", "POST"),
    ]


@pytest.mark.parametrize(
    "argv",
    [
        ["invite"],
        ["version"],
        ["list-installations"],
        ["revoke-installation", "a" * 64],
    ],
)
def test_cli_timeout_is_actionable_and_token_safe(monkeypatch, capsys, argv):
    class FakeOpener:
        def open(self, request, timeout):
            assert timeout == 30.0
            raise urllib.error.URLError(TimeoutError(TOKEN))

    monkeypatch.setattr(urllib.request, "build_opener", lambda *args: FakeOpener())
    monkeypatch.setenv("WATTRACKER_CLOUD_ENDPOINT", "https://cloud.example")
    monkeypatch.setenv("WATTRACKER_CLOUD_OPERATOR_TOKEN", TOKEN)

    assert admin.main(argv) == 2
    captured = capsys.readouterr()
    assert captured.err == (
        "cloud admin request timed out: service did not respond within 30 seconds, "
        "may be scaling up from zero; it should be retried\n"
    )
    assert TOKEN not in captured.err
    assert TOKEN not in captured.out


def test_cli_transport_failure_stays_generic_and_token_safe(monkeypatch, capsys):
    class FakeOpener:
        def open(self, request, timeout):
            raise urllib.error.URLError(TOKEN)

    monkeypatch.setattr(urllib.request, "build_opener", lambda *args: FakeOpener())
    monkeypatch.setenv("WATTRACKER_CLOUD_ENDPOINT", "https://cloud.example")
    monkeypatch.setenv("WATTRACKER_CLOUD_OPERATOR_TOKEN", TOKEN)

    assert admin.main(["version"]) == 2
    captured = capsys.readouterr()
    assert captured.err.strip() == "cloud admin request failed"
    assert TOKEN not in captured.err
    assert TOKEN not in captured.out


def test_cli_help_explains_opaque_handles_and_credential_id_revoke():
    parser = admin._parser()
    subparsers = next(
        action for action in parser._actions if getattr(action, "choices", None)
    )
    help_text = "\n".join(
        subparsers.choices[name].format_help()
        for name in ("list-installations", "revoke-installation")
    )
    assert "opaque operator handles" in help_text
    assert "opaque operator handle or writer credential id" in help_text


@pytest.mark.parametrize("status, expected", [(404, "no such installation"), (503, "retry in 37 seconds")])
def test_revoke_cli_distinguishes_missing_and_busy_without_echoing_token(
    monkeypatch, capsys, status, expected
):
    installation_id = "a" * 64
    class FakeOpener:
        def open(self, request, timeout):
            raise urllib.error.HTTPError(
                request.full_url, status, "opaque", {"Retry-After": "37"}, None
            )

    monkeypatch.setattr(urllib.request, "build_opener", lambda *args: FakeOpener())
    monkeypatch.setenv("WATTRACKER_CLOUD_ENDPOINT", "https://cloud.example")
    monkeypatch.setenv("WATTRACKER_CLOUD_OPERATOR_TOKEN", TOKEN)

    assert admin.main(["revoke-installation", installation_id]) == 2
    captured = capsys.readouterr()
    assert expected in captured.err
    assert "operator-token-for-admin-tests" not in captured.err
    assert "operator-token-for-admin-tests" not in captured.out
    if status == 503:
        assert "no such installation" not in captured.err


def test_revoke_cli_uses_generic_message_for_malformed_retry_after(monkeypatch, capsys):
    installation_id = "a" * 64
    class FakeOpener:
        def open(self, request, timeout):
            raise urllib.error.HTTPError(
                request.full_url, 503, "opaque", {"Retry-After": "9" * 6}, None
            )

    monkeypatch.setattr(urllib.request, "build_opener", lambda *args: FakeOpener())
    monkeypatch.setenv("WATTRACKER_CLOUD_ENDPOINT", "https://cloud.example")
    monkeypatch.setenv("WATTRACKER_CLOUD_OPERATOR_TOKEN", TOKEN)

    assert admin.main(["revoke-installation", installation_id]) == 2
    captured = capsys.readouterr()
    assert captured.err.strip() == "cloud admin unavailable; retry later"
    assert "secret" not in captured.err


def test_operator_token_falls_back_to_keychain(monkeypatch):
    class Backend:
        def get(self, account):
            assert account == "operator-token"
            return TOKEN

    monkeypatch.delenv("WATTRACKER_CLOUD_OPERATOR_TOKEN", raising=False)
    monkeypatch.setattr(admin, "KeyringBackend", Backend)
    assert admin._load_operator_token() == TOKEN
