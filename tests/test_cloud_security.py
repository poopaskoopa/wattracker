import hashlib
import hmac

import pytest

from wattracker.cloud.security import (
    DEVICE_PAIRING_CODE_BITS,
    INSTALLATION_ID_BYTES,
    MAX_DEVICE_PAIRING_TTL_SECONDS,
    CredentialRegistry,
    DevicePairingBinding,
    DevicePairingRegistry,
    EnrollmentRegistry,
    MemorySecurityStateBackend,
    MIN_REPLAY_TTL_SECONDS,
    NonceReplayGuard,
    canonical_request,
    derive_installation_namespace,
    digest_body,
    generate_p256_keypair,
    generate_signing_keypair,
    new_installation_id,
    sign_request,
    sign_request_ecdsa_p256,
    sign_request_ed25519,
    format_pairing_code,
    generate_pairing_code,
    normalize_pairing_code,
    validate_public_key,
    verify_signature,
)
from wattracker.cloud.security import _PAIRING_ALPHABET


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


def test_nonce_replay_uses_server_time_and_survives_guard_restart():
    backend = MemorySecurityStateBackend()
    namespace = _namespace()
    credential = "a" * 64
    first = NonceReplayGuard(backend=backend, clock=lambda: 1_000)
    assert first._ttl >= MIN_REPLAY_TTL_SECONDS
    assert first.check_and_record(namespace, credential, "same", now=1_000)

    restarted = NonceReplayGuard(backend=backend, clock=lambda: 1_001)
    assert not restarted.check_and_record(namespace, credential, "same", now=1_001)
    assert restarted.check_and_record(namespace, credential, "same", now=1_600)


def test_invitations_expire_are_single_use_and_invalid_tokens_are_indistinguishable():
    registry = EnrollmentRegistry(b"server secret", invitation_ttl_seconds=10)
    installation_id = _installation()
    invitation = registry.create(installation_id, "local-scope", "subject", now=100)
    assert invitation.installation_id == installation_id
    assert invitation.expires_at == 110
    binding = registry.consume(invitation.token, subject="subject", now=100)
    assert binding is not None
    assert binding.namespace == _namespace()
    assert registry.consume(invitation.token, subject="subject", now=100) is None
    expired = registry.create(installation_id, "local-scope", "subject", now=100)
    assert registry.consume(expired.token, subject="subject", now=110) is None
    assert registry.consume("not-a-real-token", subject="subject", now=100) is None
    with pytest.raises(TypeError):
        registry.consume(invitation.token, public_key=b"public-key", subject="subject")


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


# ---------------------------------------------------------------------------
# Signature algorithms
#
# Ed25519 and raw P-256 signatures are both exactly 128 hexadecimal
# characters.  Length therefore proves nothing, and every case below depends
# on the algorithm coming from stored credential state rather than the wire.
# ---------------------------------------------------------------------------


def _p256_keypair():
    pytest.importorskip("cryptography")
    return generate_p256_keypair()


def test_p256_public_keys_are_uncompressed_sec1_points():
    private_key, public_key = _p256_keypair()
    assert len(private_key) == 32
    assert len(public_key) == 65
    assert public_key[0] == 0x04


def test_p256_signatures_are_raw_r_s_and_detect_tampering():
    private_key, public_key = _p256_keypair()
    canonical = canonical_request(
        "POST", "/api/v1/context/refresh", _namespace(), 1700000000, "nonce-1",
        digest_body(b""), "context-refresh",
    )
    signature = sign_request_ecdsa_p256(private_key, canonical)
    assert len(signature) == 128
    assert all(char in "0123456789abcdef" for char in signature)
    assert verify_signature(
        public_key, canonical, signature, algorithm="ecdsa-p256-sha256"
    )
    assert not verify_signature(
        public_key, canonical + b"x", signature, algorithm="ecdsa-p256-sha256"
    )
    flipped = ("1" if signature[0] == "0" else "0") + signature[1:]
    assert not verify_signature(
        public_key, canonical, flipped, algorithm="ecdsa-p256-sha256"
    )
    _other_private, other_public = _p256_keypair()
    assert not verify_signature(
        other_public, canonical, signature, algorithm="ecdsa-p256-sha256"
    )


def test_p256_verification_rejects_der_and_out_of_range_scalars():
    # There is deliberately no DER parser at this boundary: a well-formed DER
    # signature is simply not 128 hexadecimal characters and is refused on
    # shape alone, before any ASN.1 is looked at.
    pytest.importorskip("cryptography")
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec

    private_key, public_key = _p256_keypair()
    canonical = b"signed request"
    signature = sign_request_ecdsa_p256(private_key, canonical)
    der = ec.derive_private_key(
        int.from_bytes(private_key, "big"), ec.SECP256R1()
    ).sign(canonical, ec.ECDSA(hashes.SHA256()))
    assert not verify_signature(
        public_key, canonical, der.hex(), algorithm="ecdsa-p256-sha256"
    )
    order = "ffffffff00000000ffffffffffffffffbce6faada7179e84f3b9cac2fc632551"
    for bad in (
        "0" * 128,                                 # r = s = 0
        "0" * 64 + signature[64:],                 # r = 0
        signature[:64] + "0" * 64,                 # s = 0
        order + signature[64:],                    # r = n
        signature[:64] + order,                    # s = n
        signature[:127],                           # too short
        signature + "0",                           # too long
        signature.upper(),                         # not lowercase hex
        "",                                        # empty
    ):
        assert not verify_signature(
            public_key, canonical, bad, algorithm="ecdsa-p256-sha256"
        )


def test_stored_algorithm_cannot_be_downgraded_between_the_three_schemes():
    pytest.importorskip("cryptography")
    canonical = b"signed request"
    p256_private, p256_public = _p256_keypair()
    ed_private, ed_public = generate_signing_keypair()
    p256_signature = sign_request_ecdsa_p256(p256_private, canonical)
    ed_signature = sign_request_ed25519(ed_private, canonical)

    # Each credential verifies only under its own stored algorithm.
    assert verify_signature(p256_public, canonical, p256_signature,
                            algorithm="ecdsa-p256-sha256")
    assert verify_signature(ed_public, canonical, ed_signature,
                            algorithm="ed25519")

    # Ed25519 credential presented as P-256, and the reverse.  Both signatures
    # are 128 hex characters, so only the stored algorithm separates them.
    assert not verify_signature(ed_public, canonical, ed_signature,
                                algorithm="ecdsa-p256-sha256")
    assert not verify_signature(p256_public, canonical, p256_signature,
                                algorithm="ed25519")
    assert not verify_signature(ed_public, canonical, p256_signature,
                                algorithm="ed25519")
    assert not verify_signature(p256_public, canonical, ed_signature,
                                algorithm="ecdsa-p256-sha256")

    # Neither public key may be re-used as an HMAC secret, in either direction.
    assert not verify_signature(p256_public, canonical,
                                sign_request(p256_public, canonical),
                                algorithm="ecdsa-p256-sha256")
    assert not verify_signature(ed_public, canonical,
                                sign_request(ed_public, canonical),
                                algorithm="ed25519")
    assert not verify_signature(p256_public, canonical, p256_signature,
                                algorithm="hmac-sha256")
    assert not verify_signature(ed_public, canonical, ed_signature,
                                algorithm="hmac-sha256")

    # An HMAC credential is not verifiable as either public-key scheme.
    hmac_signature = sign_request(b"shared secret", canonical)
    assert verify_signature(b"shared secret", canonical, hmac_signature,
                            algorithm="hmac-sha256")
    assert not verify_signature(b"shared secret", canonical, hmac_signature,
                                algorithm="ed25519")
    assert not verify_signature(b"shared secret", canonical, hmac_signature,
                                algorithm="ecdsa-p256-sha256")

    # An unrecognized or empty algorithm never verifies.
    assert not verify_signature(ed_public, canonical, ed_signature,
                                algorithm="ecdsa-p384-sha384")
    assert not verify_signature(ed_public, canonical, ed_signature, algorithm="")


def test_public_key_validation_binds_encoding_to_the_stored_algorithm():
    pytest.importorskip("cryptography")
    _p256_private, p256_public = _p256_keypair()
    _ed_private, ed_public = generate_signing_keypair()
    assert validate_public_key("ed25519", ed_public) == ed_public
    assert validate_public_key("ecdsa-p256-sha256", p256_public) == p256_public
    with pytest.raises(ValueError):
        validate_public_key("ed25519", p256_public)
    with pytest.raises(ValueError):
        validate_public_key("ecdsa-p256-sha256", ed_public)
    with pytest.raises(ValueError):
        # Compressed SEC1 is a valid encoding but is deliberately not accepted;
        # one encoding keeps the parser at the boundary trivial.
        validate_public_key("ecdsa-p256-sha256", b"\x02" + p256_public[1:33])
    with pytest.raises(ValueError):
        # Right length and prefix, but not a point on the curve.
        validate_public_key("ecdsa-p256-sha256", b"\x04" + b"\xff" * 64)
    with pytest.raises(ValueError):
        validate_public_key("ecdsa-p384-sha384", p256_public)


# ---------------------------------------------------------------------------
# Device credentials
# ---------------------------------------------------------------------------


def test_device_credential_binds_scope_capabilities_and_algorithm():
    credentials = CredentialRegistry(b"server secret")
    device = credentials.register_device(
        _installation(), "rider-scope", b"p" * 32, subject="subject"
    )
    assert device.namespace == _namespace()
    assert device.local_user_scope == "rider-scope"
    assert device.signature_algorithm == "ed25519"
    assert device.capabilities == frozenset({"read"})
    assert device.has_capability("read")
    assert not device.has_capability("write")
    assert len(device.credential_id) == 64
    assert device.subscription_key and device.subscription_key.decode("ascii")
    # Neither the public key nor the one-time secret appears in repr.
    assert device.subscription_key.decode("ascii") not in repr(device)
    assert device.credential_id not in repr(device)


def test_device_credential_capabilities_are_data_not_a_hardcoded_default():
    credentials = CredentialRegistry(b"server secret")
    granted = credentials.register_device(
        _installation(), "rider-scope", b"p" * 32, capabilities=("read", "write")
    )
    assert granted.capabilities == frozenset({"read", "write"})
    assert granted.has_capability("write")
    for invalid in ((), ("",), ("READ",), ("read write",), (1,), "read",
                    tuple(f"cap{index}" for index in range(17))):
        with pytest.raises(ValueError):
            credentials.register_device(
                _installation(), "rider-scope", b"p" * 32, capabilities=invalid
            )


def test_device_credential_refuses_symmetric_and_mismatched_keys():
    credentials = CredentialRegistry(b"server secret")
    with pytest.raises(ValueError):
        # A device's private half lives in hardware; a shared secret the
        # server could also sign with is never a device verification key.
        credentials.register_device(
            _installation(), "scope", b"shared secret",
            signature_algorithm="hmac-sha256",
        )
    with pytest.raises(ValueError):
        credentials.register_device(_installation(), "scope", b"p" * 31)
    with pytest.raises(ValueError):
        credentials.register_device(
            _installation(), "scope", b"p" * 32,
            signature_algorithm="ecdsa-p256-sha256",
        )


def test_device_lookup_resolution_namespace_binding_and_revocation():
    credentials = CredentialRegistry(b"server secret")
    device = credentials.register_device(_installation(), "scope", b"p" * 32)
    namespace = _namespace()
    assert credentials.lookup_device(device.credential_id) == device
    assert credentials.resolve_device(device.credential_id, namespace) == device
    # A device is invisible outside the namespace it was paired into.
    assert credentials.resolve_device(
        device.credential_id, _namespace(b"z" * 32)
    ) is None
    # Unknown identifiers resolve exactly as revoked ones do: to None.
    assert credentials.resolve_device("f" * 64) is None
    assert credentials.resolve_device("not-an-id") is None
    assert credentials.lookup_device("f" * 64) is None
    assert credentials.revoke_device(device.credential_id)
    assert not credentials.revoke_device(device.credential_id)
    assert credentials.resolve_device(device.credential_id) is None
    assert credentials.resolve_device("f" * 64) is None
    revoked = credentials.lookup_device(device.credential_id)
    assert revoked is not None and revoked.revoked and not revoked.active
    # Devices and writers are separate registries and never cross-resolve.
    writer = credentials.register_writer(
        _installation(), "scope", b"signing", b"subscription"
    )
    assert credentials.resolve_device(writer.credential_id) is None
    assert credentials.resolve_writer(device.credential_id) is None


def test_device_subscription_key_is_server_generated_and_compared_by_digest():
    credentials = CredentialRegistry(b"server secret")
    device = credentials.register_device(_installation(), "scope", b"p" * 32)
    subscription = device.subscription_key.decode("ascii")
    assert credentials.authenticate_device(
        device.credential_id, subscription
    ) == device
    assert credentials.authenticate_device(device.credential_id, "wrong") is None
    assert credentials.authenticate_device(device.credential_id, "") is None
    assert credentials.authenticate_device("f" * 64, subscription) is None
    credentials.revoke_device(device.credential_id)
    assert credentials.authenticate_device(device.credential_id, subscription) is None


def test_device_credentials_survive_restart_and_propagate_revocation():
    backend = MemorySecurityStateBackend()
    first = CredentialRegistry(b"server secret", backend=backend)
    device = first.register_device(
        _installation(), "scope", b"p" * 32, capabilities=("read",), subject="subject"
    )
    subscription = device.subscription_key.decode("ascii")

    restarted = CredentialRegistry(b"server secret", backend=backend)
    loaded = restarted.authenticate_device(device.credential_id, subscription)
    assert loaded is not None
    assert loaded.verification_key == b"p" * 32
    assert loaded.capabilities == frozenset({"read"})
    assert loaded.subject == "subject"
    # Only the verifier is persisted; the one-time secret is not recoverable.
    assert loaded.subscription_key == b""

    assert restarted.revoke_device(device.credential_id)
    assert first.resolve_device(device.credential_id) is None


def test_persisted_device_without_capabilities_is_rejected_not_defaulted():
    backend = MemorySecurityStateBackend()
    credentials = CredentialRegistry(b"server secret", backend=backend)
    device = credentials.register_device(_installation(), "scope", b"p" * 32)
    digest = hashlib.sha256(device.credential_id.encode("utf-8")).digest()
    stored = backend.read("device", digest.hex())
    assert stored is not None
    del stored["capabilities"]
    backend.write("device", digest.hex(), stored)
    reloaded = CredentialRegistry(b"server secret", backend=backend)
    assert reloaded.resolve_device(device.credential_id) is None


# ---------------------------------------------------------------------------
# Desktop-minted device pairing codes (#152)
# ---------------------------------------------------------------------------


def test_pairing_codes_are_single_use_expire_and_are_indistinguishable():
    registry = DevicePairingRegistry(b"server secret", ttl_seconds=900, clock=lambda: 100)
    minted = registry.create(_namespace(), "local-scope", subject="subject", now=100)
    assert minted.expires_at == 1_000

    binding = registry.consume(minted.code, "subject", now=500)
    assert binding is not None
    assert binding.namespace == _namespace()
    assert binding.local_user_scope == "local-scope"

    # Consumed, expired, and never-issued are the same observable outcome.
    consumed = registry.consume(minted.code, "subject", now=500)
    expired_code = registry.create(_namespace(), "local-scope", subject="subject", now=100)
    expired = registry.consume(expired_code.code, "subject", now=1_000)
    unknown = registry.consume(generate_pairing_code(), "subject", now=500)
    malformed = registry.consume("not-a-pairing-code", "subject", now=500)
    assert consumed is expired is unknown is malformed is None


def test_pairing_code_is_bound_to_the_minting_scope_and_subject():
    registry = DevicePairingRegistry(b"server secret", clock=lambda: 100)
    minted = registry.create(_namespace(b"rider-a"), "scope-a", subject="rider-a")

    # A different verified subject cannot redeem it -- and does not burn it,
    # so the rider who mistypes an account does not lose the code.
    assert registry.consume(minted.code, "rider-b") is None
    assert registry.consume(minted.code, None) is None
    binding = registry.consume(minted.code, "rider-a")
    assert binding is not None
    assert binding.namespace == _namespace(b"rider-a")
    assert binding.namespace != _namespace(b"rider-b")


def test_pairing_binding_cannot_be_forged_or_redirected():
    registry = DevicePairingRegistry(b"server secret", clock=lambda: 100)
    credentials = CredentialRegistry(b"server secret", pairing_registry=registry)
    # A raw 32-byte Ed25519 key: this test is about the binding proof, so it
    # must run whether or not the optional crypto extra is installed.
    public_key = b"k" * 32
    minted = registry.create(_namespace(b"rider-a"), "scope-a")
    binding = registry.consume(minted.code)
    assert binding is not None
    assert registry.verify_binding(binding)

    # A hand-assembled binding naming somebody else's namespace has no proof.
    forged = DevicePairingBinding(
        _namespace(b"rider-b"), "scope-a", binding.pairing_id, b"forged-proof"
    )
    assert not registry.verify_binding(forged)
    with pytest.raises(ValueError):
        credentials.pair_device(forged, public_key)

    # Reusing a real proof under a swapped namespace fails the same way: the
    # namespace is inside the HMAC.
    redirected = DevicePairingBinding(
        _namespace(b"rider-b"), "scope-a", binding.pairing_id, binding._proof
    )
    assert not registry.verify_binding(redirected)
    with pytest.raises(ValueError):
        credentials.pair_device(redirected, public_key)

    device = credentials.pair_device(binding, public_key)
    assert device.namespace == _namespace(b"rider-a")
    assert device.local_user_scope == "scope-a"
    assert device.capabilities == frozenset({"read"})


def test_pairing_ttl_is_capped_at_fifteen_minutes():
    # The brute-force argument for a 60-bit code is stated against a bounded
    # window, so no caller may widen it.
    with pytest.raises(ValueError):
        DevicePairingRegistry(b"server secret", ttl_seconds=901)
    registry = DevicePairingRegistry(b"server secret", ttl_seconds=900)
    assert registry.ttl_seconds == MAX_DEVICE_PAIRING_TTL_SECONDS
    with pytest.raises(ValueError):
        registry.create(_namespace(), "scope", ttl_seconds=86_400)
    with pytest.raises(ValueError):
        registry.create(_namespace(), "scope", ttl_seconds=0)


def test_pairing_code_shape_normalization_and_entropy():
    """The code must survive being typed, and survive being guessed.

    Entropy budget, spelled out because the number is the control:

    * alphabet: Crockford Base32, 32 symbols -> 5 bits per symbol
    * length: 12 symbols -> 60 bits -> 2**60 == 1.15e18 codes
    * TTL ceiling: 900 s
    * APIM: 60 requests/minute and 1000/day per subscription key, so one key
      buys at most 15 * 60 == 900 guesses inside a code's whole lifetime
      (under the 1000/day cap, so the per-minute limit is what binds)

    One key therefore succeeds with probability 900 / 2**60 = 7.8e-16.  The
    floor asserted below is stated against a far richer attacker: 1000
    subscription keys, each spending its entire daily budget on one 15-minute
    window, must still stay under 2**-32.
    """

    assert len(set(_PAIRING_ALPHABET)) == 32
    assert not set("ILOU") & set(_PAIRING_ALPHABET)
    assert DEVICE_PAIRING_CODE_BITS == 60

    codes = [generate_pairing_code() for _ in range(500)]
    assert len(set(codes)) == 500
    for code in codes:
        assert len(code) == 14 and code.count("-") == 2
        canonical = normalize_pairing_code(code)
        assert canonical is not None and len(canonical) == 12
        assert set(canonical) <= set(_PAIRING_ALPHABET)
        # QR alphanumeric mode covers 0-9, A-Z and '-', so the display form
        # encodes without falling back to byte mode.
        assert set(code) <= set("0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ-")

    guesses_per_key = int(MAX_DEVICE_PAIRING_TTL_SECONDS / 60) * 60
    assert guesses_per_key == 900 and guesses_per_key <= 1_000
    assert guesses_per_key / 2 ** DEVICE_PAIRING_CODE_BITS < 2 ** -32
    assert (1_000 * guesses_per_key) / 2 ** DEVICE_PAIRING_CODE_BITS < 2 ** -32

    # Typing tolerance: case, grouping, spaces, and the Crockford I/L/O folds
    # all reach the same code.  Nothing else does.
    canonical = normalize_pairing_code("ABCD-EFGH-JKMN")
    assert normalize_pairing_code("abcd efgh jkmn") == canonical
    assert normalize_pairing_code("ABCDEFGHJKMN") == canonical
    assert normalize_pairing_code("0LOI") is None  # folds, but too short
    assert normalize_pairing_code("0110-EFGH-JKMN") == normalize_pairing_code(
        "OIIO-EFGH-JKMN"
    )
    assert normalize_pairing_code("UUUU-EFGH-JKMN") is None
    assert normalize_pairing_code("ABCD-EFGH-JKM") is None
    assert normalize_pairing_code("ABCD-EFGH-JKMNP") is None
    assert normalize_pairing_code("ABCD-EFGH-JK.N") is None
    assert normalize_pairing_code(None) is None
    assert normalize_pairing_code(b"ABCDEFGHJKMN") is None
    assert normalize_pairing_code("A" * 65) is None

    assert format_pairing_code("abcdefghjkmn") == "ABCD-EFGH-JKMN"
    with pytest.raises(ValueError):
        format_pairing_code("nope")


def test_pairing_codes_and_enrollment_invitations_are_separate_token_spaces():
    """An enrollment token buys a writer; a pairing code must not.

    The two registries share their one-time semantics but never a token
    space, so neither can be made to spend the other's secret.
    """

    backend = MemorySecurityStateBackend()
    enrollments = EnrollmentRegistry(b"server secret", backend=backend)
    pairings = DevicePairingRegistry(b"server secret", backend=backend)
    invitation = enrollments.create(_installation(), "scope")
    minted = pairings.create(_namespace(), "scope")

    assert pairings.consume(invitation.token) is None
    # Both the display form and the canonical form: the enrollment registry
    # digests what it is handed, so the separation has to hold for either.
    assert enrollments.consume(minted.code) is None
    assert enrollments.consume(normalize_pairing_code(minted.code)) is None
    # Each still works in its own registry afterwards.
    assert enrollments.consume(invitation.token) is not None
    assert pairings.consume(minted.code) is not None


def test_pairing_codes_survive_a_restart_and_stay_single_use():
    backend = MemorySecurityStateBackend()
    minted = DevicePairingRegistry(
        b"server secret", backend=backend, clock=lambda: 100
    ).create(_namespace(), "scope", subject="rider")
    restarted = DevicePairingRegistry(
        b"server secret", backend=backend, clock=lambda: 200
    )
    assert restarted.consume(minted.code, "rider") is not None
    again = DevicePairingRegistry(b"server secret", backend=backend, clock=lambda: 300)
    assert again.consume(minted.code, "rider") is None
    stale = DevicePairingRegistry(
        b"server secret", backend=backend, clock=lambda: 100
    ).create(_namespace(), "scope", subject="rider")
    expired = DevicePairingRegistry(
        b"server secret", backend=backend, clock=lambda: 1_000
    )
    assert expired.consume(stale.code, "rider") is None
