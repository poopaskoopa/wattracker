"""Tests: Zwift player-folder detection, export-target resolution, auto-export."""
import os

import pytest

pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from wattracker import db, paths  # noqa: E402
from wattracker.server import create_app  # noqa: E402


def _zwift_root() -> str:
    return os.environ["WATTRACKER_ZWIFT_WORKOUTS_ROOT"]


def _mk_player_folder(zwift_id: str) -> str:
    p = os.path.join(_zwift_root(), zwift_id)
    os.makedirs(p, exist_ok=True)
    return p


@pytest.fixture()
def client():
    app = create_app()
    with TestClient(app) as c:
        yield c


def _register(client, username="rider"):
    client.post("/register", data={"username": username, "password": "password123"})


PLAN_FORM = {
    "name": "Base Plan",
    "weeks": "2",
    "hours_per_week": "6",
    "hit_days_per_week": "1",
    "start_date": "2026-08-03",
    "days": ["0", "2", "4"],
}


# ------------------------------------------------------------- detection
def test_candidate_zwift_ids_numeric_only_most_recent_first(tmp_path):
    root = tmp_path / "W"
    root.mkdir()
    for name in ("7654321", "1234567", "Downloaded", "-42", "notes.txt"):
        (root / name).mkdir() if name != "notes.txt" else (root / name).write_text("x")
    os.utime(root / "1234567", (2_000_000_000, 2_000_000_000))  # most recent
    cands = paths.candidate_zwift_ids(root=str(root))
    assert [c["zwift_id"] for c in cands] == ["1234567", "7654321"]


def test_resolve_export_dir_branches(tmp_path, home_dir, monkeypatch):
    # missing: empty root (conftest default)
    assert paths.resolve_export_dir(None, None) == (None, "missing")
    # a confined override always wins
    assert paths.resolve_export_dir("123", str(home_dir / "o")) == (
        str(home_dir / "o"), "override")
    # ...but an override outside the trusted roots is refused, not obeyed.
    assert paths.resolve_export_dir("123", str(tmp_path / "outside")) == (
        None, "blocked")
    # single candidate -> detected
    only = _mk_player_folder("1234567")
    d, reason = paths.resolve_export_dir(None, None)
    assert (d, reason) == (only, "detected")
    # matching zwift_id folder -> zwift_id (even with several candidates)
    other = _mk_player_folder("7654321")
    d, reason = paths.resolve_export_dir("7654321", None)
    assert (d, reason) == (other, "zwift_id")
    # several candidates, no id -> user must choose
    assert paths.resolve_export_dir(None, None) == (None, "choose")
    assert paths.resolve_export_dir("999", None) == (None, "choose")  # id folder absent


# ------------------------------------------------------------ auto-export
def test_plan_creation_auto_exports_to_single_player_folder(client):
    _register(client)
    folder = _mk_player_folder("1234567")
    r = client.post("/generate/plan", data=PLAN_FORM)
    assert r.status_code == 200
    assert "Auto-exported" in r.text
    files = sorted(os.listdir(folder))
    assert len(files) == 2 * 3  # weeks * days
    assert all(f.endswith(".zwo") for f in files)
    assert all(f[:4] == "2026" for f in files)  # date-named


def test_plan_creation_with_multiple_folders_asks_user_to_choose(client):
    _register(client)
    a = _mk_player_folder("1234567")
    b = _mk_player_folder("7654321")
    r = client.post("/generate/plan", data=PLAN_FORM)
    assert r.status_code == 200
    assert "Pick yours" in r.text and "/settings" in r.text
    assert os.listdir(a) == [] and os.listdir(b) == []  # never guesses


def test_plan_creation_uses_saved_zwift_id(client):
    _register(client)
    _mk_player_folder("1234567")
    mine = _mk_player_folder("7654321")
    uid = db.get_user_by_username("rider")["id"]
    db.save_user_settings(uid, {"zwift_id": "7654321"})
    r = client.post("/generate/plan", data=PLAN_FORM)
    assert "Auto-exported" in r.text
    assert len(os.listdir(mine)) == 6


def test_plan_creation_without_zwift_folders_hints_settings(client):
    _register(client)
    r = client.post("/generate/plan", data=PLAN_FORM)
    assert r.status_code == 200
    assert "No Zwift Workouts folder" in r.text


# ------------------------------------------------------- settings picker
def test_settings_page_lists_detected_player_folders(client):
    _register(client)
    _mk_player_folder("1234567")
    _mk_player_folder("7654321")
    text = client.get("/settings").text
    assert 'name="zwift_id_choice"' in text
    assert "1234567" in text and "7654321" in text


def test_settings_radio_choice_persists_zwift_id(client):
    _register(client)
    _mk_player_folder("1234567")
    r = client.post("/settings", data={"zwift_id_choice": "1234567"})
    assert r.status_code == 200
    uid = db.get_user_by_username("rider")["id"]
    assert db.get_user_settings(uid)["zwift_id"] == "1234567"


def test_settings_free_text_still_works_without_choice(client):
    _register(client)
    client.post("/settings", data={"zwift_id": "424242"})
    uid = db.get_user_by_username("rider")["id"]
    assert db.get_user_settings(uid)["zwift_id"] == "424242"
