"""Desktop cloud controls stay local to the request and scheduler seam."""

import base64
import json
import time

import pytest

pytest.importorskip("httpx")
from fastapi.testclient import TestClient

from wattracker import db
from wattracker.cloud.client import SyncCredentials
from wattracker.cloud.credentials import CloudCredentialStore
from wattracker.cloud.desktop_sync import DesktopCloudSync
import wattracker.server as server


class FakeSync:
    def __init__(self, path):
        self.calls = []
        self.state = {"enabled": False, "enrolled": False, "pending": 0,
                      "last_success": None, "last_error": None,
                      "retry": 0, "devices": [],
                      "endpoint": "https://cloud.example"}
        self.revoke_result = True
        self.wipe_result = True
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
        self.state["endpoint"] = endpoint
    def mint_pairing_code(self, uid):
        self.calls.append(("pairing", uid))
        return {"pairing_code": "123456", "expires_at": time.time() + 900}
    def list_devices(self, uid):
        self.calls.append(("list", uid))
        return list(self.state["devices"])
    def revoke_device(self, uid, credential_id):
        self.calls.append(("revoke", credential_id))
        return self.revoke_result
    def wipe_cloud_data(self, uid):
        self.calls.append(("wipe", uid))
        return self.wipe_result


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
    assert [call[0] for call in sync.calls] == ["enroll", "enabled"]
    sync.calls.clear()
    web.post("/settings/cloud", data={"endpoint": "https://cloud.example"})
    assert ("enabled", False) in sync.calls
    assert not any(call[0] == "enroll" for call in sync.calls)


@pytest.mark.parametrize(
    "initial_enabled,submitted_enabled,expected_enabled,expected_calls",
    [
        (False, "on", False, []),
        (True, "on", True, []),
        (False, "", False, [("enabled", False)]),
        (True, "", False, [("enabled", False)]),
    ],
)
@pytest.mark.parametrize("endpoint", ["https://new.example", ""])
def test_endpoint_edit_without_invitation_only_applies_disable(
    client, initial_enabled, submitted_enabled, expected_enabled, expected_calls,
    endpoint,
):
    web, sync = client
    sync.state.update(enabled=initial_enabled, enrolled=True)
    before = dict(sync.state)
    response = web.post(
        "/settings/cloud", data={
            "endpoint": endpoint, "invitation": "  ",
            "enabled": submitted_enabled,
        }, follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/settings"
    assert sync.state == {**before, "enabled": expected_enabled}
    assert sync.calls == expected_calls
    message = "Enter an invitation to enroll against a new endpoint"
    page = web.get(response.headers["location"])
    assert message in page.text
    assert 'value="https://cloud.example"' in page.text
    assert message not in web.get("/settings").text


@pytest.mark.parametrize("initial_enabled", [False, True])
@pytest.mark.parametrize("error,message", [
    (server.CloudDependencyUnavailable(), "Install wattracker[cloud] and try again."),
    (server.CloudCredentialUnavailable(), "OS secure credential storage is unavailable."),
    (ValueError("private invitation"), "Enter a valid HTTPS cloud endpoint and invitation."),
    (RuntimeError("private invitation"), "Cloud enrollment could not be completed."),
])
def test_failed_enrollment_does_not_change_enabled_state(client, initial_enabled, error, message):
    web, sync = client
    sync.state.update(enabled=initial_enabled, enrolled=initial_enabled)
    before = dict(sync.state)
    sync.enroll_error = error
    response = web.post(
        "/settings/cloud", data={
            "endpoint": "https://new.example", "invitation": "private invitation",
            "enabled": "on",
        }, follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/settings"
    assert sync.state == before
    assert [call[0] for call in sync.calls] == ["enroll"]
    page = web.get("/settings")
    assert message in page.text
    assert "private invitation" not in page.text


def test_missing_endpoint_does_not_enable_or_enroll(client):
    web, sync = client
    page = web.post("/settings/cloud", data={"enabled": "on", "invitation": "invite"})
    assert "Enter a valid HTTPS cloud endpoint and invitation." in page.text
    assert not sync.calls
    assert not sync.state["enabled"]


@pytest.mark.parametrize("path,data,operation", [
    ("/settings/cloud", {"endpoint": "https://cloud.example", "enabled": "on"}, "enabled"),
    ("/settings/cloud/pairing", {}, "pairing"),
    ("/settings/cloud/devices", {}, "list"),
])
def test_cloud_posts_redirect_and_settings_refresh_does_not_repeat_actions(client, path, data, operation):
    web, sync = client
    assert web.get("/settings").status_code == 200
    assert sync.calls == []
    response = web.post(path, data=data, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/settings"
    assert [call[0] for call in sync.calls] == [operation]
    calls = list(sync.calls)
    for _ in range(2):
        page = web.get(response.headers["location"])
        assert page.status_code == 200
        if operation == "pairing":
            assert "123456" in page.text
    assert sync.calls == calls


@pytest.mark.parametrize("path,method,message", [
    ("/settings/cloud/pairing", "mint_pairing_code", "A pairing code could not be generated right now."),
    ("/settings/cloud/devices", "list_devices", "Paired devices could not be loaded right now."),
])
def test_cloud_action_errors_survive_redirect_once(client, monkeypatch, path, method, message):
    web, sync = client

    def fail(uid):
        raise RuntimeError("private credential")

    monkeypatch.setattr(sync, method, fail)
    response = web.post(path, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/settings"
    page = web.get("/settings")
    assert message in page.text
    assert "private credential" not in page.text
    assert message not in web.get("/settings").text


def test_sync_route_only_queues(client):
    web, sync = client
    response = web.post("/settings/cloud/sync", follow_redirects=False)
    assert response.status_code == 303
    assert any(call[0] == "request" for call in sync.calls)


@pytest.mark.parametrize("path", [
    "/settings/cloud", "/settings/cloud/sync",
    "/settings/cloud/pairing", "/settings/cloud/devices",
    "/settings/cloud/wipe",
])
def test_cloud_state_changes_require_same_origin(client, path):
    web, sync = client
    response = web.post(
        path, headers={"Origin": "http://evil.example"},
        follow_redirects=False,
    )
    assert response.status_code == 403
    assert not sync.calls


def test_pairing_payload_stays_out_of_the_session(client):
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


@pytest.mark.parametrize("revoked,message", [
    (True, "Paired device revoked."),
    (False, "Paired device could not be revoked. Try again."),
])
def test_revoke_redirects_with_one_time_message_and_does_not_repeat(client, revoked, message):
    web, sync = client
    sync.state["devices"] = [{"id": "device-1", "label": "Phone"}]
    sync.revoke_result = revoked

    response = web.post(
        "/settings/cloud/devices/device-1/revoke", follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/settings"
    assert sync.calls[0] == ("revoke", "device-1")
    assert [call[0] for call in sync.calls] == ["revoke", "list"]
    calls = list(sync.calls)

    page = web.get(response.headers["location"])
    assert message in page.text
    assert sync.calls == calls

    refreshed = web.get("/settings")
    assert message not in refreshed.text
    assert sync.calls == calls


def test_cloud_wipe_is_distinct_and_requires_typed_confirmation(client):
    web, sync = client
    sync.state.update(enabled=True, enrolled=True)
    text = web.get("/settings").text
    assert "Delete all cloud data" in text
    assert "This permanently destroys" in text
    assert "DELETE ALL CLOUD DATA" in text

    rejected = web.post(
        "/settings/cloud/wipe",
        data={"confirmation": "delete all cloud data"},
        follow_redirects=False,
    )
    assert rejected.status_code == 303
    assert not [call for call in sync.calls if call[0] == "wipe"]
    assert "Type DELETE ALL CLOUD DATA to confirm" in web.get("/settings").text

    accepted = web.post(
        "/settings/cloud/wipe",
        data={"confirmation": "DELETE ALL CLOUD DATA"},
        follow_redirects=False,
    )
    assert accepted.status_code == 303
    assert [call[0] for call in sync.calls] == ["wipe"]
    assert "Cloud data permanently deleted." in web.get("/settings").text


def test_cloud_wipe_does_not_retry_an_unconfirmed_request(client):
    web, sync = client
    sync.state.update(enabled=True, enrolled=True)
    sync.wipe_result = None

    response = web.post(
        "/settings/cloud/wipe",
        data={"confirmation": "DELETE ALL CLOUD DATA"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "Cloud data wipe could not be confirmed" in web.get("/settings").text
    assert [call[0] for call in sync.calls] == ["wipe"]


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


class _MemorySecrets:
    def __init__(self):
        self.values = {}

    def get(self, account):
        return self.values.get(account)

    def set(self, account, value):
        self.values[account] = value

    def delete(self, account):
        self.values.pop(account, None)


@pytest.fixture()
def cold_disabled_cloud(monkeypatch):
    """The real DesktopCloudSync behind the routes, enrolled but switched off.

    The fake above cannot answer the question this covers - whether a signed
    request actually leaves - so these tests drive the real adapter over a
    captured transport, with a device cache that has never been filled.
    """
    captured = []
    store = CloudCredentialStore(_MemorySecrets())

    def transport(url, headers, body, method):
        captured.append((method, url))
        if url.endswith("/api/v1/devices"):
            return 200, json.dumps({"devices": [
                {"credential_id": "b" * 64, "label": "Phone"},
            ]}).encode()
        if url.endswith("/revoke"):
            return 200, b'{"revoked":true}'
        if url.endswith("pairing-codes"):
            return 200, b'{"pairing_code":"123456","expires_at":1800000000}'
        raise AssertionError(f"unexpected cloud URL while disabled: {url}")

    monkeypatch.setattr(
        server, "DesktopCloudSync",
        lambda path: DesktopCloudSync(path, store, transport=transport),
    )
    app = server.create_app()
    with TestClient(app) as web:
        web.post("/register", data={"username": "rider", "password": "password123"})
        uid = db.get_user_by_username("rider")["id"]
        store.save_writer(
            SyncCredentials("c" * 64, "subscription", b"signing-key", namespace="a" * 64),
            user_id=uid,
        )
        db.save_cloud_sync_state(
            uid, {"endpoint": "https://cloud.example", "enabled": False},
        )
        assert app.state.cloud_sync.status(uid).devices == []
        yield web, app.state.cloud_sync, captured, uid


def test_refresh_reaches_the_revoke_button_with_sync_off(cold_disabled_cloud):
    """After a restart with sync off, the rider can still reach Revoke.

    This is the case the revocation exception exists for - a lost phone, sync
    killed - and before this it was the one case that could not get to the
    button, because the device cache is in memory only and the refresh control
    was disabled alongside everything else.
    """
    web, sync, captured, uid = cold_disabled_cloud

    text = web.get("/settings").text
    refresh_form = text.split('action="/settings/cloud/devices"', 1)[1].split("</form>", 1)[0]
    assert "Refresh paired devices" in refresh_form
    assert "disabled" not in refresh_form
    # Nothing to revoke yet: the cache is cold and the page has not asked.
    assert "/revoke" not in text
    assert captured == []

    text = web.post("/settings/cloud/devices").text
    assert captured == [("GET", "https://cloud.example/api/v1/devices")]
    revoke_form = f'action="/settings/cloud/devices/{"b" * 64}/revoke"'
    assert revoke_form in text
    assert "Phone" in text
    revoke_button = text.split(revoke_form, 1)[1].split("</form>", 1)[0]
    assert "disabled" not in revoke_button
    assert not sync.status(uid).enabled

    captured.clear()
    assert "Paired device revoked." in web.post(
        f"/settings/cloud/devices/{'b' * 64}/revoke"
    ).text
    assert captured[0] == (
        "POST", "https://cloud.example/api/v1/devices/" + "b" * 64 + "/revoke",
    )
    assert not sync.status(uid).enabled


def test_pairing_and_sync_stay_gated_while_disabled(cold_disabled_cloud):
    """The exception is exactly two calls wide; nothing else may leave.

    Asserted on the transport rather than the response, so widening the
    exception later cannot pass by returning the same page.
    """
    web, sync, captured, uid = cold_disabled_cloud

    text = web.get("/settings").text
    pairing_button = text.split('action="/settings/cloud/pairing"', 1)[1].split("</form>", 1)[0]
    assert "disabled" in pairing_button
    sync_button = text.split('action="/settings/cloud/sync"', 1)[1].split("</form>", 1)[0]
    assert "disabled" in sync_button

    assert sync.mint_pairing_code(uid) is None
    assert web.post("/settings/cloud/sync", follow_redirects=False).status_code == 303
    assert sync.sync_once(uid) == []
    assert captured == []
