"""Focused regression coverage for first-run onboarding."""
import logging
import os
import sqlite3
import inspect
import time

import pytest

pytest.importorskip("httpx")
from fastapi.testclient import TestClient

from wattracker import auth, db, paths
import wattracker.credstore as credstore
import wattracker.ingest.importer as importer
import wattracker.server as server_mod
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


def test_onboarding_posts_reject_cross_origin_without_mutation(
    client, home_dir, monkeypatch
):
    uid = _register(client)
    activities = home_dir / "Activities"
    activities.mkdir()

    def unexpected_import(*_args, **_kwargs):
        raise AssertionError("cross-origin upload reached the importer")

    monkeypatch.setattr(importer, "ingest_upload", unexpected_import)
    requests = (
        ("/setup/check-directory", {"data": {"activities_dir": str(activities)}}),
        ("/setup/upload", {
            "files": [("files", ("ride.fit", b"fit", "application/octet-stream"))]
        }),
        ("/setup/ftp", {"data": {"choice": "manual", "manual_ftp": "275"}}),
        ("/setup/complete", {"data": {
            "weight_kg": "72", "ftp_choice": "manual", "manual_ftp": "275",
            "zwiftpower": "no", "activities_dir": str(activities),
        }}),
    )

    for route, kwargs in requests:
        response = client.post(
            route, headers={"origin": "https://evil.example.com"}, **kwargs
        )
        assert response.status_code == 403, route

    settings = db.get_user_settings(uid)
    assert settings["activities_dir"] is None
    assert settings["weight_kg"] is None
    assert settings["ftp"] is None
    assert db.latest_ftp(uid) is None
    assert db.list_activities(uid) == []
    assert db.onboarding_complete(uid) is False


@pytest.mark.parametrize(
    ("files", "expected_status"),
    (
        ([("files", ("large.fit", b"123456", "application/octet-stream"))], 413),
        ([
            ("files", ("first.fit", b"123", "application/octet-stream")),
            ("files", ("second.fit", b"456", "application/octet-stream")),
        ], 413),
        ([
            ("files", ("first.fit", b"1", "application/octet-stream")),
            ("files", ("second.fit", b"2", "application/octet-stream")),
            ("files", ("third.fit", b"3", "application/octet-stream")),
        ], 400),
    ),
    ids=("per-file-bytes", "running-total-bytes", "file-count"),
)
def test_onboarding_upload_limits_are_json(
    client, monkeypatch, files, expected_status
):
    _register(client)
    monkeypatch.setattr(server_mod, "MAX_ONBOARDING_UPLOAD_BYTES", 5)
    monkeypatch.setattr(server_mod, "MAX_ONBOARDING_UPLOAD_FILES", 2)
    monkeypatch.setattr(
        importer,
        "ingest_upload",
        lambda *_args, **_kwargs: pytest.fail("rejected upload reached the importer"),
    )

    response = client.post("/setup/upload", files=files)

    assert response.status_code == expected_status
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["error"]


def test_onboarding_upload_rejects_non_fit_before_import(client, monkeypatch):
    _register(client)
    monkeypatch.setattr(
        importer,
        "ingest_upload",
        lambda *_args, **_kwargs: pytest.fail("non-FIT upload reached the importer"),
    )

    response = client.post(
        "/setup/upload",
        files=[("files", ("notes.txt", b"not a FIT file", "text/plain"))],
    )

    assert response.status_code == 400
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["error"]


def test_onboarding_directory_rejections_preserve_accepted_directory(
    client, tmp_path, home_dir
):
    uid = _register(client)
    accepted = home_dir / "accepted"
    accepted.mkdir()
    response = client.post(
        "/setup/check-directory", data={"activities_dir": str(accepted)}
    )
    assert response.status_code == 202
    _wait_scan(client)
    assert db.get_user_settings(uid)["activities_dir"] == str(accepted)

    outside = tmp_path / "outside"
    outside.mkdir()
    rejected_values = ("", "   ", str(home_dir / ".." / outside.name))
    for value in rejected_values:
        rejected = client.post(
            "/setup/check-directory", data={"activities_dir": value}
        )
        assert rejected.status_code == 400, value
        assert db.get_user_settings(uid)["activities_dir"] == str(accepted)


@pytest.mark.skipif(os.name != "posix", reason="symlink semantics tested on POSIX")
def test_onboarding_directory_symlink_escape_preserves_accepted_directory(
    client, tmp_path, home_dir
):
    uid = _register(client)
    accepted = home_dir / "accepted"
    accepted.mkdir()
    response = client.post(
        "/setup/check-directory", data={"activities_dir": str(accepted)}
    )
    assert response.status_code == 202
    _wait_scan(client)

    outside = tmp_path / "outside"
    outside.mkdir()
    escaped = home_dir / "escaped"
    escaped.symlink_to(outside, target_is_directory=True)
    rejected = client.post(
        "/setup/check-directory", data={"activities_dir": str(escaped)}
    )

    assert rejected.status_code == 400
    assert db.get_user_settings(uid)["activities_dir"] == str(accepted)


def test_setup_upload_is_offloaded_from_event_loop(client):
    _register(client)
    endpoint = next(
        route.endpoint for route in client.app.routes
        if getattr(route, "path", None) == "/setup/upload"
    )
    assert not inspect.iscoroutinefunction(endpoint)


def test_setup_upload_refreshes_derived_state_once_per_batch(client, monkeypatch):
    _register(client)
    calls = {"ftp": 0, "completions": 0, "profile": 0}

    monkeypatch.setattr(
        importer, "ingest_file", lambda user_id, path, ftp=None: 1
    )
    monkeypatch.setattr(
        importer,
        "evaluate_ftp",
        lambda user_id: calls.__setitem__("ftp", calls["ftp"] + 1),
    )
    monkeypatch.setattr(
        importer,
        "match_plan_completions",
        lambda user_id: calls.__setitem__("completions", calls["completions"] + 1),
    )
    monkeypatch.setattr(
        importer.profile_store,
        "refresh",
        lambda user_id: calls.__setitem__("profile", calls["profile"] + 1),
    )
    monkeypatch.setattr(importer, "recent_best_effort_ftp", lambda user_id: 241.25)

    response = client.post(
        "/setup/upload",
        files=[
            ("files", ("one.fit", b"one", "application/octet-stream")),
            ("files", ("two.fit", b"two", "application/octet-stream")),
        ],
    )

    assert response.status_code == 200
    assert response.json()["imported"] == 2
    assert calls == {"ftp": 1, "completions": 1, "profile": 1}


def test_setup_upload_uses_one_ftp_snapshot_for_batch(client, monkeypatch):
    uid = _register(client)
    ftp_calls = []
    ingested = []

    monkeypatch.setattr(
        importer,
        "current_ftp",
        lambda user_id: ftp_calls.append(user_id) or 245.0,
    )
    monkeypatch.setattr(
        importer,
        "ingest_upload",
        lambda *args, **kwargs: ingested.append((args, kwargs)) or 1,
    )
    monkeypatch.setattr(importer, "evaluate_ftp", lambda user_id: None)
    monkeypatch.setattr(importer, "match_plan_completions", lambda user_id: 0)
    monkeypatch.setattr(importer.profile_store, "refresh", lambda user_id: None)
    monkeypatch.setattr(importer, "recent_best_effort_ftp", lambda user_id: 245.0)

    response = client.post(
        "/setup/upload",
        files=[
            ("files", ("one.fit", b"one", "application/octet-stream")),
            ("files", ("two.fit", b"two", "application/octet-stream")),
        ],
    )

    assert response.status_code == 200
    assert ftp_calls == [uid]
    assert [kwargs["ftp"] for _, kwargs in ingested] == [245.0, 245.0]


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
    latest = db.latest_ftp(uid)
    assert latest["source"] == "estimated"
    assert latest["ftp_watts"] == pytest.approx(241.2)
    assert importer.current_ftp(uid) == pytest.approx(241.2)


def test_setup_ftp_estimated_replaces_same_day_manual_history(client, monkeypatch):
    uid = _register(client)
    assert client.post(
        "/setup/ftp", data={"choice": "manual", "manual_ftp": "275"}
    ).status_code == 200
    monkeypatch.setattr(importer, "recent_best_effort_ftp", lambda user_id: 241.25)

    response = client.post("/setup/ftp", data={"choice": "estimated"})

    assert response.status_code == 200
    latest = db.latest_ftp(uid)
    assert latest["source"] == "estimated"
    assert latest["ftp_watts"] == pytest.approx(241.2)
    assert importer.current_ftp(uid) == pytest.approx(241.2)


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


def test_credential_save_failure_does_not_expose_password_in_response_or_logs(
    client, monkeypatch, caplog
):
    _register(client)
    password = "onboarding-secret-password"

    def fail_save(_user_id, _email, supplied_password):
        raise RuntimeError(f"credential backend rejected {supplied_password}")

    monkeypatch.setattr(credstore, "save_zwift_credentials", fail_save)
    with caplog.at_level(logging.WARNING, logger="wattracker.server"):
        response = client.post("/setup/complete", data={
            "weight_kg": "70", "ftp_choice": "estimated",
            "zwiftpower": "yes", "zwift_id": "12345",
            "zwift_email": "rider@example.com", "zwift_password": password,
        })

    assert response.status_code == 400
    assert password not in response.text
    assert password not in caplog.text


def test_onboarding_directory_validation_delegates_to_paths(
    client, home_dir, monkeypatch
):
    _register(client)
    accepted = home_dir / "accepted"
    accepted.mkdir()
    calls = []

    def confine(value, must_exist):
        calls.append((value, must_exist))
        return str(accepted), None

    monkeypatch.setattr(paths, "confine_storage_dir", confine)
    checked = client.post(
        "/setup/check-directory", data={"activities_dir": "check-delegation"}
    )
    completed = client.post("/setup/complete", data={
        "weight_kg": "70", "ftp_choice": "estimated", "zwiftpower": "no",
        "activities_dir": "complete-delegation",
    })

    assert calls == [
        ("check-delegation", True),
        ("complete-delegation", True),
    ]
    assert checked.status_code == 202
    assert completed.status_code == 200


def test_register_and_root_dashboard_remain_compatible(client):
    response = client.post("/register", data={"username": "compatible", "password": "password123"})
    assert response.status_code == 200
    assert "Dashboard" in response.text
    assert client.get("/").status_code == 200
