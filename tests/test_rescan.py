"""Tests for the Activities rescan UX (directory input, counts, recommendations)."""
import pytest

pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

import tranalyzer.ingest.importer as importer  # noqa: E402
from tranalyzer import db, paths  # noqa: E402
from tranalyzer.server import create_app  # noqa: E402


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


def _register(client, username="rider"):
    client.post("/register", data={"username": username, "password": "password123"})


def test_annotated_candidates_have_exists_flag():
    cands = paths.annotated_candidates()
    assert cands, "expected at least one candidate"
    for c in cands:
        assert "path" in c and "exists" in c
        assert isinstance(c["exists"], bool)


def test_rescan_explicit_dir_imports_and_reports(client, tmp_path, monkeypatch):
    _register(client)
    # A directory with one .fit file (parser mocked, so contents don't matter).
    act_dir = tmp_path / "Activities"
    act_dir.mkdir()
    (act_dir / "ride.fit").write_bytes(b"dummy")
    monkeypatch.setattr(importer, "parse_fit", lambda path: _fake_parsed())

    r = client.post("/activities/rescan", data={"activities_dir": str(act_dir)})
    assert r.status_code == 200
    # Reports the scanned path and the counts.
    assert str(act_dir) in r.text
    assert "1 .fit\n" in r.text or "1 .fit" in r.text
    assert "1 imported" in r.text

    # The activity was actually imported for this user.
    uid = db.get_user_by_username("rider")["id"]
    assert len(db.list_activities(uid)) == 1


def test_rescan_nonexistent_dir_reports_not_found(client, tmp_path):
    _register(client)
    missing = tmp_path / "does_not_exist"
    r = client.post("/activities/rescan", data={"activities_dir": str(missing)})
    assert r.status_code == 200
    assert "not found" in r.text
    assert str(missing) in r.text
    # No activities imported, no error.
    uid = db.get_user_by_username("rider")["id"]
    assert db.list_activities(uid) == []


def test_rescan_empty_dir_reports_zero(client, tmp_path, monkeypatch):
    _register(client)
    empty = tmp_path / "Empty"
    empty.mkdir()
    r = client.post("/activities/rescan", data={"activities_dir": str(empty)})
    assert r.status_code == 200
    assert "no .fit files found" in r.text


def test_rescan_persists_activities_dir_setting(client, tmp_path):
    _register(client)
    act_dir = tmp_path / "MyRides"
    act_dir.mkdir()
    client.post("/activities/rescan", data={"activities_dir": str(act_dir)})

    uid = db.get_user_by_username("rider")["id"]
    assert db.get_user_settings(uid)["activities_dir"] == str(act_dir)

    # And it is reflected on both the Activities and Settings pages.
    assert str(act_dir) in client.get("/activities").text
    assert str(act_dir) in client.get("/settings").text


def test_activities_page_prefills_recommended_when_unset(client):
    _register(client)
    text = client.get("/activities").text
    # Top recommended candidate appears as helper text / prefill.
    top = paths.candidate_activities_dirs()[0]
    assert top in text
