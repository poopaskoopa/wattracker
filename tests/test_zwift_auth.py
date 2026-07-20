"""Tests: credential storage, Zwift SSO / ZwiftPower auth flows (all mocked),
and the authenticated race-results refresh integration."""
import base64
import io
import json
import os
import secrets
import stat
import urllib.error

import pytest

from wattracker import config, credstore, db, races, zwiftauth

pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from wattracker.server import create_app  # noqa: E402


@pytest.fixture()
def client():
    app = create_app()
    with TestClient(app) as c:
        yield c


def _register(client, username="rider"):
    client.post("/register", data={"username": username, "password": "password123"})


def _legacy_enc1(password: str) -> str:
    """Build the unauthenticated format written before enc2 was introduced."""
    key = credstore._install_key()
    nonce = secrets.token_bytes(16)
    data = password.encode("utf-8")
    cipher = bytes(
        a ^ b for a, b in
        zip(data, credstore._keystream(key, nonce, len(data)))
    )
    return "enc1$" + base64.b64encode(nonce + cipher).decode("ascii")


# ------------------------------------------------------------- credstore
def test_credentials_roundtrip_with_file_key_backend(user_id):
    # conftest sets WATTRACKER_KEYRING=0 -> encrypted file-key backend.
    backend = credstore.save_zwift_credentials(user_id, "a@b.com", "hunter2!")
    assert backend == "encrypted local file key"
    got = credstore.get_zwift_credentials(user_id)
    assert got == ("a@b.com", "hunter2!")
    assert credstore.credentials_saved(user_id) is True

    # The DB never holds the plaintext password.
    _email, enc = db.get_zwift_credentials_row(user_id)
    assert "hunter2" not in (enc or "")
    assert enc.startswith("enc2$")  # authenticated format

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
    from wattracker import auth

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
    assert enc.startswith("@keyring:v1:")
    assert credstore.get_zwift_credentials(user_id).password == "s3cret"
    credstore.clear_zwift_credentials(user_id)
    assert fake.store == {}
    assert credstore.get_zwift_credentials(user_id) is None


def test_keyring_absent_falls_back(monkeypatch, user_id):
    monkeypatch.setenv("WATTRACKER_KEYRING", "1")
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


def test_windows_keyring_backend_allowlist(monkeypatch):
    monkeypatch.setattr(credstore, "_is_windows", lambda: True)
    WinVault = type("WinVaultKeyring", (), {})
    WinVault.__module__ = "keyring.backends.Windows"
    Plaintext = type("PlaintextKeyring", (), {})
    Plaintext.__module__ = "keyrings.alt.file"
    Mac = type("Keyring", (), {})
    Mac.__module__ = "keyring.backends.macOS"
    Fail = type("FailKeyring", (), {})
    Fail.__module__ = "keyring.backends.fail"

    assert credstore._keyring_backend_allowed(WinVault()) is True
    assert credstore._keyring_backend_allowed(Plaintext()) is False
    assert credstore._keyring_backend_allowed(Mac()) is False
    assert credstore._keyring_backend_allowed(Fail()) is False


def test_windows_falls_back_to_dpapi_never_file_key(user_id, monkeypatch):
    monkeypatch.setattr(credstore, "_is_windows", lambda: True)
    monkeypatch.setattr(credstore, "_keyring", lambda: None)
    seen = []

    def protect(password, service, uid):
        seen.append((password, service, uid))
        return "dpapi1$cHJvdGVjdGVk"

    monkeypatch.setattr(credstore.windows_secrets, "protect_password", protect)
    monkeypatch.setattr(
        credstore.windows_secrets, "unprotect_password",
        lambda marker, service, uid: "win-secret",
    )
    monkeypatch.setattr(
        credstore, "_encrypt",
        lambda password: pytest.fail("Windows must never create enc1"),
    )

    assert credstore.save_zwift_credentials(
        user_id, "a@b.com", "win-secret"
    ) == "Windows DPAPI"
    assert seen == [("win-secret", "wattracker-Zwift", user_id)]
    assert db.get_zwift_credentials_row(user_id)[1].startswith("dpapi1$")
    assert credstore.get_zwift_credentials(user_id).password == "win-secret"


def test_windows_backend_failure_leaves_existing_row(user_id, monkeypatch):
    db.set_zwift_credentials_row(user_id, "old@example.com", "enc1$old")
    monkeypatch.setattr(credstore, "_is_windows", lambda: True)

    class BrokenKeyring:
        def set_password(self, *args):
            raise RuntimeError("vault unavailable")

        def delete_password(self, *args):
            pass

    monkeypatch.setattr(credstore, "_keyring", lambda: BrokenKeyring())

    def fail_dpapi(*args):
        raise credstore.windows_secrets.DPAPIError("DPAPI unavailable")

    monkeypatch.setattr(credstore.windows_secrets, "protect_password", fail_dpapi)
    with pytest.raises(credstore.CredentialStorageError) as exc:
        credstore.save_zwift_credentials(user_id, "new@example.com", "secret")
    assert "not saved" in str(exc.value)
    assert db.get_zwift_credentials_row(user_id) == (
        "old@example.com", "enc1$old"
    )


def test_windows_reads_legacy_enc1_without_migrating(user_id, monkeypatch):
    # Store a genuine historical row; current saves intentionally produce enc2.
    db.set_zwift_credentials_row(
        user_id, "legacy@example.com", _legacy_enc1("old-secret")
    )
    before = db.get_zwift_credentials_row(user_id)
    assert before[1].startswith("enc1$")

    monkeypatch.setattr(credstore, "_is_windows", lambda: True)
    assert credstore.get_zwift_credentials(user_id).password == "old-secret"
    assert db.get_zwift_credentials_row(user_id) == before


def test_windows_explicit_resave_replaces_legacy_then_clear(user_id, monkeypatch):
    db.set_zwift_credentials_row(
        user_id, "legacy@example.com", _legacy_enc1("old-secret")
    )
    monkeypatch.setattr(credstore, "_is_windows", lambda: True)
    monkeypatch.setattr(credstore, "_keyring", lambda: None)
    monkeypatch.setattr(
        credstore.windows_secrets, "protect_password",
        lambda password, service, uid: "dpapi1$bmV3",
    )

    assert credstore.save_zwift_credentials(
        user_id, "new@example.com", "new-secret"
    ) == "Windows DPAPI"
    assert db.get_zwift_credentials_row(user_id) == (
        "new@example.com", "dpapi1$bmV3"
    )
    credstore.clear_zwift_credentials(user_id)
    assert db.get_zwift_credentials_row(user_id) == (None, None)


def test_keyring_db_failure_preserves_old_and_cleans_staged_slot(
    user_id, monkeypatch
):
    class FakeKeyring:
        def __init__(self):
            self.store = {("wattracker-Zwift", f"user{user_id}"): "old-password"}

        def set_password(self, service, key, value):
            self.store[(service, key)] = value

        def get_password(self, service, key):
            return self.store.get((service, key))

        def delete_password(self, service, key):
            self.store.pop((service, key), None)

    fake = FakeKeyring()
    db.set_zwift_credentials_row(user_id, "old@example.com", "@keyring")
    monkeypatch.setattr(credstore, "_keyring", lambda: fake)
    monkeypatch.setattr(
        credstore.db, "set_zwift_credentials_row",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("DB failed")),
    )

    with pytest.raises(RuntimeError, match="DB failed"):
        credstore.save_zwift_credentials(user_id, "new@example.com", "new-password")
    assert fake.get_password(
        "wattracker-Zwift", f"user{user_id}"
    ) == "old-password"
    assert fake.store == {
        ("wattracker-Zwift", f"user{user_id}"): "old-password"
    }


def test_unreadable_old_keyring_secret_survives_failed_resave(
    user_id, monkeypatch
):
    old_target = f"user{user_id}"

    class UnverifiableKeyring:
        def __init__(self):
            self.store = {("wattracker-Zwift", old_target): "old-password"}

        def set_password(self, service, key, value):
            self.store[(service, key)] = value

        def get_password(self, service, key):
            if key == old_target:
                raise RuntimeError("old slot cannot be read")
            return None  # force verification failure for the staged slot

        def delete_password(self, service, key):
            self.store.pop((service, key), None)

    fake = UnverifiableKeyring()
    db.set_zwift_credentials_row(user_id, "old@example.com", "@keyring")
    monkeypatch.setattr(credstore, "_is_windows", lambda: True)
    monkeypatch.setattr(credstore, "_keyring", lambda: fake)
    monkeypatch.setattr(
        credstore.windows_secrets, "protect_password",
        lambda *args: (_ for _ in ()).throw(
            credstore.windows_secrets.DPAPIError("DPAPI unavailable")
        ),
    )

    with pytest.raises(credstore.CredentialStorageError):
        credstore.save_zwift_credentials(
            user_id, "new@example.com", "new-password"
        )
    assert db.get_zwift_credentials_row(user_id) == (
        "old@example.com", "@keyring"
    )
    assert fake.store == {
        ("wattracker-Zwift", old_target): "old-password"
    }


def test_legacy_bare_keyring_marker_still_reads_and_clears(
    user_id, monkeypatch
):
    target = f"user{user_id}"

    class FakeKeyring:
        def __init__(self):
            self.store = {("wattracker-Zwift", target): "legacy-password"}

        def get_password(self, service, key):
            return self.store.get((service, key))

        def delete_password(self, service, key):
            self.store.pop((service, key), None)

    fake = FakeKeyring()
    db.set_zwift_credentials_row(user_id, "old@example.com", "@keyring")
    monkeypatch.setattr(credstore, "_keyring", lambda: fake)

    assert credstore.get_zwift_credentials(user_id).password == "legacy-password"
    credstore.clear_zwift_credentials(user_id)
    assert fake.store == {}
    assert db.get_zwift_credentials_row(user_id) == (None, None)


def test_versioned_keyring_marker_get_resave_and_clear(user_id, monkeypatch):
    class FakeKeyring:
        def __init__(self):
            self.store = {}

        def set_password(self, service, key, value):
            self.store[(service, key)] = value

        def get_password(self, service, key):
            return self.store.get((service, key))

        def delete_password(self, service, key):
            self.store.pop((service, key), None)

    fake = FakeKeyring()
    monkeypatch.setattr(credstore, "_keyring", lambda: fake)

    credstore.save_zwift_credentials(user_id, "a@b.com", "first")
    first_marker = db.get_zwift_credentials_row(user_id)[1]
    first_target = credstore._keyring_target(user_id, first_marker)
    assert credstore.get_zwift_credentials(user_id).password == "first"

    credstore.save_zwift_credentials(user_id, "a@b.com", "second")
    second_marker = db.get_zwift_credentials_row(user_id)[1]
    second_target = credstore._keyring_target(user_id, second_marker)
    assert second_marker != first_marker
    assert ("wattracker-Zwift", first_target) not in fake.store
    assert fake.store[("wattracker-Zwift", second_target)] == "second"

    credstore.clear_zwift_credentials(user_id)
    assert fake.store == {}
    assert db.get_zwift_credentials_row(user_id) == (None, None)


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
            "f_t": "TYPE_RACE TYPE_RACE ",
            "position_in_cat": 4,
            "category": "B",
            "avg_power": [255, 1],
            "np": [270, 1],
            "w15": [400, 0], "w60": [320, 0],
        },
        {  # a group ride - must be filtered out, never listed as a race
            "event_date": 1779900000,
            "event_title": "Coffee Ride",
            "f_t": "TYPE_RIDE",
            "avg_power": [150, 1],
        },
    ]
}


def test_parse_filters_group_rides_and_workouts_keeps_races():
    doc = {"data": [
        {"event_date": 1780000000, "event_title": "Crit City Race",
         "f_t": "TYPE_RACE TYPE_RACE ", "position_in_cat": 3, "category": "A"},
        {"event_date": 1779900000, "event_title": "Group Ride",
         "f_t": "TYPE_RIDE"},
        {"event_date": 1779800000, "event_title": "Workout #6",
         "f_t": "TYPE_WORKOUT TYPE_WORKOUT "},
        {"event_date": 1779700000, "event_title": "Short Race", "f_t": "TYPE_RACE"},
    ]}
    rows = races.parse_zwiftpower_profile(doc)
    titles = [r["event_title"] for r in rows]
    assert titles == ["Crit City Race", "Short Race"]
    assert all("TYPE_RACE" in r["source_type"] for r in rows)


def test_parse_extracts_zwiftpower_power_periods():
    doc = {"data": [{
        "event_date": 1780000000, "event_title": "R", "f_t": "TYPE_RACE",
        "w5": [500, 0], "w15": [420, 0], "w30": [360, 0], "w60": [300, 0],
        "w120": [270, 0], "w300": [240, 0], "w1200": [210, 0],
        "time": [2796.0, 0],
    }]}
    row = races.parse_zwiftpower_profile(doc)[0]
    assert row["power"] == {"5": 500, "15": 420, "30": 360, "60": 300,
                            "120": 270, "300": 240, "1200": 210}
    assert row["duration_s"] == 2796
    # 1s and 10m (600) are not published by ZwiftPower.
    assert "1" not in row["power"] and "600" not in row["power"]


def test_refresh_retroactively_purges_cached_group_rides(user_id, monkeypatch):
    # An old cache mixed a race with a group ride; a fresh fetch (filtered)
    # replaces the whole zwiftpower set, dropping the group ride.
    db.replace_race_results(user_id, "zwiftpower", [
        {"event_date": "2026-05-01", "event_title": "Old Group Ride",
         "position": None, "category": None, "source_type": "TYPE_RIDE",
         "activity_id": None, "duration_s": None, "avg_power": 150.0,
         "np": None, "if_": None, "power": {}, "fetched_at": "2026-05-01T00:00:00"},
        {"event_date": "2026-05-02", "event_title": "Old Race",
         "position": "1", "category": "A", "source_type": "TYPE_RACE",
         "activity_id": None, "duration_s": None, "avg_power": 250.0,
         "np": None, "if_": None, "power": {}, "fetched_at": "2026-05-01T00:00:00"}])
    credstore.save_zwift_credentials(user_id, "a@b.com", "pw")
    monkeypatch.setattr(
        zwiftauth, "fetch_results_authenticated",
        lambda email, password, rider_id=None: (ZP_DOC, "5555", 72.5))
    races.refresh_race_results(user_id)
    titles = [r["event_title"] for r in db.list_race_results(user_id)]
    assert titles == ["WTRL TTT"]  # only the race from the fresh fetch remains


def test_refresh_uses_credentials_and_autodetects_rider_id(user_id, monkeypatch):
    credstore.save_zwift_credentials(user_id, "a@b.com", "pw")
    monkeypatch.setattr(
        zwiftauth, "fetch_results_authenticated",
        lambda email, password, rider_id=None: (ZP_DOC, "5555", 72.5),
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
        lambda email, password, rider_id=None: (ZP_DOC, "5555", 72.5))
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


def test_successful_fetch_purges_heuristic_rows(user_id, monkeypatch):
    # A previous fallback run left FIT-derived rows; a real fetch removes them.
    db.replace_race_results(user_id, "local", [{
        "event_date": "2026-06-01", "event_title": "Race effort - old",
        "position": None, "category": None, "activity_id": 1,
        "duration_s": 3600, "avg_power": 250.0, "np": 255.0, "if_": 0.9,
        "power": {}, "fetched_at": "2026-06-01T12:00:00"}])
    credstore.save_zwift_credentials(user_id, "a@b.com", "pw")
    monkeypatch.setattr(
        zwiftauth, "fetch_results_authenticated",
        lambda email, password, rider_id=None: (ZP_DOC, "5555", 72.5))
    races.refresh_race_results(user_id)
    rows = db.list_race_results(user_id)
    assert len(rows) == 1
    assert rows[0]["source"] == "zwiftpower"
    assert all("Race effort" not in r["event_title"] for r in rows)


def test_failed_refresh_keeps_stale_real_results(user_id, monkeypatch):
    # Real results cached, then the login starts failing: keep the real rows
    # (stale) - never regenerate heuristic entries next to them.
    credstore.save_zwift_credentials(user_id, "a@b.com", "pw")
    monkeypatch.setattr(
        zwiftauth, "fetch_results_authenticated",
        lambda email, password, rider_id=None: (ZP_DOC, "5555", 72.5))
    races.refresh_race_results(user_id)
    # An imported hard ride that WOULD be flagged by the heuristic.
    db.insert_activity(user_id, {
        "dedup_hash": "hard1", "filename": "hard.fit",
        "start_time": "2026-06-20T10:00:00", "duration_s": 3600,
        "distance_m": 0, "avg_power": 260, "avg_hr": 0, "np": 265,
        "if_": 0.9, "tss": 81.0, "streams": {"power": [260.0] * 100}})
    monkeypatch.setattr(
        zwiftauth, "fetch_results_authenticated",
        lambda email, password, rider_id=None: (_ for _ in ()).throw(
            zwiftauth.ZwiftAuthError("login failed", credential_problem=True)))
    out = races.refresh_race_results(user_id)
    assert out["source"] == "zwiftpower"  # stale real results still shown
    rows = db.list_race_results(user_id)
    assert len(rows) == 1 and rows[0]["source"] == "zwiftpower"


def test_page_data_hides_heuristics_when_real_results_exist(user_id):
    fetched = "2026-06-01T12:00:00"
    db.replace_race_results(user_id, "zwiftpower", [{
        "event_date": "2026-06-02", "event_title": "Real Race", "position": "1",
        "category": "A", "activity_id": None, "duration_s": None,
        "avg_power": 250.0, "np": None, "if_": None, "power": {},
        "fetched_at": fetched}])
    db.replace_race_results(user_id, "local", [{
        "event_date": "2026-06-01", "event_title": "Race effort - x",
        "position": None, "category": None, "activity_id": 1,
        "duration_s": 3600, "avg_power": 240.0, "np": None, "if_": 0.9,
        "power": {}, "fetched_at": fetched}])
    data = races.race_page_data(user_id)
    assert [r["event_title"] for r in data["results"]] == ["Real Race"]


def test_refresh_persists_weight_from_zwift_profile(user_id, monkeypatch):
    credstore.save_zwift_credentials(user_id, "a@b.com", "pw")
    monkeypatch.setattr(
        zwiftauth, "fetch_results_authenticated",
        lambda email, password, rider_id=None: (ZP_DOC, "5555", 72.5))
    races.refresh_race_results(user_id)
    assert db.get_user_settings(user_id)["weight_kg"] == 72.5


def test_refresh_uses_zwiftpower_weight_as_secondary(user_id, monkeypatch):
    doc = dict(ZP_DOC)
    doc["data"] = [dict(ZP_DOC["data"][0], weight=[71.2, 0])]
    credstore.save_zwift_credentials(user_id, "a@b.com", "pw")
    monkeypatch.setattr(
        zwiftauth, "fetch_results_authenticated",
        lambda email, password, rider_id=None: (doc, "5555", None))
    races.refresh_race_results(user_id)
    assert db.get_user_settings(user_id)["weight_kg"] == 71.2


def test_weight_kg_from_profile_grams():
    assert zwiftauth.weight_kg_from_profile({"weight": 72500}) == 72.5
    assert zwiftauth.weight_kg_from_profile({"weight": 0}) is None
    assert zwiftauth.weight_kg_from_profile({}) is None


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
        lambda email, password, rider_id=None: (ZP_DOC, "5555", 72.5))
    r = client.post("/races/refresh", data={"rider_id": ""})
    assert "using your Zwift login" in r.text
    assert "WTRL TTT" in r.text
    # Rider id was auto-detected and now prefills the field.
    assert 'value="5555"' in r.text
