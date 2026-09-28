import datetime as dt
import sqlite3

import pytest
from fastapi.testclient import TestClient

from wattracker import db
from wattracker import server as desktop_server
from wattracker.cloud.api import CloudConfig, CloudState, create_cloud_app
from wattracker.cloud.client import CloudSyncClient, SyncCredentials
from wattracker.cloud.models import CloudObject, SyncBatch
from wattracker.cloud.security import (
    MemorySecurityStateBackend,
    generate_signing_keypair,
)
from wattracker.cloud.credentials import CloudCredentialStore
from wattracker.cloud.desktop_sync import DesktopCloudSync


READ_ENDPOINT = "https://read.example"
SYNC_ENDPOINT = "https://sync.example"
SECRET = b"cloud-plane-test-server-secret-32-bytes"


def _transport(read_client, sync_client, calls):
    def send(url, headers, body, method="POST"):
        if url.startswith(READ_ENDPOINT):
            target = read_client
            path = url[len(READ_ENDPOINT):]
        elif url.startswith(SYNC_ENDPOINT):
            target = sync_client
            path = url[len(SYNC_ENDPOINT):]
        else:  # pragma: no cover - the assertion below is the useful failure
            raise AssertionError(f"unexpected cloud endpoint: {url}")
        calls.append((method, url))
        response = target.request(method, path, headers=headers, content=body)
        return response.status_code, response.content

    send.supports_method = True
    return send


def _desktop_db(tmp_path, *, count=1):
    path = tmp_path / "cloud-planes.db"
    db.init_db(str(path))
    user_id = db.create_user("rider", "not-a-password", path=str(path))
    base = dt.datetime(2026, 1, 1, 10, 0, 0)
    for number in range(count):
        db.insert_activity(
            user_id,
            {
                "dedup_hash": f"cloud-plane-ride-{number}",
                "filename": f"ride-{number}.fit",
                "start_time": (base + dt.timedelta(minutes=number)).isoformat(),
                "duration_s": 600,
                "distance_m": 10_000.0,
                "avg_power": 180.0,
                "avg_hr": 140.0,
            },
            path=str(path),
        )
    return path, user_id


def test_split_plane_desktop_enrolls_syncs_pairs_and_lists_devices(tmp_path):
    pytest.importorskip("cryptography")
    backend = MemorySecurityStateBackend()
    read_config = CloudConfig(
        server_secret=SECRET,
        operator_token="operator-token",
        plane="read",
        sync_endpoint=SYNC_ENDPOINT,
        require_gateway_proof=False,
        require_verified_subject=False,
        clock=lambda: 1_000,
    )
    sync_config = CloudConfig(
        server_secret=SECRET,
        operator_token="operator-token",
        plane="sync",
        require_gateway_proof=False,
        require_verified_subject=False,
        clock=lambda: 1_000,
    )
    read_state = CloudState.create(read_config, security_backend=backend)
    sync_state = CloudState.create(sync_config, security_backend=backend)
    path, user_id = _desktop_db(tmp_path, count=101)
    calls = []

    with (
        TestClient(create_cloud_app(read_config, state=read_state)) as read_client,
        TestClient(create_cloud_app(sync_config, state=sync_state)) as sync_client,
    ):
        transport = _transport(read_client, sync_client, calls)
        store = CloudCredentialStore(_MemorySecrets())
        desktop = DesktopCloudSync(
            str(path), store, transport=transport, include_derived=False,
            clock=lambda: 1_000,
        )
        started = read_client.post(
            "/api/v1/enrollment/start",
            headers={"X-Operator-Token": "operator-token"},
        )
        assert started.status_code == 200, started.text
        desktop.enroll(
            user_id,
            READ_ENDPOINT,
            started.json()["invitation"],
        )
        enrolled = store.load_writer(user_id=user_id)
        assert enrolled is not None
        assert sync_state.credentials.authenticate_writer(
            enrolled.credential_id, enrolled.subscription_key
        ) is not None
        desktop.set_enabled(user_id, True)

        pushed = desktop.sync_once(user_id)
        assert len(pushed) == 2
        assert all(result.ok for result in pushed)

        pairing = desktop.mint_pairing_code(user_id)
        assert pairing is not None
        _private_key, public_key = generate_signing_keypair()
        paired = read_client.post(
            "/api/v1/devices/pair",
            json={
                "code": pairing["pairing_code"],
                "public_key": public_key.hex(),
                "signature_algorithm": "ed25519",
            },
        )
        assert paired.status_code == 200, paired.text
        devices = desktop.list_devices(user_id)
        assert len(devices) == 1
        assert devices[0]["credential_id"] == paired.json()["device_credential"]

    assert any(url.startswith(SYNC_ENDPOINT) for _method, url in calls)
    assert sum("/api/v1/sync/batches" in url for _method, url in calls) == 2
    assert any(
        url.startswith(READ_ENDPOINT) and "/api/v1/devices" in url
        for _method, url in calls
    )
    assert all(
        not (url.startswith(READ_ENDPOINT) and "/api/v1/sync/" in url)
        for _method, url in calls
    )
    assert all(
        not (url.startswith(SYNC_ENDPOINT) and "/api/v1/sync/" not in url)
        for _method, url in calls
    )


def test_split_plane_settings_route_ignores_crafted_endpoint_fields_in_db(
    monkeypatch,
):
    pytest.importorskip("cryptography")
    backend = MemorySecurityStateBackend()
    common = dict(
        server_secret=SECRET,
        operator_token="operator-token",
        require_gateway_proof=False,
        require_verified_subject=False,
        clock=lambda: 1_000,
    )
    read_config = CloudConfig(plane="read", sync_endpoint=SYNC_ENDPOINT, **common)
    sync_config = CloudConfig(plane="sync", **common)
    read_state = CloudState.create(read_config, security_backend=backend)
    sync_state = CloudState.create(sync_config, security_backend=backend)
    calls = []
    store = CloudCredentialStore(_MemorySecrets())

    def transport(url, headers, body, method="POST"):
        if url.startswith(READ_ENDPOINT):
            target = read_client
            path = url[len(READ_ENDPOINT):]
        elif url.startswith(SYNC_ENDPOINT):
            target = sync_client
            path = url[len(SYNC_ENDPOINT):]
        else:  # pragma: no cover - the assertion below is the useful failure
            raise AssertionError(f"unexpected cloud endpoint: {url}")
        calls.append((method, url))
        response = target.request(method, path, headers=headers, content=body)
        return response.status_code, response.content

    transport.supports_method = True

    def desktop_sync_factory(path):
        return DesktopCloudSync(
            path, store, transport=transport, include_derived=False,
            clock=lambda: 1_000,
        )

    monkeypatch.setattr(desktop_server, "DesktopCloudSync", desktop_sync_factory)
    with (
        TestClient(create_cloud_app(read_config, state=read_state)) as read_client,
        TestClient(create_cloud_app(sync_config, state=sync_state)) as sync_client,
        TestClient(desktop_server.create_app()) as desktop_client,
    ):
        registered = desktop_client.post(
            "/register", data={"username": "rider", "password": "password123"},
            follow_redirects=False,
        )
        assert registered.status_code in (303, 307), registered.text
        started = read_client.post(
            "/api/v1/enrollment/start",
            headers={"X-Operator-Token": "operator-token"},
        )
        assert started.status_code == 200, started.text
        response = desktop_client.post(
            "/settings/cloud",
            data={
                "read_endpoint": READ_ENDPOINT,
                "invitation": started.json()["invitation"],
                "enabled": "",
                "sync_endpoint": "https://crafted-sync.example",
                "endpoint": "https://crafted-read.example",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303, response.text
        user_id = db.get_user_by_username("rider")["id"]
        state = db.get_cloud_sync_state(user_id)

    assert state["read_endpoint"] == READ_ENDPOINT
    assert state["sync_endpoint"] == SYNC_ENDPOINT
    assert state["sync_endpoint"] not in {
        "https://crafted-sync.example", "https://crafted-read.example",
    }
    assert any(url.startswith(READ_ENDPOINT) for _method, url in calls)


def test_v37_cloud_endpoint_migrates_to_read_endpoint(tmp_path, monkeypatch):
    path = tmp_path / "old-cloud-state.db"
    db.init_db(str(path))
    user_id = db.create_user("rider", "not-a-password", path=str(path))
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TABLE cloud_sync_state")
        conn.execute(
            """
            CREATE TABLE cloud_sync_state (
                user_id INTEGER PRIMARY KEY,
                enabled INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0, 1)),
                endpoint TEXT,
                last_success_at REAL,
                retry_count INTEGER NOT NULL DEFAULT 0 CHECK (retry_count >= 0),
                next_retry_at REAL,
                last_error TEXT,
                FOREIGN KEY(user_id) REFERENCES users(id)
            )
            """
        )
        conn.execute(
            "INSERT INTO cloud_sync_state (user_id, endpoint) VALUES (?, ?)",
            (user_id, READ_ENDPOINT),
        )
        conn.execute("PRAGMA user_version = 37")

    monkeypatch.setattr("wattracker.backup.create_backup", lambda *args, **kwargs: None)
    db.init_db(str(path))
    state = db.get_cloud_sync_state(user_id, path=str(path))
    assert state["read_endpoint"] == READ_ENDPOINT
    assert state["sync_endpoint"] is None
    assert "endpoint" not in state
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION


def test_cloud_client_routes_sync_and_read_paths_to_their_own_endpoints():
    calls = []

    def transport(url, _headers, _body, method):
        calls.append((method, url))
        if url.endswith("/api/v1/devices"):
            return 200, b'{"devices": []}'
        return 200, b'{"revision": 1}'

    client = CloudSyncClient(
        READ_ENDPOINT,
        SyncCredentials("c" * 64, "subscription", b"signing-key", "a" * 64),
        sync_endpoint=SYNC_ENDPOINT,
        transport=transport,
    )
    assert client.list_devices() == []
    result = client.push(
        SyncBatch(
            "batch-1", 1,
            (CloudObject("activity-1", "activity", 1, {}),),
        )
    )
    assert result.ok
    assert calls == [
        ("GET", READ_ENDPOINT + "/api/v1/devices"),
        ("POST", SYNC_ENDPOINT + "/api/v1/sync/batches"),
    ]


def test_missing_sync_endpoint_reports_clear_status_without_request(tmp_path):
    path, user_id = _desktop_db(tmp_path)
    store = CloudCredentialStore(_MemorySecrets())
    store.save_writer(
        SyncCredentials("c" * 64, "subscription", b"signing-key", "a" * 64),
        user_id=user_id,
    )
    calls = []
    desktop = DesktopCloudSync(
        str(path), store,
        transport=lambda *args: calls.append(args) or (404, b"{}"),
        include_derived=False,
    )
    db.save_cloud_sync_state(
        user_id,
        {"read_endpoint": READ_ENDPOINT, "sync_endpoint": None, "enabled": True},
        path=str(path),
    )
    results = desktop.sync_once(user_id)
    assert results and not results[0].ok
    assert "sync endpoint is not configured" in results[0].detail.lower()
    assert "sync endpoint is not configured" in desktop.status(user_id).last_error.lower()
    assert calls == []


class _MemorySecrets:
    def __init__(self):
        self.values = {}

    def get(self, account):
        return self.values.get(account)

    def set(self, account, value):
        self.values[account] = value

    def delete(self, account):
        self.values.pop(account, None)
