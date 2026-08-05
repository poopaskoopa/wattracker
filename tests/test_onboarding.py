"""Focused regression coverage for first-run onboarding."""
import sqlite3
import time

import pytest

pytest.importorskip("httpx")
from fastapi.testclient import TestClient

from wattracker import auth, db
import wattracker.credstore as credstore
import wattracker.ingest.importer as importer
from wattracker.server import create_app


@pytest.fixture()
def client():
    with TestClient(create_app()) as value:
        yield value


def _register(client, username="rider"):
    response = client.post("/register", data={"username": username, "password": "password123"})
    assert response.status_code == 200
    return db.get_user_by_username(username)["id"]


def _wait_scan(client):
    deadline = time.time() + 5
    while time.time() < deadline:
        if not client.get("/api/scan/status").json().get("running"):
            return
        time.sleep(0.02)
    raise AssertionError("onboarding scan did not finish")


def test_new_user_is_incomplete_and_v26_migration_preserves_user(tmp_path, monkeypatch):
    db.init_db()
    uid = db.create_user("new", auth.hash_password("password123"))
    assert db.onboarding_complete(uid) is False

    old_db = tmp_path / "old.db"
    conn = sqlite3.connect(old_db)
    conn.executescript(
        "CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, "
        "password_hash TEXT NOT NULL, created TEXT NOT NULL, calendar_token_hash TEXT);"
        "INSERT INTO users VALUES (7, 'legacy', 'hash', '2026-01-01', NULL);"
        "PRAGMA user_version = 26;"
    )
    conn.commit()
    conn.close()
    db.init_db(str(old_db))
    assert db.onboarding_complete(7, str(old_db)) is True
    assert db.get_user_by_username("legacy", str(old_db))["id"] == 7


def test_setup_copy_and_dashboard_include_guidance(client):
    _register(client)
    dashboard = client.get("/").text
    setup = client.get("/setup").text
    text = dashboard + setup
    for phrase in ("W/kg", "weight-to-power ratio", "FIT", "FTP", "ZwiftPower", "Races", "Activities"):
        assert phrase in text
    assert 'webkitdirectory directory' in setup
    assert "Numeric ZwiftPower rider ID" in setup
    assert "password is sent only" in setup


def test_directory_check_reports_missing_empty_fit_and_rejects_outside(client, tmp_path, monkeypatch):
    _register(client)
    monkeypatch.setenv("HOME", str(tmp_path))
    missing = client.post("/setup/check-directory", data={"activities_dir": str(tmp_path / "missing")})
    assert missing.status_code == 400
    assert "not found or not a directory" in missing.json()["error"]
    empty = tmp_path / "empty"
    empty.mkdir()
    response = client.post("/setup/check-directory", data={"activities_dir": str(empty)})
    assert response.status_code == 202
    assert response.json()["status"] == "no-files"
    _wait_scan(client)
    (empty / "ride.FIT").write_bytes(b"x")
    response = client.post("/setup/check-directory", data={"activities_dir": str(empty)})
    assert response.status_code == 202
    assert response.json()["fit_count"] == 1
    _wait_scan(client)
    outside = tmp_path.parent / "outside-onboarding"
    outside.mkdir()
    rejected = client.post("/setup/check-directory", data={"activities_dir": str(outside)})
    assert rejected.status_code == 400
    assert "inside your home directory" in rejected.json()["error"]


def test_fit_upload_imports_and_counts_with_parser_mocked(client, monkeypatch):
    uid = _register(client)
    monkeypatch.setattr(importer, "parse_fit", lambda path: {"start_time": "2026-07-01T10:00:00", "duration_s": 60, "streams": {"power": [200] * 60}})
    response = client.post("/setup/upload", files=[("files", ("ride.fit", b"not-real-fit", "application/octet-stream"))])
    assert response.status_code == 200
    assert response.json()["selected"] == response.json()["fit_count"] == 1
    assert response.json()["imported"] == 1
    assert len(db.list_activities(uid)) == 1


def test_ftp_choices_persist_and_bad_manual_input_is_rejected(client, monkeypatch):
    uid = _register(client)
    bad = client.post("/setup/ftp", data={"choice": "manual", "manual_ftp": "watts"})
    assert bad.status_code == 400
    manual = client.post("/setup/ftp", data={"choice": "manual", "manual_ftp": "275"})
    assert manual.status_code == 200
    assert manual.json()["ftp"] == 275
    assert db.get_user_settings(uid)["ftp"] == 275
    monkeypatch.setattr(importer, "recent_best_effort_ftp", lambda user_id: 241.25)
    estimated_uid = _register(client, "estimated")
    estimated = client.post("/setup/ftp", data={"choice": "estimated"})
    assert estimated.json()["ftp"] == 241.2
    assert db.latest_ftp(estimated_uid)["ftp_watts"] == pytest.approx(241.2)


def test_estimated_choice_clears_a_previous_manual_override(client, monkeypatch):
    uid = _register(client)
    client.post("/setup/complete", data={
        "weight_kg": "72", "ftp_choice": "manual", "manual_ftp": "275",
        "zwiftpower": "no",
    })
    assert db.get_user_settings(uid)["ftp"] == pytest.approx(275)

    # Resume setup explicitly and choose the analyzed estimate.
    conn = db.connect()
    try:
        conn.execute("UPDATE users SET onboarding_complete = 0 WHERE id = ?", (uid,))
        conn.commit()
    finally:
        conn.close()
    monkeypatch.setattr(importer, "recent_best_effort_ftp", lambda user_id: 241.25)
    response = client.post("/setup/complete", data={
        "weight_kg": "72", "ftp_choice": "estimated", "zwiftpower": "no",
    })
    assert response.status_code == 200
    assert db.get_user_settings(uid)["ftp"] is None


def test_completion_no_profile_sets_flag_and_saves_settings(client):
    uid = _register(client)
    response = client.post("/setup/complete", data={"weight_kg": "72.5", "ftp_choice": "manual", "manual_ftp": "250", "zwiftpower": "no"})
    assert response.status_code == 200
    assert db.onboarding_complete(uid)
    settings = db.get_user_settings(uid)
    assert settings["weight_kg"] == pytest.approx(72.5)
    assert settings["ftp"] == pytest.approx(250)


def test_completion_yes_profile_saves_credentials_without_password_in_response(client, monkeypatch):
    uid = _register(client)
    saved = {}
    monkeypatch.setattr(credstore, "save_zwift_credentials", lambda user_id, email, password: saved.update(user_id=user_id, email=email, password=password) or "file-key")
    response = client.post("/setup/complete", data={"weight_kg": "70", "ftp_choice": "estimated", "zwiftpower": "yes", "zwift_id": "12345", "zwift_email": "rider@example.com", "zwift_password": "secret-pass"})
    assert response.status_code == 200
    assert db.onboarding_complete(uid)
    assert db.get_user_settings(uid)["zwift_id"] == "12345"
    assert saved == {"user_id": uid, "email": "rider@example.com", "password": "secret-pass"}
    assert "secret-pass" not in response.text


def test_register_and_root_dashboard_remain_compatible(client):
    response = client.post("/register", data={"username": "compatible", "password": "password123"})
    assert response.status_code == 200
    assert "Dashboard" in response.text
    assert client.get("/").status_code == 200
