"""Tests for the Activities rescan UX (directory input, counts, recommendations).

The rescan endpoint is asynchronous: POST /activities/rescan starts a background
scan (202) and the client polls GET /api/scan/status for progress/results.
"""
import os
import time
from pathlib import Path

import pytest

pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

import wattracker.ingest.importer as importer  # noqa: E402
from wattracker import db, paths  # noqa: E402
from wattracker.server import create_app  # noqa: E402


def _wait_done(client, timeout=10.0):
    """Poll the status endpoint until the scan finishes; return final status."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        s = client.get("/api/scan/status").json()
        if not s.get("running"):
            return s
        time.sleep(0.02)
    raise AssertionError("scan did not finish in time")


def _fake_parsed(start_time="2026-06-01T10:00:00", seconds=1800, watts=200.0):
    return {
        "start_time": start_time,
        "duration_s": seconds,
        "streams": {
            "time": [None] * seconds,
            "power": [watts] * seconds,
            "heartrate": [140.0] * seconds,
            "cadence": [90.0] * seconds,
            "distance": list(range(seconds)),
            "altitude": [0.0] * seconds,
        },
    }


@pytest.fixture()
def client():
    app = create_app()
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """A scratch tree the rescan endpoint is allowed to scan.

    POST /activities/rescan now confines the posted folder to the trusted
    storage roots (home, the OS Documents/Zwift roots, env overrides), so these
    tests put their scratch folders under HOME instead of a bare tmp_path.
    Realpath'd because the endpoint canonicalises what it stores and reports.
    """
    root = Path(os.path.realpath(tmp_path))
    monkeypatch.setenv("HOME", str(root))
    return root


def _register(client, username="rider"):
    client.post("/register", data={"username": username, "password": "password123"})


def test_annotated_candidates_have_exists_flag():
    cands = paths.annotated_candidates()
    assert cands, "expected at least one candidate"
    for c in cands:
        assert "path" in c and "exists" in c
        assert isinstance(c["exists"], bool)


def test_rescan_explicit_dir_imports_and_reports(client, home, monkeypatch):
    _register(client)
    # A directory with one .fit file (parser mocked, so contents don't matter).
    act_dir = home / "Activities"
    act_dir.mkdir()
    (act_dir / "ride.fit").write_bytes(b"dummy")
    monkeypatch.setattr(importer, "parse_fit", lambda path: _fake_parsed())

    r = client.post("/activities/rescan", data={"activities_dir": str(act_dir)})
    assert r.status_code == 202
    status = _wait_done(client)
    # Reports the scanned path and the counts.
    assert status["directory"] == str(act_dir)
    assert status["found"] == 1
    assert status["imported"] == 1

    # The activity was actually imported for this user.
    uid = db.get_user_by_username("rider")["id"]
    assert len(db.list_activities(uid)) == 1


def test_rescan_nonexistent_dir_reports_not_found(client, home):
    _register(client)
    missing = home / "does_not_exist"
    r = client.post("/activities/rescan", data={"activities_dir": str(missing)})
    assert r.status_code == 202
    status = _wait_done(client)
    assert status["exists"] is False
    assert status["directory"] == str(missing)
    # No activities imported, no error.
    uid = db.get_user_by_username("rider")["id"]
    assert db.list_activities(uid) == []


def test_rescan_empty_dir_reports_zero(client, home, monkeypatch):
    _register(client)
    empty = home / "Empty"
    empty.mkdir()
    r = client.post("/activities/rescan", data={"activities_dir": str(empty)})
    assert r.status_code == 202
    status = _wait_done(client)
    assert status["exists"] is True
    assert status["found"] == 0


def test_rescan_persists_activities_dir_setting(client, home):
    _register(client)
    act_dir = home / "MyRides"
    act_dir.mkdir()
    client.post("/activities/rescan", data={"activities_dir": str(act_dir)})
    _wait_done(client)

    uid = db.get_user_by_username("rider")["id"]
    assert db.get_user_settings(uid)["activities_dir"] == str(act_dir)

    # And it is reflected on both the Activities and Settings pages.
    assert str(act_dir) in client.get("/activities").text
    assert str(act_dir) in client.get("/settings").text


def test_scan_status_lifecycle(client, home, monkeypatch):
    _register(client)
    act_dir = home / "Activities"
    act_dir.mkdir()
    (act_dir / "ride.fit").write_bytes(b"dummy")
    monkeypatch.setattr(importer, "parse_fit", lambda path: _fake_parsed())

    # No scan yet: status reports not running.
    assert client.get("/api/scan/status").json()["running"] is False

    r = client.post("/activities/rescan", data={"activities_dir": str(act_dir)})
    assert r.status_code == 202
    started = r.json()
    assert started["running"] is True
    assert started["directory"] == str(act_dir)

    final = _wait_done(client)
    assert final["running"] is False
    assert final["finished_at"]
    assert final["imported"] == 1
    assert final["error"] is None


def test_scan_status_conflict_when_running(client, home, monkeypatch):
    _register(client)
    act_dir = home / "Activities"
    act_dir.mkdir()
    (act_dir / "ride.fit").write_bytes(b"dummy")

    # Make parsing block so the first scan is still running when we post again.
    import threading
    release = threading.Event()

    def slow_parse(path):
        release.wait(timeout=5)
        return _fake_parsed()

    monkeypatch.setattr(importer, "parse_fit", slow_parse)

    r1 = client.post("/activities/rescan", data={"activities_dir": str(act_dir)})
    assert r1.status_code == 202
    # Second post while the first is in flight -> 409 with the running status.
    r2 = client.post("/activities/rescan", data={"activities_dir": str(act_dir)})
    assert r2.status_code == 409
    assert r2.json()["running"] is True

    release.set()
    _wait_done(client)


def test_connect_enables_wal(tmp_path):
    """A fresh connect must put the DB in WAL journal mode."""
    dbfile = tmp_path / "wal.db"
    conn = db.connect(str(dbfile))
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        conn.close()
    assert mode.lower() == "wal"


def test_dashboard_responsive_during_scan(client, home, monkeypatch):
    """A long background rescan must not block dashboard reads. With WAL + a
    per-file GIL yield, GET / returns 200 well before the scan finishes."""
    _register(client)
    act_dir = home / "Activities"
    act_dir.mkdir()
    for i in range(10):
        (act_dir / f"ride{i}.fit").write_bytes(b"dummy")

    # ~60ms of pure-Python parse work per file (10 files -> ~0.6s scan).
    def slow_parse(path):
        time.sleep(0.06)
        return _fake_parsed(start_time=f"2026-06-{int(path[-5]) + 1:02d}T10:00:00")

    monkeypatch.setattr(importer, "parse_fit", slow_parse)

    r = client.post("/activities/rescan", data={"activities_dir": str(act_dir)})
    assert r.status_code == 202

    # While the scan is still running, the dashboard must answer promptly.
    assert client.get("/api/scan/status").json()["running"] is True
    t0 = time.time()
    resp = client.get("/")
    elapsed = time.time() - t0
    assert resp.status_code == 200
    # Still running (the scan is far from done) and the read was fast.
    assert client.get("/api/scan/status").json()["running"] is True
    assert elapsed < 1.0, f"dashboard blocked for {elapsed:.2f}s during scan"

    _wait_done(client)


def test_activities_page_prefills_recommended_when_unset(client):
    _register(client)
    text = client.get("/activities").text
    # Top recommended candidate appears as helper text / prefill.
    top = paths.candidate_activities_dirs()[0]
    assert top in text
