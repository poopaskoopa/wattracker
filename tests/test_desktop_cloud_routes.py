"""Desktop cloud controls stay local to the request and scheduler seam."""

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
                      "retry": None, "devices": []}

    def start(self): pass
    def stop(self): pass
    def status(self, uid): return dict(self.state)
    def set_enabled(self, uid, enabled): self.calls.append(("enabled", enabled)); self.state["enabled"] = enabled
    def request_sync(self, uid): self.calls.append(("request", uid))
    def sync_once(self, uid): self.calls.append(("sync", uid))
    def enroll(self, uid, endpoint, invitation): self.calls.append(("enroll", endpoint, invitation)); self.state["enrolled"] = True
    def mint_pairing_code(self, uid): return {"pairing_code": "123456", "expires_in": 900}
    def revoke_device(self, uid, credential_id): self.calls.append(("revoke", credential_id))


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


def test_enable_disable_and_enrollment_are_local_controls(client):
    web, sync = client
    web.post("/settings/cloud", data={"enabled": "on", "endpoint": "https://cloud.example", "invitation": "invite"})
    assert ("enroll", "https://cloud.example", "invite") in sync.calls
    assert ("enabled", True) in sync.calls
    web.post("/settings/cloud", data={})
    assert ("enabled", False) in sync.calls


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
    text = web.post("/settings/cloud/pairing").text
    assert "123456" in text
    assert "Scan the QR code" in text
    assert '<svg class="cloud-qr-svg"' in text
    sync.state["devices"] = [{"id": "device-1", "label": "Phone"}]
    web.post("/settings/cloud/devices/device-1/revoke")
    assert ("revoke", "device-1") in sync.calls
