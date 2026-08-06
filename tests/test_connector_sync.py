"""File sync driven through the real HTTP routes, with a connector attached.

test_backend_parity.py proves the two backends agree; this proves the routes
on top of them behave in server mode - including when no connector is there,
which is the state a user will actually meet first.
"""
import pytest

pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from wattracker import connectorhub, db  # noqa: E402
from wattracker.ingest import importer  # noqa: E402
from wattracker.prescribe import zwo  # noqa: E402
from wattracker.server import create_app  # noqa: E402

from conftest import redirect_home  # noqa: E402
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


@pytest.fixture()
def zwift_home(tmp_path):
    home = tmp_path / "zwift"
    (home / "Activities").mkdir(parents=True)
    (home / "Workouts").mkdir(parents=True)
    return home


def _register(client, username="rider"):
    client.post("/register", data={"username": username, "password": "password123"})
    return db.get_user_by_username(username)["id"]


def _fake_parsed(start_time="2026-06-01T10:00:00", seconds=1800):
    return {
        "start_time": start_time,
        "duration_s": seconds,
        "streams": {
            "time": [None] * seconds,
            "power": [200.0] * seconds,
            "heartrate": [140.0] * seconds,
            "cadence": [90.0] * seconds,
            "distance": list(range(seconds)),
            "altitude": [0.0] * seconds,
        },
    }


def _wait_done(client, timeout=10.0):
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        status = client.get("/api/scan/status").json()
        if not status.get("running"):
            return status
        time.sleep(0.02)
    raise AssertionError("scan did not finish in time")


def test_rescan_over_a_connector_imports_the_remote_file(
    client, zwift_home, monkeypatch
):
    monkeypatch.setenv("WATTRACKER_MODE", "server")
    redirect_home(monkeypatch, str(zwift_home.parent))
    monkeypatch.setattr(importer, "parse_fit", lambda path: _fake_parsed())
    (zwift_home / "Activities" / "ride.fit").write_bytes(b"dummy")

    uid = _register(client)
    attached, _config = attach_connector(client, uid, zwift_home)
    with attached:
        response = client.post(
            "/activities/rescan",
            data={"activities_dir": str(zwift_home / "Activities")},
        )
        assert response.status_code == 202
        status = _wait_done(client)

    assert status["exists"] is True
    assert status["imported"] == 1
    assert status["directory"] == str(zwift_home / "Activities")
    assert len(db.list_activities(uid)) == 1


def test_scan_status_exists_reflects_the_connector_not_the_server(
    client, zwift_home, monkeypatch
):
    """The folder lives on another machine, so an isdir() here is meaningless.

    The server's own filesystem has no such path; a local check would report
    "not found" for a folder that is sitting there perfectly fine.
    """
    monkeypatch.setenv("WATTRACKER_MODE", "server")
    redirect_home(monkeypatch, str(zwift_home.parent))
    uid = _register(client)
    attached, _config = attach_connector(client, uid, zwift_home)
    with attached:
        client.post(
            "/activities/rescan",
            data={"activities_dir": str(zwift_home / "Activities")},
        )
        status = _wait_done(client)
    assert status["exists"] is True
    assert status["found"] == 0  # exists, but empty


def test_pages_render_with_no_connector_attached(client, monkeypatch):
    """Settings is where you go to fix a broken connector - it must load."""
    monkeypatch.setenv("WATTRACKER_MODE", "server")
    _register(client)
    assert connectorhub.is_attached(
        db.get_user_by_username("rider")["id"]
    ) is False

    for path in ("/settings", "/activities", "/"):
        response = client.get(path)
        assert response.status_code == 200, f"{path} -> {response.status_code}"


def test_export_all_reports_offline_rather_than_failing(client, monkeypatch):
    monkeypatch.setenv("WATTRACKER_MODE", "server")
    _register(client)
    response = client.post("/plan/export-all")
    # Renders a page rather than 500ing; the download routes still work.
    assert response.status_code == 200


def test_download_routes_work_without_a_connector(client, monkeypatch):
    """The offline fallback: exporting by hand never touches a backend."""
    monkeypatch.setenv("WATTRACKER_MODE", "server")
    uid = _register(client)
    plan_id = db.create_plan(uid, "Test", "2026-08-01", 1, 8, {})
    db.add_plan_workout(
        uid, plan_id, "2026-08-01", "VO2 5x4", "vo2max", 3600, 80.0,
        "<workout_file><name>VO2 5x4</name></workout_file>",
    )
    response = client.get(f"/plan/{plan_id}/download.zip")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/")


def test_workout_prune_rules_travel_over_the_connector(
    client, zwift_home, monkeypatch
):
    """OOTO days must prune the .zwo on the connector's machine, not ours."""
    monkeypatch.setenv("WATTRACKER_MODE", "server")
    # As in the sibling tests: workouts_dir is confined to the trusted roots on
    # read, so the Zwift tree has to sit under HOME or the export is refused
    # and this reads as "prune did nothing" instead of "the folder was blocked".
    redirect_home(monkeypatch, str(zwift_home.parent))
    uid = _register(client)
    workouts = zwift_home / "Workouts"
    db.save_user_settings(uid, {"zwift_id": "12345",
                                "workouts_dir": str(workouts)})
    plan_id = db.create_plan(uid, "Test", "2026-08-01", 1, 8, {})
    db.add_plan_workout(
        uid, plan_id, "2026-08-01", "VO2 5x4", "vo2max", 3600, 80.0,
        "<workout_file><name>VO2 5x4</name></workout_file>",
    )
    from wattracker import exporter

    attached, _config = attach_connector(client, uid, zwift_home)
    written = workouts / zwo.plan_filename("2026-08-01", "VO2 5x4")
    with attached:
        result = exporter.sync_plan_exports(uid)
        assert result["status"] == "ok", result
        assert written.exists()

        # Mark the day out-of-office: the file must be pruned remotely.
        db.add_ooto_range(uid, "2026-08-01", "2026-08-01", "holiday")
        result = exporter.sync_plan_exports(uid)
        assert result["removed"] == 1, result
        assert not written.exists()


def test_reads_follow_the_folder_the_listing_actually_used(
    client, zwift_home, monkeypatch, tmp_path
):
    """The server's activities_dir override and the connector's own may differ.

    Setting one in the web UI is exactly how they come to differ, and the two
    RPCs have to agree about what is in scope. They did not: activities.list
    honoured the server's folder while activities.read only ever checked the
    connector's, so the listing offered files and then every read of them was
    refused as "outside the activities folder" - a scan that found the ride and
    imported nothing.
    """
    monkeypatch.setenv("WATTRACKER_MODE", "server")
    redirect_home(monkeypatch, str(zwift_home.parent))
    monkeypatch.setattr(importer, "parse_fit", lambda path: _fake_parsed())

    # A real folder on the connector's machine that is *not* its configured
    # one; trusted because it is inside the home directory above.
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "ride.fit").write_bytes(b"dummy")

    uid = _register(client)
    attached, config = attach_connector(client, uid, zwift_home)
    assert config.activities_dir != str(elsewhere)

    with attached:
        response = client.post(
            "/activities/rescan", data={"activities_dir": str(elsewhere)}
        )
        assert response.status_code == 202
        status = _wait_done(client)

    assert status["directory"] == str(elsewhere)
    assert status["exists"] is True
    assert status["imported"] == 1, "listed the file, then refused to read it"
    assert len(db.list_activities(uid)) == 1


def test_a_folder_the_connector_does_not_trust_is_refused_outright(
    zwift_home, monkeypatch, tmp_path
):
    """The connector answers to its own trusted roots, not the server's word.

    The rescan route already refuses an untrusted folder by asking this same
    connector to validate it, so in normal operation these handlers never see
    one. That is exactly why the check belongs here too: the route is the
    server being careful, and a server that has stopped being careful is the
    case the connector has to survive on its own. Driven directly, without a
    route in front, because that is the only way to represent it.
    """
    import asyncio

    from wattracker_connector.handlers import ConnectorConfig, build_handlers

    redirect_home(monkeypatch, str(zwift_home.parent))
    outside = tmp_path.parent / "outside-the-home"
    outside.mkdir(exist_ok=True)
    (outside / "ride.fit").write_bytes(b"dummy")

    handlers = build_handlers(
        ConnectorConfig(activities_dir=str(zwift_home / "Activities"))
    )

    listing = asyncio.run(handlers["activities.list"](directory=str(outside)))
    assert listing["exists"] is False
    assert listing["files"] == []

    with pytest.raises(ValueError):
        asyncio.run(handlers["activities.read"](
            path=str(outside / "ride.fit"), directory=str(outside)
        ))


def test_the_connectors_own_folder_needs_no_blessing_from_trusted_roots(
    zwift_home, monkeypatch, tmp_path
):
    """Whoever configured the connector was sitting at the machine.

    Its own folder is therefore trusted without a containment check - which
    also keeps a Zwift install somewhere unusual working, rather than making
    the connector refuse the folder its owner deliberately pointed it at.
    """
    import asyncio

    from wattracker_connector.handlers import ConnectorConfig, build_handlers

    redirect_home(monkeypatch, str(tmp_path / "somewhere-else"))
    activities = zwift_home / "Activities"
    (activities / "ride.fit").write_bytes(b"dummy")

    handlers = build_handlers(ConnectorConfig(activities_dir=str(activities)))
    # Both with no folder named, and with the connector's own echoed back.
    for directory in (None, str(activities)):
        listing = asyncio.run(handlers["activities.list"](directory=directory))
        assert listing["exists"] is True, directory
        assert [f["name"] for f in listing["files"]] == ["ride.fit"], directory
        read = asyncio.run(handlers["activities.read"](
            path=str(activities / "ride.fit"), directory=directory
        ))
        assert read["path"] == str(activities / "ride.fit")
