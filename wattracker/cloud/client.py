"""Opt-in local sync client.

The client is deliberately transport-injected.  A normal Wattracker process
can keep using its local SQLite database without importing this module or
opening a socket.  When enabled, callers provide a private key obtained from
the OS secure store and a read-only snapshot path; the client never persists
that key or exposes storage credentials. Snapshot publication acknowledgements
are kept in the local database so failed pushes can resume safely.
"""
from __future__ import annotations

import json
import inspect
import re
import secrets
import ssl
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional
from urllib.parse import urlsplit
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener

from .models import SyncBatch
from .security import canonical_request, digest_body, sign_request, sign_request_ed25519
from .snapshot import (
    clear_snapshot_publication,
    commit_snapshot_batch,
    snapshot_batch,
    snapshot_counts,
    SnapshotError,
)

SYNC_PATH = "/api/v1/sync/batches"
ENROLLMENT_PATH = "/api/v1/enrollment/complete"
PAIRING_CODE_PATH = "/api/v1/devices/pairing-codes"
DEVICES_PATH = "/api/v1/devices"
PAIRING_IDEMPOTENCY_KEY = "device-pairing-code"
DEVICE_LIST_IDEMPOTENCY_KEY = "device-list"
DEVICE_REVOKE_IDEMPOTENCY_KEY = "device-revoke"
OFFLINE_MESSAGE = "Cloud sync offline — local data and features are unaffected."
MAX_RESPONSE_BYTES = 1 * 1024 * 1024
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{16,256}$")
_CREDENTIAL_ID_RE = re.compile(r"^[0-9a-f]{64}$")
_NAMESPACE_RE = re.compile(r"^[0-9a-f]{64}$")
_PUBLIC_KEY_RE = re.compile(r"^[0-9a-fA-F]{64}$")


class CloudEnrollmentError(ValueError):
    """Enrollment failed before a credential could be stored."""


class _NoRedirect(HTTPRedirectHandler):
    """Never forward signed cloud headers to a redirect target."""

    def redirect_request(self, req, fp, code, msg, headers, new_url):
        del req, fp, code, msg, headers, new_url
        return None


def validate_cloud_endpoint(endpoint: str) -> str:
    """Validate and normalize a cloud base URL at the trust boundary."""
    if not isinstance(endpoint, str) or not endpoint or len(endpoint) > 2048:
        raise ValueError("cloud endpoint must be an absolute HTTPS URL")
    if any(
        char.isspace()
        or char == "\\"
        or ord(char) < 0x20
        or ord(char) == 0x7f
        for char in endpoint
    ):
        raise ValueError("cloud endpoint contains control characters")
    parsed = urlsplit(endpoint)
    try:
        hostname = parsed.hostname
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("cloud endpoint is invalid") from exc
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or not hostname
        or not hostname.isascii()
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("cloud endpoint must be an absolute HTTPS URL")
    return endpoint.rstrip("/")


def _validate_invitation(invitation: str) -> str:
    if not isinstance(invitation, str) or not _TOKEN_RE.fullmatch(invitation):
        raise CloudEnrollmentError("invalid enrollment invitation")
    return invitation


def _validate_public_key_hex(public_key: str) -> bytes:
    if not isinstance(public_key, str) or not _PUBLIC_KEY_RE.fullmatch(public_key):
        raise CloudEnrollmentError("invalid enrollment public key")
    return bytes.fromhex(public_key)


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
    opener = build_opener(_NoRedirect, HTTPSHandler(context=context))

    def send(
        url: str, headers: Mapping[str, str], body: bytes, method: str = "POST",
    ) -> tuple[int, bytes]:
        validate_cloud_endpoint(url.rsplit("/api/v1/", 1)[0])
        method = method.upper()
        if method not in {"GET", "POST"}:
            raise ValueError("unsupported cloud method")
        request = Request(
            url,
            data=None if method == "GET" else body,
            headers=dict(headers),
            method=method,
        )
        try:
            response = opener.open(request, timeout=timeout)
        except HTTPError as exc:
            return exc.code, exc.read(MAX_RESPONSE_BYTES)
        with response:
            return int(response.status), response.read(MAX_RESPONSE_BYTES)

    send.supports_method = True  # type: ignore[attr-defined]
    return send


class CloudSyncClient:
    """Sign and send bounded batches through an injected HTTPS transport."""

    def __init__(
        self,
        endpoint: str,
        credentials: Optional[SyncCredentials] = None,
        *,
        transport: Optional[Callable[[str, Mapping[str, str], bytes], tuple[int, bytes]]] = None,
        mtls_headers: Optional[Mapping[str, str]] = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.endpoint = validate_cloud_endpoint(endpoint)
        self.credentials = credentials
        self.transport = transport
        self.mtls_headers = dict(mtls_headers or {})
        self.clock = clock
        self._transport_accepts_method = bool(
            getattr(self.transport, "supports_method", False)
        )
        if self.transport is not None and not self._transport_accepts_method:
            try:
                signature = inspect.signature(self.transport)
                parameters = tuple(signature.parameters.values())
                self._transport_accepts_method = (
                    any(p.kind == inspect.Parameter.VAR_POSITIONAL for p in parameters)
                    or len(parameters) >= 4
                )
            except (TypeError, ValueError):
                self._transport_accepts_method = False

    def _request(
        self, method: str, path: str, body: bytes,
        *, headers: Optional[Mapping[str, str]] = None,
    ) -> tuple[Optional[int], dict[str, Any]]:
        if self.transport is None:
            return None, {}
        request_headers = {"Content-Length": str(len(body))}
        if body:
            request_headers["Content-Type"] = "application/json"
        request_headers.update(self.mtls_headers)
        request_headers.update(headers or {})
        try:
            if self._transport_accepts_method:
                status, response_body = self.transport(
                    self.endpoint + path, request_headers, body, method,
                )
            else:
                status, response_body = self.transport(
                    self.endpoint + path, request_headers, body,
                )
        except Exception:
            return None, {}
        try:
            payload = json.loads(response_body.decode("utf-8"))
            if not isinstance(payload, dict):
                payload = {}
        except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
            payload = {}
        try:
            status_code = int(status)
        except (TypeError, ValueError):
            return None, {}
        return status_code, payload

    @classmethod
    def enroll(
        cls,
        endpoint: str,
        invitation: str,
        *,
        transport: Optional[Callable[..., tuple[int, bytes]]] = None,
        mtls_headers: Optional[Mapping[str, str]] = None,
        clock: Callable[[], float] = time.time,
    ) -> SyncCredentials:
        """Complete an invitation and return private material for keyring storage."""
        endpoint = validate_cloud_endpoint(endpoint)
        invitation = _validate_invitation(invitation)
        try:
            from .security import generate_signing_keypair
            private_key, public_key = generate_signing_keypair()
        except Exception as exc:
            raise CloudEnrollmentError("cloud signing crypto is unavailable") from exc
        client = cls(
            endpoint, transport=transport, mtls_headers=mtls_headers, clock=clock,
        )
        body = json.dumps(
            {"invitation": invitation, "public_key": public_key.hex()},
            sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")
        status, payload = client._request("POST", ENROLLMENT_PATH, body)
        if status != 200:
            raise CloudEnrollmentError("cloud enrollment failed")
        credential_id = payload.get("credential")
        subscription = payload.get("subscription_key")
        namespace = payload.get("signing_namespace")
        algorithm = payload.get("signature_algorithm", "ed25519")
        if (
            not isinstance(credential_id, str)
            or not _CREDENTIAL_ID_RE.fullmatch(credential_id)
            or not isinstance(subscription, str)
            or not subscription
            or len(subscription) > 512
            or any(ord(char) < 0x21 or ord(char) > 0x7e for char in subscription)
            or not isinstance(namespace, str)
            or not _NAMESPACE_RE.fullmatch(namespace)
            or algorithm != "ed25519"
        ):
            raise CloudEnrollmentError("cloud enrollment response is invalid")
        return SyncCredentials(
            credential_id=credential_id,
            subscription_key=subscription,
            signing_key=private_key,
            namespace=namespace,
            signature_algorithm="ed25519",
        )

    def _signed_headers(
        self, method: str, path: str, body: bytes,
        *, idempotency_key: str, revision: int,
    ) -> dict[str, str]:
        credentials = self.credentials
        if credentials is None:
            raise CloudEnrollmentError("cloud credentials are unavailable")
        timestamp = int(self.clock())
        nonce = secrets.token_urlsafe(24)
        canonical = canonical_request(
            method, path, credentials.namespace, timestamp, nonce,
            digest_body(body), idempotency_key, str(revision),
        )
        if credentials.signer is not None:
            signature = credentials.signer(credentials.signing_key, canonical)
        elif credentials.signature_algorithm == "ed25519":
            signature = sign_request_ed25519(credentials.signing_key, canonical)
        else:
            signature = sign_request(credentials.signing_key, canonical)
        return {
            "Authorization": "Writer " + credentials.credential_id,
            "Content-Length": str(len(body)),
            "Ocp-Apim-Subscription-Key": credentials.subscription_key,
            "X-Writer-Credential": credentials.credential_id,
            "X-Writer-Timestamp": str(timestamp),
            "X-Writer-Nonce": nonce,
            "X-Writer-Idempotency-Key": idempotency_key,
            "X-Writer-Revision": str(revision),
            "X-Writer-Signature": signature,
        }

    def push(self, batch: SyncBatch, *, namespace: Optional[str] = None) -> SyncResult:
        # A namespace supplied at construction is the server-issued signing
        # context.  The optional argument only preserves compatibility with
        # callers written before enrollment returned that context; it can
        # never override the bound value.
        del namespace  # Compatibility argument; the enrolled binding wins.
        if self.credentials is None:
            return SyncResult(False, None, OFFLINE_MESSAGE)
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
        try:
            headers = self._signed_headers(
                "POST", SYNC_PATH, raw,
                idempotency_key=batch.batch_id, revision=batch.revision,
            )
        except Exception:
            return SyncResult(False, None, OFFLINE_MESSAGE)
        status, payload = self._request("POST", SYNC_PATH, raw, headers=headers)
        if status is None:
            return SyncResult(False, None, OFFLINE_MESSAGE)
        if status in (200, 201):
            return SyncResult(
                True, status, "ok", payload.get("revision"), bool(payload.get("replayed"))
            )
        if status in (401, 403, 404, 409, 413, 429, 503):
            return SyncResult(False, status, str(payload.get("detail") or OFFLINE_MESSAGE))
        return SyncResult(False, status, OFFLINE_MESSAGE)

    def mint_pairing_code(self) -> dict[str, Any]:
        """Mint a pairing code using the fixed signed writer envelope."""
        headers = self._signed_headers(
            "POST", PAIRING_CODE_PATH, b"",
            idempotency_key=PAIRING_IDEMPOTENCY_KEY, revision=0,
        )
        status, payload = self._request("POST", PAIRING_CODE_PATH, b"", headers=headers)
        if status not in (200, 201) or not isinstance(payload.get("pairing_code"), str):
            raise CloudEnrollmentError("pairing code request failed")
        result = {"pairing_code": payload["pairing_code"]}
        for key in ("expires_at", "expires_in"):
            value = payload.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                result[key] = value
        return result

    def list_devices(self) -> list[dict[str, Any]]:
        """List allowlisted device metadata; response secrets are discarded."""
        headers = self._signed_headers(
            "GET", DEVICES_PATH, b"",
            idempotency_key=DEVICE_LIST_IDEMPOTENCY_KEY, revision=0,
        )
        status, payload = self._request("GET", DEVICES_PATH, b"", headers=headers)
        if status != 200 or not isinstance(payload.get("devices"), list):
            raise CloudEnrollmentError("device listing request failed")
        allowed = {
            "credential_id", "label", "capabilities", "created_at",
            "last_seen_at", "revoked", "self",
        }
        devices: list[dict[str, Any]] = []
        for device in payload["devices"]:
            if not isinstance(device, dict):
                continue
            safe = {key: device[key] for key in allowed if key in device}
            credential_id = safe.get("credential_id")
            if not isinstance(credential_id, str) or not _CREDENTIAL_ID_RE.fullmatch(credential_id):
                continue
            devices.append(safe)
        return devices

    def revoke_device(self, credential_id: str) -> bool:
        """Revoke one device by its server-issued opaque credential id."""
        if not isinstance(credential_id, str) or not _CREDENTIAL_ID_RE.fullmatch(credential_id):
            raise ValueError("credential_id is invalid")
        path = DEVICES_PATH + "/" + credential_id + "/revoke"
        headers = self._signed_headers(
            "POST", path, b"",
            idempotency_key=DEVICE_REVOKE_IDEMPOTENCY_KEY, revision=0,
        )
        status, payload = self._request("POST", path, b"", headers=headers)
        return status == 200 and payload.get("revoked") is True


    def push_snapshot(
        self,
        path: str,
        user_id: int,
        *,
        limit: int = 1_000,
        include_streams: bool = False,
        include_derived: bool = True,
        republish: bool = False,
        derived_first: bool = False,
        should_continue: Optional[Callable[[], bool]] = None,
    ) -> list[SyncResult]:
        """Upload resumable local deltas, acknowledging each successful page."""
        if should_continue is not None and not should_continue():
            return []
        if republish:
            if self.transport is None:
                return [SyncResult(False, None, OFFLINE_MESSAGE)]
            try:
                clear_snapshot_publication(
                    path, user_id, reject_pending=True,
                )
            except SnapshotError as exc:
                return [SyncResult(False, None, str(exc))]
        results: list[SyncResult] = []
        while True:
            if should_continue is not None and not should_continue():
                return results
            batch = build_snapshot_batch(
                path, user_id, limit=limit,
                include_streams=include_streams,
                include_derived=include_derived,
                derived_first=derived_first,
            )
            if batch is None:
                return results
            if should_continue is not None and not should_continue():
                return results
            result = self.push(batch)
            results.append(result)
            if not result.ok:
                return results
            commit_snapshot_batch(path, user_id, batch)


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
    batch_id: Optional[str] = None,
    revision: Optional[int] = None,
    limit: int = 1_000,
    include_streams: bool = False,
    include_derived: bool = True,
    derived_first: bool = False,
    offset: int = 0,
    previously_published: Optional[Mapping[str, Mapping[str, object]]] = None,
    complete: Optional[bool] = None,
    republish: bool = False,
) -> Optional[SyncBatch]:
    """Convenience seam used by an opt-in background sync worker."""
    return snapshot_batch(
        path,
        user_id,
        batch_id=batch_id,
        revision=revision,
        limit=limit,
        include_streams=include_streams,
        include_derived=include_derived,
        derived_first=derived_first,
        offset=offset,
        previously_published=previously_published,
        complete=complete,
        republish=republish,
    )
