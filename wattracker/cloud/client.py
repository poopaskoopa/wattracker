"""Opt-in local sync client.

The client is deliberately transport-injected.  A normal Wattracker process
can keep using its local SQLite database without importing this module or
opening a socket.  When enabled, callers provide a private key obtained from
the OS secure store and a read-only snapshot path; the client never persists
that key or exposes storage credentials.
"""
from __future__ import annotations

import json
import secrets
import ssl
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional
from urllib.parse import urlsplit
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from .models import SyncBatch
from .security import canonical_request, digest_body, sign_request, sign_request_ed25519
from .snapshot import snapshot_batch, snapshot_counts

SYNC_PATH = "/api/v1/sync/batches"
OFFLINE_MESSAGE = "Cloud sync offline — local data and features are unaffected."
MAX_RESPONSE_BYTES = 1 * 1024 * 1024


@dataclass(frozen=True)
class SyncCredentials:
    credential_id: str
    subscription_key: str
    signing_key: bytes
    namespace: str
    signer: Optional[Callable[[bytes, bytes], str]] = None
    signature_algorithm: str = "hmac-sha256"

    def __post_init__(self) -> None:
        if not self.credential_id or not self.subscription_key or not self.signing_key or not self.namespace:
            raise ValueError("complete sync credentials are required")
        if self.signature_algorithm not in {"hmac-sha256", "ed25519"}:
            raise ValueError("unsupported signature algorithm")


@dataclass(frozen=True)
class SyncResult:
    ok: bool
    status_code: Optional[int]
    detail: str
    revision: Optional[int] = None
    replayed: bool = False


def https_transport(
    *,
    client_certificate: Optional[str] = None,
    client_key: Optional[str] = None,
    ca_file: Optional[str] = None,
    timeout: float = 30.0,
) -> Callable[[str, Mapping[str, str], bytes], tuple[int, bytes]]:
    """Create a strict HTTPS/mTLS transport for an enabled desktop worker.

    Certificate verification remains enabled; there is no insecure HTTP or
    ``verify=False`` fallback.  The key paths are deployment-managed secure
    material and are never serialized into a request or log message.
    """
    if (client_certificate is None) != (client_key is None):
        raise ValueError("client certificate and key must be configured together")
    context = ssl.create_default_context(cafile=ca_file)
    if client_certificate is not None and client_key is not None:
        context.load_cert_chain(client_certificate, client_key)

    def send(url: str, headers: Mapping[str, str], body: bytes) -> tuple[int, bytes]:
        request = Request(url, data=body, headers=dict(headers), method="POST")
        try:
            response = urlopen(request, context=context, timeout=timeout)
        except HTTPError as exc:
            return exc.code, exc.read(MAX_RESPONSE_BYTES)
        with response:
            return int(response.status), response.read(MAX_RESPONSE_BYTES)

    return send


class CloudSyncClient:
    """Sign and send bounded batches through an injected HTTPS transport."""

    def __init__(
        self,
        endpoint: str,
        credentials: SyncCredentials,
        *,
        transport: Optional[Callable[[str, Mapping[str, str], bytes], tuple[int, bytes]]] = None,
        mtls_headers: Optional[Mapping[str, str]] = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        parsed = urlsplit(endpoint)
        if parsed.scheme != "https" or not parsed.netloc or parsed.query or parsed.fragment:
            raise ValueError("cloud endpoint must be an absolute HTTPS URL")
        self.endpoint = endpoint.rstrip("/")
        self.credentials = credentials
        self.transport = transport
        self.mtls_headers = dict(mtls_headers or {})
        self.clock = clock

    def push(self, batch: SyncBatch, *, namespace: Optional[str] = None) -> SyncResult:
        # A namespace supplied at construction is the server-issued signing
        # context.  The optional argument only preserves compatibility with
        # callers written before enrollment returned that context; it can
        # never override the bound value.
        del namespace  # Compatibility argument; the enrolled binding wins.
        signing_namespace = self.credentials.namespace
        raw = json.dumps(
            {
                "batch_id": batch.batch_id,
                "revision": batch.revision,
                "objects": [item.wire() for item in batch.objects],
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        timestamp = int(self.clock())
        nonce = secrets.token_urlsafe(24)
        body_hash = digest_body(raw)
        canonical = canonical_request(
            "POST", SYNC_PATH, signing_namespace, timestamp, nonce, body_hash,
            batch.batch_id, str(batch.revision),
        )
        if self.credentials.signer is not None:
            signature = self.credentials.signer(self.credentials.signing_key, canonical)
        elif self.credentials.signature_algorithm == "ed25519":
            signature = sign_request_ed25519(self.credentials.signing_key, canonical)
        else:
            signature = sign_request(self.credentials.signing_key, canonical)
        headers = {
            "Authorization": "Writer " + self.credentials.credential_id,
            "Content-Type": "application/json",
            "Content-Length": str(len(raw)),
            "Ocp-Apim-Subscription-Key": self.credentials.subscription_key,
            "X-Writer-Credential": self.credentials.credential_id,
            "X-Writer-Timestamp": str(timestamp),
            "X-Writer-Nonce": nonce,
            "X-Writer-Idempotency-Key": batch.batch_id,
            "X-Writer-Revision": str(batch.revision),
            "X-Writer-Signature": signature,
        }
        headers.update(self.mtls_headers)
        if self.transport is None:
            return SyncResult(False, None, OFFLINE_MESSAGE)
        try:
            status, response_body = self.transport(
                self.endpoint + SYNC_PATH, headers, raw
            )
        except Exception:
            return SyncResult(False, None, OFFLINE_MESSAGE)
        try:
            payload = json.loads(response_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = {}
        if status in (200, 201):
            return SyncResult(
                True, status, "ok", payload.get("revision"), bool(payload.get("replayed"))
            )
        if status in (401, 403, 404, 409, 413, 429, 503):
            return SyncResult(False, status, str(payload.get("detail") or OFFLINE_MESSAGE))
        return SyncResult(False, status, OFFLINE_MESSAGE)


def local_snapshot_status(path: str, user_id: int) -> dict[str, Any]:
    """Read integrity counts without taking a write connection or changing DB state."""
    try:
        return {"ok": True, "counts": snapshot_counts(path, user_id)}
    except Exception:
        return {"ok": False, "detail": OFFLINE_MESSAGE}


def build_snapshot_batch(
    path: str,
    user_id: int,
    *,
    batch_id: str,
    revision: int,
    limit: int = 1_000,
    include_streams: bool = False,
) -> SyncBatch:
    """Convenience seam used by an opt-in background sync worker."""
    return snapshot_batch(
        path,
        user_id,
        batch_id=batch_id,
        revision=revision,
        limit=limit,
        include_streams=include_streams,
    )
