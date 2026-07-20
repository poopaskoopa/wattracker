"""Tests for the audit security fixes: file modes, upload/FIT caps, directory
confinement, authenticated credential storage, WS origin, trusted hosts, and
disabled docs."""
import base64
import os
import secrets
import stat
import types

import pytest

pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402
from starlette.websockets import WebSocketDisconnect  # noqa: E402

from wattracker import backup, config, credstore, db  # noqa: E402
from wattracker.ingest import fit_parser  # noqa: E402
from wattracker.server import create_app  # noqa: E402


@pytest.fixture()
def client():
    app = create_app()
    with TestClient(app) as c:
        yield c


def _register(client, username="tester", password="password123"):
    return client.post("/register", data={"username": username, "password": password})


# ------------------------------------------------------- H1 file modes
@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes only")
def test_data_dir_db_config_and_backups_are_owner_only():
    d = config.app_data_dir()
    assert stat.S_IMODE(os.stat(d).st_mode) == 0o700

    config.set_anthropic_api_key("secret-key")  # writes config.json
    cfg = config.config_path()
    assert stat.S_IMODE(os.stat(cfg).st_mode) == 0o600

    db.init_db()
    assert stat.S_IMODE(os.stat(config.db_path()).st_mode) == 0o600

    bpath = backup.create_backup("manual")
    assert stat.S_IMODE(os.stat(bpath).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(backup.backups_dir()).st_mode) == 0o700


# --------------------------------------------------- M2a upload size cap
def test_upload_rejects_oversized_file(client, monkeypatch):
    _register(client)
    import wattracker.server as srv

    monkeypatch.setattr(srv, "MAX_UPLOAD_BYTES", 16)
    r = client.post(
        "/activities/upload",
        files={"file": ("big.fit", b"x" * 1024, "application/octet-stream")},
    )
    assert r.status_code == 413


# --------------------------------------------------- M2b FIT record cap
def test_parse_fit_caps_record_count(monkeypatch):
    class FakeMsg:
        name = "record"

        def has_field(self, f):
            return False

        def get_value(self, n):
            return None

    class FakeReader:
        def __init__(self, path):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def __iter__(self):
            for _ in range(50):
                yield FakeMsg()

    fake = types.SimpleNamespace(FitReader=FakeReader, FitDataMessage=FakeMsg)
    monkeypatch.setattr(fit_parser, "fitdecode", fake)
    monkeypatch.setattr(fit_parser, "MAX_FIT_RECORDS", 10)
    with pytest.raises(ValueError):
        fit_parser.parse_fit("dummy.fit")


# --------------------------------------------------- M3 directory confinement
@pytest.mark.skipif(os.name == "nt", reason="/etc is POSIX-specific")
def test_settings_rejects_dir_outside_home(client):
    _register(client)
    r = client.post("/settings", data={"activities_dir": "/etc"})
    assert r.status_code == 200
    assert "inside your home directory" in r.text
    uid = db.get_user_by_username("tester")["id"]
    assert db.get_user_settings(uid)["activities_dir"] is None


def test_settings_rejects_nonexistent_dir(client):
    _register(client)
    r = client.post(
        "/settings", data={"activities_dir": "/nonexistent/path/xyz123"}
    )
    assert "not found or not a directory" in r.text
    uid = db.get_user_by_username("tester")["id"]
    assert db.get_user_settings(uid)["activities_dir"] is None


def test_settings_accepts_dir_under_home(client, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    _register(client)
    good = tmp_path / "acts"
    good.mkdir()
    r = client.post("/settings", data={"activities_dir": str(good)})
    assert r.status_code == 200
    uid = db.get_user_by_username("tester")["id"]
    assert db.get_user_settings(uid)["activities_dir"] == str(good)


def test_settings_accepts_configured_redirect_outside_home(
    client, tmp_path, monkeypatch
):
    home = tmp_path / "home"
    redirected = tmp_path / "redirected-documents" / "Zwift" / "Activities"
    home.mkdir()
    redirected.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("WATTRACKER_ACTIVITIES_DIR", str(redirected))
    _register(client)
    response = client.post(
        "/settings", data={"activities_dir": str(redirected)}
    )
    assert response.status_code == 200
    uid = db.get_user_by_username("tester")["id"]
    assert db.get_user_settings(uid)["activities_dir"] == str(redirected)


@pytest.mark.skipif(os.name != "posix", reason="symlink semantics tested on POSIX")
def test_settings_rejects_symlink_escape_from_trusted_root(
    client, tmp_path, monkeypatch
):
    home = tmp_path / "home"
    outside = tmp_path / "outside"
    home.mkdir()
    outside.mkdir()
    link = home / "escaped"
    link.symlink_to(outside, target_is_directory=True)
    monkeypatch.setenv("HOME", str(home))
    _register(client)
    response = client.post("/settings", data={"activities_dir": str(link)})
    assert "inside your home directory" in response.text
    uid = db.get_user_by_username("tester")["id"]
    assert db.get_user_settings(uid)["activities_dir"] is None


# --------------------------------------------- L2 authenticated credstore
def test_credstore_new_format_roundtrip_and_tamper_detection(user_id):
    token = credstore._encrypt("s3cret-password")
    assert token.startswith("enc2$")
    assert credstore._decrypt(token) == "s3cret-password"

    # Flipping any ciphertext byte must fail the HMAC and refuse to decrypt.
    raw = bytearray(base64.b64decode(token[len("enc2$"):]))
    raw[18] ^= 0x01  # inside the ciphertext region (after the 16-byte nonce)
    tampered = "enc2$" + base64.b64encode(bytes(raw)).decode("ascii")
    assert credstore._decrypt(tampered) is None

    # Tampering the appended tag also fails.
    raw2 = bytearray(base64.b64decode(token[len("enc2$"):]))
    raw2[-1] ^= 0x01
    tampered2 = "enc2$" + base64.b64encode(bytes(raw2)).decode("ascii")
    assert credstore._decrypt(tampered2) is None


def test_credstore_legacy_blob_still_decrypts(user_id):
    # An old unauthenticated enc1$ blob written with the raw key keystream.
    key = credstore._install_key()
    nonce = secrets.token_bytes(16)
    data = b"legacy-pw"
    cipher = bytes(
        a ^ b for a, b in zip(data, credstore._keystream(key, nonce, len(data)))
    )
    token = "enc1$" + base64.b64encode(nonce + cipher).decode("ascii")
    assert credstore._decrypt(token) == "legacy-pw"


# ------------------------------------------------------ L3 WS origin allowlist
def test_ws_rejects_cross_origin(client):
    _register(client)
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(
            "/ride/ws?sim=1", headers={"origin": "http://evil.example.com"}
        ) as ws:
            ws.receive_json()


def test_ws_allows_local_origin(client):
    _register(client)
    with client.websocket_connect(
        "/ride/ws?sim=1&type=endurance&minutes=30",
        headers={"origin": "http://localhost:8000"},
    ) as ws:
        msg = ws.receive_json()
        assert "status" in msg


# ------------------------------------------------------ L4 trusted hosts
def test_untrusted_host_rejected(client):
    r = client.get("/login", headers={"host": "evil.example.com"})
    assert r.status_code == 400


def test_trusted_host_accepted(client):
    assert client.get("/login").status_code == 200  # default host "testserver"


@pytest.mark.parametrize("host", ["[::1]", "[::1]:8000", "::1"])
def test_ipv6_loopback_trusted_host_accepted(client, host):
    assert client.get("/login", headers={"host": host}).status_code == 200


@pytest.mark.parametrize("host", ["[::2]", "[2001:db8::1]:8000", "localhost:bad"])
def test_other_ipv6_and_malformed_hosts_rejected(client, host):
    assert client.get("/login", headers={"host": host}).status_code == 400


# ------------------------------------------------------ L5 docs disabled
def test_interactive_docs_disabled(client):
    _register(client)  # authenticate so we see 404, not the auth redirect
    assert client.get("/docs").status_code == 404
    assert client.get("/redoc").status_code == 404
    assert client.get("/openapi.json").status_code == 404


# ------------------------------------------------------ L1 logout is POST-only
def test_logout_get_not_allowed(client):
    _register(client)
    # GET /logout no longer exists; it must not clear the session.
    r = client.get("/logout", follow_redirects=False)
    assert r.status_code in (404, 405)
    assert client.get("/", follow_redirects=False).status_code == 200  # still authed
