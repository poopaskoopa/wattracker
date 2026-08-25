"""Focused regression coverage for first-run onboarding."""
import ast
import logging
import os
import pathlib
import re
import sqlite3
import inspect
import time

import pytest

pytest.importorskip("httpx")
from fastapi.testclient import TestClient

from conftest import redirect_home

from conftest_connector import attach_connector

import wattracker
from wattracker import auth, connectorhub, db, paths
from wattracker import backend as backend_mod
from wattracker.backend import remote as remote_backend
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


def _pre_onboarding_db(tmp_path, name="pre28.db"):
    """A database from before migration 28 added onboarding_complete."""
    old_db = tmp_path / name
    conn = sqlite3.connect(old_db)
    conn.executescript(
        "CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, "
        "password_hash TEXT NOT NULL, created TEXT NOT NULL, calendar_token_hash TEXT);"
        "CREATE UNIQUE INDEX idx_users_calendar_token_hash ON users(calendar_token_hash);"
        "INSERT INTO users VALUES (7, 'legacy', 'hash', '2026-01-01', 'abc123');"
        "INSERT INTO users VALUES (8, 'legacy2', 'hash', '2026-01-02', NULL);"
        "INSERT INTO users VALUES (9, 'legacy3', 'hash', '2026-01-03', NULL);"
        "PRAGMA user_version = 26;"
    )
    conn.commit()
    conn.close()
    return old_db


def _users_ddl(path):
    conn = sqlite3.connect(path)
    try:
        return conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='users'"
        ).fetchone()[0]
    finally:
        conn.close()


def _insert_user_without_onboarding_column(path, username):
    conn = sqlite3.connect(path)
    try:
        cur = conn.execute(
            "INSERT INTO users (username, password_hash, created) VALUES (?, ?, ?)",
            (username, "hash", "2026-02-02"),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def test_fresh_and_migrated_schemas_default_onboarding_to_incomplete(tmp_path):
    fresh = tmp_path / "fresh.db"
    db.init_db(str(fresh))
    migrated = _pre_onboarding_db(tmp_path)
    db.init_db(str(migrated))

    for path in (fresh, migrated):
        assert "onboarding_complete INTEGER NOT NULL DEFAULT 0" in _users_ddl(path)
        # A future INSERT that forgets the column must not pre-complete setup.
        uid = _insert_user_without_onboarding_column(path, "omitted")
        assert db.onboarding_complete(uid, str(path)) is False


def test_migration_28_keeps_existing_accounts_onboarded_and_intact(tmp_path):
    migrated = _pre_onboarding_db(tmp_path)

    db.init_db(str(migrated))

    conn = sqlite3.connect(migrated)
    conn.row_factory = sqlite3.Row
    try:
        rows = {r["id"]: r for r in conn.execute("SELECT * FROM users ORDER BY id")}
        indexes = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='users'"
            )
        }
    finally:
        conn.close()
    assert set(rows) == {7, 8, 9}
    assert [rows[i]["onboarding_complete"] for i in (7, 8, 9)] == [1, 1, 1]
    assert rows[7]["calendar_token_hash"] == "abc123"
    assert rows[8]["calendar_token_hash"] is None
    assert rows[9]["calendar_token_hash"] is None
    assert "idx_users_calendar_token_hash" in indexes
    # New accounts on a migrated database still start incomplete.
    new_uid = db.create_user("after-migration", "hash", str(migrated))
    assert db.onboarding_complete(new_uid, str(migrated)) is False


def test_init_db_is_rerunnable_after_the_onboarding_migration(tmp_path):
    migrated = _pre_onboarding_db(tmp_path)
    db.init_db(str(migrated))
    uid = db.create_user("fresh-account", "hash", str(migrated))
    db.complete_onboarding(7, str(migrated))

    db.init_db(str(migrated))

    conn = sqlite3.connect(migrated)
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        usernames = [r[0] for r in conn.execute("SELECT username FROM users ORDER BY id")]
    finally:
        conn.close()
    assert version == db.SCHEMA_VERSION
    assert usernames == ["legacy", "legacy2", "legacy3", "fresh-account"]
    assert db.onboarding_complete(7, str(migrated)) is True
    assert db.onboarding_complete(uid, str(migrated)) is False


def _legacy_default_one_db(tmp_path, name="legacy-default1.db"):
    """A database carrying the shipped-once ALTER ... DEFAULT 1 users DDL.

    This is the state the owner's live database is in and can never leave:
    SQLite cannot alter a column default, so the stored DDL keeps DEFAULT 1
    forever. Any INSERT INTO users that omits onboarding_complete creates an
    account with the setup wizard already closed.
    """
    path = _pre_onboarding_db(tmp_path, name)
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "ALTER TABLE users ADD COLUMN onboarding_complete "
            "INTEGER NOT NULL DEFAULT 1"
        )
        conn.execute(f"PRAGMA user_version = {db.SCHEMA_VERSION}")
        conn.commit()
    finally:
        conn.close()
    assert "DEFAULT 1" in _users_ddl(path)
    return path


def test_user_creation_paths_leave_setup_open_on_a_legacy_default_1_db(
    tmp_path, monkeypatch
):
    """Behavioural barrier: no creation path may inherit the DEFAULT 1."""
    legacy = _legacy_default_one_db(tmp_path)

    direct = db.create_user("direct", auth.hash_password("password123"), str(legacy))
    assert db.onboarding_complete(direct, str(legacy)) is False

    # And through the real registration route, on the same legacy database.
    monkeypatch.setenv("WATTRACKER_DB", str(legacy))
    with TestClient(create_app()) as registering:
        assert _users_ddl(legacy).count("DEFAULT 1") == 1  # startup kept the DDL
        registered = _register(registering, "registered")
    assert db.onboarding_complete(registered, str(legacy)) is False
    assert db.onboarding_complete(registered) is False


# --------------------------------------------------------------- source scan

_INSERT_USERS = re.compile(
    r'insert\s+(?:or\s+\w+\s+)?into\s+["\'`\[]?users["\'`\]]?\s*',
    re.IGNORECASE,
)
# Stands in for any run-time-computed piece of a string (f-string field,
# non-literal concatenation operand). Its presence inside a statement means the
# column list cannot be verified statically, which counts as a violation.
_UNKNOWN = "\x00"


def _literal_text(node):
    """Best-effort static text of a string expression, or None."""
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else None
    if isinstance(node, ast.JoinedStr):
        parts = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
            else:
                parts.append(_UNKNOWN)
        return "".join(parts)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _literal_text(node.left)
        right = _literal_text(node.right)
        if left is None and right is None:
            return None
        return (left or _UNKNOWN) + (right or _UNKNOWN)
    return None


def _docstring_nodes(tree):
    holders = (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
    found = set()
    for node in ast.walk(tree):
        if not isinstance(node, holders):
            continue
        body = getattr(node, "body", None)
        if body and isinstance(body[0], ast.Expr):
            first = body[0].value
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                found.add(id(first))
    return found


def _sql_literals(tree):
    """Yield (lineno, text) for every string expression in the module.

    Comments never reach the AST at all, and docstrings are excluded: prose
    that mentions the statement must not be able to stand in for it.
    """
    skip = _docstring_nodes(tree)
    for node in ast.walk(tree):
        if id(node) in skip:
            continue
        text = _literal_text(node)
        if text is None:
            continue
        # A composed expression reports for its operands; don't report twice.
        for child in ast.walk(node):
            if child is not node and _literal_text(child) is not None:
                skip.add(id(child))
        yield node.lineno, text


def _column_list(remainder):
    """Columns named between the balanced parens starting `remainder`, or None."""
    if not remainder.startswith("("):
        return None
    depth = 0
    for index, char in enumerate(remainder):
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                inner = remainder[1:index]
                return [c.strip().strip('"\'`[]').lower() for c in inner.split(",")]
    return None


def _user_insert_violations(path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    violations = []
    found = 0
    for lineno, text in _sql_literals(tree):
        for match in _INSERT_USERS.finditer(text):
            found += 1
            statement = text[match.start():match.start() + 200]
            columns = _column_list(text[match.end():])
            if columns is None:
                violations.append(
                    f"{path}:{lineno}: INSERT INTO users with no readable "
                    f"column list: {statement!r}"
                )
            elif any(_UNKNOWN in column for column in columns):
                violations.append(
                    f"{path}:{lineno}: INSERT INTO users with a computed "
                    f"column list: {statement!r}"
                )
            elif "onboarding_complete" not in columns:
                violations.append(
                    f"{path}:{lineno}: INSERT INTO users omits "
                    f"onboarding_complete: {statement!r}"
                )
    return found, violations


def test_every_user_insert_names_onboarding_complete():
    """Defends databases migrated while the ALTER still said DEFAULT 1.

    Parses the whole package rather than grepping: a comment, a docstring or an
    unrelated nearby line must not be able to satisfy this.
    """
    package = pathlib.Path(inspect.getfile(wattracker)).parent
    sources = sorted(
        p for p in package.rglob("*.py") if "__pycache__" not in p.parts
    )
    assert sources

    found = 0
    violations = []
    for source in sources:
        hits, problems = _user_insert_violations(source)
        found += hits
        violations.extend(problems)

    assert not violations, "\n".join(violations)
    # The barrier is only worth anything while it still sees the real inserts.
    assert found >= 1, "no INSERT INTO users found in the package"


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
    # redirect_home, not setenv: HOME alone is inert on Windows, where
    # ntpath.expanduser reads USERPROFILE. Left as setenv, the sandboxed HOME
    # stays at tmp_path/home and the folders below are siblings of it, so the
    # containment check (correctly) refuses the ones this test expects it to
    # accept.
    redirect_home(monkeypatch, os.path.realpath(tmp_path))
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
        importer, "ingest_file", lambda user_id, path, ftp=None, **kwargs: 1
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


def _spy_discovery(monkeypatch):
    """Count the setup-only discovery helpers the dashboard may call."""
    calls = {"candidates": 0, "estimate": 0}

    def candidates():
        calls["candidates"] += 1
        return []

    def estimate(user_id, *_args, **_kwargs):
        calls["estimate"] += 1
        return 241.25

    monkeypatch.setattr(paths, "annotated_candidates", candidates)
    monkeypatch.setattr(importer, "recent_best_effort_ftp", estimate)
    return calls


def test_dashboard_skips_setup_discovery_once_onboarding_is_complete(
    client, monkeypatch
):
    uid = _register(client)
    db.complete_onboarding(uid)
    calls = _spy_discovery(monkeypatch)

    response = client.get("/")

    assert response.status_code == 200
    assert calls == {"candidates": 0, "estimate": 0}
    assert "setup-wizard" not in response.text


def test_dashboard_still_runs_setup_discovery_while_onboarding_is_incomplete(
    client, monkeypatch
):
    uid = _register(client)
    assert db.onboarding_complete(uid) is False
    calls = _spy_discovery(monkeypatch)

    response = client.get("/")

    assert response.status_code == 200
    assert calls == {"candidates": 1, "estimate": 1}
    # The wizard still renders every value it derives from that work.
    assert "setup-wizard" in response.text
    assert "(241.2 W)" in response.text


def _setup_post_requests(activities_dir):
    return (
        ("/setup/check-directory", {"data": {"activities_dir": str(activities_dir)}}),
        ("/setup/upload", {
            "files": [("files", ("ride.fit", b"fit", "application/octet-stream"))]
        }),
        ("/setup/ftp", {"data": {"choice": "manual", "manual_ftp": "999"}}),
        ("/setup/complete", {"data": {
            "weight_kg": "99", "ftp_choice": "manual", "manual_ftp": "999",
            "zwiftpower": "no", "activities_dir": str(activities_dir),
        }}),
    )


def test_setup_page_redirects_to_settings_once_onboarding_is_complete(client):
    uid = _register(client)
    assert client.get("/setup").status_code == 200

    db.complete_onboarding(uid)
    response = client.get("/setup", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/settings"


def test_setup_posts_are_rejected_without_mutating_after_completion(
    client, home_dir, monkeypatch
):
    uid = _register(client)
    activities = home_dir / "Activities"
    activities.mkdir()
    client.post("/setup/complete", data={
        "weight_kg": "72", "ftp_choice": "manual", "manual_ftp": "275",
        "zwiftpower": "no",
    })
    assert db.onboarding_complete(uid) is True
    before = db.get_user_settings(uid)
    before_ftp = db.latest_ftp(uid)

    monkeypatch.setattr(
        importer,
        "ingest_upload",
        lambda *_a, **_k: pytest.fail("closed setup reached the importer"),
    )
    monkeypatch.setattr(
        credstore,
        "save_zwift_credentials",
        lambda *_a, **_k: pytest.fail("closed setup reached the credential store"),
    )

    for route, kwargs in _setup_post_requests(activities):
        response = client.post(route, follow_redirects=False, **kwargs)
        if route == "/setup/complete":
            assert response.status_code == 303, route
            assert response.headers["location"] == "/settings"
        else:
            assert response.status_code == 409, route
            assert response.headers["content-type"].startswith("application/json")
            assert response.json()["error"]

    after = db.get_user_settings(uid)
    assert after["ftp"] == before["ftp"] == pytest.approx(275)
    assert after["weight_kg"] == before["weight_kg"] == pytest.approx(72)
    assert after["activities_dir"] == before["activities_dir"] is None
    assert after["zwift_id"] == before["zwift_id"]
    assert db.latest_ftp(uid)["ftp_watts"] == before_ftp["ftp_watts"]
    assert db.list_activities(uid) == []


def test_setup_stays_resumable_for_an_incomplete_rider(client, home_dir, monkeypatch):
    uid = _register(client)
    activities = home_dir / "Activities"
    activities.mkdir()
    monkeypatch.setattr(
        importer,
        "parse_fit",
        lambda path: {"start_time": "2026-07-01T10:00:00", "duration_s": 60,
                      "streams": {"power": [200] * 60}},
    )

    assert client.get("/setup").status_code == 200
    checked = client.post(
        "/setup/check-directory", data={"activities_dir": str(activities)}
    )
    assert checked.status_code == 202
    _wait_scan(client)
    uploaded = client.post(
        "/setup/upload",
        files=[("files", ("ride.fit", b"fit", "application/octet-stream"))],
    )
    assert uploaded.status_code == 200
    assert client.post(
        "/setup/ftp", data={"choice": "manual", "manual_ftp": "255"}
    ).status_code == 200
    # Abandon and come back: the wizard is still there and still finishes.
    assert client.get("/setup").status_code == 200
    completed = client.post("/setup/complete", data={
        "weight_kg": "71", "ftp_choice": "manual", "manual_ftp": "265",
        "zwiftpower": "no",
    })

    assert completed.status_code == 200
    assert db.onboarding_complete(uid) is True
    assert db.get_user_settings(uid)["ftp"] == pytest.approx(265)


def test_closed_setup_posts_still_require_authentication(client, home_dir):
    uid = _register(client)
    activities = home_dir / "Activities"
    activities.mkdir()
    db.complete_onboarding(uid)
    client.post("/logout")

    for route, kwargs in _setup_post_requests(activities):
        response = client.post(route, follow_redirects=False, **kwargs)
        assert response.status_code == 303, route
        assert response.headers["location"] == "/login", route
    assert client.get("/setup", follow_redirects=False).headers["location"] == "/login"


def test_setup_completion_state_is_read_per_user(client, home_dir):
    finished = _register(client, "finished")
    db.complete_onboarding(finished)
    client.post("/logout")
    starting = _register(client, "starting")

    assert client.get("/setup").status_code == 200
    response = client.post("/setup/ftp", data={"choice": "manual", "manual_ftp": "245"})

    assert response.status_code == 200
    assert db.get_user_settings(starting)["ftp"] == pytest.approx(245)
    assert db.get_user_settings(finished)["ftp"] is None


def test_register_and_root_dashboard_remain_compatible(client):
    response = client.post("/register", data={"username": "compatible", "password": "password123"})
    assert response.status_code == 200
    assert "Dashboard" in response.text
    assert client.get("/").status_code == 200


# --------------------------------------------------------------- server mode
# The wizard is the first surface a new account sees, and in a split install
# every folder question in it is about the RIDER's machine. Asked on the
# server - a Linux container with no Zwift install - the candidate list came
# back empty and every typed path failed as "not found or not a directory",
# which made onboarding unfinishable. These hold the questions on the machine
# that can answer them.


@pytest.fixture()
def _hub_reset():
    connectorhub.reset()
    yield
    connectorhub.reset()


def _rpc_trace(monkeypatch):
    """Record every method the server asks the connector for."""
    calls = []
    original = remote_backend.RemoteBackend._call

    def recording(self, method, params=None, **kwargs):
        calls.append(method)
        return original(self, method, params, **kwargs)

    monkeypatch.setattr(remote_backend.RemoteBackend, "_call", recording)
    return calls


def test_wizard_asks_the_connector_for_its_folders(
    client, home_dir, monkeypatch, _hub_reset
):
    monkeypatch.setenv("WATTRACKER_MODE", "server")
    # Under the sandboxed HOME: the connector confines a submitted folder to
    # its own trusted roots, and it is right to - a tmp_path sibling is a
    # folder it would refuse in production too.
    zwift_home = home_dir / "zwift"
    (zwift_home / "Activities").mkdir(parents=True)
    (zwift_home / "Workouts").mkdir(parents=True)
    uid = _register(client)
    assert db.onboarding_complete(uid) is False

    calls = _rpc_trace(monkeypatch)
    attached, config = attach_connector(client, uid, zwift_home)
    with attached:
        wizard = client.get("/setup")
        assert wizard.status_code == 200
        assert "paths.activity_candidates" in calls, (
            "the wizard listed candidates without asking the connector, so it "
            "was reading the server's own filesystem"
        )
        calls.clear()

        # A folder that only the connector's machine can see.
        checked = client.post(
            "/setup/check-directory",
            data={"activities_dir": config.activities_dir},
        )
        assert checked.status_code == 202, checked.json()
        assert "paths.validate_dir" in calls
        assert db.get_user_settings(uid)["activities_dir"] == config.activities_dir
        _wait_scan(client)

        # And the same folder survives the form that finishes onboarding.
        completed = client.post("/setup/complete", data={
            "weight_kg": "72", "ftp_choice": "manual", "manual_ftp": "275",
            "zwiftpower": "no", "activities_dir": config.activities_dir,
        })
        assert completed.status_code == 200
        assert db.onboarding_complete(uid) is True
        assert db.get_user_settings(uid)["activities_dir"] == config.activities_dir


def test_wizard_candidates_keep_the_fit_count_over_the_wire(
    client, monkeypatch, _hub_reset
):
    """The count is what makes one candidate obviously the right one.

    Answered at the transport, not by a real connector: in-process the two
    sides share one ``paths`` module, so a candidate list built from it would
    look identical whether or not the RPC layer preserved the field. Here the
    count can only reach the page by surviving RemoteBackend's parsing.
    """
    monkeypatch.setenv("WATTRACKER_MODE", "server")
    uid = _register(client)

    def canned(self, method, params=None, **kwargs):
        assert method == "paths.activity_candidates", method
        return [{"path": r"C:\Zwift\Activities", "exists": True, "fit_count": 7}]

    monkeypatch.setattr(remote_backend.RemoteBackend, "_call", canned)
    monkeypatch.setattr(backend_mod, "is_offline", lambda user_id=None: False)
    page = client.get("/setup")

    assert page.status_code == 200
    assert r"C:\Zwift\Activities" in page.text
    assert "7 FIT files" in page.text
    assert db.onboarding_complete(uid) is False


def test_wizard_names_an_offline_connector_instead_of_showing_nothing(
    client, monkeypatch, _hub_reset
):
    monkeypatch.setenv("WATTRACKER_MODE", "server")
    uid = _register(client)
    assert db.onboarding_complete(uid) is False

    page = client.get("/setup")
    assert page.status_code == 200
    assert "connector is not attached" in page.text
    assert "No standard candidate was found" not in page.text

    # Not a 500: an offline connector is a validation answer, and the rider is
    # one page away from the pairing screen that fixes it.
    checked = client.post(
        "/setup/check-directory",
        data={"activities_dir": r"C:\Zwift\Activities"},
    )
    assert checked.status_code == 400
    assert "connector is offline" in checked.json()["error"]
    assert db.get_user_settings(uid).get("activities_dir") in (None, "")
