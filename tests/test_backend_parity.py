"""The two backends must be indistinguishable from upstream.

Runs the same scenario twice against the same Zwift folder - once with the
app reading it directly (local mode), once with a connector reading it over a
socket (server mode) - and asserts the database ends up in the same state.

Two users in one database rather than two databases, because everything in
this app is user-scoped: it makes "identical" a straight comparison of rows
instead of a comparison of two files that differ in ways that do not matter.
"""
import pytest

pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from wattracker import auth, backend, connectorhub, db  # noqa: E402
from wattracker.backend import ExportManifest, get_backend  # noqa: E402
from wattracker.ingest import importer  # noqa: E402
from wattracker.prescribe import zwo  # noqa: E402
from wattracker.server import create_app  # noqa: E402

from conftest_connector import attach_connector  # noqa: E402


@pytest.fixture()
def client():
    app = create_app()
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def _clean_hub():
    connectorhub.reset()
    yield
    connectorhub.reset()


def _fake_parsed(start_time, seconds=1800, watts=200.0):
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
def zwift_home(tmp_path):
    """A Zwift-shaped tree the app is actually allowed to read and write.

    Under the sandboxed HOME (``tmp_path/home``), not a sibling of it:
    activities_dir/workouts_dir are confined to the trusted roots on read as
    well as on write, so a bare tmp_path sibling is refused and the export
    silently lands nowhere - which shows up as an inscrutable "the .zwo is not
    there" rather than as a containment error. See the ``home_dir`` fixture.
    """
    home = tmp_path / "home" / "zwift"
    (home / "Activities").mkdir(parents=True)
    (home / "Workouts").mkdir(parents=True)
    return home


def _activity_fingerprint(uid):
    """Everything about a user's activities except ids and user scoping."""
    rows = []
    for a in db.list_activities(uid):
        rows.append(
            (
                a.get("filename"),
                a.get("start_time"),
                a.get("duration_s"),
                round(float(a.get("tss") or 0), 3),
                round(float(a.get("np") or 0), 3),
            )
        )
    return sorted(rows)


def test_scan_lands_identical_rows_either_way(
    client, zwift_home, monkeypatch
):
    monkeypatch.setattr(
        importer, "parse_fit",
        lambda path: _fake_parsed("2026-06-01T10:00:00"),
    )
    activities = zwift_home / "Activities"
    (activities / "ride.fit").write_bytes(b"dummy")
    # Must be filtered by whichever side does the listing.
    (activities / "inProgressActivity.fit").write_bytes(b"dummy")

    local_uid = db.create_user("localrider", auth.hash_password("password123"))
    remote_uid = db.create_user("remoterider", auth.hash_password("password123"))

    # --- local: the app reads the folder itself
    monkeypatch.setenv("WATTRACKER_MODE", "local")
    local_result = importer.scan_activities(local_uid, directory=str(activities))

    # --- remote: a connector reads it and ships the bytes
    monkeypatch.setenv("WATTRACKER_MODE", "server")
    attached, _config = attach_connector(client, remote_uid, zwift_home)
    with attached:
        remote_result = importer.scan_activities(
            remote_uid, directory=str(activities)
        )

    for key in ("found", "imported", "skipped", "exists"):
        assert local_result[key] == remote_result[key], (
            f"{key} differs: {local_result[key]} vs {remote_result[key]}"
        )
    assert local_result["imported"] == 1
    assert local_result["skipped"] == 1  # the in-progress buffer

    assert _activity_fingerprint(local_uid) == _activity_fingerprint(remote_uid)
    # Including the filename, which is what tells an imported ride from an
    # in-app one - the connector must not leak a temp file's name into it.
    assert db.list_activities(remote_uid)[0]["filename"] == "ride.fit"

    # The rescan cache is keyed the same way on both sides.
    assert sorted(db.seen_files(local_uid)) == sorted(db.seen_files(remote_uid))


def test_rescan_is_incremental_either_way(client, zwift_home, monkeypatch):
    calls = {"n": 0}

    def counting(path):
        calls["n"] += 1
        return _fake_parsed("2026-06-01T10:00:00")

    monkeypatch.setattr(importer, "parse_fit", counting)
    activities = zwift_home / "Activities"
    (activities / "ride.fit").write_bytes(b"dummy")
    uid = db.create_user("rider", auth.hash_password("password123"))

    monkeypatch.setenv("WATTRACKER_MODE", "server")
    attached, _config = attach_connector(client, uid, zwift_home)
    with attached:
        first = importer.scan_activities(uid, directory=str(activities))
        assert first["imported"] == 1 and calls["n"] == 1

        # Unchanged mtime+size: not parsed, and not even transferred.
        second = importer.scan_activities(uid, directory=str(activities))
        assert second["imported"] == 0
        assert second["skipped"] == 1
        assert calls["n"] == 1, "an unchanged file was re-parsed over the wire"


def test_export_writes_the_same_files_either_way(
    client, zwift_home, monkeypatch
):
    workouts = zwift_home / "Workouts"
    uid = db.create_user("rider", auth.hash_password("password123"))
    manifest = ExportManifest(
        zwift_id="12345",
        override=str(workouts),
        write=[{"date": "2026-08-01", "name": "VO2 5x4",
                "zwo": "<workout_file><name>VO2 5x4</name></workout_file>"}],
        resolution="direct",
    )
    expected = workouts / zwo.plan_filename("2026-08-01", "VO2 5x4")

    monkeypatch.setenv("WATTRACKER_MODE", "local")
    local_result = get_backend(uid).apply_exports(manifest)
    assert expected.exists()
    local_bytes = expected.read_bytes()
    expected.unlink()

    monkeypatch.setenv("WATTRACKER_MODE", "server")
    attached, _config = attach_connector(client, uid, zwift_home)
    with attached:
        remote_result = get_backend(uid).apply_exports(manifest)
    assert expected.exists()
    assert expected.read_bytes() == local_bytes

    for key in ("status", "exported", "removed", "directory", "paths"):
        assert local_result[key] == remote_result[key], f"{key} differs"


def test_prune_removes_the_same_files_either_way(
    client, zwift_home, monkeypatch
):
    workouts = zwift_home / "Workouts"
    uid = db.create_user("rider", auth.hash_password("password123"))
    filename = zwo.plan_filename("2026-08-01", "VO2 5x4")
    manifest = ExportManifest(
        zwift_id="12345", override=str(workouts),
        remove=[filename], resolution="direct",
    )

    for mode in ("local", "server"):
        (workouts / filename).write_text("<workout_file/>")
        monkeypatch.setenv("WATTRACKER_MODE", mode)
        if mode == "local":
            result = get_backend(uid).apply_exports(manifest)
        else:
            attached, _config = attach_connector(client, uid, zwift_home)
            with attached:
                result = get_backend(uid).apply_exports(manifest)
        assert result["removed"] == 1, mode
        assert not (workouts / filename).exists(), mode


def test_offline_export_degrades_instead_of_raising(monkeypatch):
    """No connector attached: a clear status, not a 500."""
    db.init_db()
    uid = db.create_user("rider", auth.hash_password("password123"))
    monkeypatch.setenv("WATTRACKER_MODE", "server")
    result = get_backend(uid).apply_exports(
        ExportManifest(zwift_id=None, write=[], remove=[])
    )
    assert result["status"] == "offline"
    assert result["exported"] == 0
    assert backend.is_offline(uid) is True


def test_offline_discovery_degrades_for_page_rendering(monkeypatch):
    """Settings must load without a connector - it is where you go to fix one."""
    db.init_db()
    uid = db.create_user("rider", auth.hash_password("password123"))
    monkeypatch.setenv("WATTRACKER_MODE", "server")
    assert backend.discover(uid, "activity_candidates") == []
    assert backend.discover(uid, "zwift_id_candidates") == []
    assert backend.discover(uid, "default_activities_dir") is None
    assert backend.discover(uid, "workouts_root") is None


@pytest.mark.parametrize("blank", ["", "   "])
def test_offline_validate_dir_of_empty_matches_local(monkeypatch, blank):
    """An empty folder field means "unchanged" and must not need a connector.

    Both backends owe the same ("", None) here. The remote one used to ask the
    connector anyway, so with none attached it raised ConnectorUnavailable.
    """
    db.init_db()
    uid = db.create_user("rider", auth.hash_password("password123"))

    monkeypatch.setenv("WATTRACKER_MODE", "local")
    assert get_backend(uid).validate_dir(blank) == ("", None)

    monkeypatch.setenv("WATTRACKER_MODE", "server")
    assert get_backend(uid).validate_dir(blank) == ("", None)


def test_offline_settings_save_is_not_a_500(client, monkeypatch):
    """Saving settings without a connector must work - pairing lives on that page.

    settings_save validates both folder fields unconditionally, so a save that
    only touches FTP used to 500 in server mode purely because no connector was
    attached to answer for two fields the user left alone.
    """
    client.post("/register", data={"username": "rider", "password": "password123"})
    uid = db.get_user_by_username("rider")["id"]
    monkeypatch.setenv("WATTRACKER_MODE", "server")

    response = client.post("/settings", data={"ftp": "250"})

    assert response.status_code == 200
    assert db.get_user_settings(uid)["ftp"] == 250


def test_offline_validate_dir_of_a_real_folder_is_an_answer_not_a_500(
    client, monkeypatch
):
    """"The connector is offline" is a validation result, not a server error.

    RemoteBackend.validate_dir short-circuited only the empty string, so any
    non-empty folder value raised ConnectorUnavailable straight out of the
    route. That is the first-run order of operations - pair a device, set your
    folders, then start the connector - which made a 500 the likely first
    experience of server mode, on both the settings save and the rescan.
    """
    client.post("/register", data={"username": "rider", "password": "password123"})
    uid = db.get_user_by_username("rider")["id"]
    monkeypatch.setenv("WATTRACKER_MODE", "server")

    clean, error = get_backend(uid).validate_dir("C:/Zwift/Activities")
    assert clean is None
    assert "offline" in error.lower()

    response = client.post(
        "/settings", data={"ftp": "250", "activities_dir": "C:/Zwift/Activities"}
    )
    assert response.status_code == 200
    response = client.post(
        "/activities/rescan", json={"directory": "C:/Zwift/Activities"}
    )
    assert response.status_code < 500


@pytest.mark.parametrize("hostile", ["\x00", "/tmp/x\x00y"])
def test_local_validate_dir_does_not_raise_on_an_unresolvable_path(
    client, monkeypatch, hostile
):
    """LocalBackend.validate_dir is paths.confine_storage_dir, not a copy of it.

    It started as a copy and had already drifted: the copy let realpath raise
    on an embedded NUL, so POST /settings with one returned 200 on main and
    500 here. Confinement was never bypassed - but "moved behind an interface,
    unchanged" has to be true, and one rule for a submitted path means one
    implementation of it.
    """
    client.post("/register", data={"username": "rider", "password": "password123"})
    uid = db.get_user_by_username("rider")["id"]
    monkeypatch.setenv("WATTRACKER_MODE", "local")

    clean, error = get_backend(uid).validate_dir(hostile)
    assert clean is None
    assert error  # refused with a message, not an exception

    response = client.post("/settings", data={"ftp": "250", "activities_dir": hostile})
    assert response.status_code == 200
