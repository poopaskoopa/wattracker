#!/usr/bin/env python3
"""Regenerate the shared canonical-request interop vectors.

The vectors in ``tests/vectors/canonical_request_v1.json`` are the contract
between :func:`wattracker.cloud.security.canonical_request` and every client
that has to reproduce it byte for byte -- today the Swift client in ``ios/``.
They are generated from the Python implementation, which is the reference, and
asserted against by both languages.  A change here that moves a byte breaks
both suites at once, which is the point: a canonical request that quietly
changes shape is an opaque 401 on a phone and no clue anywhere.

No private key is written into the file.  The signature vectors carry a public
key and signatures only, so the repository never holds signing material.

Usage:  python scripts/generate_canonical_vectors.py
"""
from __future__ import annotations

import base64
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from wattracker.cloud.security import (  # noqa: E402
    _CANONICAL_DOMAIN,
    _P256_ORDER,
    canonical_request,
    digest_body,
    generate_p256_keypair,
    sign_request_ecdsa_p256,
    verify_signature,
)

OUTPUT = ROOT / "tests" / "vectors" / "canonical_request_v1.json"

NAMESPACE_A = "a" * 64
NAMESPACE_B = "0123456789abcdef" * 4

# Every case below exists for a reason a reader can check.
CASES: list[dict] = [
    {
        "name": "empty-body-refresh",
        "why": (
            "The device refresh envelope: no body, fixed idempotency key, and "
            "an EMPTY revision string. The empty field still gets a 4-byte "
            "length prefix of zero, which is the field a naive implementation "
            "drops."
        ),
        "method": "POST",
        "path": "/api/v1/context/refresh",
        "namespace": NAMESPACE_A,
        "timestamp": 1000,
        "nonce": "refresh-1",
        "body": b"",
        "idempotency_key": "context-refresh",
        "revision": "",
    },
    {
        "name": "sync-batch-json-body",
        "why": "A real sync push: non-empty body, numeric revision as text.",
        "method": "POST",
        "path": "/api/v1/sync/batches",
        "namespace": NAMESPACE_B,
        "timestamp": 1735689600,
        "nonce": "3nQ4Xk9v-Zt2Lp7Rw0Ma",
        "body": b'{"batch_id":"batch-1","objects":[],"revision":1}',
        "idempotency_key": "batch-1",
        "revision": "1",
    },
    {
        "name": "unicode-idempotency-key",
        "why": (
            "Length framing counts UTF-8 BYTES, not characters. The accented "
            "letters and the astral-plane emoji make a UTF-16 count or a "
            "grapheme count produce a different prefix, which is exactly how "
            "a Swift client goes wrong."
        ),
        "method": "POST",
        "path": "/api/v1/sync/batches",
        "namespace": NAMESPACE_A,
        "timestamp": 1000,
        "nonce": "nonce",
        "body": b"",
        "idempotency_key": "café-\U0001f6b4-バッチ",
        "revision": "1",
    },
    {
        "name": "unicode-path",
        "why": "The path is UTF-8 encoded, not percent-encoded, before framing.",
        "method": "GET",
        "path": "/api/v1/context/プロフィール",
        "namespace": NAMESPACE_B,
        "timestamp": "1000",
        "nonce": "ünicode-nonce-åäö",
        "body": b"",
        "idempotency_key": "profile",
        "revision": "",
    },
    {
        "name": "boundary-timestamp-1-nonce-23",
        "why": (
            "Half of a field-boundary pair. Without length framing this "
            "serializes identically to boundary-timestamp-12-nonce-3."
        ),
        "method": "POST",
        "path": "/api/v1/context/refresh",
        "namespace": NAMESPACE_A,
        "timestamp": "1",
        "nonce": "23",
        "body": b"",
        "idempotency_key": "context-refresh",
        "revision": "",
    },
    {
        "name": "boundary-timestamp-12-nonce-3",
        "why": "The other half of the timestamp/nonce boundary pair.",
        "method": "POST",
        "path": "/api/v1/context/refresh",
        "namespace": NAMESPACE_A,
        "timestamp": "12",
        "nonce": "3",
        "body": b"",
        "idempotency_key": "context-refresh",
        "revision": "",
    },
    {
        "name": "boundary-idem-batch-1-revision-empty",
        "why": (
            "The idempotency/revision boundary, and the one a real client "
            "hits: 'batch-1' + '' and 'batch-' + '1' are the same bytes "
            "unframed."
        ),
        "method": "POST",
        "path": "/api/v1/sync/batches",
        "namespace": NAMESPACE_A,
        "timestamp": 1000,
        "nonce": "nonce",
        "body": b"",
        "idempotency_key": "batch-1",
        "revision": "",
    },
    {
        "name": "boundary-idem-batch-revision-1",
        "why": "The other half of the idempotency/revision boundary pair.",
        "method": "POST",
        "path": "/api/v1/sync/batches",
        "namespace": NAMESPACE_A,
        "timestamp": 1000,
        "nonce": "nonce",
        "body": b"",
        "idempotency_key": "batch-",
        "revision": "1",
    },
    {
        "name": "lowercase-method-is-uppercased",
        "why": (
            "canonical_request upper-cases the method itself. A client that "
            "signs 'get' and sends 'GET' must still match."
        ),
        "method": "get",
        "path": "/api/v1/context/profile",
        "namespace": NAMESPACE_B,
        "timestamp": 1000,
        "nonce": "profile-1",
        "body": b"",
        "idempotency_key": "profile",
        "revision": "",
    },
    {
        "name": "long-nonce-256-bytes",
        "why": "A nonce long enough that a 1-byte or varint length prefix breaks.",
        "method": "POST",
        "path": "/api/v1/context/refresh",
        "namespace": NAMESPACE_A,
        "timestamp": 1000,
        "nonce": "n" * 256,
        "body": b"",
        "idempotency_key": "context-refresh",
        "revision": "",
    },
]

BODY_CASES: list[tuple[str, bytes]] = [
    ("empty", b""),
    ("ascii-json", b'{"batch_id":"batch-1","objects":[],"revision":1}'),
    ("utf8-multibyte", "バッチ-\U0001f6b4".encode("utf-8")),
    ("nul-and-newline", b"\x00\r\n binary \xff\xfe"),
]

DISTINCT_PAIRS = [
    ["boundary-timestamp-1-nonce-23", "boundary-timestamp-12-nonce-3"],
    ["boundary-idem-batch-1-revision-empty", "boundary-idem-batch-revision-1"],
]


def _case(entry: dict) -> dict:
    body: bytes = entry["body"]
    body_digest = digest_body(body)
    canonical = canonical_request(
        entry["method"],
        entry["path"],
        entry["namespace"],
        entry["timestamp"],
        entry["nonce"],
        body_digest,
        entry["idempotency_key"],
        entry["revision"],
    )
    return {
        "name": entry["name"],
        "why": entry["why"],
        "method": entry["method"],
        "path": entry["path"],
        "namespace": entry["namespace"],
        "timestamp": entry["timestamp"],
        "nonce": entry["nonce"],
        "body_base64": base64.b64encode(body).decode("ascii"),
        "body_digest": body_digest,
        "idempotency_key": entry["idempotency_key"],
        "revision": entry["revision"],
        "canonical_base64": base64.b64encode(canonical).decode("ascii"),
        "canonical_sha256": hashlib.sha256(canonical).hexdigest(),
        "canonical_length": len(canonical),
    }


def _signature_vectors() -> dict:
    """A fixed public key with signatures over one canonical request.

    The private half is generated here and deliberately thrown away: the file
    must never carry signing material.  What it does carry is enough to prove
    that a client's ECDSA-P256/SHA-256 raw ``r || s`` verification agrees with
    the server's -- including that a high-s signature, which the Secure
    Enclave emits about half the time, is accepted rather than normalised.
    """

    target = _case(CASES[0])
    canonical = base64.b64decode(target["canonical_base64"])
    private_key, public_key = generate_p256_keypair()
    signature = sign_request_ecdsa_p256(private_key, canonical)
    r = int(signature[:64], 16)
    s = int(signature[64:], 16)
    if s > _P256_ORDER // 2:  # normalise once here so the pair below is stable
        s = _P256_ORDER - s
        signature = f"{r:064x}{s:064x}"
    high_s = f"{r:064x}{_P256_ORDER - s:064x}"
    tampered = f"{r:064x}{(s + 1) % _P256_ORDER:064x}"
    del private_key

    vectors = {
        "algorithm": "ecdsa-p256-sha256",
        "canonical_vector": target["name"],
        "public_key_x963_hex": public_key.hex(),
        "must_verify": [
            {
                "name": "low-s",
                "why": "The ordinary case.",
                "signature_hex": signature,
            },
            {
                "name": "high-s",
                "why": (
                    "ECDSA is malleable and the server accepts that on "
                    "purpose (see verify_signature). A client must NOT "
                    "normalise s: the Secure Enclave emits high-s about half "
                    "the time, and a normalising client would be sending "
                    "something other than what it signed."
                ),
                "signature_hex": high_s,
            },
        ],
        "must_not_verify": [
            {
                "name": "tampered-s",
                "why": "s+1 is neither the signature nor its malleable twin.",
                "signature_hex": tampered,
            },
            {
                "name": "zero-s",
                "why": (
                    "s = 0 is outside [1, n-1] and must be refused before any "
                    "curve arithmetic happens."
                ),
                "signature_hex": f"{r:064x}{0:064x}",
            },
        ],
    }
    for entry in vectors["must_verify"]:
        assert verify_signature(
            public_key,
            canonical,
            entry["signature_hex"],
            algorithm="ecdsa-p256-sha256",
        ), entry["name"]
    for entry in vectors["must_not_verify"]:
        assert not verify_signature(
            public_key,
            canonical,
            entry["signature_hex"],
            algorithm="ecdsa-p256-sha256",
        ), entry["name"]
    return vectors


def build() -> dict:
    return {
        "version": 1,
        "generated_by": "scripts/generate_canonical_vectors.py",
        "purpose": (
            "Byte-for-byte interop vectors for canonical_request. Both the "
            "Python suite (tests/test_canonical_request_vectors.py) and the "
            "Swift suite (ios/WatTracker/WatTrackerTests) assert against this "
            "one file. Regenerate only when the wire format deliberately "
            "changes; a regeneration that is not accompanied by a client "
            "change is a break."
        ),
        "domain_separator_base64": base64.b64encode(_CANONICAL_DOMAIN).decode("ascii"),
        "field_order": [
            "method",
            "path",
            "namespace",
            "timestamp",
            "nonce",
            "body_digest",
            "idempotency_key",
            "revision",
        ],
        "framing": (
            "The domain separator, then for each field in field_order a "
            "4-byte big-endian length of its UTF-8 bytes followed by those "
            "bytes. The method is upper-cased first; the timestamp is decimal "
            "text; the body digest is lowercase hex SHA-256 of the body."
        ),
        "body_digests": [
            {
                "name": name,
                "body_base64": base64.b64encode(body).decode("ascii"),
                "digest": digest_body(body),
            }
            for name, body in BODY_CASES
        ],
        "canonical_requests": [_case(entry) for entry in CASES],
        "distinct_pairs": DISTINCT_PAIRS,
        "signature_vectors": _signature_vectors(),
    }


def main() -> int:
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(
        json.dumps(build(), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {OUTPUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
