"""Shared pytest fixtures: isolate config + database in a temp directory."""
import os

import pytest


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    """Point the app's data dir and DB at a fresh temp location per test."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setenv("TRANALYZER_DATA_DIR", str(data_dir))
    monkeypatch.setenv("TRANALYZER_DB", str(tmp_path / "test.db"))
    # Ensure no ambient config leaks in.
    monkeypatch.setenv("TRANALYZER_SECRET", "test-secret-key")
    for key in (
        "TRANALYZER_FTP",
        "TRANALYZER_ZWIFT_ID",
        "TRANALYZER_ACTIVITIES_DIR",
        "TRANALYZER_WORKOUTS_DIR",
        "ANTHROPIC_API_KEY",
    ):
        monkeypatch.delenv(key, raising=False)
    yield


@pytest.fixture()
def user_id():
    """Create a test user and return its id."""
    from tranalyzer import auth, db

    db.init_db()
    return db.create_user("tester", auth.hash_password("password123"))
