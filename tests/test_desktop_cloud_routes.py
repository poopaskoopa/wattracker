"""Desktop cloud controls stay local to the request and scheduler seam."""

import base64
import time

import pytest

pytest.importorskip("httpx")
from fastapi.testclient import TestClient

from wattracker import db
import wattracker.server as server


class FakeSync:
    def __init__(self, path):
        self.calls = []
        self.state = {"enabled": False, "enrolled": False, "pending": 0,
                      "last_success": None, "last_error": None,
                      "retry": 0, "devices": [],
                      "endpoint": "https://cloud.example"}
        self.revoke_result = True
        self.enroll_error = None

    def start(self): pass
    def stop(self): pass
    def status(self, uid): return dict(self.state)
    def set_enabled(self, uid, enabled): self.calls.append(("enabled", enabled)); self.state["enabled"] = enabled
    def request_sync(self, uid): self.calls.append(("request", uid))
    def sync_once(self, uid): self.calls.append(("sync", uid))
    def enroll(self, uid, endpoint, invitation):
        self.calls.append(("enroll", endpoint, invitation))
        if self.enroll_error:
            raise self.enroll_error
        self.state["enrolled"] = True
    def mint_pairing_code(self, uid):
        return {"pairing_code": "123456", "expires_at": time.time() + 900}
    def list_devices(self, uid):
        self.calls.append(("list", uid))
        return list(self.state["devices"])
    def revoke_device(self, uid, credential_id):
        self.calls.append(("revoke", credential_id))
        return self.revoke_result


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(server, "DesktopCloudSync", FakeSync)
    app = server.create_app()
    with TestClient(app) as client:
        client.post("/register", data={"username": "rider", "password": "password123"})
        yield client, app.state.cloud_sync


def test_cloud_is_off_by_default_and_has_no_secret_echo(client):
    web, sync = client
    text = web.get("/settings").text
    assert 'name="enabled"' in text
    assert "Queued/offline" not in text
    assert "value=\"\"" in text
    assert "123456" not in text
    assert "Generate pairing code</button>" in text
    assert "Generate pairing code" in text and "disabled" in text


def test_enable_disable_and_enrollment_are_local_controls(client):
    web, sync = client
    web.post("/settings/cloud", data={"enabled": "on", "endpoint": "https://cloud.example", "invitation": "invite"})
    assert ("enroll", "https://cloud.example", "invite") in sync.calls
    assert ("enabled", True) in sync.calls
    sync.calls.clear()
    web.post("/settings/cloud", data={"endpoint": "https://cloud.example"})
    assert ("enabled", False) in sync.calls
    assert not any(call[0] == "enroll" for call in sync.calls)


def test_sync_route_only_queues(client):
    web, sync = client
    response = web.post("/settings/cloud/sync", follow_redirects=False)
    assert response.status_code == 303
    assert any(call[0] == "request" for call in sync.calls)


def test_cloud_state_changes_require_same_origin(client):
    web, sync = client
    response = web.post(
        "/settings/cloud/sync", headers={"Origin": "http://evil.example"},
        follow_redirects=False,
    )
    assert response.status_code == 403
    assert not sync.calls


def test_pairing_payload_and_revoke_use_server_ids(client):
    web, sync = client
    sync.state["enabled"] = True
    text = web.post("/settings/cloud/pairing").text
    assert "123456" in text
    assert "Scan the QR code" in text
    assert '<svg class="cloud-qr-svg"' in text
    assert 'data-expires-at="' in text
    cookie = web.cookies.get("wattracker_session")
    payload = cookie.split(".", 1)[0]
    decoded = base64.b64decode(payload + "=" * (-len(payload) % 4))
    assert b"123456" not in decoded
    pairing = next(iter(web.app.state.cloud_pairings.values()))
    assert pairing["code"] == "123456"

    sync.state["devices"] = [{"id": "device-1", "label": "Phone"}]
    response = web.post("/settings/cloud/devices/device-1/revoke")
    assert ("revoke", "device-1") in sync.calls
    assert any(call[0] == "list" for call in sync.calls)
    assert "Paired device revoked." in response.text

    sync.revoke_result = False
    response = web.post("/settings/cloud/devices/device-1/revoke")
    assert "Paired device could not be revoked. Try again." in response.text


def test_expired_pairing_is_removed_from_server_state(client):
    web, sync = client
    sync.state["enabled"] = True
    web.post("/settings/cloud/pairing")
    uid = next(iter(web.app.state.cloud_pairings))
    web.app.state.cloud_pairings[uid]["expires_at"] = time.time() - 1

    text = web.get("/settings").text

    assert "123456" not in text
    assert uid not in web.app.state.cloud_pairings


def test_missing_cloud_extra_and_retry_status_are_clear(client):
    web, sync = client
    sync.enroll_error = server.CloudDependencyUnavailable()
    response = web.post(
        "/settings/cloud",
        data={"endpoint": "https://cloud.example", "invitation": "invite"},
    )
    assert "Install wattracker[cloud] and try again." in response.text

    sync.enroll_error = None
    sync.state.update({"enabled": True, "retry": 3})
    text = web.get("/settings").text
    assert "Retry scheduled (attempt 3)" in text
