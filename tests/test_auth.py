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
    client.get("/logout")
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
    client.get("/logout")
    r = client.post(
        "/login", data={"username": "carol", "password": "wrongpassword"}
    )
    assert r.status_code == 200
    assert "Invalid username or password" in r.text
    # Still not authenticated.
    assert client.get("/", follow_redirects=False).status_code == 303


def test_correct_login_succeeds(client):
    client.post("/register", data={"username": "dave", "password": "password123"})
    client.get("/logout")
    r = client.post(
        "/login", data={"username": "dave", "password": "password123"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/"
