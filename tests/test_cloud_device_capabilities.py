"""Desktop-only grants and revoke-safe phone capability changes (#499)."""
import hashlib
import json

import pytest

from wattracker.cloud.client import CloudEnrollmentError, CloudSyncClient, SyncCredentials
from wattracker.cloud.limits import PUBLIC_UNAVAILABLE_DETAIL
from wattracker.cloud.security import (
    DEFAULT_DEVICE_CAPABILITIES, CredentialRegistry, MemorySecurityStateBackend,
    SecurityStateUnavailable, generate_signing_keypair,
)
from test_cloud_device_revocation import (
    deployment, _rider, _device, _envelope, _device_signer, _refresh,
)


def _path(device_id):
    return f"/api/v1/devices/{device_id}/capabilities"


def _post(deployment, writer, target, *, body=b'{"annotate":true}', nonce="grant", **envelope):
    path = _path(target)
    return deployment.client.post(path, content=body, headers=_envelope(
        writer, "POST", path, nonce=nonce, idem=envelope.pop("idem", "device-capabilities"),
        body=body, **envelope,
    ))


def test_grant_withdraw_and_refresh_use_current_persisted_capabilities(deployment):
    assert DEFAULT_DEVICE_CAPABILITIES == frozenset({"read"})
    writer = _rider(deployment)
    device, key = _device(deployment, writer)
    assert device.capabilities == DEFAULT_DEVICE_CAPABILITIES
    granted = _post(deployment, writer, device.credential_id)
    assert granted.status_code == 200, granted.text
    assert granted.json() == {"credential_id": device.credential_id, "capabilities": ["annotate", "read"]}
    assert granted.headers["cache-control"] == "no-store"
    deployment.restart()
    assert _refresh(deployment.client, device, key).json()["capabilities"] == ["annotate", "read"]
    withdrawn = _post(deployment, writer, device.credential_id, body=b'{"annotate":false}', nonce="withdraw")
    assert withdrawn.status_code == 200
    assert withdrawn.json()["capabilities"] == ["read"]
    assert _refresh(deployment.client, device, key, nonce="refresh-2").json()["capabilities"] == ["read"]


@pytest.mark.parametrize("capabilities", [frozenset({"read"}), frozenset({"read", "write"}), frozenset({"read", "annotate", "write"})])
def test_even_a_write_capable_phone_cannot_grant_or_withdraw(deployment, capabilities):
    writer = _rider(deployment)
    target, _ = _device(deployment, writer)
    private, public = generate_signing_keypair()
    phone = deployment.state.credentials.register_device_for_scope(
        writer.namespace, writer.local_user_scope, public, capabilities=capabilities,
    )
    response = _post(deployment, phone, target.credential_id, signer=_device_signer(private))
    assert response.status_code == 404
    assert response.json() == {"detail": "not found"}
    assert deployment.state.credentials.lookup_device(target.credential_id).capabilities == {"read"}


@pytest.mark.parametrize("kind", ["revoked", "inactive", "other-namespace", "other-scope", "writer", "missing", "malformed"])
def test_target_refusals_are_identical_and_leave_state_unchanged(deployment, kind):
    writer = _rider(deployment)
    owner = _rider(deployment, seed=b"b", scope="other" if kind == "other-scope" else "scope") if kind == "other-namespace" else writer
    if kind == "other-scope":
        _, public = generate_signing_keypair()
        device = deployment.state.credentials.register_device_for_scope(writer.namespace, "other", public)
    else:
        device, _ = _device(deployment, owner)
    target = device.credential_id
    if kind == "revoked":
        deployment.state.credentials.revoke_device(target)
    elif kind == "inactive":
        # An inactive, unrevoked row violates DeviceCredential's invariant;
        # this covers refusal to decode corrupt state, not a live status.
        key = hashlib.sha256(target.encode()).hexdigest()
        value = deployment.backend.read("device", key)
        value["active"] = False
        deployment.backend.write("device", key, value)
    elif kind == "writer":
        target = writer.credential_id
    elif kind == "missing":
        target = "0" * 64
    elif kind == "malformed":
        target = "invalid"
    before = deployment.state.credentials.lookup_device(device.credential_id)
    response = _post(deployment, writer, target)
    assert response.status_code == 404, response.text
    assert response.json() == {"detail": "not found"}
    assert response.headers["cache-control"] == "no-store"
    assert deployment.state.credentials.lookup_device(device.credential_id) == before


@pytest.mark.parametrize("body", [b'{}', b'[]', b'null', b'{"annotate":1}', b'{"annotate":"true"}', b'{"annotate":null}', b'{"annotate":true,"extra":0}', b'bad', b' ' * 257])
def test_strict_bounded_body(deployment, body):
    writer = _rider(deployment)
    device, _ = _device(deployment, writer)
    response = _post(deployment, writer, device.credential_id, body=body)
    assert response.status_code == 400, response.text
    assert deployment.state.credentials.lookup_device(device.credential_id).capabilities == {"read"}


@pytest.mark.parametrize("size, status", [(256, 200), (257, 400)])
def test_valid_signed_body_obeys_256_byte_boundary(deployment, size, status):
    writer = _rider(deployment)
    device, _ = _device(deployment, writer)
    body = b'{"annotate":true}'.ljust(size, b" ")
    assert len(body) == size
    assert json.loads(body) == {"annotate": True}

    response = _post(deployment, writer, device.credential_id, body=body)

    assert response.status_code == status, response.text
    expected = {"read", "annotate"} if status == 200 else {"read"}
    assert deployment.state.credentials.lookup_device(device.credential_id).capabilities == expected


@pytest.mark.parametrize("failure", ["signature", "stale", "replay", "idem", "revision", "body"])
def test_signed_envelope_refusals_do_not_change_capabilities(deployment, failure):
    writer = _rider(deployment)
    device, _ = _device(deployment, writer)
    body = b'{"annotate":true}'
    path = _path(device.credential_id)
    headers = _envelope(writer, "POST", path, nonce="envelope", idem="wrong" if failure == "idem" else "device-capabilities",
                        revision=1 if failure == "revision" else 0,
                        timestamp=0 if failure == "stale" else 1000, body=body)
    if failure == "signature":
        headers["X-Writer-Signature"] = "0" * 64
    if failure == "body":
        body = b'{"annotate":false}'
    if failure == "replay":
        # Spend the nonce on a valid withdrawal, without granting annotation.
        body = b'{"annotate":false}'
        headers = _envelope(writer, "POST", path, nonce="envelope", idem="device-capabilities", body=body)
        assert deployment.client.post(path, content=body, headers=headers).status_code == 200
    response = deployment.client.post(path, content=body, headers=headers)
    assert response.status_code == 401, response.text
    assert deployment.state.credentials.lookup_device(device.credential_id).capabilities == {"read"}


def test_kill_switch_precedes_auth_and_body_parsing(deployment, monkeypatch):
    writer = _rider(deployment)
    device, _ = _device(deployment, writer)
    deployment.state.quotas.set_public_enabled(False)
    def unexpected(*args, **kwargs):
        raise AssertionError("disabled endpoint attempted authentication")
    monkeypatch.setattr(deployment.state.credentials, "authenticate_writer", unexpected)
    response = deployment.client.post(_path(device.credential_id), content=b"x" * 257)
    assert response.status_code == 503
    assert response.json() == {"detail": PUBLIC_UNAVAILABLE_DETAIL}
    assert response.headers["retry-after"] == "300"


def test_contention_uses_neutral_retryable_503(deployment, monkeypatch):
    writer = _rider(deployment)
    device, _ = _device(deployment, writer)
    def unavailable(*args, **kwargs):
        raise SecurityStateUnavailable("contended")
    monkeypatch.setattr(deployment.state.credentials, "set_device_capabilities_in_scope", unavailable)
    response = _post(deployment, writer, device.credential_id)
    assert response.status_code == 503
    assert response.json() == {"detail": PUBLIC_UNAVAILABLE_DETAIL}
    assert response.headers["retry-after"] == "300"


class _RevokingBackend(MemorySecurityStateBackend):
    """Inject a revocation between transform/read and commit, like an etag race."""
    armed = False

    def _revoke(self, kind, key):
        self.armed = False
        value = super().read(kind, key)
        value.update(active=False, revoked=True)
        super().write(kind, key, value)

    def read(self, kind, key):
        value = super().read(kind, key)
        if self.armed and kind == "device":
            self._revoke(kind, key)
        return value

    def update(self, kind, key, transform):
        if not self.armed or kind != "device":
            return super().update(kind, key, transform)
        # The first transform sees an active row. A concurrent revoke wins
        # its etag before our write, so the backend retries the transform.
        transform(super().read(kind, key))
        self._revoke(kind, key)
        return super().update(kind, key, transform)


def test_capability_update_cannot_resurrect_concurrently_revoked_device():
    backend = _RevokingBackend()
    registry = CredentialRegistry(b"test-server-secret-32-bytes-long", backend=backend)
    _, public = generate_signing_keypair()
    device = registry.register_device_for_scope("a" * 64, "scope", public)
    backend.armed = True
    outcome = registry.set_device_capabilities_in_scope(
        device.credential_id, device.namespace, device.local_user_scope,
        frozenset({"read", "annotate"}),
    )
    stored = registry.lookup_device(device.credential_id)
    assert stored.revoked is True and stored.active is False
    assert stored.capabilities == {"read"}
    assert outcome is None


def test_registry_memory_path_and_invalid_capability_sets():
    registry = CredentialRegistry(b"test-server-secret-32-bytes-long")
    _, public = generate_signing_keypair()
    device = registry.register_device_for_scope("a" * 64, "scope", public)
    assert registry.set_device_capabilities_in_scope(device.credential_id, device.namespace, "scope", frozenset({"read", "annotate"})) == {"read", "annotate"}
    with pytest.raises(ValueError):
        registry.set_device_capabilities_in_scope(device.credential_id, device.namespace, "scope", frozenset({"read", "write"}))


def _client(payload, captured=None):
    def transport(url, headers, body, method):
        if captured is not None:
            captured.append((url, headers, body, method))
        return 200, json.dumps(payload).encode()
    return CloudSyncClient("https://cloud.example", SyncCredentials(
        credential_id="a" * 64, subscription_key=b"sub", signing_key=b"key", namespace="b" * 64,
    ), transport=transport)


@pytest.mark.parametrize("payload", [
    {}, {"credential_id": "c" * 64, "capabilities": ["read", "annotate"], "extra": "secret"},
    {"credential_id": "d" * 64, "capabilities": ["read", "annotate"]},
    {"credential_id": "c" * 64, "capabilities": "read"},
    {"credential_id": "c" * 64, "capabilities": ["read", "unknown"]},
    {"credential_id": "c" * 64, "capabilities": ["read", {}]},
    {"credential_id": "c" * 64, "capabilities": ["read", "annotate", "annotate"]},
    {"credential_id": "c" * 64, "capabilities": ["read"]},
    {"credential_id": "c" * 64, "capabilities": ["read", "write", "annotate"]},
])
def test_desktop_client_rejects_malformed_or_unexpected_response(payload):
    with pytest.raises(CloudEnrollmentError):
        _client(payload).set_device_capabilities("c" * 64, True)


def test_desktop_client_signs_exact_body_and_fixed_envelope():
    from wattracker.cloud.security import canonical_request, digest_body, sign_request
    captured = []
    client = _client({"credential_id": "c" * 64, "capabilities": ["annotate", "read"]}, captured)
    assert client.set_device_capabilities("c" * 64, True) == {"read", "annotate"}
    url, headers, body, method = captured[0]
    assert method == "POST" and url == "https://cloud.example" + _path("c" * 64)
    assert body == b'{"annotate":true}'
    assert headers["X-Writer-Idempotency-Key"] == "device-capabilities"
    assert headers["X-Writer-Revision"] == "0"
    canonical = canonical_request(method, _path("c" * 64), "b" * 64, int(headers["X-Writer-Timestamp"]),
                                  headers["X-Writer-Nonce"], digest_body(body), "device-capabilities", "0")
    assert headers["X-Writer-Signature"] == sign_request(b"key", canonical)
    with pytest.raises(ValueError):
        client.set_device_capabilities("c" * 64, "true")
    with pytest.raises(ValueError):
        client.set_device_capabilities("invalid", True)
