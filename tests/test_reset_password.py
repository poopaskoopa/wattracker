"""Tests for the offline password-reset CLI (tranalyzer.reset_password)."""
from tranalyzer import auth, db, reset_password


def _make_user(username="lockedout", password="oldpassword"):
    db.init_db()
    db.create_user(username, auth.hash_password(password))


def test_reset_updates_hash_new_passes_old_fails(monkeypatch, capsys):
    _make_user("lockedout", "oldpassword")
    monkeypatch.setattr(
        "getpass.getpass", lambda *a, **k: "brandnewpass"
    )
    rc = reset_password.main(["lockedout"])
    assert rc == 0

    row = db.get_user_by_username("lockedout")
    assert auth.verify_password("brandnewpass", row["password_hash"]) is True
    assert auth.verify_password("oldpassword", row["password_hash"]) is False
    # New format is still the shared scrypt format; no plaintext leaked.
    assert row["password_hash"].startswith("scrypt$")
    out = capsys.readouterr().out
    assert "brandnewpass" not in out


def test_unknown_user_errors(monkeypatch, capsys):
    _make_user("someone")
    called = []
    monkeypatch.setattr(
        "getpass.getpass", lambda *a, **k: called.append(1) or "whatever8"
    )
    rc = reset_password.main(["ghost"])
    assert rc == 1
    # Never prompted for a password on a nonexistent user.
    assert called == []
    assert "no such user" in capsys.readouterr().err


def test_short_password_rejected(monkeypatch, capsys):
    _make_user("shorty")
    monkeypatch.setattr("getpass.getpass", lambda *a, **k: "abc")
    rc = reset_password.main(["shorty"])
    assert rc == 2
    # Hash unchanged: original password still verifies.
    row = db.get_user_by_username("shorty")
    assert auth.verify_password("oldpassword", row["password_hash"]) is True
    assert "at least" in capsys.readouterr().err


def test_mismatch_rejected(monkeypatch, capsys):
    _make_user("mismatch")
    answers = iter(["longpassword1", "longpassword2"])
    monkeypatch.setattr("getpass.getpass", lambda *a, **k: next(answers))
    rc = reset_password.main(["mismatch"])
    assert rc == 2
    row = db.get_user_by_username("mismatch")
    assert auth.verify_password("oldpassword", row["password_hash"]) is True
    assert "do not match" in capsys.readouterr().err


def test_list_shows_usernames_only(monkeypatch, capsys):
    db.init_db()
    db.create_user("bob", auth.hash_password("password123"))
    db.create_user("alice", auth.hash_password("password123"))
    rc = reset_password.main(["--list"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "alice" in out
    assert "bob" in out
    # No hashes / PII in the listing.
    assert "scrypt$" not in out
    assert "password_hash" not in out


def test_no_args_usage(capsys):
    rc = reset_password.main([])
    assert rc == 2
    assert "Usage" in capsys.readouterr().err
