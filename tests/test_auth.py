"""Tests for password hashing, user creation, and login endpoints."""
import pytest

pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from wattracker import auth, db  # noqa: E402
from wattracker.server import create_app  # noqa: E402


@pytest.fixture()
def client():
    app = create_app()
    with TestClient(app) as c:
        yield c


# --------------------------------------------------------- hashing
def test_password_is_hashed_not_plaintext():
    h = auth.hash_password("hunter2secret")
    assert "hunter2secret" not in h
    assert h.startswith("scrypt$")
    # Two hashes of the same password differ (random salt).
    assert h != auth.hash_password("hunter2secret")


def test_verify_password_roundtrip():
    h = auth.hash_password("correct horse battery")
    assert auth.verify_password("correct horse battery", h) is True
    assert auth.verify_password("wrong password", h) is False


def test_validate_credentials():
    assert auth.validate_credentials("", "password123") is not None
    assert auth.validate_credentials("user", "short") is not None  # < 8 chars
    assert auth.validate_credentials("user", "password123") is None


def test_validate_credentials_rejects_overlong_password():
    # Cap the scrypt input so a huge password can't be used to burn CPU.
    long_pw = "a" * (auth.MAX_PASSWORD_LEN + 1)
    assert auth.validate_credentials("user", long_pw) is not None
    assert auth.validate_credentials("user", "a" * auth.MAX_PASSWORD_LEN) is None


# ------------------------------------------ per-hash cost params + rehash
def test_new_hash_encodes_params_and_verifies():
    h = auth.hash_password("password123")
    # scrypt$<n>$<r>$<p>$<salt>$<hash>
    assert h.split("$")[:4] == ["scrypt", str(2 ** 17), "8", "1"]
    assert auth.verify_password("password123", h) is True
    assert auth.verify_password("wrong", h) is False
    assert auth.needs_rehash(h) is False


def test_legacy_hash_still_verifies_and_needs_rehash():
    # A pre-upgrade 3-field hash (n=16384, r=8, p=1).
    import hashlib
    import os

    salt = os.urandom(16)
    dk = hashlib.scrypt(b"password123", salt=salt, n=16384, r=8, p=1, dklen=32)
    legacy = f"scrypt${salt.hex()}${dk.hex()}"
    assert auth.verify_password("password123", legacy) is True
    assert auth.verify_password("nope", legacy) is False
    assert auth.needs_rehash(legacy) is True


def test_needs_rehash_false_for_garbage():
    assert auth.needs_rehash("not-a-hash") is False
    assert auth.needs_rehash("bcrypt$x$y") is False


# ----------------------------------------------------- login throttle
def test_login_throttle_locks_resets_and_is_case_insensitive():
    clock = [0.0]
    thr = auth.LoginThrottle(threshold=3, base_seconds=2.0,
                             clock=lambda: clock[0])
    thr.record_failure("Bob")
    thr.record_failure("bob")
    assert thr.retry_after("BOB") == 0.0  # under threshold
    thr.record_failure("bOb")  # 3rd consecutive failure -> locked
    assert thr.retry_after("bob") > 0.0
    # Clock advances past the lockout window -> allowed again.
    clock[0] += 100.0
    assert thr.retry_after("bob") == 0.0
    # A success clears the counter entirely.
    for _ in range(3):
        thr.record_failure("carol")
    assert thr.retry_after("carol") > 0.0
    thr.record_success("carol")
    assert thr.retry_after("carol") == 0.0


def test_login_transparently_upgrades_legacy_hash(client):
    import hashlib
    import os

    salt = os.urandom(16)
    dk = hashlib.scrypt(b"password123", salt=salt, n=16384, r=8, p=1, dklen=32)
    legacy = f"scrypt${salt.hex()}${dk.hex()}"
    db.init_db()
    db.create_user("legacyuser", legacy)
    r = client.post(
        "/login", data={"username": "legacyuser", "password": "password123"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    # The stored hash was upgraded to the current 6-field format on login.
    stored = db.get_user_by_username("legacyuser")["password_hash"]
    assert stored != legacy
    assert stored.startswith(f"scrypt${2 ** 17}$")
    assert auth.verify_password("password123", stored) is True


def test_login_lockout_and_generic_message():
    # Own app with a long lockout window so timing (scrypt cost) can't flake.
    app = create_app()
    app.state.login_throttle = auth.LoginThrottle(threshold=5, base_seconds=3600.0)
    with TestClient(app) as c:
        c.post("/register", data={"username": "victim", "password": "password123"})
        c.post("/logout")
        for _ in range(5):
            r = c.post("/login", data={"username": "victim", "password": "wrongpass1"})
            assert "Invalid username or password" in r.text
        # The next attempt is locked out with a generic (non-enumerating) message.
        r = c.post("/login", data={"username": "victim", "password": "wrongpass1"})
        assert r.status_code == 429
        assert "Too many failed attempts" in r.text
        # A correct password is still refused while locked.
        r = c.post("/login", data={"username": "victim", "password": "password123"},
                   follow_redirects=False)
        assert r.status_code == 429


def test_login_unknown_and_known_user_same_message(client):
    client.post("/register", data={"username": "realuser", "password": "password123"})
    client.post("/logout")
    r_known = client.post(
        "/login", data={"username": "realuser", "password": "wrongpass1"}
    )
    r_unknown = client.post(
        "/login", data={"username": "ghostuser", "password": "wrongpass1"}
    )
    assert "Invalid username or password" in r_known.text
    assert "Invalid username or password" in r_unknown.text
    assert r_known.status_code == r_unknown.status_code == 200


def test_stored_hash_in_db_is_not_plaintext():
    db.init_db()
    db.create_user("alice", auth.hash_password("password123"))
    row = db.get_user_by_username("alice")
    assert "password123" not in row["password_hash"]
    assert auth.verify_password("password123", row["password_hash"])


# --------------------------------------------------------- endpoints
def test_register_creates_user(client):
    r = client.post(
        "/register", data={"username": "newuser", "password": "password123"}
    )
    assert r.status_code == 200
    assert db.get_user_by_username("newuser") is not None


def test_duplicate_username_rejected(client):
    client.post("/register", data={"username": "dupe", "password": "password123"})
    client.post("/logout")
    r = client.post("/register", data={"username": "dupe", "password": "different1"})
    assert r.status_code == 200
    assert "already taken" in r.text


def test_short_password_rejected(client):
    r = client.post("/register", data={"username": "shorty", "password": "abc"})
    assert r.status_code == 200
    assert "at least 8" in r.text
    assert db.get_user_by_username("shorty") is None


def test_wrong_password_fails_login(client):
    client.post("/register", data={"username": "carol", "password": "password123"})
    client.post("/logout")
    r = client.post(
        "/login", data={"username": "carol", "password": "wrongpassword"}
    )
    assert r.status_code == 200
    assert "Invalid username or password" in r.text
    # Still not authenticated.
    assert client.get("/", follow_redirects=False).status_code == 303


def test_correct_login_succeeds(client):
    client.post("/register", data={"username": "dave", "password": "password123"})
    client.post("/logout")
    r = client.post(
        "/login", data={"username": "dave", "password": "password123"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/"
