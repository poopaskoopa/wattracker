"""The Python half of the shared canonical-request interop vectors.

``tests/vectors/canonical_request_v1.json`` is read by this module and by the
Swift suite in ``ios/WatTracker/WatTrackerTests``.  It is the only artifact
that makes the two implementations verifiably identical rather than hoped to
be: a canonical request that disagrees between them produces a 401 with no
diagnostic anywhere, and no amount of reading either side finds it.

If a change to :func:`wattracker.cloud.security.canonical_request` breaks this
module, the Swift suite is broken too and the shipped app stops being able to
sign anything.  Regenerating the file is a deliberate wire-format change, not
a way to make a test pass.
"""
from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

import pytest

from wattracker.cloud.security import (
    canonical_request,
    digest_body,
    verify_signature,
)

VECTOR_PATH = Path(__file__).parent / "vectors" / "canonical_request_v1.json"


@pytest.fixture(scope="module")
def vectors() -> dict:
    return json.loads(VECTOR_PATH.read_text(encoding="utf-8"))


def _canonical(case: dict) -> bytes:
    return canonical_request(
        case["method"],
        case["path"],
        case["namespace"],
        case["timestamp"],
        case["nonce"],
        case["body_digest"],
        case["idempotency_key"],
        case["revision"],
    )


def test_vector_file_declares_the_framing_the_code_implements(vectors):
    # The domain separator is the first thing a foreign implementation gets
    # wrong, and it is invisible in every other assertion here because it is
    # constant across all of them.
    from wattracker.cloud.security import _CANONICAL_DOMAIN

    assert base64.b64decode(vectors["domain_separator_base64"]) == _CANONICAL_DOMAIN
    assert vectors["field_order"] == [
        "method", "path", "namespace", "timestamp", "nonce",
        "body_digest", "idempotency_key", "revision",
    ]
    assert vectors["version"] == 1
    assert vectors["canonical_requests"], "vectors must not be empty"


def test_every_body_digest_vector_matches(vectors):
    for case in vectors["body_digests"]:
        body = base64.b64decode(case["body_base64"])
        assert digest_body(body) == case["digest"], case["name"]


def test_every_canonical_request_vector_matches_byte_for_byte(vectors):
    for case in vectors["canonical_requests"]:
        expected = base64.b64decode(case["canonical_base64"])
        produced = _canonical(case)
        assert produced == expected, case["name"]
        assert len(produced) == case["canonical_length"], case["name"]
        assert hashlib.sha256(produced).hexdigest() == case["canonical_sha256"], (
            case["name"]
        )


def test_the_recorded_body_digest_is_the_digest_of_the_recorded_body(vectors):
    # Otherwise a client could match every canonical vector while hashing the
    # body wrongly, and only fail against a live server.
    for case in vectors["canonical_requests"]:
        body = base64.b64decode(case["body_base64"])
        assert digest_body(body) == case["body_digest"], case["name"]


def test_boundary_pairs_are_distinct(vectors):
    """The pairs that collide without length framing must not collide here."""
    by_name = {case["name"]: case for case in vectors["canonical_requests"]}
    assert vectors["distinct_pairs"], "the framing claim needs at least one pair"
    for left_name, right_name in vectors["distinct_pairs"]:
        left, right = by_name[left_name], by_name[right_name]
        assert _canonical(left) != _canonical(right), (left_name, right_name)
        # ... and they really are the same bytes once the framing is removed,
        # which is what makes the pair evidence rather than decoration.
        assert "".join([
            str(left["timestamp"]), left["nonce"],
            left["idempotency_key"], left["revision"],
        ]) == "".join([
            str(right["timestamp"]), right["nonce"],
            right["idempotency_key"], right["revision"],
        ])


def test_unicode_fields_are_framed_by_utf8_byte_length(vectors):
    """A UTF-16 or character count would produce a shorter prefix here."""
    case = next(
        entry for entry in vectors["canonical_requests"]
        if entry["name"] == "unicode-idempotency-key"
    )
    key = case["idempotency_key"]
    assert len(key.encode("utf-8")) > len(key), "vector lost its multibyte content"
    framed = len(key.encode("utf-8")).to_bytes(4, "big") + key.encode("utf-8")
    assert framed in _canonical(case)


def test_signature_vectors_agree_with_the_verifier(vectors):
    pytest.importorskip("cryptography")
    signatures = vectors["signature_vectors"]
    by_name = {case["name"]: case for case in vectors["canonical_requests"]}
    canonical = base64.b64decode(
        by_name[signatures["canonical_vector"]]["canonical_base64"]
    )
    public_key = bytes.fromhex(signatures["public_key_x963_hex"])
    # Uncompressed SEC1, which is what CryptoKit's x963Representation emits
    # and the only encoding validate_public_key accepts.
    assert len(public_key) == 65 and public_key[0] == 0x04
    for case in signatures["must_verify"]:
        assert verify_signature(
            public_key, canonical, case["signature_hex"],
            algorithm=signatures["algorithm"],
        ), case["name"]
    for case in signatures["must_not_verify"]:
        assert not verify_signature(
            public_key, canonical, case["signature_hex"],
            algorithm=signatures["algorithm"],
        ), case["name"]


def test_high_s_signature_is_the_malleable_twin_of_the_low_s_one(vectors):
    """Both must verify: the client must not normalise, and neither may we.

    The Secure Enclave emits high-s roughly half the time.  If either side
    ever starts enforcing low-s, this is the test that says so out loud rather
    than half of all iOS refreshes failing in production.
    """
    from wattracker.cloud.security import _P256_ORDER

    signatures = vectors["signature_vectors"]
    low = next(c for c in signatures["must_verify"] if c["name"] == "low-s")
    high = next(c for c in signatures["must_verify"] if c["name"] == "high-s")
    assert low["signature_hex"][:64] == high["signature_hex"][:64]
    low_s = int(low["signature_hex"][64:], 16)
    high_s = int(high["signature_hex"][64:], 16)
    assert low_s + high_s == _P256_ORDER
    assert low_s < _P256_ORDER // 2 < high_s
