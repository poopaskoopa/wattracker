"""Security and lifecycle tests for the cloud edit queue (#500)."""
import json
from dataclasses import replace

import pytest

from wattracker.cloud.api import _EDIT_ACK_IDEMPOTENCY_KEY
from wattracker.cloud.edits import (
    EDIT_QUEUE_KIND,
    EditQueue,
    EditQueueFull,
    _PAYLOAD_LIMIT,
    _serialized_size,
    edit_queue_key,
)
from wattracker.cloud.limits import (
    DEVICE_SUBJECT, METRIC_EDITS, QUOTA_RECORD_KIND, QuotaExceeded,
    PUBLIC_UNAVAILABLE_DETAIL, counter_key,
)
from wattracker.cloud.models import CloudObject, SyncBatch
from wattracker.cloud.security import (
    ExpiredRecordSweeper,
    NEVER_SWEEP_RECORD_KINDS,
    SecurityStateUnavailable,
    generate_signing_keypair,
)
from wattracker.cloud.wipe import WIPE_RECORD_KINDS, wipe_scope

from test_cloud_device_revocation import (
    deployment, _rider, _device, _envelope, _device_signer,
)


def _activity(deployment, writer, activity_id):
    obj = CloudObject(f"activity-{activity_id}", "activity", activity_id, {})
    deployment.state.store.apply(
        writer.namespace, writer.local_user_scope,
        SyncBatch(f"activity-batch-{activity_id}", activity_id, (obj,)),
    )


def _request(deployment, credential, private_key, method, path, *, nonce, idem,
             body=b"", revision=0, timestamp=1_000):
    headers = _envelope(
        credential, method, path, nonce=nonce, idem=idem, revision=revision,
        body=body, timestamp=timestamp, signer=_device_signer(private_key)
        if private_key else None,
    )
    return deployment.client.request(method, path, content=body, headers=headers)


def _submit(deployment, device, private_key, edit_id="a" * 32, activity_id=1,
            rpe=5, base_rpe=4, **kwargs):
    body = json.dumps({
        "edit_id": edit_id, "activity_id": activity_id,
        "rpe": rpe, "base_rpe": base_rpe,
    }, separators=(",", ":")).encode()
    return _request(
        deployment, device, private_key, "POST", "/api/v1/edits",
        nonce=kwargs.pop("nonce", f"submit-{edit_id}-{activity_id}"),
        idem=kwargs.pop("idem", edit_id), body=body,
        timestamp=kwargs.pop("timestamp", 1_000), **kwargs,
    )


def _writer_request(deployment, writer, method, path, *, nonce, idem, body=b""):
    return _request(
        deployment, writer, None, method, path, nonce=nonce, idem=idem, body=body
    )


def _grant(deployment, writer, device, enabled, nonce):
    path = f"/api/v1/devices/{device.credential_id}/capabilities"
    body = json.dumps({"annotate": enabled}, separators=(",", ":")).encode()
    headers = _envelope(writer, "POST", path, nonce=nonce,
                        idem="device-capabilities", body=body)
    return deployment.client.post(path, content=body, headers=headers)


def _pull(deployment, writer, nonce="pull"):
    return _writer_request(deployment, writer, "POST", "/api/v1/edits/pull",
                           nonce=nonce, idem="edit-pull")


def test_submit_requires_annotate_live_and_withdrawal_is_immediate(deployment):
    writer = _rider(deployment)
    device, private = _device(deployment, writer)
    _activity(deployment, writer, 1)
    body = json.dumps({"edit_id": "a" * 32, "activity_id": 1,
                       "rpe": 5, "base_rpe": 4}, separators=(",", ":")).encode()
    assert _submit(deployment, device, private).status_code == 401
    assert _grant(deployment, writer, device, True, "grant").status_code == 200
    assert _submit(deployment, device, private).status_code == 202
    path = "/api/v1/edits"
    status = _request(deployment, device, private, "GET", path,
                      nonce="status", idem="edit-status")
    assert status.status_code == 200
    assert _grant(deployment, writer, device, False, "withdraw").status_code == 200
    denied = _submit(deployment, device, private, nonce="after-withdraw")
    assert denied.status_code == 401
    denied_status = _request(deployment, device, private, "GET", "/api/v1/edits",
                             nonce="status-after-withdraw", idem="edit-status")
    assert denied_status.status_code == 401
    queue = deployment.backend.read(EDIT_QUEUE_KIND, edit_queue_key(writer.namespace, "scope"))
    assert len(queue["pending"]) == 1
    assert queue["pending"][0]["edit_id"] == "a" * 32


def test_writer_and_read_only_phone_cannot_submit_or_pull(deployment):
    writer = _rider(deployment)
    device, private = _device(deployment, writer)
    _activity(deployment, writer, 1)
    assert _submit(deployment, writer, writer.signing_key).status_code == 401
    assert _submit(deployment, device, private).status_code == 401
    assert _pull(deployment, device).status_code == 401
    path = "/api/v1/edits/ack"
    body = b'{"results":[]}'
    response = _request(deployment, device, private, "POST", path,
                        nonce="phone-ack", idem="edit-ack", body=body)
    assert response.status_code == 401


def test_writer_with_an_explicit_annotate_capability_is_still_refused(deployment, monkeypatch):
    writer = _rider(deployment)
    _activity(deployment, writer, 1)
    writer_with_annotate = replace(writer, capabilities=frozenset({"write", "annotate"}))
    monkeypatch.setattr(
        deployment.state.credentials,
        "authenticate_writer",
        lambda *_args: writer_with_annotate,
    )
    response = _submit(deployment, writer, None)
    assert response.status_code == 401


def test_kill_switch_precedes_anonymous_authentication(deployment):
    deployment.state.quotas.set_public_enabled(False)
    response = deployment.client.post("/api/v1/edits")
    assert response.status_code == 503
    assert response.json() == {"detail": PUBLIC_UNAVAILABLE_DETAIL}
    assert response.headers["retry-after"] == "300"


@pytest.mark.parametrize("failure", ["signature", "stale", "idem", "revision", "replay"])
def test_invalid_envelopes_do_not_change_queue(deployment, failure):
    writer = _rider(deployment)
    device, private = _device(deployment, writer)
    _grant(deployment, writer, device, True, "grant-" + failure)
    _activity(deployment, writer, 1)
    before = deployment.backend.read(EDIT_QUEUE_KIND, edit_queue_key(writer.namespace, "scope"))
    kwargs = {}
    if failure == "signature":
        # Sign normally, then make the signature invalid without changing the
        # body digest or the rest of the envelope.
        response = _submit(deployment, device, private)
        assert response.status_code == 202
        before = deployment.backend.read(EDIT_QUEUE_KIND, edit_queue_key(writer.namespace, "scope"))
        headers = _envelope(device, "POST", "/api/v1/edits", nonce="bad-signature",
                            idem="c" * 32, body=json.dumps({"edit_id": "c" * 32,
                            "activity_id": 1, "rpe": 5, "base_rpe": 4},
                            separators=(",", ":")).encode(), signer=_device_signer(private))
        headers["X-Writer-Signature"] = "0" * 64
        response = deployment.client.post("/api/v1/edits", headers=headers,
                                          content=json.dumps({"edit_id": "c" * 32,
                                          "activity_id": 1, "rpe": 5, "base_rpe": 4},
                                          separators=(",", ":")))
    else:
        if failure == "stale":
            kwargs["timestamp"] = 0
        if failure == "idem":
            kwargs["idem"] = "b" * 32
        if failure == "revision":
            kwargs["revision"] = 1
        response = _submit(deployment, device, private, **kwargs)
        if failure == "replay":
            assert _submit(deployment, device, private, nonce="replay-first").status_code == 202
            before = deployment.backend.read(EDIT_QUEUE_KIND, edit_queue_key(writer.namespace, "scope"))
            response = _submit(deployment, device, private, nonce="replay-first", edit_id="c" * 32)
    assert response.status_code == 401
    assert deployment.backend.read(EDIT_QUEUE_KIND, edit_queue_key(writer.namespace, "scope")) == before


@pytest.mark.parametrize("payload", [
    {"rpe": True}, {"rpe": 0}, {"rpe": 11}, {"rpe": "5"},
    {"base_rpe": 5}, {"extra": 0},
])
def test_submit_strict_fields_and_ranges(deployment, payload):
    writer = _rider(deployment)
    device, private = _device(deployment, writer)
    _grant(deployment, writer, device, True, "grant")
    _activity(deployment, writer, 1)
    data = {"edit_id": "a" * 32, "activity_id": 1, "rpe": 6, "base_rpe": 4}
    data.update(payload)
    if "base_rpe" in payload:
        data["rpe"] = 5
    body = json.dumps(data, separators=(",", ":")).encode()
    response = _request(deployment, device, private, "POST", "/api/v1/edits",
                        nonce="invalid", idem="a" * 32, body=body)
    assert response.status_code == 400


def test_submit_size_activity_idempotency_replacement_and_settlement(deployment):
    writer = _rider(deployment)
    device, private = _device(deployment, writer)
    _grant(deployment, writer, device, True, "grant")
    _activity(deployment, writer, 1)
    _activity(deployment, writer, 2)
    assert _submit(deployment, device, private, "a" * 32, nonce="same-first").status_code == 202
    assert _submit(deployment, device, private, "a" * 32, nonce="same-retry").status_code == 202
    assert _submit(deployment, device, private, "a" * 32, rpe=6, nonce="same-conflict").status_code == 409
    assert _submit(deployment, device, private, "b" * 32).status_code == 202
    pulled = _pull(deployment, writer)
    assert pulled.json()["edits"][0]["edit_id"] == "b" * 32
    assert "submitted_by" not in pulled.text
    ack = json.dumps({"results": [{"edit_id": "b" * 32, "status": "applied"}]},
                     separators=(",", ":")).encode()
    response = _writer_request(deployment, writer, "POST", "/api/v1/edits/ack",
                               nonce="ack", idem="edit-ack", body=ack)
    assert response.json() == {"acked": 1}
    settled = _submit(deployment, device, private, "b" * 32, nonce="settled-retry")
    assert settled.status_code == 200 and settled.json()["status"] == "applied"
    oversized = _request(deployment, device, private, "POST", "/api/v1/edits",
                         nonce="large", idem="d" * 32, body=b" " * 1025)
    assert oversized.status_code == 413


def test_identical_edit_body_from_another_phone_is_idempotent(deployment):
    writer = _rider(deployment)
    first, first_key = _device(deployment, writer)
    second, second_key = _device(deployment, writer)
    _grant(deployment, writer, first, True, "grant-first")
    _grant(deployment, writer, second, True, "grant-second")
    _activity(deployment, writer, 1)
    assert _submit(deployment, first, first_key, "a" * 32, nonce="first-submit").status_code == 202
    assert _submit(deployment, second, second_key, "a" * 32, nonce="second-submit").status_code == 202
    row = deployment.backend.read(EDIT_QUEUE_KIND, edit_queue_key(writer.namespace, "scope"))
    assert len(row["pending"]) == 1
    assert row["pending"][0]["submitted_by"] == first.credential_id


def test_unknown_activity_ack_unknown_and_pull_without_row(deployment):
    writer = _rider(deployment)
    device, private = _device(deployment, writer)
    _grant(deployment, writer, device, True, "grant")
    absent_key = edit_queue_key(writer.namespace, "scope")
    assert _pull(deployment, writer).json() == {"edits": []}
    assert deployment.backend.read(EDIT_QUEUE_KIND, absent_key) is None
    ack = b'{"results":[{"edit_id":"a1111111111111111111111111111111","status":"applied"}]}'
    result = _writer_request(deployment, writer, "POST", "/api/v1/edits/ack",
                             nonce="ack-empty", idem="edit-ack", body=ack)
    assert result.json() == {"acked": 0}
    assert deployment.backend.read(EDIT_QUEUE_KIND, absent_key) is None
    malformed = b'{"results":[{"edit_id":"a1111111111111111111111111111111","status":[]}]}'
    refused = _writer_request(deployment, writer, "POST", "/api/v1/edits/ack",
                              nonce="ack-bad-status", idem="edit-ack", body=malformed)
    assert refused.status_code == 400
    response = _submit(deployment, device, private)
    assert response.status_code == 404
    _activity(deployment, writer, 3)
    deployment.state.store.apply(
        writer.namespace, writer.local_user_scope,
        SyncBatch("deleted-activity", 4,
                  (CloudObject("activity-4", "activity", 4, {}, deleted=True),)),
    )
    assert _submit(deployment, device, private, "d" * 32, 4,
                   nonce="deleted-activity").status_code == 404


def test_status_is_caller_filtered_scope_bound_and_rejects_get_body(deployment):
    writer = _rider(deployment)
    first, first_key = _device(deployment, writer)
    second, second_key = _device(deployment, writer)
    _grant(deployment, writer, first, True, "grant-first")
    _grant(deployment, writer, second, True, "grant-second")
    _activity(deployment, writer, 1)
    _activity(deployment, writer, 2)
    assert _submit(deployment, first, first_key, "a" * 32, 1).status_code == 202
    assert _submit(deployment, second, second_key, "b" * 32, 2).status_code == 202
    response = _request(deployment, first, first_key, "GET", "/api/v1/edits",
                        nonce="own-status", idem="edit-status")
    assert [item["edit_id"] for item in response.json()["edits"]] == ["a" * 32]
    nonempty = _request(deployment, first, first_key, "GET", "/api/v1/edits",
                        nonce="body-status", idem="edit-status", body=b"x")
    assert nonempty.status_code == 400
    other = _rider(deployment, seed=b"b")
    assert _pull(deployment, other, nonce="other-scope").json() == {"edits": []}


def test_status_prunes_only_in_memory_and_does_not_create_missing_row(deployment):
    writer = _rider(deployment)
    device, _private = _device(deployment, writer)
    queue = EditQueue(deployment.backend, clock=deployment.clock)
    key = edit_queue_key(writer.namespace, "scope")
    expired = {
        "namespace": writer.namespace,
        "local_user_scope": "scope",
        "pending": [{
            "edit_id": "a" * 32,
            "activity_id": 1,
            "rpe": 5,
            "base_rpe": 4,
            "submitted_by": device.credential_id,
            "created_at": 1_000.0,
        }],
        "settled": [],
    }
    deployment.backend.write(EDIT_QUEUE_KIND, key, expired)
    deployment.clock.value += 14 * 24 * 60 * 60 + 1

    rows = queue.status(writer.namespace, "scope", device.credential_id)
    assert [(row["edit_id"], row["status"]) for row in rows] == [("a" * 32, "expired")]
    assert deployment.backend.read(EDIT_QUEUE_KIND, key) == expired

    absent = edit_queue_key(writer.namespace, "missing")
    assert queue.status(writer.namespace, "missing", device.credential_id) == []
    assert deployment.backend.read(EDIT_QUEUE_KIND, absent) is None


def _maximal_queue(namespace, scope, submitted_by):
    maximum_time = 1.7976931348623157e308
    return {
        "namespace": namespace,
        "local_user_scope": scope,
        "pending": [{
            "edit_id": f"{index:032x}",
            "activity_id": 2**53 - 1,
            "rpe": 10,
            "base_rpe": 9,
            "submitted_by": submitted_by,
            "created_at": maximum_time,
        } for index in range(50)],
        "settled": [{
            "edit_id": f"{index + 50:032x}",
            "submitted_by": submitted_by,
            "activity_id": 2**53 - 1,
            "rpe": 10,
            "status": "rejected",
            "settled_at": maximum_time,
        } for index in range(100)],
    }


def test_full_maximal_queue_status_pull_and_ack_prune_settled_but_reject_pending_overflow(deployment):
    writer = _rider(deployment)
    device, private = _device(deployment, writer)
    _grant(deployment, writer, device, True, "grant")
    key = edit_queue_key(writer.namespace, "scope")
    full = _maximal_queue(writer.namespace, "scope", device.credential_id)
    assert _serialized_size(full) > _PAYLOAD_LIMIT
    deployment.backend.write(EDIT_QUEUE_KIND, key, full)

    status = _request(deployment, device, private, "GET", "/api/v1/edits",
                      nonce="full-status", idem="edit-status")
    assert status.status_code == 200
    assert deployment.backend.read(EDIT_QUEUE_KIND, key) == full

    pulled = _pull(deployment, writer, nonce="full-pull")
    assert pulled.status_code == 200
    assert len(pulled.json()["edits"]) == 50
    after_pull = deployment.backend.read(EDIT_QUEUE_KIND, key)
    assert len(after_pull["settled"]) < 100
    assert _serialized_size(after_pull) <= _PAYLOAD_LIMIT
    assert after_pull["settled"] == full["settled"][-len(after_pull["settled"]):]

    deployment.backend.write(EDIT_QUEUE_KIND, key, full)
    ack = _writer_request(
        deployment, writer, "POST", "/api/v1/edits/ack", nonce="full-ack",
        idem="edit-ack",
        body=json.dumps({"results": [{"edit_id": "0" * 32, "status": "applied"}]},
                        separators=(",", ":")).encode(),
    )
    assert ack.status_code == 200
    assert ack.json() == {"acked": 1}
    after_ack = deployment.backend.read(EDIT_QUEUE_KIND, key)
    assert _serialized_size(after_ack) <= _PAYLOAD_LIMIT
    assert after_ack["settled"][-1]["edit_id"] == "0" * 32

    # Settled history can be trimmed; pending edits must never be discarded to fit.
    pending_only = dict(full, pending=full["pending"] + [dict(full["pending"][0], edit_id="f" * 32)], settled=[])
    deployment.backend.write(EDIT_QUEUE_KIND, key, pending_only)
    with pytest.raises(EditQueueFull):
        EditQueue(deployment.backend, clock=deployment.clock).ack(
            writer.namespace, "scope", []
        )


def test_full_maximal_queue_keeps_newest_settled_and_prunes_oldest(deployment):
    writer = _rider(deployment)
    device, _private = _device(deployment, writer)
    key = edit_queue_key(writer.namespace, "scope")
    full = _maximal_queue(writer.namespace, "scope", device.credential_id)
    deployment.backend.write(EDIT_QUEUE_KIND, key, full)

    assert EditQueue(deployment.backend, clock=deployment.clock).ack(
        writer.namespace, "scope", []
    ) == 0

    trimmed = deployment.backend.read(EDIT_QUEUE_KIND, key)
    assert len(trimmed["settled"]) < len(full["settled"])
    assert trimmed["settled"] == full["settled"][-len(trimmed["settled"]):]
    assert trimmed["settled"][-1]["edit_id"] == full["settled"][-1]["edit_id"]


def test_submit_binds_attested_subject_when_required(deployment):
    writer = _rider(deployment)
    private, public = generate_signing_keypair()
    device = deployment.state.credentials.register_device_for_scope(
        writer.namespace, "scope", public, subject="entra-user"
    )
    _grant(deployment, writer, device, True, "grant")
    _activity(deployment, writer, 1)
    deployment.config = replace(
        deployment.config,
        require_gateway_proof=True,
        gateway_proof_value="test-gateway-secret",
        require_verified_subject=True,
    )
    deployment.state.config = deployment.config

    body = json.dumps({
        "edit_id": "a" * 32, "activity_id": 1, "rpe": 5, "base_rpe": 4,
    }, separators=(",", ":")).encode()
    headers = _envelope(
        device, "POST", "/api/v1/edits", nonce="subject-mismatch",
        idem="a" * 32, body=body, signer=_device_signer(private),
    )
    headers["X-Gateway-Request-Proof"] = "test-gateway-secret"
    headers["X-Verified-Entra-Subject"] = "different-user"
    mismatch = deployment.client.post("/api/v1/edits", content=body, headers=headers)
    assert mismatch.status_code == 401
    assert deployment.backend.read(
        EDIT_QUEUE_KIND, edit_queue_key(writer.namespace, "scope")
    ) is None

    headers = _envelope(
        device, "POST", "/api/v1/edits", nonce="subject-match",
        idem="b" * 32,
        body=json.dumps({
            "edit_id": "b" * 32, "activity_id": 1, "rpe": 6, "base_rpe": 4,
        }, separators=(",", ":")).encode(),
        signer=_device_signer(private),
    )
    headers["X-Gateway-Request-Proof"] = "test-gateway-secret"
    matched_body = json.dumps({
        "edit_id": "b" * 32, "activity_id": 1, "rpe": 6, "base_rpe": 4,
    }, separators=(",", ":")).encode()
    accepted = deployment.client.post("/api/v1/edits", content=matched_body, headers=headers)
    assert accepted.status_code == 202


def test_submit_requires_attested_subject_header_when_configured(deployment):
    writer = _rider(deployment)
    private, public = generate_signing_keypair()
    device = deployment.state.credentials.register_device_for_scope(
        writer.namespace, "scope", public, subject="entra-user"
    )
    _grant(deployment, writer, device, True, "grant")
    _activity(deployment, writer, 1)
    deployment.config = replace(
        deployment.config,
        require_gateway_proof=True,
        gateway_proof_value="test-gateway-secret",
        require_verified_subject=True,
    )
    deployment.state.config = deployment.config

    body = json.dumps({
        "edit_id": "c" * 32, "activity_id": 1, "rpe": 5, "base_rpe": 4,
    }, separators=(",", ":")).encode()
    headers = _envelope(
        device, "POST", "/api/v1/edits", nonce="subject-missing",
        idem="c" * 32, body=body, signer=_device_signer(private),
    )
    headers["X-Gateway-Request-Proof"] = "test-gateway-secret"
    del headers["X-Verified-Entra-Subject"]

    refused = deployment.client.post("/api/v1/edits", content=body, headers=headers)
    assert refused.status_code == 401
    assert deployment.backend.read(
        EDIT_QUEUE_KIND, edit_queue_key(writer.namespace, "scope")
    ) is None


def test_pull_resolves_devices_before_backend_update_and_filters_scope(deployment, monkeypatch):
    writer = _rider(deployment)
    device, _private = _device(deployment, writer)
    _grant(deployment, writer, device, True, "grant")
    key = edit_queue_key(writer.namespace, "scope")
    row = {
        "namespace": writer.namespace,
        "local_user_scope": "scope",
        "pending": [{
            "edit_id": "a" * 32, "activity_id": 1, "rpe": 5,
            "base_rpe": 4, "submitted_by": device.credential_id,
            "created_at": deployment.clock(),
        }],
        "settled": [],
    }
    deployment.backend.write(EDIT_QUEUE_KIND, key, row)
    original_update = deployment.backend.update
    updating = False

    def tracked_update(kind, record_key, transform):
        def guarded(current):
            nonlocal updating
            updating = True
            try:
                return transform(current)
            finally:
                updating = False
        return original_update(kind, record_key, guarded)

    monkeypatch.setattr(deployment.backend, "update", tracked_update)

    def resolve(credential_id):
        assert not updating
        return deployment.state.credentials.resolve_device(credential_id)

    queue = EditQueue(deployment.backend, clock=deployment.clock, resolve_device=resolve)
    assert [item["edit_id"] for item in queue.pull(writer.namespace, "scope")] == ["a" * 32]

    other_writer = _rider(deployment, seed=b"b")
    foreign_device, _foreign_private = _device(deployment, other_writer)
    _grant(deployment, other_writer, foreign_device, True, "grant-foreign")
    foreign_row = dict(row, pending=[dict(row["pending"][0], submitted_by=foreign_device.credential_id)])
    deployment.backend.write(EDIT_QUEUE_KIND, key, foreign_row)
    assert queue.pull(writer.namespace, "scope") == []
    rejected = deployment.backend.read(EDIT_QUEUE_KIND, key)
    assert rejected["pending"] == []
    assert [(item["edit_id"], item["status"]) for item in rejected["settled"]] == [
        ("a" * 32, "rejected")
    ]


def test_pull_rejects_device_in_same_namespace_with_different_scope(deployment):
    writer = _rider(deployment)
    _private, public = generate_signing_keypair()
    foreign_scope_device = deployment.state.credentials.register_device_for_scope(
        writer.namespace,
        "other-scope",
        public,
        capabilities=frozenset({"annotate"}),
    )
    key = edit_queue_key(writer.namespace, "scope")
    deployment.backend.write(EDIT_QUEUE_KIND, key, {
        "namespace": writer.namespace,
        "local_user_scope": "scope",
        "pending": [{
            "edit_id": "a" * 32,
            "activity_id": 1,
            "rpe": 5,
            "base_rpe": 4,
            "submitted_by": foreign_scope_device.credential_id,
            "created_at": deployment.clock(),
        }],
        "settled": [],
    })

    queue = EditQueue(
        deployment.backend,
        clock=deployment.clock,
        resolve_device=deployment.state.credentials.resolve_device,
    )
    assert queue.pull(writer.namespace, "scope") == []
    rejected = deployment.backend.read(EDIT_QUEUE_KIND, key)
    assert rejected["pending"] == []
    assert [(item["edit_id"], item["status"]) for item in rejected["settled"]] == [
        ("a" * 32, "rejected")
    ]


def test_durable_daily_edit_limit_is_per_device_and_scope(deployment):
    writer = _rider(deployment)
    device, _ = _device(deployment, writer)
    for _ in range(60):
        deployment.state.quotas.admit_edit(writer.namespace, "scope", device.credential_id)
    with pytest.raises(QuotaExceeded):
        deployment.state.quotas.admit_edit(writer.namespace, "scope", device.credential_id)
    # The quota backend is the shared durable MemorySecurityStateBackend.
    row = deployment.backend.read(
        QUOTA_RECORD_KIND,
        counter_key(DEVICE_SUBJECT, writer.namespace, device.credential_id, METRIC_EDITS),
    )
    assert row is not None and row["value"] == 60


def test_submit_enforces_the_durable_daily_limit(deployment):
    writer = _rider(deployment)
    device, private = _device(deployment, writer)
    _grant(deployment, writer, device, True, "grant")
    _activity(deployment, writer, 1)
    for index in range(60):
        response = _submit(
            deployment, device, private, f"{index:032x}", 1,
            nonce=f"daily-{index}",
        )
        assert response.status_code == 202
    limited = _submit(deployment, device, private, f"{60:032x}", 1,
                      nonce="daily-limit")
    assert limited.status_code == 429
    row = deployment.backend.read(
        QUOTA_RECORD_KIND,
        counter_key(DEVICE_SUBJECT, writer.namespace, device.credential_id, METRIC_EDITS),
    )
    assert row is not None and row["value"] == 60


def test_queue_capacity_counts_replacement_and_is_bounded(deployment):
    writer = _rider(deployment)
    device, private = _device(deployment, writer)
    _grant(deployment, writer, device, True, "grant")
    for activity_id in range(1, 52):
        _activity(deployment, writer, activity_id)
    for activity_id in range(1, 51):
        response = _submit(deployment, device, private, f"{activity_id:032x}", activity_id)
        assert response.status_code == 202
    replacement = _submit(deployment, device, private, "f" * 32, 1)
    assert replacement.status_code == 202
    full = _submit(deployment, device, private, "e" * 32, 51)
    assert full.status_code == 429
    assert full.json()["code"] == "edit_queue_full"
    assert len(deployment.backend.read(EDIT_QUEUE_KIND, edit_queue_key(writer.namespace, "scope"))["pending"]) == 50


def test_pull_re_resolves_revocation_and_capability_and_status_is_private(deployment, monkeypatch):
    writer = _rider(deployment)
    first, first_key = _device(deployment, writer)
    second, second_key = _device(deployment, writer)
    _grant(deployment, writer, first, True, "grant-first")
    _grant(deployment, writer, second, True, "grant-second")
    _activity(deployment, writer, 1)
    _activity(deployment, writer, 2)
    assert _submit(deployment, first, first_key, "a" * 32, 1).status_code == 202
    monkeypatch.setattr("wattracker.cloud.api.drop_revoked_submitter", lambda *args, **kwargs: 0)
    first_path = f"/api/v1/devices/{first.credential_id}/revoke"
    revoked = _writer_request(deployment, writer, "POST", first_path,
                              nonce="revoke", idem="device-revoke")
    assert revoked.status_code == 200
    queue_key = edit_queue_key(writer.namespace, "scope")
    queue = deployment.backend.read(EDIT_QUEUE_KIND, queue_key)
    assert [row["edit_id"] for row in queue["pending"]] == ["a" * 32]

    # The queue's live pull filter remains authoritative even if cleanup fails.
    response = _pull(deployment, writer, nonce="pull-revoked")
    assert response.json() == {"edits": []}
    queue = deployment.backend.read(EDIT_QUEUE_KIND, queue_key)
    assert queue["pending"] == []
    assert [(row["edit_id"], row["status"]) for row in queue["settled"]] == [
        ("a" * 32, "rejected")
    ]
    assert _request(deployment, first, first_key, "GET", "/api/v1/edits",
                    nonce="status-revoked", idem="edit-status").status_code == 401

    # Withdrawal is also resolved at pull time, without an intervening refresh.
    assert _submit(deployment, second, second_key, "b" * 32, 2).status_code == 202
    _grant(deployment, writer, second, False, "withdraw-second")
    queue = deployment.backend.read(EDIT_QUEUE_KIND, queue_key)
    assert [row["edit_id"] for row in queue["pending"]] == ["b" * 32]
    response = _pull(deployment, writer, nonce="pull-withdrawn")
    assert response.json() == {"edits": []}
    queue = deployment.backend.read(EDIT_QUEUE_KIND, queue_key)
    assert queue["pending"] == []
    assert [(row["edit_id"], row["status"]) for row in queue["settled"]] == [
        ("a" * 32, "rejected"), ("b" * 32, "rejected")
    ]


def test_edit_expiry_sweep_protection_and_scope_wipe(deployment):
    writer = _rider(deployment)
    device, _ = _device(deployment, writer)
    queue = EditQueue(deployment.backend, clock=deployment.clock)
    queue.submit(writer.namespace, "scope", edit_id="a" * 32, activity_id=1,
                 rpe=5, base_rpe=4, submitted_by=device.credential_id)
    deployment.clock.value += 14 * 24 * 60 * 60 + 1
    assert queue.status(writer.namespace, "scope", device.credential_id)[0]["status"] == "expired"
    assert EDIT_QUEUE_KIND in NEVER_SWEEP_RECORD_KINDS
    with pytest.raises(ValueError, match="never be swept"):
        ExpiredRecordSweeper(deployment.backend, kinds={EDIT_QUEUE_KIND})
    assert EDIT_QUEUE_KIND in WIPE_RECORD_KINDS
    report = wipe_scope(writer.namespace, "scope", security_backend=deployment.backend,
                        irreversible=True)
    assert report.records.get(EDIT_QUEUE_KIND) == 1
    assert deployment.backend.read(EDIT_QUEUE_KIND, edit_queue_key(writer.namespace, "scope")) is None


def test_replacement_prunes_settled_to_the_most_recent_100(deployment):
    writer = _rider(deployment)
    device, _ = _device(deployment, writer)
    queue = EditQueue(deployment.backend, clock=deployment.clock)
    for index in range(102):
        queue.submit(
            writer.namespace, "scope", edit_id=f"{index:032x}", activity_id=1,
            rpe=(index % 10) + 1, base_rpe=None, submitted_by=device.credential_id,
        )
    row = deployment.backend.read(EDIT_QUEUE_KIND, edit_queue_key(writer.namespace, "scope"))
    assert len(row["pending"]) == 1
    assert len(row["settled"]) == 100
    assert row["settled"][-1]["edit_id"] == f"{100:032x}"


def test_queue_compare_and_set_failure_is_503_not_a_lost_edit(deployment, monkeypatch):
    writer = _rider(deployment)
    device, private = _device(deployment, writer)
    _grant(deployment, writer, device, True, "grant")
    _activity(deployment, writer, 1)
    original = deployment.backend.update
    calls = 0

    def conflict_once(kind, key, transform):
        nonlocal calls
        if kind == EDIT_QUEUE_KIND and calls == 0:
            calls += 1
            raise SecurityStateUnavailable("etag conflict")
        return original(kind, key, transform)

    monkeypatch.setattr(deployment.backend, "update", conflict_once)
    response = _submit(deployment, device, private)
    assert response.status_code == 503
    assert deployment.backend.read(EDIT_QUEUE_KIND, edit_queue_key(writer.namespace, "scope")) is None

