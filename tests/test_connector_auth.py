"""Per-device connector tokens: minting, resolving, revoking, and the UI.

The security contract mirrors the calendar feed's (see test_calendar_feed.py):
only a hash is ever stored, the plaintext is shown exactly once, and every
failure mode collapses to a single "no".
"""
import hashlib

import pytest

pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from wattracker import connectorauth, db  # noqa: E402
from wattracker.server import create_app  # noqa: E402


@pytest.fixture()
def client():
    app = create_app()
    with TestClient(app) as c:
        yield c


def _register(client, username="rider"):
    client.post("/register", data={"username": username, "password": "password123"})
    return db.get_user_by_username(username)["id"]


# ------------------------------------------------------------------ model
def test_token_resolves_to_its_owner(user_id):
    device_id, token = connectorauth.generate_token(user_id, "Zwift PC")
    resolved = connectorauth.device_for_token(token)
    assert resolved["user_id"] == user_id
    assert resolved["device_id"] == device_id
    assert resolved["label"] == "Zwift PC"


def test_plaintext_token_is_never_stored(user_id):
    """Asserted against the COLUMN, not the listing.

    The listing was the only thing checked here, and it does not select
    ``token_hash`` at all - so making ``hash_token`` return its argument, which
    puts the plaintext straight into the column, left the whole suite green.
    The digest is also recomputed here rather than through ``hash_token``, so
    the test does not agree with the code it is checking by construction.
    """
    _device_id, token = connectorauth.generate_token(user_id, "Zwift PC")
    stored = db.list_connector_devices(user_id)
    assert len(stored) == 1
    # Neither the token nor anything containing it survives in the row...
    assert token not in repr(stored[0])
    assert "token_hash" not in stored[0]

    # ...and the column the listing does not select holds a sha256 digest.
    conn = db.connect(None)
    try:
        rows = conn.execute(
            "SELECT token_hash FROM connector_devices"
        ).fetchall()
    finally:
        conn.close()
    assert len(rows) == 1
    at_rest = rows[0]["token_hash"]
    assert at_rest != token
    assert token not in at_rest
    assert at_rest == hashlib.sha256(token.encode("utf-8")).hexdigest()


def test_unknown_malformed_and_missing_tokens_all_return_none(user_id):
    connectorauth.generate_token(user_id, "Zwift PC")
    for bad in (
        None, "", 42, [], "short",
        "not-base64url!!!!!!!!!!!!!!!!!!!!",
        "a" * 300,                       # over the length bound
        "A" * 43,                        # well-formed but never issued
    ):
        assert connectorauth.device_for_token(bad) is None


def test_each_device_gets_a_distinct_token(user_id):
    _a_id, a = connectorauth.generate_token(user_id, "Desktop")
    _b_id, b = connectorauth.generate_token(user_id, "Laptop")
    assert a != b
    # Pairing a second machine must not disturb the first.
    assert connectorauth.device_for_token(a)["label"] == "Desktop"
    assert connectorauth.device_for_token(b)["label"] == "Laptop"


def test_revoking_one_device_leaves_the_others(user_id):
    a_id, a = connectorauth.generate_token(user_id, "Desktop")
    _b_id, b = connectorauth.generate_token(user_id, "Laptop")
    assert connectorauth.revoke(user_id, a_id) is True
    assert connectorauth.device_for_token(a) is None
    assert connectorauth.device_for_token(b) is not None


def test_cannot_revoke_another_users_device(user_id):
    other = db.create_user("other", "hash")
    device_id, token = connectorauth.generate_token(other, "Their PC")
    assert connectorauth.revoke(user_id, device_id) is False
    assert connectorauth.device_for_token(token) is not None


def test_last_seen_is_stamped_on_successful_resolve(user_id):
    connectorauth.generate_token(user_id, "Zwift PC")
    assert db.list_connector_devices(user_id)[0]["last_seen"] is None
    connectorauth.device_for_token(
        connectorauth.generate_token(user_id, "Second")[1]
    )
    seen = {d["label"]: d["last_seen"] for d in db.list_connector_devices(user_id)}
    assert seen["Second"] is not None
    assert seen["Zwift PC"] is None  # only the resolved one is stamped


def test_labels_are_bounded_and_stripped(user_id):
    assert connectorauth.clean_label("  Zwift PC  ") == "Zwift PC"
    assert connectorauth.clean_label("") == "Connector"
    assert connectorauth.clean_label(None) == "Connector"
    assert connectorauth.clean_label("a\x00b\nc") == "abc"  # control chars dropped
    assert len(connectorauth.clean_label("x" * 500)) == connectorauth.MAX_LABEL_LEN


# --------------------------------------------------------------------- UI
def test_pair_shows_token_once_and_never_caches_it(client):
    _register(client)
    response = client.post("/settings/connector", data={"label": "Zwift PC"})
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "private, no-store"

    uid = db.get_user_by_username("rider")["id"]
    # The page carries the plaintext exactly once, and reloading never shows it.
    assert "Zwift PC" in response.text
    again = client.get("/settings")
    for device in db.list_connector_devices(uid):
        assert device["label"] in again.text
    assert "not shown again" not in again.text


def test_revoke_via_the_settings_page(client):
    uid = _register(client)
    client.post("/settings/connector", data={"label": "Zwift PC"})
    device_id = db.list_connector_devices(uid)[0]["id"]

    response = client.post(f"/settings/connector/{device_id}/revoke")
    assert response.status_code == 200
    assert db.list_connector_devices(uid) == []


def test_pairing_requires_a_session(client):
    # No login: AuthMiddleware redirects rather than pairing anything.
    response = client.post(
        "/settings/connector", data={"label": "Zwift PC"}, follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_pairing_rejects_a_cross_origin_post(client):
    _register(client)
    response = client.post(
        "/settings/connector",
        data={"label": "Zwift PC"},
        headers={"Origin": "http://evil.example"},
    )
    assert response.status_code == 403
    assert db.list_connector_devices(
        db.get_user_by_username("rider")["id"]
    ) == []
