"""Versioned, server-mediated cloud API.

This app is intentionally separate from ``wattracker.server.create_app``.
Local installations do not import or mount it, so an unavailable cloud never
changes local startup, routes, SQLite migrations, or request paths.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response

from .limits import QuotaExceeded, QuotaManager, QuotaPolicy
from .models import ModelError, SyncBatch
from .security import (
    CredentialRegistry,
    EnrollmentRegistry,
    NonceReplayGuard,
    SecurityStateBackend,
    canonical_request,
    digest_body,
    MIN_REPLAY_TTL_SECONDS,
    new_installation_id,
    verify_signature,
)
from .storage import MemoryTenantStore, StorageConflict, StaleRevision

_NOT_FOUND_BODY = {"detail": "not found"}
_MAX_TIMESTAMP = 60 * 5
_MAX_QUERY_LIMIT = 100


@dataclass
class CloudConfig:
    """Deployment-provided trust settings; no secrets have repository defaults."""

    server_secret: bytes
    operator_token: str
    plane: str = "all"  # all | read | sync
    require_subscription: bool = True
    apim_proof_header: str = "X-APIM-Request-Proof"
    apim_proof_value: str = field(default="", repr=False)
    require_apim_proof: bool = True
    verified_subject_header: str = "X-Verified-Entra-Subject"
    subscription_header: str = "Ocp-Apim-Subscription-Key"
    allowed_origins: tuple[str, ...] = ()
    max_request_bytes: int = 8 * 1024 * 1024
    max_decompressed_batch_bytes: int = 32 * 1024 * 1024
    replay_ttl_seconds: float = MIN_REPLAY_TTL_SECONDS
    clock: Callable[[], float] = time.time

    def __post_init__(self) -> None:
        if not isinstance(self.server_secret, bytes) or len(self.server_secret) < 32:
            raise ValueError("server_secret must contain at least 256 bits")
        if not isinstance(self.operator_token, str) or len(self.operator_token) < 8:
            raise ValueError("operator_token must be configured")
        if self.plane not in {"all", "read", "sync"}:
            raise ValueError("plane must be all, read, or sync")
        if self.max_request_bytes < 1 or self.max_decompressed_batch_bytes < 1:
            raise ValueError("body limits must be positive")
        if (
            isinstance(self.replay_ttl_seconds, bool)
            or not isinstance(self.replay_ttl_seconds, (int, float))
            or self.replay_ttl_seconds < MIN_REPLAY_TTL_SECONDS
        ):
            raise ValueError("replay TTL must cover the timestamp freshness window")
        if not isinstance(self.apim_proof_value, str) or len(self.apim_proof_value) > 512:
            raise ValueError("apim proof value is invalid")
        if any(
            not isinstance(origin, str) or not origin or "*" in origin
            for origin in self.allowed_origins
        ):
            raise ValueError("allowed_origins must be exact non-wildcard origins")


@dataclass
class CloudState:
    """Dependency-injection seam for the API and its tests."""

    config: CloudConfig
    credentials: CredentialRegistry
    enrollments: EnrollmentRegistry
    store: Any
    quotas: QuotaManager
    nonces: NonceReplayGuard

    @classmethod
    def create(
        cls,
        config: CloudConfig,
        *,
        store: Optional[Any] = None,
        quotas: Optional[QuotaManager] = None,
        security_backend: SecurityStateBackend | None = None,
        replay_backend: SecurityStateBackend | None = None,
        require_persistent_security: bool = False,
    ) -> "CloudState":
        replay_required = config.plane in {"sync", "all"}
        if replay_required:
            replay_backend = replay_backend or security_backend
        if require_persistent_security and (
            security_backend is None
            or not bool(getattr(security_backend, "durable", False))
            or (
                replay_required
                and (
                    replay_backend is None
                    or not bool(getattr(replay_backend, "durable", False))
                )
            )
        ):
            raise RuntimeError("production cloud runtime requires durable auth state")
        enrollments = EnrollmentRegistry(
            config.server_secret, backend=security_backend, clock=config.clock
        )
        return cls(
            config=config,
            credentials=CredentialRegistry(
                config.server_secret,
                enrollment_registry=enrollments,
                backend=security_backend,
                clock=config.clock,
            ),
            enrollments=enrollments,
            store=store if store is not None else MemoryTenantStore(),
            quotas=quotas or QuotaManager(
                QuotaPolicy(
                    max_request_bytes=config.max_request_bytes,
                    max_decompressed_batch_bytes=config.max_decompressed_batch_bytes,
                )
            ),
            nonces=NonceReplayGuard(
                ttl_seconds=config.replay_ttl_seconds,
                clock=config.clock,
                backend=replay_backend,
            ),
        )


def _not_found() -> JSONResponse:
    # Same status, body, cache policy, and content type for missing, revoked,
    # and unauthorized contexts/objects.  No storage metadata is reflected.
    return JSONResponse(
        _NOT_FOUND_BODY,
        status_code=404,
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
    )


def _error(status: int, detail: str, *, retry_after: Optional[int] = None) -> JSONResponse:
    headers = {"Cache-Control": "no-store"}
    if retry_after is not None:
        headers["Retry-After"] = str(retry_after)
    return JSONResponse({"detail": detail}, status_code=status, headers=headers)


def _safe_limit(raw: Optional[str]) -> int:
    try:
        value = int(raw or "100")
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="invalid limit")
    if value < 1 or value > _MAX_QUERY_LIMIT:
        raise HTTPException(status_code=400, detail="invalid limit")
    return value


def _etag(namespace: str, scope: str, body: bytes) -> str:
    # The scope is included in the cache validator but is never returned.
    material = namespace.encode() + b"\0" + scope.encode() + b"\0" + body
    return '"' + hashlib.sha256(material).hexdigest() + '"'


async def _bounded_body(request: Request, maximum: int) -> bytes:
    raw_length = request.headers.get("content-length")
    if raw_length is not None:
        try:
            if int(raw_length) > maximum:
                raise HTTPException(status_code=413, detail="request body too large")
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid content length")
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > maximum:
            raise HTTPException(status_code=413, detail="request body too large")
        chunks.append(chunk)
    return b"".join(chunks)


def _decompress_bounded(body: bytes, maximum: int) -> bytes:
    if len(body) > maximum:
        raise HTTPException(status_code=413, detail="request body too large")
    # gzip is optional; accepting it does not make an unbounded decompression
    # primitive.  ``gzip.decompress`` is avoided because it allocates first.
    if not body.startswith(b"\x1f\x8b"):
        if len(body) > maximum:
            raise HTTPException(status_code=413, detail="batch too large")
        return body
    import zlib

    decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
    out = bytearray()
    try:
        for start in range(0, len(body), 64 * 1024):
            out.extend(decoder.decompress(body[start : start + 64 * 1024], maximum - len(out) + 1))
            if len(out) > maximum:
                raise HTTPException(status_code=413, detail="decompressed batch too large")
        out.extend(decoder.flush(maximum - len(out) + 1))
    except HTTPException:
        raise
    except zlib.error as exc:
        raise HTTPException(status_code=400, detail="invalid compressed body") from exc
    if len(out) > maximum or not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
        raise HTTPException(status_code=413 if len(out) > maximum else 400,
                            detail="invalid compressed body")
    return bytes(out)


def _writer_header(request: Request, name: str) -> str:
    value = request.headers.get(name.lower(), "")
    if not value or len(value) > 512:
        raise HTTPException(status_code=401, detail="writer authorization required")
    return value


def _safe_compare_text(left: object, right: object) -> bool:
    """Compare arbitrary header text without ASCII-only compare_digest errors."""
    if not isinstance(left, str) or not isinstance(right, str):
        return False
    try:
        return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))
    except (UnicodeEncodeError, TypeError):
        return False


def _apim_proof_valid(state: CloudState, request: Request) -> bool:
    if not state.config.require_apim_proof:
        return True
    marker = request.headers.get(state.config.apim_proof_header.lower(), "")
    return bool(state.config.apim_proof_value) and _safe_compare_text(
        marker, state.config.apim_proof_value
    )


def _writer_auth(state: CloudState, request: Request) -> Any:
    """Authenticate the APIM proof and writer subscription without reading the body."""
    if not state.quotas.public_enabled:
        raise HTTPException(status_code=403, detail="public API disabled")
    if not _apim_proof_valid(state, request):
        raise HTTPException(status_code=401, detail="APIM authorization required")
    credential_id = _writer_header(request, "X-Writer-Credential")
    subscription = request.headers.get(state.config.subscription_header.lower(), "")
    if state.config.require_subscription and not subscription:
        raise HTTPException(status_code=401, detail="subscription authorization required")
    writer = state.credentials.authenticate_writer(credential_id, subscription)
    if writer is None:
        raise HTTPException(status_code=401, detail="writer authorization required")
    return writer


def _writer_signature_fields(request: Request) -> tuple[int, str, str, int, str]:
    timestamp_raw = _writer_header(request, "X-Writer-Timestamp")
    nonce = _writer_header(request, "X-Writer-Nonce")
    idem = _writer_header(request, "X-Writer-Idempotency-Key")
    revision_raw = _writer_header(request, "X-Writer-Revision")
    signature = _writer_header(request, "X-Writer-Signature")
    try:
        timestamp = int(timestamp_raw)
        revision = int(revision_raw)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail="writer authorization required") from exc
    return timestamp, nonce, idem, revision, signature


def _credential_key(writer: Any) -> bytes:
    key = getattr(writer, "verification_key", None) or getattr(writer, "signing_key", None)
    if not isinstance(key, bytes) or not key:
        raise HTTPException(status_code=401, detail="writer authorization required")
    return key


def _scope(writer: Any) -> tuple[str, str]:
    namespace = getattr(writer, "namespace", None)
    local_user_scope = getattr(writer, "local_user_scope", None)
    if not isinstance(namespace, str) or not namespace or not isinstance(local_user_scope, str) or not local_user_scope:
        raise HTTPException(status_code=401, detail="writer authorization required")
    return namespace, local_user_scope


def _resolve_reader(state: CloudState, request: Request) -> Any:
    if not _apim_proof_valid(state, request):
        return None
    raw = request.headers.get("authorization", "")
    scheme, _, token = raw.partition(" ")
    if scheme.lower() != "bearer" or not token or len(token) > 512:
        return None
    subject = request.headers.get(state.config.verified_subject_header.lower(), "")
    if not subject or len(subject) > 256:
        return None
    context = state.credentials.resolve_reader(token)
    if context is None or not state.quotas.public_enabled:
        return None
    bound_subject = getattr(context, "subject", None)
    if bound_subject is not None and (
        not subject or not _safe_compare_text(bound_subject, subject)
    ):
        return None
    return context


def _context_scope(context: Any) -> tuple[str, str]:
    namespace = getattr(context, "namespace", None)
    local_user_scope = getattr(context, "local_user_scope", None)
    if not isinstance(namespace, str) or not namespace or not isinstance(local_user_scope, str) or not local_user_scope:
        raise ValueError("invalid reader context")
    return namespace, local_user_scope


def _reader_payload(items: list[Any]) -> bytes:
    return json.dumps(
        {"items": [item.wire(include_deleted=False) for item in items]},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def create_cloud_app(
    config: CloudConfig,
    *,
    state: Optional[CloudState] = None,
) -> FastAPI:
    """Construct the explicit cloud app; never called by local startup."""
    state = state or CloudState.create(config)
    if state.config is not config:
        raise ValueError("state and config must be the same deployment")

    app = FastAPI(
        title="Wattracker Cloud API",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.cloud = state
    if config.allowed_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(config.allowed_origins),
            allow_credentials=False,
            allow_methods=["GET", "HEAD", "POST"],
            allow_headers=[
                "Authorization", "Content-Type", config.subscription_header,
                "X-Writer-Credential", "X-Writer-Timestamp", "X-Writer-Nonce",
                "X-Writer-Idempotency-Key", "X-Writer-Revision", "X-Writer-Signature",
            ],
        )

    def read_enabled() -> bool:
        return config.plane in {"all", "read"}

    def sync_enabled() -> bool:
        return config.plane in {"all", "sync"}

    if read_enabled():
        @app.post("/api/v1/enrollment/start")
        async def enrollment_start(request: Request) -> Response:
            if not _safe_compare_text(
                request.headers.get("x-operator-token", ""), config.operator_token
            ):
                return _error(404, "not found")
            if (
                not _apim_proof_valid(state, request)
            ):
                return _not_found()
            subject = request.headers.get(config.verified_subject_header.lower(), "")
            if not subject or len(subject) > 256:
                return _not_found()
            try:
                invitation = state.enrollments.create(
                    new_installation_id(), new_installation_id(), subject=subject
                )
            except (ValueError, RuntimeError):
                return _not_found()
            return JSONResponse({
                "invitation": invitation.token,
                "expires_at": invitation.expires_at,
            }, headers={"Cache-Control": "no-store"})

        @app.post("/api/v1/enrollment/complete")
        async def enrollment_complete(request: Request) -> Response:
            if (
                not _apim_proof_valid(state, request)
            ):
                return _error(401, "verified reader authorization required")
            subject = request.headers.get(config.verified_subject_header.lower(), "")
            if not subject:
                return _error(401, "verified reader authorization required")
            subscription = request.headers.get(config.subscription_header.lower(), "")
            if len(subscription) > 512:
                return _error(401, "subscription authorization required")
            # The invitation is the only selector. The body contains a public
            # verification key, never an installation/account selector or a
            # private signing key.
            body = await _bounded_body(request, 64 * 1024)
            try:
                payload = json.loads(body.decode("utf-8"))
                token = payload.get("invitation")
                public_key_hex = payload.get("public_key")
                if not isinstance(token, str) or not isinstance(public_key_hex, str):
                    raise ValueError
                public_key = bytes.fromhex(public_key_hex)
                if len(public_key) != 32:
                    raise ValueError
            except (ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError, RecursionError):
                return _error(400, "invalid enrollment request")
            try:
                binding = state.enrollments.consume(token, subject=subject)
                if (
                    binding is None
                    or not _safe_compare_text(
                        getattr(binding, "subject", None) or "", subject
                    )
                ):
                    return _not_found()
                writer = state.credentials.enroll_writer(
                    binding, signing_key=public_key,
                    subscription_key=(subscription.encode("utf-8") if subscription else None),
                    enrollment_registry=state.enrollments,
                )
            except (ValueError, RuntimeError):
                return _not_found()
            context_token, _context = state.credentials.issue_reader_context_for_scope(
                writer.namespace, writer.local_user_scope, subject
            )
            return JSONResponse({
                "credential": writer.credential_id,
                "subscription_key": writer.subscription_key.decode("ascii"),
                "signature_algorithm": writer.signature_algorithm,
                # This is an opaque request-signing context, not a storage
                # partition key or installation identifier.  The client may
                # sign it but cannot choose or alter it.
                "signing_namespace": writer.namespace,
                "reader_context": context_token,
            }, headers={"Cache-Control": "no-store"})

        @app.get("/api/v1/context")
        async def context(request: Request) -> Response:
            context_value = _resolve_reader(state, request)
            if context_value is None:
                return _not_found()
            namespace, scope = _context_scope(context_value)
            revision = state.store.revision(namespace, scope)
            body = json.dumps({
                "capabilities": {
                    "activities": True,
                    "activity_details": True,
                    "calendar": True,
                    "races": True,
                },
                "revision": revision,
            }, separators=(",", ":")).encode()
            try:
                state.quotas.admit_read(namespace, scope, response_bytes=len(body))
            except QuotaExceeded as exc:
                return _error(exc.status_code, "read quota exceeded", retry_after=exc.retry_after)
            return Response(body, media_type="application/json",
                            headers={"Cache-Control": "private, no-store",
                                     "ETag": _etag(namespace, scope, body)})

        async def collection(request: Request, kinds: set[str]) -> Response:
            context_value = _resolve_reader(state, request)
            if context_value is None:
                return _not_found()
            namespace, scope = _context_scope(context_value)
            limit = _safe_limit(request.query_params.get("limit"))
            try:
                state.quotas.admit_read(namespace, scope, response_bytes=0)
                with state.quotas.backend_slot():
                    items = state.store.list_objects(namespace, scope, kinds=kinds, limit=limit)
                body = _reader_payload(items)
                # Count exact returned bytes without charging a second request.
                state.quotas.record_read_bytes(namespace, scope, len(body))
            except QuotaExceeded as exc:
                return _error(exc.status_code, "read quota exceeded", retry_after=exc.retry_after)
            response = Response(body, media_type="application/json",
                                headers={"Cache-Control": "private, no-store",
                                         "ETag": _etag(namespace, scope, body)})
            return response

        @app.get("/api/v1/context/calendar")
        async def calendar(request: Request) -> Response:
            return await collection(request, {"calendar", "scheduled_workout"})

        @app.get("/api/v1/context/activities")
        async def activities(request: Request) -> Response:
            return await collection(request, {"activity"})

        @app.get("/api/v1/context/activities/{object_id}")
        async def activity_detail(request: Request, object_id: str) -> Response:
            context_value = _resolve_reader(state, request)
            if context_value is None:
                return _not_found()
            namespace, scope = _context_scope(context_value)
            try:
                state.quotas.admit_read(namespace, scope, response_bytes=0)
                with state.quotas.backend_slot():
                    item = state.store.get(namespace, scope, object_id)
                if item is None or item.kind not in {"activity", "activity_detail", "stream"}:
                    return _not_found()
                body = json.dumps(item.wire(include_deleted=False), separators=(",", ":")).encode()
                state.quotas.record_read_bytes(namespace, scope, len(body))
            except QuotaExceeded as exc:
                return _error(exc.status_code, "read quota exceeded", retry_after=exc.retry_after)
            except (KeyError, TypeError, ValueError):
                return _not_found()
            return Response(body, media_type="application/json",
                            headers={"Cache-Control": "private, no-store",
                                     "ETag": _etag(namespace, scope, body)})

        @app.get("/api/v1/context/races")
        async def races(request: Request) -> Response:
            return await collection(request, {"race"})

    if sync_enabled():
        @app.post("/api/v1/sync/batches")
        async def sync_batch(request: Request) -> Response:
            writer = _writer_auth(state, request)
            timestamp, nonce, idem, revision, signature = _writer_signature_fields(request)
            now = config.clock()
            if abs(now - timestamp) > _MAX_TIMESTAMP:
                return _error(401, "writer authorization required")
            namespace, scope = _scope(writer)
            raw = await _bounded_body(request, config.max_request_bytes)
            body_hash = digest_body(raw)
            try:
                canonical = canonical_request(
                    request.method, request.url.path, namespace, timestamp, nonce,
                    body_hash, idem, str(revision),
                )
            except (TypeError, ValueError, UnicodeError):
                return _error(401, "writer authorization required")
            if not verify_signature(
                _credential_key(writer), canonical, signature,
                algorithm=getattr(writer, "signature_algorithm", ""),
            ):
                return _error(401, "writer authorization required")
            if not state.nonces.accept(
                namespace, getattr(writer, "credential_id", ""), nonce, now=now
            ):
                return _error(401, "writer authorization required")
            decoded = _decompress_bounded(raw, config.max_decompressed_batch_bytes)
            try:
                payload = json.loads(decoded.decode("utf-8"))
                batch = SyncBatch.from_wire(payload)
            except (UnicodeDecodeError, json.JSONDecodeError, ModelError, TypeError, ValueError, RecursionError):
                return _error(400, "invalid sync batch")
            if batch.batch_id != idem or batch.revision != revision:
                return _error(401, "writer authorization required")
            try:
                # The body is bounded and the store's current scope is selected
                # only from the verified writer.  No caller partition/path is
                # passed to the storage backend.
                current_usage = state.store.usage(namespace, scope)
                installation_usage = state.store.usage_for_namespace(namespace)
                state.quotas.admit_write(
                    namespace, scope, request_bytes=len(raw),
                    decompressed_bytes=len(decoded), object_count=len(batch.objects),
                    stored_bytes=current_usage + len(decoded),
                    installation_stored_bytes=installation_usage + len(decoded),
                )
                with state.quotas.backend_slot():
                    result = state.store.apply(namespace, scope, batch)
            except QuotaExceeded as exc:
                return _error(exc.status_code, "write quota exceeded", retry_after=exc.retry_after)
            except StaleRevision:
                return _error(409, "stale revision")
            except StorageConflict:
                return _error(409, "idempotency conflict")
            return JSONResponse({
                "accepted": result.accepted,
                "revision": result.revision,
                "replayed": result.replay,
            }, headers={"Cache-Control": "no-store"})

        @app.get("/api/v1/sync/status")
        async def sync_status(request: Request) -> Response:
            writer = _writer_auth(state, request)
            namespace, scope = _scope(writer)
            try:
                state.quotas.admit_read(namespace, scope, response_bytes=0)
                body = json.dumps({
                    "revision": state.store.revision(namespace, scope),
                    "quota": state.quotas.scope_status(namespace, scope),
                }, separators=(",", ":")).encode()
                state.quotas.record_read_bytes(namespace, scope, len(body))
            except QuotaExceeded as exc:
                return _error(exc.status_code, "read quota exceeded", retry_after=exc.retry_after)
            return Response(body, media_type="application/json",
                            headers={"Cache-Control": "no-store"})

    # Surface checks are useful in deployment tests and prevent accidental
    # addition of a mutation route to the read-only app.
    app.state.read_plane_enabled = read_enabled()
    app.state.sync_plane_enabled = sync_enabled()
    return app


def cloud_enabled_from_environment() -> bool:
    """Cloud is opt-in; local startup never calls this function implicitly."""
    return os.environ.get("WATTRACKER_CLOUD_ENABLED", "0").strip().lower() in {
        "1", "true", "yes", "on"
    }
