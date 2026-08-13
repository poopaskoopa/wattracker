import hashlib
import hmac

import pytest

from wattracker.cloud.security import (
    INSTALLATION_ID_BYTES,
    CredentialRegistry,
    EnrollmentRegistry,
    MemorySecurityStateBackend,
    NonceReplayGuard,
    canonical_request,
    derive_installation_namespace,
    digest_body,
    new_installation_id,
    sign_request,
    verify_signature,
)


def _namespace(seed: bytes = b"installation") -> str:
    return derive_installation_namespace(
        b"server secret", (seed * INSTALLATION_ID_BYTES)[:INSTALLATION_ID_BYTES].hex()
    )


def _installation(seed: bytes = b"installation") -> str:
    return (seed * INSTALLATION_ID_BYTES)[:INSTALLATION_ID_BYTES].hex()


def test_installation_ids_are_random_opaque_lowercase_hex():
    first = new_installation_id()
    second = new_installation_id()
    assert len(first) == INSTALLATION_ID_BYTES * 2
    assert len(second) == INSTALLATION_ID_BYTES * 2
    assert first.isascii() and first == first.lower()
    assert all(char in "0123456789abcdef" for char in first)
    assert first != second


def test_namespaces_are_separated_by_installation_id_and_secret():
    installation_a = bytes(range(INSTALLATION_ID_BYTES)).hex()
    installation_b = bytes(reversed(range(INSTALLATION_ID_BYTES))).hex()
    namespace_a = derive_installation_namespace(b"server secret", installation_a)
    namespace_b = derive_installation_namespace(b"server secret", installation_b)
    assert namespace_a != namespace_b
    assert namespace_a != derive_installation_namespace(b"other secret", installation_a)
    assert len(namespace_a) == 64
    with pytest.raises(ValueError):
        derive_installation_namespace(b"server secret", installation_a.upper())
    with pytest.raises(ValueError):
        derive_installation_namespace(b"server secret", "00" * 31)


def test_canonical_request_digest_and_signature_tamper_detection():
    body = b'{"watts":250}'
    namespace = _namespace()
    canonical = canonical_request(
        "post", "/cloud/sync", namespace, 1700000000, "nonce-1",
        digest_body(body), "request-1", revision="7",
    )
    assert digest_body(body) == hashlib.sha256(body).hexdigest()
    signature = sign_request(b"writer signing key", canonical)
    assert verify_signature(b"writer signing key", canonical, signature)
    assert not verify_signature(b"writer signing key", canonical + b"x", signature)
    assert not verify_signature(b"writer signing key", canonical, "0" + signature[1:])
    assert not verify_signature(b"different key", canonical, signature)


def test_canonical_request_uses_unambiguous_field_framing():
    namespace = _namespace()
    common = dict(
        method="POST", path="/x", namespace=namespace, timestamp=1,
        body_digest="0" * 64, idempotency_key="a",
    )
    assert canonical_request(nonce="bc", **common) != canonical_request(
        nonce="b", revision="c", **common
    )
    with pytest.raises(ValueError):
        invalid = dict(common)
        invalid["path"] = "relative"
        canonical_request(nonce="nonce", **invalid)


def test_nonce_replay_is_scoped_and_expires():
    namespace_a = _namespace(b"a" * INSTALLATION_ID_BYTES)
    namespace_b = _namespace(b"b" * INSTALLATION_ID_BYTES)
    guard = NonceReplayGuard(ttl_seconds=10)
    assert guard.check_and_record(namespace_a, "a" * 64, "same", now=100)
    assert not guard.check_and_record(namespace_a, "a" * 64, "same", now=101)
    assert guard.check_and_record(namespace_b, "a" * 64, "same", now=101)
    assert guard.check_and_record(namespace_a, "b" * 64, "same", now=101)
    assert guard.check_and_record(namespace_a, "a" * 64, "same", now=110)


def test_nonce_replay_capacity_is_bounded():
    guard = NonceReplayGuard(capacity=1, ttl_seconds=30)
    namespace = _namespace()
    assert guard.check_and_record(namespace, "a" * 64, "one", now=0)
    assert not guard.check_and_record(namespace, "a" * 64, "two", now=1)
    assert guard.check_and_record(namespace, "a" * 64, "two", now=30)


def test_invitations_expire_are_single_use_and_invalid_tokens_are_indistinguishable():
    registry = EnrollmentRegistry(b"server secret", invitation_ttl_seconds=10)
    installation_id = _installation()
    invitation = registry.create(installation_id, "local-scope", "subject", now=100)
    assert invitation.installation_id == installation_id
    assert invitation.expires_at == 110
    credential = registry.consume(invitation.token, b"public-key", "subject", now=100)
    assert credential is not None
    assert credential.namespace == _namespace()
    assert registry.consume(invitation.token, b"public-key", "subject", now=100) is None
    expired = registry.create(installation_id, "local-scope", "subject", now=100)
    assert registry.consume(expired.token, b"public-key", "subject", now=110) is None
    assert registry.consume("not-a-real-token", b"public-key", "subject", now=100) is None


def test_invitation_token_is_opaque_and_registry_is_bounded():
    registry = EnrollmentRegistry(b"server secret", capacity=1)
    token = registry.create(_installation(), "scope").token
    assert len(token) >= 40
    with pytest.raises(RuntimeError):
        registry.create(_installation(b"other"), "scope")


def test_writer_enrollment_binding_lookup_revocation_and_resolution():
    credentials = CredentialRegistry(b"server secret")
    installation_id = _installation()
    credential = credentials.register_writer(
        installation_id, "user-scope", b"signing-key", b"subscription-key", "subject"
    )
    namespace = _namespace()
    assert credential.namespace == namespace
    assert credential.local_user_scope == "user-scope"
    assert credential.active and not credential.revoked
    assert credentials.lookup_writer(credential.credential_id) == credential
    assert credentials.get_writer(credential.credential_id) == credential
    assert credentials.resolve_writer(credential.credential_id, namespace) == credential
    assert credentials.resolve_writer("f" * 64, namespace) is None
    assert credentials.resolve_writer(credential.credential_id, _namespace(b"z" * 32)) is None
    assert credentials.revoke(credential.credential_id)
    revoked = credentials.lookup_writer(credential.credential_id)
    assert revoked is not None and revoked.revoked and not revoked.active
    assert credentials.resolve_writer(credential.credential_id, namespace) is None
    assert credentials.resolve_writer("f" * 64, namespace) is None


def test_writer_cannot_be_enrolled_from_a_forged_binding():
    enrollment = EnrollmentRegistry(b"server secret")
    credentials = CredentialRegistry(b"server secret", enrollment_registry=enrollment)
    namespace = _namespace()
    token = enrollment.create(_installation(), "scope").token
    binding = enrollment.consume(token)
    assert binding is not None
    forged = type(binding)(binding.namespace, binding.local_user_scope, binding.invitation_id, b"x" * 32)
    with pytest.raises(ValueError):
        credentials.enroll_writer(forged)


def test_reader_context_tokens_bind_scope_expire_and_revoke():
    credentials = CredentialRegistry(b"server secret")
    installation_id = _installation()
    token, context = credentials.issue_reader_context(
        installation_id, "scope", "subject", ttl_seconds=10, now=100
    )
    assert credentials.read_context_token(token, now=100) == context
    assert credentials.resolve_reader(token, subject="other") is None
    assert credentials.read_context_token(token, namespace=_namespace(b"q" * 32), now=100) is None
    assert credentials.read_context_token(token, local_user_scope="other", now=100) is None
    assert credentials.read_context_token(token, now=110) is None

    token, context = credentials.issue_reader_context(installation_id, "scope", "subject")
    assert credentials.lookup_reader(context.context_id) == context
    assert credentials.revoke_reader(context.context_id)
    revoked = credentials.lookup_reader(context.context_id)
    assert revoked is not None and revoked.revoked and not revoked.active
    assert credentials.resolve_reader(token) is None
    assert credentials.resolve_reader("not-a-real-token") is None


def test_credentials_and_contexts_use_opaque_ids():
    credentials = CredentialRegistry(b"server secret")
    writer = credentials.register_writer(
        _installation(), "scope", b"signing", b"subscription"
    )
    token, context = credentials.issue_reader_context(_installation(), "scope")
    assert writer is not None
    assert len(writer.credential_id) == 64
    assert len(context.context_id) == 64
    assert token not in repr(context)
    assert token not in repr(writer)
    assert hmac.compare_digest(writer.credential_id, writer.credential_id)


def test_shared_backend_survives_registry_restart_and_propagates_revocation():
    backend = MemorySecurityStateBackend()
    first_enrollments = EnrollmentRegistry(b"server secret", backend=backend)
    invitation = first_enrollments.create(
        _installation(), "scope", "subject", now=100
    )

    restarted_enrollments = EnrollmentRegistry(b"server secret", backend=backend)
    binding = restarted_enrollments.consume(invitation.token, now=101)
    assert binding is not None
    assert restarted_enrollments.consume(invitation.token, now=101) is None

    first_credentials = CredentialRegistry(b"server secret", backend=backend)
    writer = first_credentials.enroll_writer(
        binding,
        signing_key=b"p" * 32,
        subscription_key=b"subscription-secret",
        enrollment_registry=restarted_enrollments,
    )
    reader_token, reader = first_credentials.issue_reader_context_for_scope(
        writer.namespace, writer.local_user_scope, "subject", now=101
    )

    restarted_credentials = CredentialRegistry(b"server secret", backend=backend)
    loaded = restarted_credentials.authenticate_writer(
        writer.credential_id, b"subscription-secret"
    )
    assert loaded is not None
    assert loaded.verification_key == b"p" * 32
    assert loaded.subscription_key == b""
    assert restarted_credentials.resolve_reader(reader_token, now=102) == reader

    assert restarted_credentials.revoke_writer(writer.credential_id)
    assert first_credentials.resolve_writer(writer.credential_id) is None
    assert restarted_credentials.revoke_reader(reader.context_id)
    assert first_credentials.resolve_reader(reader_token, now=102) is None


def test_ed25519_credential_rejects_public_key_hmac_downgrade():
    canonical = b"signed request"
    public_key = b"p" * 32
    forged = sign_request(public_key, canonical)
    assert not verify_signature(
        public_key, canonical, forged, algorithm="ed25519"
    )
