"""Versioned, server-mediated cloud API.

This app is intentionally separate from ``wattracker.server.create_app``.
Local installations do not import or mount it, so an unavailable cloud never
changes local startup, routes, SQLite migrations, or request paths.
"""
from __future__ import annotations

import base64
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

from .limits import (
    DurableKillSwitch,
    DurableQuotaCounters,
    QuotaExceeded,
    QuotaManager,
    QuotaPolicy,
)
from .models import ModelError, SyncBatch
from .security import (
    CredentialRegistry,
    DEFAULT_DEVICE_CAPABILITIES,
    DEVICE_SIGNATURE_ALGORITHMS,
    DevicePairingRegistry,
    EnrollmentRegistry,
    ExpiredRecordSweeper,
    NonceReplayGuard,
    PublicKeyUnavailable,
    READER_CONTEXT_TTL_SECONDS,
    SecurityStateBackend,
    canonical_request,
    digest_body,
    MIN_REPLAY_TTL_SECONDS,
    new_installation_id,
    validate_device_label,
    validate_public_key,
    verify_signature,
)
from .storage import MAX_QUERY_LIMIT, MemoryTenantStore, StorageConflict, StaleRevision

_NOT_FOUND_BODY = {"detail": "not found"}
_MAX_TIMESTAMP = 60 * 5
# The API bound and the store's accepted range are one fact, defined in
# storage (which cannot import this module without a cycle).
_MAX_QUERY_LIMIT = MAX_QUERY_LIMIT
# The refresh envelope carries no body and no idempotent effect, so both
# fields are fixed constants rather than caller-chosen values.  The nonce is
# what makes each signed refresh unique and single-use.
_REFRESH_IDEMPOTENCY_KEY = "context-refresh"
_REFRESH_REVISION = ""
_MAX_REFRESH_BODY_BYTES = 4 * 1024
# A device public key is at most a 65-byte uncompressed P-256 point.
_MAX_DEVICE_KEY_HEX = 130
# Minting a pairing code carries no body and no idempotent effect either, so
# the same fixed-envelope treatment applies.  The request path is already part
# of the canonical request, so this does not add cross-route replay protection
# -- it only keeps the signed envelope canonical and unambiguous.
_PAIRING_IDEMPOTENCY_KEY = "device-pairing-code"
_PAIRING_REVISION = 0
_MAX_PAIRING_BODY_BYTES = 4 * 1024
# Listing and revoking devices carry no body worth choosing and no idempotent
# effect, so they get the same fixed envelope as minting.  What separates one
# signed revoke from another is the *path*, which carries the target
# credential id and is part of the canonical request: a request signed to
# revoke one device cannot be re-aimed at another, and the replay guard stops
# it being sent twice.
_DEVICE_LIST_IDEMPOTENCY_KEY = "device-list"
_DEVICE_LIST_REVISION = 0
_DEVICE_REVOKE_IDEMPOTENCY_KEY = "device-revoke"
_DEVICE_REVOKE_REVISION = 0
_MAX_DEVICE_ADMIN_BODY_BYTES = 4 * 1024
# A credential id is 32 bytes of hex.  The path parameter is bounded before it
# reaches the registry so an enormous path cannot be used to probe anything.
_MAX_CREDENTIAL_ID_CHARS = 64


@dataclass
class CloudConfig:
    """Deployment-provided trust settings; no secrets have repository defaults."""

    server_secret: bytes
    operator_token: str
    plane: str = "all"  # all | read | sync
    require_subscription: bool = True
    gateway_proof_header: str = "X-Gateway-Request-Proof"
    gateway_proof_value: str = field(default="", repr=False)
    require_gateway_proof: bool = True
    verified_subject_header: str = "X-Verified-Entra-Subject"
    # A verified-subject header is worth exactly as much as the gateway that
    # overwrites it.  Deployments without such a gateway must set this False,
    # and then no route reads the header at all: a header that is trusted in
    # one deployment and forgeable in another, with nothing at startup telling
    # them apart, is the failure mode this flag exists to remove.
    require_verified_subject: bool = True
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
        if not isinstance(self.gateway_proof_header, str) or not self.gateway_proof_header:
            raise ValueError("gateway proof header is invalid")
        if not isinstance(self.gateway_proof_value, str) or len(self.gateway_proof_value) > 512:
            raise ValueError("gateway proof value is invalid")
        if self.require_gateway_proof and self.gateway_proof_value:
            # An empty value deliberately means "no gateway", and
            # `gateway_attests_subject` reads False for it.  A *non-empty*
            # value reads True, and that is what licenses every route to trust
            # the verified-subject header -- so a blank or trivially short one
            # claims a gateway while being guessable, and whoever guesses it
            # then dictates the subject.  Refuse the claim rather than let it
            # stand on a placeholder.
            if not self.gateway_proof_value.strip() or len(self.gateway_proof_value) < 8:
                raise ValueError("gateway proof value must be a secret, not a placeholder")
        if not isinstance(self.require_verified_subject, bool):
            raise ValueError("require_verified_subject must be a boolean")
        if any(
            not isinstance(origin, str) or not origin or "*" in origin
            for origin in self.allowed_origins
        ):
            raise ValueError("allowed_origins must be exact non-wildcard origins")

    @property
    def gateway_attests_subject(self) -> bool:
        """Whether some gateway actually vouches for the subject header.

        The header is only meaningful when a proof-carrying gateway sits in
        front and overwrites it on every request.  Without the proof, the
        header is whatever the caller typed.
        """

        return bool(self.require_gateway_proof and self.gateway_proof_value)


def _build_quota_manager(
    config: "CloudConfig",
    quota_backend: SecurityStateBackend | None,
    kill_backend: SecurityStateBackend | None = None,
) -> QuotaManager:
    """Build the daily-counter manager and kill switch, durable wherever they can be.

    A durable backend is used whenever one is available, including in
    tests, so the durable path is the one that is exercised rather than a
    production-only branch nobody runs until it fails.
    """

    policy = QuotaPolicy(
        max_request_bytes=config.max_request_bytes,
        max_decompressed_batch_bytes=config.max_decompressed_batch_bytes,
    )
    counters = None
    if quota_backend is not None and bool(getattr(quota_backend, "durable", False)):
        try:
            counters = DurableQuotaCounters(quota_backend)
        except ValueError:
            # A durable backend that cannot charge counters is a backend
            # from an older contract.  Fall back to the process-local
            # counters, which `require_persistent_security` then refuses
            # rather than letting a deployment believe it is metered.
            counters = None
    kill_switch = None
    if kill_backend is not None and bool(getattr(kill_backend, "durable", False)):
        try:
            kill_switch = DurableKillSwitch(kill_backend)
        except ValueError:
            kill_switch = None
    return QuotaManager(policy, counters=counters, kill_switch=kill_switch)


@dataclass
class CloudState:
    """Dependency-injection seam for the API and its tests."""

    config: CloudConfig
    credentials: CredentialRegistry
    enrollments: EnrollmentRegistry
    store: Any
    quotas: QuotaManager
    nonces: NonceReplayGuard
    pairings: DevicePairingRegistry
    # Housekeeping, not authorization.  ``None`` on any plane that must not
    # delete from the shared auth table -- which is every plane but the read
    # one, whose managed identity is the only one holding the delete action.
    sweeper: Any = None

    @classmethod
    def create(
        cls,
        config: CloudConfig,
        *,
        store: Optional[Any] = None,
        quotas: Optional[QuotaManager] = None,
        security_backend: SecurityStateBackend | None = None,
        replay_backend: SecurityStateBackend | None = None,
        quota_backend: SecurityStateBackend | None = None,
        kill_backend: SecurityStateBackend | None = None,
        require_persistent_security: bool = False,
    ) -> "CloudState":
        # Every plane now consumes replay nonces: the read plane verifies
        # signed device refresh requests, so a process-local guard there would
        # re-open a captured refresh across a cold start.  The read identity
        # can write its own auth table, so the shared security backend is a
        # valid replay store when a dedicated one is not supplied.
        replay_required = config.plane in {"read", "sync", "all"}
        if replay_required:
            replay_backend = replay_backend or security_backend
        # Daily quota counters need a table this identity may *write*.  The
        # replay backend is that table on every plane: the read plane claims
        # nonces in CloudAuth, which it writes, and the sync plane claims
        # them in CloudReplay, which it writes while holding only read
        # access to CloudAuth.  Following the replay backend therefore needs
        # no new table and no new role, and #164 owns the Bicep.
        quota_backend = quota_backend or replay_backend or security_backend
        # The kill switch deliberately does *not* follow the quota backend.
        # The counters split by plane (the read plane counts in ``CloudAuth``,
        # the sync plane in ``CloudReplay``) because each identity may write
        # only its own table.  A kill switch that split the same way would be
        # two switches: throwing it would stop one plane and leave the other
        # serving.  Production gives it its own ``CloudControl`` table, which
        # every plane can read while the external budget hook is the only
        # deployment identity that can write it.  The fallback keeps the
        # dependency-injection seam useful for tests and local development.
        kill_backend = kill_backend or security_backend
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
        if quotas is None:
            quotas = _build_quota_manager(config, quota_backend, kill_backend)
        # A process-local counter resets on every replica change, and these
        # containers scale to zero, so in production it is not a weaker
        # control -- it is no control.  Refuse it the same way an in-memory
        # auth backend is refused, rather than booting something that will
        # report quotas it never enforced.
        if require_persistent_security and not quotas.durable:
            raise RuntimeError(
                "production cloud runtime requires durable quota counters"
            )
        # And the same argument, one step further: a process-local kill switch
        # in a scale-to-zero deployment does not merely forget, it re-enables.
        # The budget action would stop the replicas that were up and the next
        # cold start would come back serving, so the switch would disable
        # spending and undo itself minutes later.  Refuse to boot instead.
        if require_persistent_security and not quotas.kill_switch_durable:
            raise RuntimeError(
                "production cloud runtime requires a durable kill switch"
            )
        # A deployment must not be able to claim it enforces a verified
        # subject while nothing issues one.  Without a proof-carrying gateway
        # the header is attacker-supplied, so trusting it would be a control
        # in name only -- refuse to start rather than serve one.  Removing the
        # gateway is therefore a deliberate configuration change here, not a
        # silent downgrade of every subject check in the app.
        if (
            require_persistent_security
            and config.require_verified_subject
            and not config.gateway_attests_subject
        ):
            raise RuntimeError(
                "a verified-subject header requires a gateway that proves "
                "itself and overwrites it; configure the gateway proof or set "
                "require_verified_subject=False"
            )
        enrollments = EnrollmentRegistry(
            config.server_secret, backend=security_backend, clock=config.clock
        )
        # Pairing codes live in the same durable auth state as invitations but
        # in their own record kind and their own digest space, so neither
        # registry can ever spend the other's token.
        pairings = DevicePairingRegistry(
            config.server_secret, backend=security_backend, clock=config.clock
        )
        # Contexts, their index, invitations, pairing codes and replay claims
        # all accumulate in ``CloudAuth`` and nothing ever reads an expired
        # one again.  The read plane is the only identity that may delete from
        # that table, so it is the only plane that sweeps; the sync plane holds
        # read access there and would only ever get a 403.
        sweeper = None
        if security_backend is not None and config.plane in {"all", "read"}:
            try:
                sweeper = ExpiredRecordSweeper(
                    security_backend, clock=config.clock
                )
            except ValueError:
                # A backend from an older contract cannot enumerate or delete.
                # Rows accumulating is a cost problem, never a correctness or
                # authorization one, so this is not a reason to refuse to boot.
                sweeper = None
        return cls(
            config=config,
            credentials=CredentialRegistry(
                config.server_secret,
                enrollment_registry=enrollments,
                pairing_registry=pairings,
                backend=security_backend,
                clock=config.clock,
            ),
            enrollments=enrollments,
            pairings=pairings,
            store=store if store is not None else MemoryTenantStore(),
            quotas=quotas,
            nonces=NonceReplayGuard(
                ttl_seconds=config.replay_ttl_seconds,
                clock=config.clock,
                backend=replay_backend,
            ),
            sweeper=sweeper,
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
        value = int(raw) if raw else _MAX_QUERY_LIMIT
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


def _gateway_proof_valid(state: CloudState, request: Request) -> bool:
    if not state.config.require_gateway_proof:
        return True
    marker = request.headers.get(state.config.gateway_proof_header.lower(), "")
    return bool(state.config.gateway_proof_value) and _safe_compare_text(
        marker, state.config.gateway_proof_value
    )


def _attested_subject(state: CloudState, request: Request) -> str | None:
    """The subject some gateway vouches for, or ``None`` when nothing does.

    Where the deployment attests no subject the header is not read at all:
    reading a header nobody vouches for is worse than not having one, because
    it looks like a check.  This never fails a request -- a missing header is
    simply no subject, and a caller that needs one says so itself.
    """

    if not state.config.require_verified_subject:
        return None
    subject = request.headers.get(state.config.verified_subject_header.lower(), "")
    if not subject or len(subject) > 256:
        return None
    return subject


def _subject_binding(state: CloudState, request: Request) -> tuple[bool, str | None]:
    """``(ok, subject)`` for routes that demand a subject wherever one exists.

    ``ok`` is False only when the deployment attests a subject and the request
    did not carry a usable one.  Routes whose own secret is sufficient
    authorization use :func:`_attested_subject` instead and let the stored
    record decide whether a subject is needed.
    """

    if not state.config.require_verified_subject:
        return True, None
    subject = _attested_subject(state, request)
    return subject is not None, subject


def _has_capability(credential: Any, capability: str) -> bool:
    """Fail closed unless the stored credential carries ``capability``.

    Capability is read from credential state only.  A credential type that
    does not declare capabilities is never granted one by default.
    """
    granted = getattr(credential, "capabilities", None)
    if not isinstance(granted, (frozenset, set)):
        return False
    return capability in granted


def _writer_auth(state: CloudState, request: Request, *, capability: str) -> Any:
    """Authenticate a signing credential and assert one required capability.

    Both writer and paired-device credentials are resolvable here, and both
    must satisfy the same app-issued subscription factor.  What separates them is
    ``capability``: a device issued read-only is refused by a route that
    asserts ``"write"``, and widening it later is a capability grant, not a
    change to this function.
    """
    # The durable kill state, read here rather than trusted from a boolean
    # this replica set for itself.  Disabled raises 403; unreadable raises
    # 503, and neither is a request this route may serve.
    state.quotas.require_public_enabled()
    if not _gateway_proof_valid(state, request):
        raise HTTPException(status_code=401, detail="gateway authorization required")
    credential_id = _writer_header(request, "X-Writer-Credential")
    subscription = request.headers.get(state.config.subscription_header.lower(), "")
    if state.config.require_subscription and not subscription:
        raise HTTPException(status_code=401, detail="subscription authorization required")
    credential = state.credentials.authenticate_writer(credential_id, subscription)
    if credential is None:
        credential = state.credentials.authenticate_device(credential_id, subscription)
    if credential is None or not _has_capability(credential, capability):
        raise HTTPException(status_code=401, detail="writer authorization required")
    return credential


def _sweep_expired_auth_state(state: CloudState) -> None:
    """Opportunistic housekeeping, attached to a route that already wrote.

    Bounded, rate-limited and best-effort inside the sweeper; wrapped again
    here because no request may fail because a delete did.
    """

    sweeper = getattr(state, "sweeper", None)
    if sweeper is None:
        return
    try:
        sweeper.maybe_sweep()
    except Exception:  # pragma: no cover - the sweeper already swallows
        pass


def _verified_scope(
    state: CloudState,
    request: Request,
    credential: Any,
    body: bytes,
    *,
    idempotency_key: str,
    revision: int,
) -> tuple[str, str] | None:
    """Verify a fixed signed envelope and return the credential's own scope.

    The scope comes from the stored credential and from nowhere else -- no
    route below reads a namespace, scope, installation or subject from a
    parameter, a body field or a header.  A writer credential and a paired
    device are both acceptable signers, which is what lets the rider's
    remaining phone revoke the lost one without the desktop; the capability
    ``_writer_auth`` asserted is what decides, and it is read from stored
    credential state.

    Returns ``None`` for every envelope failure -- bad signature, stale
    timestamp, replayed nonce, wrong fixed envelope, missing or mismatched
    attested subject -- so the caller answers all of them identically.
    """

    verified = _verify_writer_request(state, request, credential, body)
    if verified is None:
        return None
    namespace, scope, idem, sent_revision = verified
    if not _safe_compare_text(idem, idempotency_key) or sent_revision != revision:
        return None
    # Subject outcomes are only reachable once the caller has already proven
    # possession of the credential's private key.
    ok, verified_subject = _subject_binding(state, request)
    if not ok:
        return None
    stored_subject = getattr(credential, "subject", None)
    if (
        stored_subject is not None
        and verified_subject is not None
        and not _safe_compare_text(stored_subject, verified_subject)
    ):
        return None
    return namespace, scope


def _device_listing_entry(
    state: CloudState, device: Any, caller_credential_id: object
) -> dict[str, Any]:
    """The only shape a device is ever described in over the wire.

    Built by naming every field explicitly rather than by filtering a stored
    record, because a field added to ``DeviceCredential`` later must not
    appear here by default.  Nothing key-shaped is listed: no verification
    key, no subscription key, not even the digest of one, and no signature
    algorithm -- none of it helps a rider decide which phone to revoke, and
    the response is the one place device state leaves the deployment.  The
    namespace and local scope are omitted for the same reason every other
    route omits them.
    """

    credential_id = device.credential_id
    return {
        "credential_id": credential_id,
        "label": device.label,
        "capabilities": sorted(getattr(device, "capabilities", ())),
        "created_at": device.created_at or None,
        "last_seen_at": state.credentials.device_last_seen(credential_id),
        "revoked": bool(getattr(device, "revoked", False)),
        # So a client can grey out "this is the device you are holding".
        "self": _safe_compare_text(credential_id, caller_credential_id),
    }


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


def _verify_writer_request(
    state: CloudState, request: Request, writer: Any, body: bytes
) -> tuple[str, str, str, int] | None:
    """Verify the signed request envelope and consume its replay nonce."""
    try:
        timestamp, nonce, idem, revision, signature = _writer_signature_fields(request)
        now = state.config.clock()
        if abs(now - timestamp) > _MAX_TIMESTAMP:
            return None
        namespace, scope = _scope(writer)
        canonical = canonical_request(
            request.method, request.url.path, namespace, timestamp, nonce,
            digest_body(body), idem, str(revision),
        )
        if not verify_signature(
            _credential_key(writer), canonical, signature,
            algorithm=getattr(writer, "signature_algorithm", ""),
        ):
            return None
        if not state.nonces.accept(
            namespace, getattr(writer, "credential_id", ""), nonce, now=now
        ):
            return None
        return namespace, scope, idem, revision
    except (HTTPException, TypeError, ValueError, UnicodeError):
        return None


def _device_signature_fields(request: Request) -> tuple[int, str, str]:
    timestamp_raw = _writer_header(request, "X-Device-Timestamp")
    nonce = _writer_header(request, "X-Device-Nonce")
    signature = _writer_header(request, "X-Device-Signature")
    try:
        timestamp = int(timestamp_raw)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail="device authorization required") from exc
    return timestamp, nonce, signature


def _resolve_device(state: CloudState, request: Request) -> Any:
    """Resolve an active paired device; unknown and revoked are both ``None``."""
    # A disabled public API keeps this route's uniform 404, which says nothing
    # about the credential.  An unreadable kill state does not: it raises, and
    # the refusal is a 503.  That distinction is deliberate -- a global outage
    # is not credential-dependent so a 503 leaks nothing, while answering 404
    # would tell a healthy device its credential is gone and invite it to
    # re-pair exactly when the backend is sick.
    credential_id = request.headers.get("x-device-credential", "")
    if not credential_id or len(credential_id) > 512:
        return None
    if not state.quotas.kill_state().public_enabled:
        return None
    return state.credentials.resolve_device(credential_id)


def _verify_device_request(
    state: CloudState, request: Request, device: Any, body: bytes
) -> bool:
    """Verify a signed device envelope and consume its replay nonce.

    Identical framing, freshness window, and replay guard to the writer path;
    the algorithm still comes from the stored credential, never the request.
    """
    try:
        timestamp, nonce, signature = _device_signature_fields(request)
        now = state.config.clock()
        if abs(now - timestamp) > _MAX_TIMESTAMP:
            return False
        namespace, _local_scope = _scope(device)
        canonical = canonical_request(
            request.method, request.url.path, namespace, timestamp, nonce,
            digest_body(body), _REFRESH_IDEMPOTENCY_KEY, _REFRESH_REVISION,
        )
        if not verify_signature(
            _credential_key(device), canonical, signature,
            algorithm=getattr(device, "signature_algorithm", ""),
        ):
            return False
        return bool(
            state.nonces.accept(
                namespace, getattr(device, "credential_id", ""), nonce, now=now
            )
        )
    except (HTTPException, TypeError, ValueError, UnicodeError):
        return False


def _resolve_reader(state: CloudState, request: Request) -> Any:
    # Read the kill state first and unconditionally.  Folding it into the
    # `context is None` test below would let a short circuit skip it: an
    # unreadable kill state on an unknown token would answer 404 instead of
    # refusing, and the one flag that must never be bypassed would be bypassed
    # by whichever caller guessed wrong.
    public_enabled = state.quotas.kill_state().public_enabled
    if not _gateway_proof_valid(state, request):
        return None
    raw = request.headers.get("authorization", "")
    scheme, _, token = raw.partition(" ")
    if scheme.lower() != "bearer" or not token or len(token) > 512:
        return None
    ok, subject = _subject_binding(state, request)
    if not ok:
        return None
    context = state.credentials.resolve_reader(token)
    if context is None or not public_enabled:
        return None
    bound_subject = getattr(context, "subject", None)
    # A bound subject is only checkable where one is attested.  Where it is
    # not, the bearer context token -- 32 bytes of server-generated secret --
    # is the whole authorization, and comparing it against a header the caller
    # wrote would add nothing but the appearance of a second factor.
    if (
        bound_subject is not None
        and subject is not None
        and not _safe_compare_text(bound_subject, subject)
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


_CONTEXT_KINDS = {
    "dashboard": {"profile", "training_state", "load_point", "curve"},
    "volume": {"volume_week"},
    "curve": {"curve"},
}


def _safe_since(raw: Optional[str]) -> Optional[int]:
    if raw is None or raw == "":
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="invalid since")
    if value < 0:
        raise HTTPException(status_code=400, detail="invalid since")
    return value


def _cursor_key(secret: bytes, namespace: str, scope: str) -> bytes:
    return hmac.new(
        secret, b"context-cursor\0" + namespace.encode() + b"\0" + scope.encode(),
        hashlib.sha256,
    ).digest()


def _encode_cursor(secret: bytes, namespace: str, scope: str, route: str,
                   since: Optional[int], after: str, revision: int) -> str:
    """Sign one page position plus the checkpoint pinned when paging began.

    ``revision`` travels inside the signed payload rather than as a query
    parameter so a client cannot move its own checkpoint forward: it is a
    server fact about when this pagination started, not caller input.  The
    scope binding deliberately stays in the HMAC *key* (see ``_cursor_key``)
    so a cursor minted for one scope cannot verify under another.
    """
    payload = json.dumps(
        {"route": route, "since": since, "after": after, "revision": revision},
        separators=(",", ":"), sort_keys=True,
    ).encode()
    mac = hmac.new(_cursor_key(secret, namespace, scope), payload, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(payload + mac).decode().rstrip("=")


def _decode_cursor(secret: bytes, namespace: str, scope: str, route: str,
                   since: Optional[int], raw: Optional[str],
                   ) -> Optional[tuple[str, int]]:
    """Verify a cursor and return ``(after, pinned_revision)``.

    The pinned revision is validated exactly as strictly as ``route`` and
    ``since``: a cursor without it, or with a non-``int``/negative one, is
    rejected rather than defaulted.  Silently substituting a fresh scope
    revision for a missing field would reintroduce the checkpoint-advance
    data loss this field exists to prevent, so there is no lenient path.
    No mobile client has shipped, so there is no old cursor to accommodate.
    """
    if raw is None:
        return None
    try:
        encoded = raw.encode("ascii")
        encoded += b"=" * (-len(encoded) % 4)
        value = base64.urlsafe_b64decode(encoded)
        payload, mac = value[:-32], value[-32:]
        expected = hmac.new(
            _cursor_key(secret, namespace, scope), payload, hashlib.sha256,
        ).digest()
        if not hmac.compare_digest(mac, expected):
            raise ValueError
        decoded = json.loads(payload.decode())
        if not isinstance(decoded, dict):
            raise ValueError
        pinned = decoded.get("revision")
        if (decoded.get("route") != route or decoded.get("since") != since
                or not isinstance(decoded.get("after"), str)
                # ``bool`` is an ``int`` subclass in Python; a JSON ``true``
                # must not pass for a revision.
                or isinstance(pinned, bool) or not isinstance(pinned, int)
                or pinned < 0):
            raise ValueError
        return decoded["after"], pinned
    except (AttributeError, ValueError, TypeError, UnicodeError, json.JSONDecodeError,
            base64.binascii.Error):
        raise HTTPException(status_code=400, detail="invalid cursor")


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

    @app.exception_handler(QuotaExceeded)
    async def _refused(_request: Request, exc: QuotaExceeded) -> Response:
        """Turn an admission refusal raised outside a route body into a response.

        The kill state is now read inside the credential-resolution helpers,
        which have no response to return -- they answer ``None``.  Registering
        the refusal here means a helper cannot accidentally turn a kill-switch
        or unreadable-state refusal into a served request by forgetting to
        catch it: the only way past this handler is not to raise.
        """

        return _error(exc.status_code, exc.reason, retry_after=exc.retry_after)

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
                "X-Device-Credential", "X-Device-Timestamp", "X-Device-Nonce",
                "X-Device-Signature",
            ],
        )

    def read_enabled() -> bool:
        return config.plane in {"all", "read"}

    def sync_enabled() -> bool:
        return config.plane in {"all", "sync"}

    if read_enabled():
        @app.post("/api/v1/enrollment/start")
        async def enrollment_start(request: Request) -> Response:
            # Disabled keeps enrollment's refusal uniform and, critically,
            # happens before operator or gateway authentication and invitation
            # creation.
            if not state.quotas.kill_state().public_enabled:
                return _not_found()
            if not _safe_compare_text(
                request.headers.get("x-operator-token", ""), config.operator_token
            ):
                return _error(404, "not found")
            if (
                not _gateway_proof_valid(state, request)
            ):
                return _not_found()
            ok, subject = _subject_binding(state, request)
            if not ok:
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
            # Refuse before authentication, invitation consumption, or any
            # credential/context write when the public API is disabled.
            if not state.quotas.kill_state().public_enabled:
                return _not_found()
            if (
                not _gateway_proof_valid(state, request)
            ):
                return _error(401, "verified reader authorization required")
            ok, subject = _subject_binding(state, request)
            if not ok:
                return _error(401, "verified reader authorization required")
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
                # Optional: pair a rider device in the same operator-gated
                # exchange.  The algorithm here selects how a *new* key is
                # stored; it is never consulted when verifying a signature.
                device_key_hex = payload.get("device_public_key")
                device_algorithm = payload.get(
                    "device_signature_algorithm", "ed25519"
                )
                device_key: bytes | None = None
                if device_key_hex is not None:
                    if (
                        not isinstance(device_key_hex, str)
                        or len(device_key_hex) > _MAX_DEVICE_KEY_HEX
                        or not isinstance(device_algorithm, str)
                        or device_algorithm not in DEVICE_SIGNATURE_ALGORITHMS
                    ):
                        raise ValueError
                    device_key = bytes.fromhex(device_key_hex)
            except (ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError, RecursionError):
                return _error(400, "invalid enrollment request")
            if device_key is not None:
                # The device key is fully validated -- encoding, length, and
                # the on-curve proof -- before the one-time invitation is
                # spent.  Validating it afterwards would leave the rider with
                # a consumed invitation and a writer credential but no device,
                # and no way to retry without a new operator-issued token.
                try:
                    validate_public_key(device_algorithm, device_key)
                except ValueError:
                    return _error(400, "invalid enrollment request")
                except PublicKeyUnavailable:
                    # The deployment is missing the crypto extra.  Refuse
                    # without spending the invitation, and without saying so.
                    return _not_found()
            try:
                binding = state.enrollments.consume(token, subject=subject)
                if (
                    binding is None
                    or (
                        subject is not None
                        and not _safe_compare_text(
                            getattr(binding, "subject", None) or "", subject
                        )
                    )
                ):
                    return _not_found()
                writer = state.credentials.enroll_writer(
                    binding, signing_key=public_key,
                    enrollment_registry=state.enrollments,
                )
                device = None
                if device_key is not None:
                    # Issued with the read capability only.  This is the one
                    # place the initial grant is decided; no route infers
                    # read-only from the credential's type.
                    device = state.credentials.register_device_for_scope(
                        writer.namespace,
                        writer.local_user_scope,
                        device_key,
                        signature_algorithm=device_algorithm,
                        capabilities=DEFAULT_DEVICE_CAPABILITIES,
                        subject=subject,
                    )
            except (ValueError, RuntimeError, PublicKeyUnavailable):
                return _not_found()
            context_token, _context = state.credentials.issue_reader_context_for_scope(
                writer.namespace, writer.local_user_scope, subject
            )
            enrolled: dict[str, Any] = {
                "credential": writer.credential_id,
                "subscription_key": writer.subscription_key.decode("ascii"),
                "signature_algorithm": writer.signature_algorithm,
                # This is an opaque request-signing context, not a storage
                # partition key or installation identifier.  The client may
                # sign it but cannot choose or alter it.
                "signing_namespace": writer.namespace,
                "reader_context": context_token,
            }
            if device is not None:
                enrolled.update({
                    "device_credential": device.credential_id,
                    "device_subscription_key": device.subscription_key.decode("ascii"),
                    "device_signature_algorithm": device.signature_algorithm,
                    "device_capabilities": sorted(device.capabilities),
                })
            return JSONResponse(enrolled, headers={"Cache-Control": "no-store"})

        @app.post("/api/v1/context/refresh")
        async def context_refresh(request: Request) -> Response:
            """Trade a durable device credential for a fresh reader context.

            Every authentication rejection -- unknown device, revoked device,
            bad signature, stale timestamp, replayed nonce, wrong subject,
            missing capability -- returns the identical 404 body and headers
            as an unknown reader context, so nothing about credential state is
            observable from the response.  The one non-404 outcome, a quota
            refusal, is reachable only after the caller has already proven
            possession of the device key.
            """
            if not _gateway_proof_valid(state, request):
                return _not_found()
            ok, subject = _subject_binding(state, request)
            if not ok:
                return _not_found()
            try:
                body = await _bounded_body(request, _MAX_REFRESH_BODY_BYTES)
            except HTTPException:
                return _not_found()
            device = _resolve_device(state, request)
            if device is None:
                return _not_found()
            # Proof of possession first: capability and subject outcomes are
            # only reachable by a caller that already holds the private key.
            if not _verify_device_request(state, request, device, body):
                return _not_found()
            bound_subject = getattr(device, "subject", None)
            if (
                bound_subject is not None
                and subject is not None
                and not _safe_compare_text(bound_subject, subject)
            ):
                return _not_found()
            if not _has_capability(device, "read"):
                return _not_found()
            try:
                namespace, scope = _context_scope(device)
            except ValueError:
                return _not_found()
            try:
                # Each refresh persists a context record, so it is metered
                # like any other read rather than being free.
                state.quotas.admit_read(namespace, scope, response_bytes=0)
            except QuotaExceeded as exc:
                return _error(
                    exc.status_code, "read quota exceeded", retry_after=exc.retry_after
                )
            try:
                token, _context = state.credentials.issue_reader_context_for_scope(
                    namespace, scope, bound_subject,
                    # Bound to this device, so revoking it ends every context
                    # it holds at once rather than in five minutes' time.
                    device_credential_id=getattr(device, "credential_id", None),
                )
            except (ValueError, RuntimeError):
                return _not_found()
            # "Last seen" is how the rider tells the lost phone from the one
            # in their hand, so it is recorded on the one path a device is
            # certain to take.  It goes into a row of its own, never onto the
            # credential: rewriting the credential record here would race a
            # concurrent revocation and could write the pre-revocation record
            # back.  A failure to note it is not a failure to refresh.
            try:
                state.credentials.record_device_seen(
                    getattr(device, "credential_id", "")
                )
            except Exception:  # pragma: no cover - display metadata only
                pass
            # Every refresh persists two rows that nothing will read again
            # once they expire; this is the write the sweep rides along on.
            _sweep_expired_auth_state(state)
            return JSONResponse({
                "reader_context": token,
                "expires_in": READER_CONTEXT_TTL_SECONDS,
                "capabilities": sorted(getattr(device, "capabilities", ())),
            }, headers={"Cache-Control": "no-store"})

        @app.post("/api/v1/devices/pairing-codes")
        async def mint_pairing_code(request: Request) -> Response:
            """Mint a single-use pairing code for the caller's own scope.

            The rider's desktop install is the identity authority here: it
            already holds a writer credential bound to
            ``(namespace, local_user_scope)``, and this route binds a code to
            exactly that pair.  Nothing in the request names a namespace, a
            scope, an installation, or a subject, so a compromised or curious
            caller can only ever mint a code into the account it already
            controls.

            Capability is ``"write"`` rather than a new ``"pair"``
            capability.  Pairing authority *is* installation authority, and
            every writer credential already carries ``"write"``, whereas a new
            capability would need a migration of stored records before any
            existing desktop could pair its phone.  The practical consequence
            is the one that matters: a read-only paired device cannot mint a
            code for another device.

            This route lives on the read plane because minting persists a
            record in ``CloudAuth``, and only the read plane's managed
            identity may write that table.

            Minting stays strict, and it can afford to: a writer-signed
            request is real authentication that does not borrow anything from
            a gateway.  Where a gateway does attest a subject, that subject is
            required here and must match the one bound into the writer at
            enrollment, and the code inherits it.  Where none is attested the
            header is not read and the code binds no subject -- see
            :func:`_subject_binding`.
            """
            writer = _writer_auth(state, request, capability="write")
            try:
                body = await _bounded_body(request, _MAX_PAIRING_BODY_BYTES)
            except HTTPException:
                return _error(400, "invalid pairing request")
            verified = _verify_writer_request(state, request, writer, body)
            if verified is None:
                return _error(401, "writer authorization required")
            namespace, scope, idem, revision = verified
            # A fixed envelope: there is nothing for the caller to choose.
            if not _safe_compare_text(idem, _PAIRING_IDEMPOTENCY_KEY) or (
                revision != _PAIRING_REVISION
            ):
                return _error(401, "writer authorization required")
            # Subject outcomes are only reachable once the caller has proven
            # possession of the writer key.
            ok, verified_subject = _subject_binding(state, request)
            if not ok:
                return _error(401, "writer authorization required")
            # A writer enrolled through an attesting gateway carries the
            # subject verified then; it must still be the one signed in now.
            # A writer registered without one -- the HMAC path, which never
            # saw an identity provider -- has nothing to compare against, so
            # the code takes the subject presented at mint time.
            stored_subject = getattr(writer, "subject", None)
            if (
                stored_subject is not None
                and verified_subject is not None
                and not _safe_compare_text(stored_subject, verified_subject)
            ):
                return _error(401, "writer authorization required")
            # Where nothing attests a subject, the code carries none.  Binding
            # one anyway would put a value on the device credential that every
            # later request compares against an unauthenticated header: no
            # security, and a paired phone that cannot read because it has no
            # identity provider to get that string from.
            subject = (
                None if verified_subject is None else (stored_subject or verified_subject)
            )
            try:
                # Minting persists a record, so it is metered like a read
                # rather than being free.  A refusal here is safe: nothing has
                # been created and the caller can retry.
                state.quotas.admit_read(namespace, scope, response_bytes=0)
            except QuotaExceeded as exc:
                return _error(
                    exc.status_code, "read quota exceeded", retry_after=exc.retry_after
                )
            try:
                minted = state.pairings.create(namespace, scope, subject=subject)
            except (ValueError, RuntimeError):
                return _error(503, "pairing unavailable")
            return JSONResponse({
                "pairing_code": minted.code,
                "expires_at": minted.expires_at,
                "expires_in": state.pairings.ttl_seconds,
            }, headers={"Cache-Control": "no-store"})

        @app.post("/api/v1/devices/pair")
        async def pair_device(request: Request) -> Response:
            """Redeem a pairing code for a durable device credential.

            The device supplies a public key and a code, and nothing else it
            sends is trusted.  The namespace and local scope come from the
            code's binding -- the same treatment ``SyncBatch.from_wire`` gives
            a client-supplied ``installation_id``, which it parses and
            discards.  A body field named ``namespace``, ``local_user_scope``
            or ``installation_id`` is simply not read.

            **The code is the authorization.**  Redeeming deliberately does
            not require a verified subject: 60 bits, single-use, at most 900
            seconds, and mintable only by a writer-signed request from the
            rider's own desktop is sufficient proof to issue a read-only
            credential.  Demanding an identity provider here would mean the
            rider signing in to one on the phone before pairing, which is the
            thing the code exists to avoid -- and a subject header is only
            worth anything behind a gateway that overwrites it, which this
            deployment may not have.  Where one is attested it is applied as
            an additional binding on top of the code, never instead of it.

            Every code failure -- unknown, malformed, expired, already
            consumed, or (where a subject is attested) redeemed by the wrong
            one -- returns the identical 404 body and headers as an unknown
            reader context.  A 400 is reachable only for a request that is
            malformed independently of the code (bad JSON, a missing or
            unusable public key), which reveals nothing secret because the
            wire format is public.
            """
            if not _gateway_proof_valid(state, request):
                return _not_found()
            # Disabled keeps this route's uniform 404; unreadable raises and
            # becomes a 503, before the single-use code is spent.
            if not state.quotas.kill_state().public_enabled:
                return _not_found()
            # Never demanded here.  ``consume`` enforces a subject if and only
            # if the code carries one, which can only have happened in a
            # deployment that attests one -- so the demand comes from the
            # code, never from the route, and omitting the header cannot
            # bypass a binding that exists.
            subject = _attested_subject(state, request)
            try:
                body = await _bounded_body(request, _MAX_PAIRING_BODY_BYTES)
            except HTTPException:
                return _error(400, "invalid pairing request")
            try:
                payload = json.loads(body.decode("utf-8"))
                if not isinstance(payload, dict):
                    raise ValueError
                code = payload.get("code")
                public_key_hex = payload.get("public_key")
                algorithm = payload.get("signature_algorithm", "ed25519")
                # A name the rider will read back in the device listing.  It
                # is validated -- bounded, no control characters -- here,
                # before the single-use code is spent, exactly as the public
                # key is: a label rejected afterwards would burn the code.
                label = payload.get("label")
                if (
                    not isinstance(code, str)
                    or not isinstance(public_key_hex, str)
                    or len(public_key_hex) > _MAX_DEVICE_KEY_HEX
                    or not isinstance(algorithm, str)
                    or algorithm not in DEVICE_SIGNATURE_ALGORITHMS
                    or (label is not None and not isinstance(label, str))
                ):
                    raise ValueError
                if label is not None:
                    validate_device_label(label)
                public_key = bytes.fromhex(public_key_hex)
            except (
                ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError,
                RecursionError,
            ):
                return _error(400, "invalid pairing request")
            # The key is fully validated -- encoding, length, and the on-curve
            # proof -- before the single-use code is spent, for the same
            # reason enrollment does it in that order: a key rejected
            # afterwards would burn the rider's code and leave no device.
            try:
                validate_public_key(algorithm, public_key)
            except ValueError:
                return _error(400, "invalid pairing request")
            except PublicKeyUnavailable:
                # The deployment lacks the crypto extra.  Refuse without
                # spending the code, and without saying why.
                return _not_found()
            binding = state.pairings.consume(code, subject)
            if binding is None:
                return _not_found()
            try:
                device = state.credentials.pair_device(
                    binding,
                    public_key,
                    signature_algorithm=algorithm,
                    label=label,
                    # Devices are issued read-only here, exactly as at
                    # enrollment.  Widening one is a capability grant on the
                    # stored record, never an inference from its type.
                    capabilities=DEFAULT_DEVICE_CAPABILITIES,
                    # The subject, if any, comes from the code's binding and
                    # never from this request.  A device paired where nothing
                    # attests a subject simply carries none, so no later
                    # request is checked against a header nobody vouches for.
                )
                token, _context = state.credentials.issue_reader_context_for_scope(
                    device.namespace, device.local_user_scope, device.subject,
                    device_credential_id=device.credential_id,
                )
            except (ValueError, RuntimeError, PublicKeyUnavailable):
                return _not_found()
            paired = {
                "device_credential": device.credential_id,
                "device_subscription_key": device.subscription_key.decode("ascii"),
                "device_signature_algorithm": device.signature_algorithm,
                "device_capabilities": sorted(device.capabilities),
                # The opaque signing context the device must reproduce in the
                # canonical request when it refreshes.  It is a signing
                # namespace, not something the device chose or may change.
                "signing_namespace": device.namespace,
                "reader_context": token,
                "expires_in": READER_CONTEXT_TTL_SECONDS,
            }
            body_bytes = json.dumps(paired, separators=(",", ":")).encode()
            try:
                # Metered after the fact rather than admitted before: a quota
                # refusal here would strand a rider holding a spent code and a
                # credential they never received.
                state.quotas.record_read_bytes(
                    device.namespace, device.local_user_scope, len(body_bytes)
                )
            except QuotaExceeded:
                pass
            _sweep_expired_auth_state(state)
            return Response(
                body_bytes,
                media_type="application/json",
                headers={"Cache-Control": "no-store"},
            )

        @app.get("/api/v1/devices")
        async def list_devices(request: Request) -> Response:
            """List the devices paired into the caller's own scope.

            The scope is derived from the authenticated credential, never
            from a parameter, so there is nothing here to point at another
            rider: a request cannot ask for a namespace, and the registry
            filters before a response is built.

            Signed with the writer envelope (``X-Writer-*``), which a paired
            device may also use -- ``_writer_auth`` resolves both -- so the
            phone can list the rider's devices without the desktop.  The
            capability asserted is ``"read"``, which every writer and every
            device carries.

            Revoked devices are listed, flagged, and kept: "did the
            revocation stick?" is the question the rider asks next, and a
            listing that silently drops the entry answers it ambiguously.
            """

            credential = _writer_auth(state, request, capability="read")
            verified = _verified_scope(
                state, request, credential, b"",
                idempotency_key=_DEVICE_LIST_IDEMPOTENCY_KEY,
                revision=_DEVICE_LIST_REVISION,
            )
            if verified is None:
                return _error(401, "writer authorization required")
            namespace, scope = verified
            try:
                devices = state.credentials.list_devices_for_scope(namespace, scope)
            except (ValueError, RuntimeError):
                return _error(503, "device listing unavailable")
            caller_id = getattr(credential, "credential_id", "")
            body = json.dumps(
                {"devices": [
                    _device_listing_entry(state, device, caller_id)
                    for device in devices
                ]},
                sort_keys=True, separators=(",", ":"), ensure_ascii=False,
            ).encode("utf-8")
            try:
                state.quotas.admit_read(namespace, scope, response_bytes=len(body))
            except QuotaExceeded as exc:
                return _error(
                    exc.status_code, "read quota exceeded", retry_after=exc.retry_after
                )
            return Response(body, media_type="application/json",
                            headers={"Cache-Control": "no-store"})

        @app.post("/api/v1/devices/{credential_id}/revoke")
        async def revoke_device(request: Request, credential_id: str) -> Response:
            """Revoke one device in the caller's own scope, durably.

            **Who may call.**  Any credential in the same
            ``(namespace, local_user_scope)`` that can sign: the desktop
            writer, another paired device, or the target device revoking
            itself.  That is deliberate -- the rider whose phone is gone has
            the iPad in their hand, and requiring the desktop would mean the
            revocation waits until they get home.  The cost is that a stolen
            phone can revoke its siblings.  It is bounded on purpose: only
            *device* credentials can be revoked here, never the desktop's
            writer credential, so the worst a stolen device achieves is
            forcing a re-pair it could already force by other means, and it
            can never cut the rider off from their own sync plane.

            **Cross-namespace is 404, never 403.**  An id belonging to
            another rider, an unknown id, a malformed id, and an id that
            names some other kind of record all return the identical body,
            status and headers as every other not-found in this API.  A 403
            would confirm that a credential exists in a namespace the caller
            cannot see, and confirming that is the whole attack.

            **Idempotent.**  Revoking an already-revoked device answers
            exactly as revoking a live one does, so a retry after a timeout
            is safe and the status code carries no state either way.
            """

            # Authentication and the kill state first, body second: nothing
            # unauthenticated gets to hand this route bytes to buffer.
            credential = _writer_auth(state, request, capability="read")
            try:
                body = await _bounded_body(request, _MAX_DEVICE_ADMIN_BODY_BYTES)
            except HTTPException:
                return _error(400, "invalid revocation request")
            verified = _verified_scope(
                state, request, credential, body,
                idempotency_key=_DEVICE_REVOKE_IDEMPOTENCY_KEY,
                revision=_DEVICE_REVOKE_REVISION,
            )
            if verified is None:
                return _error(401, "writer authorization required")
            namespace, scope = verified
            if not credential_id or len(credential_id) > _MAX_CREDENTIAL_ID_CHARS:
                return _not_found()
            try:
                outcome = state.credentials.revoke_device_in_scope(
                    credential_id, namespace, scope
                )
            except (ValueError, RuntimeError):
                return _error(503, "revocation unavailable")
            if outcome is None:
                # Unknown, malformed, another rider's namespace, another local
                # scope, or not a device at all -- one answer for all of them.
                return _not_found()
            body_bytes = json.dumps(
                {"revoked": True}, separators=(",", ":")
            ).encode()
            # Metered after the fact, never admitted before -- as pairing is.
            # A rider who has spent the day's read allowance must still be
            # able to revoke a lost device: refusing this call is a security
            # failure, whereas counting it late is a rounding error.
            try:
                state.quotas.record_read_bytes(namespace, scope, len(body_bytes))
            except QuotaExceeded:
                pass
            # The revocation is already durable; housekeeping comes after it.
            _sweep_expired_auth_state(state)
            return Response(
                body_bytes,
                media_type="application/json",
                headers={"Cache-Control": "no-store"},
            )

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
                    "profile": True,
                    "races": True,
                    "dashboard": True,
                    "volume": True,
                    "curve": True,
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

        async def collection(request: Request, kinds: set[str], *, route: str,
                             mobile: bool = False) -> Response:
            context_value = _resolve_reader(state, request)
            if context_value is None:
                return _not_found()
            namespace, scope = _context_scope(context_value)
            limit = _safe_limit(request.query_params.get("limit"))
            since = _safe_since(request.query_params.get("since")) if mobile else None
            cursor_state = (_decode_cursor(
                state.config.server_secret, namespace, scope, route, since,
                request.query_params.get("cursor"),
            ) if mobile else None)
            after = cursor_state[0] if cursor_state is not None else None
            pinned_revision = cursor_state[1] if cursor_state is not None else None
            include_deleted = mobile and since is not None
            try:
                state.quotas.admit_read(namespace, scope, response_bytes=0)
                with state.quotas.backend_slot():
                    if mobile:
                        scope_revision, items = state.store.list_objects_with_revision(
                            namespace, scope, kinds=kinds, limit=limit + 1,
                            include_deleted=include_deleted, after=after,
                            min_revision=since,
                        )
                        # The checkpoint is pinned when pagination starts and
                        # then carried in the signed cursor, so every page of
                        # one walk reports the same revision.  Recomputing it
                        # per page loses data: an object delivered on an early
                        # page can be mutated afterwards, and it sorts *before*
                        # the cursor, so no later page can carry it -- yet a
                        # recomputed checkpoint would advance past its new
                        # revision and the client would never ask for it again.
                        # Pinning instead makes such an edit simply reappear on
                        # the next poll (at-least-once, never at-most-once).
                        current_revision = (
                            scope_revision if pinned_revision is None
                            else pinned_revision
                        )
                    else:
                        current_revision = None
                        items = state.store.list_objects(
                            namespace, scope, kinds=kinds, limit=limit,
                        )
                has_more = len(items) > limit
                items = items[:limit]
                next_cursor = (
                    _encode_cursor(
                        state.config.server_secret, namespace, scope, route,
                        since, items[-1].object_id, current_revision,
                    ) if has_more and items else None
                )
                if mobile:
                    body = json.dumps(
                        {"items": [item.wire(include_deleted=include_deleted) for item in items],
                         "revision": current_revision, "next_cursor": next_cursor},
                        sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                    ).encode("utf-8")
                else:
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
            return await collection(request, {"calendar", "scheduled_workout"}, route="calendar")

        @app.get("/api/v1/context/activities")
        async def activities(request: Request) -> Response:
            return await collection(request, {"activity"}, route="activities")

        @app.get("/api/v1/context/profile")
        async def profile(request: Request) -> Response:
            # One object, served through the same reader-context path,
            # quota accounting and 404-for-everything policy as every other
            # collection.  It is a collection route rather than a singleton
            # one so that nothing here has to decide what an absent profile
            # looks like: a rider who has published no FTP gets an empty
            # ``items`` array, which is a fact, not an error.
            return await collection(request, {"profile"}, route="profile")

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
            return await collection(request, {"race"}, route="races")

        @app.get("/api/v1/context/dashboard")
        async def dashboard(request: Request) -> Response:
            return await collection(request, _CONTEXT_KINDS["dashboard"], route="dashboard", mobile=True)

        @app.get("/api/v1/context/volume")
        async def volume(request: Request) -> Response:
            return await collection(request, _CONTEXT_KINDS["volume"], route="volume", mobile=True)

        @app.get("/api/v1/context/curve")
        async def curve(request: Request) -> Response:
            return await collection(request, _CONTEXT_KINDS["curve"], route="curve", mobile=True)

    if sync_enabled():
        @app.post("/api/v1/sync/batches")
        async def sync_batch(request: Request) -> Response:
            # The sync plane is a write route: capability is asserted, never
            # inferred from which registry the credential came from.
            writer = _writer_auth(state, request, capability="write")
            raw = await _bounded_body(request, config.max_request_bytes)
            verified = _verify_writer_request(state, request, writer, raw)
            if verified is None:
                return _error(401, "writer authorization required")
            namespace, scope, idem, revision = verified
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
            # The sync plane is a write route: capability is asserted, never
            # inferred from which registry the credential came from.
            writer = _writer_auth(state, request, capability="write")
            verified = _verify_writer_request(state, request, writer, b"")
            if verified is None:
                return _error(401, "writer authorization required")
            namespace, scope, _idem, _revision = verified
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
