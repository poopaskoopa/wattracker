"""Tests: credential storage, Zwift SSO / ZwiftPower auth flows (all mocked),
and the authenticated race-results refresh integration."""
import io
import json
import os
import stat
import urllib.error

import pytest

from tranalyzer import config, credstore, db, races, zwiftauth

pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from tranalyzer.server import create_app  # noqa: E402


@pytest.fixture()
def client():
    app = create_app()
    with TestClient(app) as c:
        yield c


def _register(client, username="rider"):
    client.post("/register", data={"username": username, "password": "password123"})


# ------------------------------------------------------------- credstore
def test_credentials_roundtrip_with_file_key_backend(user_id):
    # conftest sets TRANALYZER_KEYRING=0 -> encrypted file-key backend.
    backend = credstore.save_zwift_credentials(user_id, "a@b.com", "hunter2!")
    assert backend == "encrypted local file key"
    got = credstore.get_zwift_credentials(user_id)
    assert got == ("a@b.com", "hunter2!")
    assert credstore.credentials_saved(user_id) is True

    # The DB never holds the plaintext password.
    _email, enc = db.get_zwift_credentials_row(user_id)
    assert "hunter2" not in (enc or "")
    assert enc.startswith("enc1$")

    # The per-install key file exists with 0600 permissions.
    key_path = os.path.join(config.app_data_dir(), "credentials.key")
    assert os.path.exists(key_path)
    assert stat.S_IMODE(os.stat(key_path).st_mode) == 0o600


def test_clear_credentials(user_id):
    credstore.save_zwift_credentials(user_id, "a@b.com", "pw")
    credstore.clear_zwift_credentials(user_id)
    assert credstore.get_zwift_credentials(user_id) is None
    assert credstore.credentials_saved(user_id) is False


def test_credentials_are_user_scoped(user_id):
    from tranalyzer import auth

    other = db.create_user("other", auth.hash_password("password123"))
    credstore.save_zwift_credentials(user_id, "a@b.com", "pw-a")
    credstore.save_zwift_credentials(other, "c@d.com", "pw-c")
    assert credstore.get_zwift_credentials(user_id).password == "pw-a"
    assert credstore.get_zwift_credentials(other).password == "pw-c"


def test_keyring_backend_used_when_available(user_id, monkeypatch):
    class FakeKeyring:
        store = {}

        def set_password(self, service, key, value):
            self.store[(service, key)] = value

        def get_password(self, service, key):
            return self.store.get((service, key))

        def delete_password(self, service, key):
            self.store.pop((service, key), None)

    fake = FakeKeyring()
    monkeypatch.setattr(credstore, "_keyring", lambda: fake)
    backend = credstore.save_zwift_credentials(user_id, "a@b.com", "s3cret")
    assert backend == "system keychain"
    # DB stores only a sentinel, no ciphertext and no plaintext.
    _email, enc = db.get_zwift_credentials_row(user_id)
    assert enc == "@keyring"
    assert credstore.get_zwift_credentials(user_id).password == "s3cret"
    credstore.clear_zwift_credentials(user_id)
    assert fake.store == {}
    assert credstore.get_zwift_credentials(user_id) is None


def test_keyring_absent_falls_back(monkeypatch, user_id):
    monkeypatch.setenv("TRANALYZER_KEYRING", "1")
    # Simulate the package being missing entirely.
    import builtins

    real_import = builtins.__import__

    def no_keyring(name, *a, **kw):
        if name == "keyring":
            raise ImportError("no module")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", no_keyring)
    assert credstore.storage_backend() == "encrypted local file key"
    assert credstore.save_zwift_credentials(user_id, "a@b.com", "pw") == (
        "encrypted local file key")
    assert credstore.get_zwift_credentials(user_id).password == "pw"


# --------------------------------------------------------- SSO (mocked)
class FakeResponse:
    def __init__(self, body, url="https://example", ctype="application/json"):
        self._body = body.encode() if isinstance(body, str) else body
        self._url = url
        self.headers = {"Content-Type": ctype}

    def read(self):
        return self._body

    def geturl(self):
        return self._url

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_sso_token_success(monkeypatch):
    seen = []

    def fake_urlopen(req, timeout=0):
        seen.append(dict(urllib.parse.parse_qsl(req.data.decode())))
        return FakeResponse(json.dumps(
            {"access_token": "AT", "refresh_token": "RT", "expires_in": 21600}))

    import urllib.parse
    monkeypatch.setattr(zwiftauth.urllib.request, "urlopen", fake_urlopen)
    token = zwiftauth.sso_token("a@b.com", "pw")
    assert token["access_token"] == "AT"
    assert seen[0]["grant_type"] == "password"
    assert seen[0]["client_id"] == "Zwift_Mobile_Link"
    assert seen[0]["username"] == "a@b.com"


def test_sso_token_bad_credentials_single_attempt(monkeypatch):
    calls = []

    def fake_urlopen(req, timeout=0):
        calls.append(1)
        raise urllib.error.HTTPError(
            zwiftauth.TOKEN_URL, 401, "Unauthorized", {},
            io.BytesIO(b'{"error":"invalid_grant"}'))

    monkeypatch.setattr(zwiftauth.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(zwiftauth.ZwiftAuthError) as e:
        zwiftauth.sso_token("a@b.com", "wrong")
    assert e.value.credential_problem is True
    assert len(calls) == 1  # bad credentials are NEVER retried


def test_sso_token_falls_back_to_alternate_client_id(monkeypatch):
    import urllib.parse

    calls = []

    def fake_urlopen(req, timeout=0):
        body = dict(urllib.parse.parse_qsl(req.data.decode()))
        calls.append(body["client_id"])
        if body["client_id"] == "Zwift_Mobile_Link":
            raise urllib.error.HTTPError(
                zwiftauth.TOKEN_URL, 400, "Bad Request", {},
                io.BytesIO(b'{"error":"invalid_client"}'))
        return FakeResponse('{"access_token": "AT2"}')

    monkeypatch.setattr(zwiftauth.urllib.request, "urlopen", fake_urlopen)
    token = zwiftauth.sso_token("a@b.com", "pw")
    assert token["access_token"] == "AT2"
    assert calls == ["Zwift_Mobile_Link", "Zwift Game Client"]


def test_detect_rider_id(monkeypatch):
    monkeypatch.setattr(zwiftauth, "sso_token",
                        lambda e, p: {"access_token": "AT"})
    monkeypatch.setattr(zwiftauth, "fetch_profile_me",
                        lambda t: {"id": 1234567, "firstName": "T"})
    rid, token = zwiftauth.detect_rider_id("a@b.com", "pw")
    assert rid == "1234567"


# ------------------------------------------- ZwiftPower cookie flow (mocked)
class FakeOpener:
    """Scripted opener: login page on secure.zwift.com, then a redirect home."""

    def __init__(self, final_url="https://zwiftpower.com/events.php",
                 json_body=None):
        self.addheaders = []
        self.opened = []
        self.final_url = final_url
        self.json_body = json_body if json_body is not None else {"data": []}

    def open(self, url_or_req, data=None, timeout=0):
        url = url_or_req if isinstance(url_or_req, str) else url_or_req.full_url
        self.opened.append((url, data))
        if "ucp.php" in url:
            return FakeResponse(
                '<html><form id="form" action="https://secure.zwift.com/auth/'
                'realms/zwift/login-actions/authenticate?code=x&amp;tab_id=y">'
                "</form></html>",
                url="https://secure.zwift.com/auth/realms/zwift/login",
                ctype="text/html",
            )
        if "login-actions" in url:
            return FakeResponse("<html>home</html>", url=self.final_url,
                                ctype="text/html")
        return FakeResponse(json.dumps(self.json_body), url=url)


def test_zwiftpower_login_posts_credentials_and_returns_cookies(monkeypatch):
    opener = FakeOpener()
    monkeypatch.setattr(zwiftauth.urllib.request, "build_opener",
                        lambda *a, **kw: opener)
    got = zwiftauth.zwiftpower_login("a@b.com", "pw")
    assert got is opener
    # The form action was unescaped and the credentials posted to it.
    post_url, post_data = opener.opened[1]
    assert "login-actions/authenticate?code=x&tab_id=y" in post_url
    assert b"username=a%40b.com" in post_data and b"password=pw" in post_data


def test_zwiftpower_login_bad_credentials(monkeypatch):
    # Keycloak re-renders its form on secure.zwift.com -> credential failure.
    opener = FakeOpener(final_url="https://secure.zwift.com/auth/realms/zwift/login")
    monkeypatch.setattr(zwiftauth.urllib.request, "build_opener",
                        lambda *a, **kw: opener)
    with pytest.raises(zwiftauth.ZwiftAuthError) as e:
        zwiftauth.zwiftpower_login("a@b.com", "wrong")
    assert e.value.credential_problem is True


def test_fetch_zwiftpower_json_rejects_login_page():
    class HtmlOpener:
        def open(self, url, timeout=0):
            return FakeResponse("<html>login</html>", url=url, ctype="text/html")

    with pytest.raises(zwiftauth.ZwiftAuthError):
        zwiftauth.fetch_zwiftpower_json(HtmlOpener(), "https://zwiftpower.com/x.json")


# ------------------------------------------- refresh integration (mocked)
ZP_DOC = {
    "data": [
        {
            "event_date": 1780000000,
            "event_title": "WTRL TTT",
            "position_in_cat": 4,
            "category": "B",
            "avg_power": [255, 1],
            "np": [270, 1],
        }
    ]
}


def test_refresh_uses_credentials_and_autodetects_rider_id(user_id, monkeypatch):
    credstore.save_zwift_credentials(user_id, "a@b.com", "pw")
    monkeypatch.setattr(
        zwiftauth, "fetch_results_authenticated",
        lambda email, password, rider_id=None: (ZP_DOC, "5555"),
    )
    out = races.refresh_race_results(user_id)
    assert out["source"] == "zwiftpower"
    assert out["count"] == 1 and out["error"] is None
    # Rider id auto-detected from the profile and persisted (still editable).
    assert db.get_user_settings(user_id)["zwift_id"] == "5555"
    rows = db.list_race_results(user_id)
    assert rows[0]["event_title"] == "WTRL TTT"
    assert rows[0]["position"] == "4"


def test_refresh_login_failure_marks_auth_failed_and_backs_off(user_id, monkeypatch):
    credstore.save_zwift_credentials(user_id, "a@b.com", "wrong")
    attempts = []

    def failing(email, password, rider_id=None):
        attempts.append(1)
        raise zwiftauth.ZwiftAuthError(
            "Zwift login failed - check your email and password",
            credential_problem=True)

    monkeypatch.setattr(zwiftauth, "fetch_results_authenticated", failing)
    out = races.refresh_race_results(user_id)
    assert out["source"] == "local"
    assert out["auth_failed"] is True
    assert "login failed" in out["error"]
    assert db.get_race_sync(user_id)["auth_failed"] == 1
    assert len(attempts) == 1

    # Daily sweep respects the backoff: NO new auth attempt.
    out2 = races.refresh_race_results(user_id, respect_backoff=True)
    assert len(attempts) == 1
    assert out2["auth_failed"] is True and "paused" in out2["error"]

    # A manual refresh tries again exactly once.
    races.refresh_race_results(user_id)
    assert len(attempts) == 2

    # Re-saving credentials re-arms the daily sweep.
    credstore.save_zwift_credentials(user_id, "a@b.com", "right")
    db.clear_race_auth_failure(user_id)
    monkeypatch.setattr(
        zwiftauth, "fetch_results_authenticated",
        lambda email, password, rider_id=None: (ZP_DOC, "5555"))
    out3 = races.refresh_race_results(user_id, respect_backoff=True)
    assert out3["source"] == "zwiftpower"


def test_transient_network_error_does_not_back_off(user_id, monkeypatch):
    credstore.save_zwift_credentials(user_id, "a@b.com", "pw")
    monkeypatch.setattr(
        zwiftauth, "fetch_results_authenticated",
        lambda email, password, rider_id=None: (_ for _ in ()).throw(
            zwiftauth.ZwiftAuthError("Zwift SSO unreachable: timeout")))
    out = races.refresh_race_results(user_id)
    assert out["source"] == "local"
    assert out["auth_failed"] is False  # transient, not a credential problem
    assert db.get_race_sync(user_id)["auth_failed"] == 0


# ----------------------------------------------------------------- routes
def test_settings_saves_credentials_and_never_echoes_password(client):
    _register(client)
    r = client.post("/settings", data={
        "zwift_email": "a@b.com", "zwift_password": "sup3r-secret-pw"})
    assert r.status_code == 200
    assert "Zwift credentials saved (encrypted local file key)" in r.text
    assert "sup3r-secret-pw" not in r.text  # never echoed back
    uid = db.get_user_by_username("rider")["id"]
    assert credstore.get_zwift_credentials(uid).password == "sup3r-secret-pw"
    # Saved state shown on later loads, still no password anywhere.
    text = client.get("/settings").text
    assert "Credentials saved" in text
    assert "sup3r-secret-pw" not in text
    assert "Clear Zwift credentials" in text


def test_settings_rejects_half_filled_credentials(client):
    _register(client)
    r = client.post("/settings", data={"zwift_email": "a@b.com"})
    assert "NOT saved" in r.text
    uid = db.get_user_by_username("rider")["id"]
    assert credstore.credentials_saved(uid) is False


def test_settings_clear_credentials_route(client):
    _register(client)
    uid = db.get_user_by_username("rider")["id"]
    credstore.save_zwift_credentials(uid, "a@b.com", "pw")
    r = client.post("/settings/zwift-credentials/clear")
    assert "Zwift credentials cleared" in r.text
    assert credstore.credentials_saved(uid) is False


def test_saving_credentials_rearms_auth(client):
    _register(client)
    uid = db.get_user_by_username("rider")["id"]
    db.save_race_sync(uid, "1", "local", "login failed", auth_failed=True)
    client.post("/settings", data={
        "zwift_email": "a@b.com", "zwift_password": "pw"})
    assert db.get_race_sync(uid)["auth_failed"] == 0


def test_races_page_shows_login_failed_state(client, monkeypatch):
    _register(client)
    uid = db.get_user_by_username("rider")["id"]
    credstore.save_zwift_credentials(uid, "a@b.com", "wrong")
    monkeypatch.setattr(
        zwiftauth, "fetch_results_authenticated",
        lambda email, password, rider_id=None: (_ for _ in ()).throw(
            zwiftauth.ZwiftAuthError("Zwift login failed - check your email "
                                     "and password", credential_problem=True)))
    r = client.post("/races/refresh", data={"rider_id": ""})
    assert "Zwift login failed" in r.text
    assert "Settings" in r.text


def test_races_page_labels_authenticated_source(client, monkeypatch):
    _register(client)
    uid = db.get_user_by_username("rider")["id"]
    credstore.save_zwift_credentials(uid, "a@b.com", "pw")
    monkeypatch.setattr(
        zwiftauth, "fetch_results_authenticated",
        lambda email, password, rider_id=None: (ZP_DOC, "5555"))
    r = client.post("/races/refresh", data={"rider_id": ""})
    assert "using your Zwift login" in r.text
    assert "WTRL TTT" in r.text
    # Rider id was auto-detected and now prefills the field.
    assert 'value="5555"' in r.text
