import json
import sqlite3

import pytest

from wattracker import db
from wattracker.cloud.client import (
    CloudEnrollmentError,
    CloudSyncClient,
    SyncCredentials,
    canonical_request,
    digest_body,
    sign_request,
    validate_cloud_endpoint,
)
from wattracker.cloud.credentials import CloudCredentialStore
from wattracker.cloud.desktop_sync import DesktopCloudSync
from wattracker.cloud.snapshot import snapshot_objects


class MemorySecrets:
    def __init__(self):
        self.values = {}

    def get(self, account):
        return self.values.get(account)

    def set(self, account, value):
        self.values[account] = value

    def delete(self, account):
        self.values.pop(account, None)


def _fixture_db(tmp_path, count=2):
    path = tmp_path / "desktop-cloud.db"
    db.init_db(str(path))
    user_id = db.create_user("rider", "not-a-password", path=str(path))
    db.save_user_settings(user_id, {"ftp": 240, "timezone": "UTC"}, path=str(path))
    for number in range(1, count + 1):
        db.insert_activity(
            user_id,
            {
                "dedup_hash": f"desktop-cloud-{number}",
                "filename": f"ride-{number}.fit",
                "start_time": f"2026-08-{number:02d}T10:00:00",
                "duration_s": 60,
                "distance_m": 1000.0,
                "avg_power": 180.0,
                "avg_hr": 140.0,
            },
            path=str(path),
        )
    return path, user_id


def _credentials():
    return SyncCredentials(
        "c" * 64, "subscription", b"signing-key", namespace="a" * 64,
    )


def test_default_off_enable_disable_and_status_is_dict_compatible(tmp_path):
    path, user_id = _fixture_db(tmp_path, count=0)
    store = CloudCredentialStore(MemorySecrets())
    sync = DesktopCloudSync(str(path), store, transport=lambda *_args: (503, b"{}"))

    status = sync.status(user_id)
    assert not status.enabled
    assert not status.enrolled
    assert status.pending == 0
    assert dict(status)["devices"] == []
    assert sync.set_enabled(user_id, True).enabled
    assert not sync.set_enabled(user_id, False).enabled


def test_enrollment_failure_never_writes_keyring_or_database(tmp_path):
    path, user_id = _fixture_db(tmp_path, count=0)
    backend = MemorySecrets()

    def offline(_url, _headers, _body):
        return 503, b'{"detail":"offline"}'

    sync = DesktopCloudSync(str(path), CloudCredentialStore(backend), transport=offline)
    with pytest.raises(CloudEnrollmentError):
        sync.enroll(user_id, "https://cloud.example", "I" * 32)
    assert backend.values == {}
    raw = path.read_bytes()
    assert b"cloud.example" not in raw
    assert b"subscription" not in raw


def test_endpoint_validation_is_https_only_and_rejects_authority_secrets():
    assert validate_cloud_endpoint("https://cloud.example/") == "https://cloud.example"
    for endpoint in (
        "http://cloud.example",
        "https://user:password@cloud.example",
        "https://cloud.example?token=secret",
        "https://cloud.example/#fragment",
        "https://cloud.example:bad",
    ):
        with pytest.raises(ValueError):
            validate_cloud_endpoint(endpoint)


def test_enrollment_success_stores_private_material_only_in_keyring(tmp_path, monkeypatch):
    path, user_id = _fixture_db(tmp_path, count=0)
    backend = MemorySecrets()
    calls = []
    monkeypatch.setattr(
        "wattracker.cloud.security.generate_signing_keypair",
        lambda: (b"p" * 32, b"u" * 32),
    )

    def enrolled(url, headers, body):
        calls.append((url, headers, json.loads(body)))
        return 200, json.dumps({
            "credential": "d" * 64,
            "subscription_key": "subscription-secret",
            "signature_algorithm": "ed25519",
            "signing_namespace": "e" * 64,
        }).encode()

    sync = DesktopCloudSync(str(path), CloudCredentialStore(backend), transport=enrolled)
    status = sync.enroll(user_id, "https://cloud.example", "I" * 32)
    assert status.endpoint == "https://cloud.example"
    assert status.enrolled
    assert calls[0][0].endswith("/api/v1/enrollment/complete")
    assert calls[0][2]["invitation"] == "I" * 32
    assert len(calls[0][2]["public_key"]) == 64
    raw = path.read_bytes()
    assert b"subscription-secret" not in raw
    assert b"I" * 32 not in raw
    assert b"private_key" not in raw


def test_enrollment_and_signing_are_scoped_to_each_local_user(tmp_path, monkeypatch):
    path, first_user_id = _fixture_db(tmp_path, count=1)
    second_user_id = db.create_user("second-rider", "not-a-password", path=str(path))
    un_enrolled_user_id = db.create_user("third-rider", "not-a-password", path=str(path))
    assert second_user_id is not None
    assert un_enrolled_user_id is not None
    db.insert_activity(
        second_user_id,
        {
            "dedup_hash": "desktop-cloud-second",
            "filename": "second-ride.fit",
            "start_time": "2026-08-03T10:00:00",
            "duration_s": 60,
            "distance_m": 1000.0,
            "avg_power": 180.0,
            "avg_hr": 140.0,
        },
        path=str(path),
    )
    monkeypatch.setattr(
        "wattracker.cloud.security.generate_signing_keypair",
        lambda: (b"p" * 32, b"u" * 32),
    )
    monkeypatch.setattr(
        "wattracker.cloud.client.sign_request_ed25519",
        lambda signing_key, canonical: sign_request(signing_key, canonical),
    )
    enrollment = {
        "A" * 32: ("a" * 64, "subscription-a", "1" * 64),
        "B" * 32: ("b" * 64, "subscription-b", "2" * 64),
    }
    signed_requests = []

    def transport(url, headers, body):
        if url.endswith("/api/v1/enrollment/complete"):
            invitation = json.loads(body)["invitation"]
            credential, subscription, namespace = enrollment[invitation]
            return 200, json.dumps({
                "credential": credential,
                "subscription_key": subscription,
                "signature_algorithm": "ed25519",
                "signing_namespace": namespace,
            }).encode()
        if url.endswith("/api/v1/sync/batches"):
            signed_requests.append((headers, body))
            return 200, b'{"revision":1}'
        raise AssertionError(f"unexpected cloud URL: {url}")

    store = CloudCredentialStore(MemorySecrets())
    sync = DesktopCloudSync(str(path), store, transport=transport)
    assert sync.enroll(first_user_id, "https://cloud.example", "A" * 32).enrolled
    assert sync.enroll(second_user_id, "https://cloud.example", "B" * 32).enrolled
    assert not sync.status(un_enrolled_user_id).enrolled

    db.save_cloud_sync_state(
        first_user_id, {"enabled": True}, path=str(path),
    )
    db.save_cloud_sync_state(
        second_user_id, {"enabled": True}, path=str(path),
    )
    assert sync.sync_once(first_user_id)[0].ok
    assert sync.sync_once(second_user_id)[0].ok

    assert [headers["X-Writer-Credential"] for headers, _body in signed_requests] == [
        "a" * 64, "b" * 64,
    ]
    users_by_credential = {
        "a" * 64: first_user_id,
        "b" * 64: second_user_id,
    }
    for headers, body in signed_requests:
        credential = store.load_writer(
            user_id=users_by_credential[headers["X-Writer-Credential"]],
        )
        canonical = canonical_request(
            "POST", "/api/v1/sync/batches", credential.namespace,
            int(headers["X-Writer-Timestamp"]), headers["X-Writer-Nonce"],
            digest_body(body), headers["X-Writer-Idempotency-Key"],
            headers["X-Writer-Revision"],
        )
        assert sign_request(credential.signing_key, canonical) == headers[
            "X-Writer-Signature"
        ]


def test_offline_queue_retry_and_drain_preserves_snapshot_ledger(tmp_path):
    path, user_id = _fixture_db(tmp_path, count=1)
    backend = MemorySecrets()
    store = CloudCredentialStore(backend)
    store.save_writer(_credentials(), user_id=user_id)
    responses = [(503, b'{"detail":"offline"}'), (200, b'{"revision":1}')]

    def transport(_url, _headers, _body):
        return responses.pop(0)

    sync = DesktopCloudSync(
        str(path), store, transport=transport, retry_base_seconds=1,
    )
    db.save_cloud_sync_state(
        user_id, {"endpoint": "https://cloud.example", "enabled": True}, path=str(path),
    )
    first = sync.sync_once(user_id)
    assert first[0].status_code == 503
    assert sync.status(user_id).pending > 0
    assert sync.status(user_id).retry == 1
    second = sync.sync_once(user_id)
    assert second[0].ok
    assert sync.status(user_id).pending == 0
    assert sync.status(user_id).retry == 0
    assert sync.status(user_id).last_success is not None


def test_disable_between_pages_stops_before_the_next_outbound_request(tmp_path):
    path, user_id = _fixture_db(tmp_path, count=2)
    store = CloudCredentialStore(MemorySecrets())
    store.save_writer(_credentials(), user_id=user_id)
    calls = []
    sync = None

    def transport(url, _headers, _body):
        calls.append(url)
        sync.set_enabled(user_id, False)
        return 200, b'{"revision":1}'

    sync = DesktopCloudSync(
        str(path), store, transport=transport, batch_limit=1,
        include_derived=False,
    )
    db.save_cloud_sync_state(
        user_id, {"endpoint": "https://cloud.example", "enabled": True}, path=str(path),
    )
    sync.sync_once(user_id)
    assert calls == ["https://cloud.example/api/v1/sync/batches"]
    conn = sqlite3.connect(path)
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM cloud_publication_ledger WHERE user_id = ?",
            (user_id,),
        ).fetchone()[0] == 1
    finally:
        conn.close()


def test_derived_first_is_opt_in_and_activity_order_is_newest_first(tmp_path):
    path, user_id = _fixture_db(tmp_path, count=2)
    legacy = snapshot_objects(str(path), user_id, include_derived=True)
    ordered = snapshot_objects(
        str(path), user_id, include_derived=True, derived_first=True,
    )
    assert legacy[0].kind == "activity"
    assert ordered[0].kind not in {"activity", "activity_detail", "stream"}
    activities = [obj for obj in ordered if obj.kind == "activity"]
    assert [obj.object_id for obj in activities] == ["activity-2", "activity-1"]


def test_request_sync_only_enqueues_and_pairing_uses_exact_signed_routes(tmp_path, monkeypatch):
    path, user_id = _fixture_db(tmp_path, count=0)
    credentials = _credentials()
    captured = []

    def transport(url, headers, body, method):
        captured.append((method, url, headers, body))
        if url.endswith("pairing-codes"):
            return 200, b'{"pairing_code":"ABCD-EFGH-JKMN","expires_in":900}'
        if url.endswith("/devices"):
            return 200, json.dumps({"devices": [{
                "credential_id": "b" * 64, "label": "Phone", "subscription_key": "leak",
            }]}).encode()
        return 200, b'{"revoked":true}'

    store = CloudCredentialStore(MemorySecrets())
    store.save_writer(credentials, user_id=user_id)
    sync = DesktopCloudSync(str(path), store, transport=transport)
    db.save_cloud_sync_state(
        user_id, {"endpoint": "https://cloud.example", "enabled": True}, path=str(path),
    )
    before = len(captured)
    assert sync.request_sync(user_id)
    assert len(captured) == before
    assert sync.mint_pairing_code(user_id)["pairing_code"] == "ABCD-EFGH-JKMN"
    assert sync.list_devices(user_id)[0] == {"credential_id": "b" * 64, "label": "Phone"}
    assert sync.revoke_device(user_id, "b" * 64)
    assert [(method, url) for method, url, _headers, _body in captured] == [
        ("POST", "https://cloud.example/api/v1/devices/pairing-codes"),
        ("GET", "https://cloud.example/api/v1/devices"),
        ("POST", "https://cloud.example/api/v1/devices/" + "b" * 64 + "/revoke"),
    ]
    for method, url, headers, body in captured:
        path_part = url.split("cloud.example", 1)[1]
        canonical = canonical_request(
            method, path_part, credentials.namespace, int(headers["X-Writer-Timestamp"]),
            headers["X-Writer-Nonce"], digest_body(body),
            headers["X-Writer-Idempotency-Key"], headers["X-Writer-Revision"],
        )
        assert sign_request(credentials.signing_key, canonical) == headers["X-Writer-Signature"]
