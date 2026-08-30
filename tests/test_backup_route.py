"""Tests for the /settings/backup web route."""
import os

import pytest

pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from wattracker import backup  # noqa: E402
from wattracker.server import create_app  # noqa: E402


@pytest.fixture()
def client():
    app = create_app()
    with TestClient(app) as c:
        yield c


def test_backup_route_requires_auth(client):
    # Registered and logged straight back out: with no account at all the
    # guard sends the visitor to the first-run wizard instead, which is a
    # different assertion (tests/test_first_run.py). What this test is about
    # is that the route refuses an unauthenticated caller.
    client.post("/register", data={"username": "alice", "password": "password123"})
    client.post("/logout")
    r = client.post("/settings/backup", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"
    assert backup.list_backups() == []


def test_backup_route_creates_file(client):
    client.post("/register", data={"username": "alice", "password": "password123"})
    before = len(backup.list_backups())
    r = client.post("/settings/backup")
    assert r.status_code == 200
    listed = backup.list_backups()
    assert len(listed) == before + 1
    assert listed[0]["reason"] == "manual"
    # The settings page reflects the new backup.
    assert "Backup created" in r.text
    assert "manual" in r.text


def test_settings_page_shows_backup_section(client):
    client.post("/register", data={"username": "bob", "password": "password123"})
    r = client.get("/settings")
    assert r.status_code == 200
    assert "Backups" in r.text
    restore_command = "wattracker-restore" if os.name == "nt" else "restore_backup"
    assert restore_command in r.text  # restore CLI command shown
