"""File sync driven through the real HTTP routes, with a connector attached.

test_backend_parity.py proves the two backends agree; this proves the routes
on top of them behave in server mode - including when no connector is there,
which is the state a user will actually meet first.
"""
import base64
import os
import pathlib

import pytest

pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from wattracker import connectorhub, db, paths  # noqa: E402
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

    Picking a second Zwift install in the web UI is exactly how they come to
    differ, and the two RPCs have to agree about what is in scope. They did
    not: activities.list honoured the server's folder while activities.read
    only ever checked the connector's, so the listing offered files and then
    every read of them was refused as "outside the activities folder" - a scan
    that found the ride and imported nothing.

    The folder here is one the connector DISCOVERED for itself, which is what
    the server may now choose between; an arbitrary folder under the
    connector's home directory is a different test below.
    """
    monkeypatch.setenv("WATTRACKER_MODE", "server")
    redirect_home(monkeypatch, str(zwift_home.parent))
    monkeypatch.setattr(importer, "parse_fit", lambda path: _fake_parsed())

    # A real Zwift Activities folder on the connector's machine that is not the
    # one it was configured with - the second-install case.
    other = zwift_home.parent / "Documents" / "Zwift" / "Activities"
    other.mkdir(parents=True)
    (other / "ride.fit").write_bytes(b"dummy")

    uid = _register(client)
    attached, config = attach_connector(client, uid, zwift_home)
    assert config.activities_dir != str(other)
    assert str(other) in paths.candidate_activities_dirs()

    with attached:
        response = client.post(
            "/activities/rescan", data={"activities_dir": str(other)}
        )
        assert response.status_code == 202
        status = _wait_done(client)

    assert status["directory"] == str(other)
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


# ------------------------------------------- the connector's trust boundary
#
# The server is not authenticated to the connector: it proves nothing, and
# whatever is attached to the socket is obeyed. The handlers therefore judge
# what they are asked to do rather than assuming a well-behaved server, and
# these drive them directly - a route in front would only prove the server is
# being careful, which is not the case under test.


def test_activities_read_only_serves_what_the_listing_offered(
    zwift_home, monkeypatch, tmp_path
):
    """``activities.read`` is not a general file-read primitive.

    It was: the check was containment under this machine's trusted roots,
    which is $HOME plus the Zwift roots, and it never consulted the ``.fit``
    filter that lives in the listing. So a server could list ~/.ssh and then
    read the private key out of it - or read the connector's own config file,
    which holds the device token in cleartext.
    """
    import asyncio

    from wattracker_connector.handlers import ConnectorConfig, build_handlers

    redirect_home(monkeypatch, str(zwift_home.parent))
    activities = zwift_home / "Activities"
    (activities / "ride.fit").write_bytes(b"dummy")
    (activities / "connector.json").write_text('{"token": "s3cret"}')
    (activities / "inprogressactivity.fit").write_bytes(b"live buffer")
    nested = activities / "sub"
    nested.mkdir()
    (nested / "deep.fit").write_bytes(b"never offered")
    ssh = zwift_home.parent / ".ssh"
    ssh.mkdir()
    (ssh / "id_ed25519").write_text("PRIVATE KEY MATERIAL")

    handlers = build_handlers(ConnectorConfig(activities_dir=str(activities)))

    # The listing offers exactly one file, and the read serves exactly that.
    listing = asyncio.run(handlers["activities.list"]())
    assert [f["name"] for f in listing["files"]] == ["ride.fit"]

    for path, directory in (
        (activities / "connector.json", None),      # not a .fit
        (activities / "inprogressactivity.fit", None),  # never a finished ride
        (nested / "deep.fit", None),                # contained, but not offered
        (ssh / "id_ed25519", str(ssh)),             # a folder the server named
    ):
        with pytest.raises(ValueError):
            asyncio.run(handlers["activities.read"](
                path=str(path), directory=directory
            ))
    # Nothing above leaked, and the legitimate file still reads.
    assert asyncio.run(handlers["activities.read"](
        path=str(activities / "ride.fit")
    ))["name"] == "ride.fit"


@pytest.mark.skipif(os.name != "posix", reason="POSIX symlinks")
def test_a_symlinked_fit_cannot_stand_in_for_another_file(
    zwift_home, monkeypatch, tmp_path
):
    """A link is resolved before it is judged, by BOTH the listing and the read.

    Otherwise the ``.fit`` filter is only a filter on the name, and anything
    on this machine can be given a .fit name inside the Activities folder.
    """
    import asyncio

    from wattracker_connector.handlers import ConnectorConfig, build_handlers

    redirect_home(monkeypatch, str(zwift_home.parent))
    activities = zwift_home / "Activities"
    secret = zwift_home.parent / "id_ed25519"
    secret.write_text("PRIVATE KEY MATERIAL")
    (activities / "sneaky.fit").symlink_to(secret)

    handlers = build_handlers(ConnectorConfig(activities_dir=str(activities)))
    listing = asyncio.run(handlers["activities.list"]())
    assert listing["files"] == []
    assert listing["skipped"] == 1
    with pytest.raises(ValueError):
        asyncio.run(handlers["activities.read"](
            path=str(activities / "sneaky.fit")
        ))


@pytest.mark.skipif(os.name != "posix", reason="POSIX symlinks")
def test_activities_read_opens_the_file_it_judged_not_the_path_it_was_given(
    zwift_home, monkeypatch
):
    """The read works on the RESOLVED path, so the check cannot be raced.

    ``_in_scope`` resolves the path before judging it, and everything after
    that - isfile, getsize, open - has to use what was judged. Opening the
    caller's path instead is a TOCTOU even when the two start out identical:
    a link that pointed at a real ride when it was checked can point at
    ``~/.ssh/id_ed25519`` by the time it is read, and the guard above becomes
    a check on a file nobody opens.

    The race is made deterministic by repointing the link from inside
    ``os.path.getsize``, which the handler calls in the window between the
    check and the open. ``swapped`` is asserted so this cannot go quiet: if
    the call ever leaves that window the test fails rather than passing on a
    race that never happened (issue #87).
    """
    import asyncio

    from wattracker_connector.handlers import ConnectorConfig, build_handlers

    redirect_home(monkeypatch, str(zwift_home.parent))
    activities = zwift_home / "Activities"
    ride = activities / "ride.fit"
    ride.write_bytes(b"a real ride")
    secret = zwift_home.parent / "id_ed25519"
    secret.write_bytes(b"PRIVATE KEY MATERIAL")
    link = activities / "offered.fit"
    link.symlink_to(ride)

    real_getsize = os.path.getsize
    swapped = []

    def swap_then_size(target):
        size = real_getsize(target)
        if not swapped:
            swapped.append(target)
            link.unlink()
            link.symlink_to(secret)
        return size

    handlers = build_handlers(ConnectorConfig(activities_dir=str(activities)))
    monkeypatch.setattr(os.path, "getsize", swap_then_size)
    result = asyncio.run(handlers["activities.read"](path=str(link)))
    monkeypatch.undo()

    assert swapped, "nothing was swapped: the race window was never entered"
    assert base64.b64decode(result["content"]) == b"a real ride"
    # The link now points at the key, and the read must not have followed it.
    assert os.path.realpath(link) == os.path.realpath(secret)


def test_a_manifest_date_cannot_write_outside_the_workouts_folder(
    zwift_home, monkeypatch, tmp_path
):
    """The date leads the .zwo filename, and it arrives over the wire.

    ``zwo.plan_filename`` interpolated it raw, which was safe only because
    every caller read it from a plan row. Reachable from a remote manifest it
    is an arbitrary-path write, and no ``..`` is even needed - an absolute
    date suffices.
    """
    import asyncio

    from wattracker_connector.handlers import ConnectorConfig, build_handlers

    redirect_home(monkeypatch, str(zwift_home.parent))
    workouts = zwift_home / "Workouts"
    victim = zwift_home.parent / "victim"
    victim.mkdir()

    handlers = build_handlers(ConnectorConfig(workouts_dir=str(workouts)))
    for hostile in (
        "../../../../.config/autostart/pwn",
        str(victim / "owned"),
        "..\\..\\evil",
        "2026-07-07/../../escape",
    ):
        result = asyncio.run(handlers["workouts.sync"](
            resolution="direct",
            write=[{"date": hostile, "name": "x", "zwo": "<workout_file/>"}],
        ))
        assert result["exported"] == 0, hostile
        assert result["paths"] == [], hostile

    assert list(victim.iterdir()) == []
    assert list(workouts.iterdir()) == []

    # A real date still exports, so the guard is a filter and not a wall.
    ok = asyncio.run(handlers["workouts.sync"](
        resolution="direct",
        write=[{"date": "2026-07-07", "name": "VO2 5x4", "zwo": "<workout_file/>"}],
    ))
    assert ok["exported"] == 1
    assert [p.name for p in workouts.iterdir()] == ["2026-07-07 VO2 5x4.zwo"]


def test_a_remove_filename_cannot_delete_outside_the_workouts_folder(
    zwift_home, monkeypatch
):
    """The ``remove`` guard, which nothing covered.

    Replacing it with ``if False:`` left the whole suite passing. It is the
    delete half of the same primitive as the write above, so it gets the same
    treatment: a name with a separator in it is refused, not joined.
    """
    import asyncio

    from wattracker_connector.handlers import ConnectorConfig, build_handlers

    redirect_home(monkeypatch, str(zwift_home.parent))
    workouts = zwift_home / "Workouts"
    treasure = zwift_home / "Activities" / "keep.fit"
    treasure.write_bytes(b"a real ride")
    (workouts / "2026-07-07 Real.zwo").write_text("<workout_file/>")

    handlers = build_handlers(ConnectorConfig(workouts_dir=str(workouts)))
    result = asyncio.run(handlers["workouts.sync"](
        resolution="direct",
        remove=[
            "../Activities/keep.fit",
            "..\\Activities\\keep.fit",
            str(treasure),
        ],
    ))
    assert result["removed"] == 0
    assert treasure.exists()

    # A bare name still removes, so the guard is on the shape and not the act.
    result = asyncio.run(handlers["workouts.sync"](
        resolution="direct", remove=["2026-07-07 Real.zwo"],
    ))
    assert result["removed"] == 1


def test_a_remove_filename_that_clears_the_extension_guard_still_cannot_traverse(
    zwift_home, monkeypatch
):
    """The basename guard, with payloads that actually reach it (issue #87).

    Every entry in the test above is a ``.fit``, so all three die one guard
    later, at the ``.zwo`` extension check that was added BELOW the basename
    check after that test was written. None of them ever reaches the basename
    guard, which is why replacing it with ``if False:`` left the whole suite
    green while ``os.path.join(target, "../sibling.zwo")`` walked straight out
    of the resolved target and deleted the rider's workout.

    So every payload here is a ``.zwo``: the extension guard passes them and
    the basename guard is the only thing left standing between them and the
    unlink. A guard in a stack needs a payload that clears every OTHER guard,
    or the test is named for it and is evidence about something else.
    """
    import asyncio

    from wattracker_connector.handlers import ConnectorConfig, build_handlers

    home = zwift_home.parent
    redirect_home(monkeypatch, str(home))
    workouts = zwift_home / "Workouts"
    player = workouts / "1234567"
    player.mkdir()
    # A real workout one level up from the target: same extension, reachable
    # only by traversing out of the folder the sync was pointed at.
    sibling = workouts / "2026-07-07 Sibling.zwo"
    sibling.write_text("<workout_file/>")
    # And one outside the Zwift folders entirely, named absolutely.
    outside = home / "2026-07-07 Outside.zwo"
    outside.write_text("<workout_file/>")
    keeper = player / "2026-07-07 Real.zwo"
    keeper.write_text("<workout_file/>")

    handlers = build_handlers(ConnectorConfig(workouts_dir=str(player)))
    result = asyncio.run(handlers["workouts.sync"](
        resolution="direct",
        remove=[
            "../2026-07-07 Sibling.zwo",
            "..\\2026-07-07 Sibling.zwo",  # a separator on Windows
            str(outside),
        ],
    ))
    assert result["status"] == "ok"
    assert result["removed"] == 0
    assert sibling.exists(), "a ../ .zwo escaped the folder the sync was given"
    assert outside.exists(), "an absolute .zwo escaped the folder the sync was given"

    # A bare .zwo in the folder itself still removes: this is a guard on the
    # shape of the name, not a wall in front of the delete.
    result = asyncio.run(handlers["workouts.sync"](
        resolution="direct", remove=[keeper.name],
    ))
    assert result["removed"] == 1
    assert not keeper.exists()


# ----------------------------------------- the folder, not just the filename
#
# B-1 and B-2 hardened what a server could name WITHIN a folder. The folder
# itself was still the server's to choose, confined only to this machine's
# trusted roots - which is the whole home directory. That is the right rule for
# a path the rider typed into their own Settings form on the machine it
# describes, and the wrong one for a path that arrived over a socket: a path
# that arrived over RPC is confined to the folder it is FOR.


def test_a_server_named_folder_cannot_turn_a_sync_into_a_delete_primitive(
    zwift_home, monkeypatch, tmp_path
):
    """``remove`` joined a bare filename onto a folder the peer chose.

    With $HOME as the confinement, that is every file the rider owns, one name
    at a time - ``.ssh/authorized_keys``, ``.zshrc``, this year's tax return.
    Sanitising the filename, which is what the previous round did, is worth
    nothing while the directory it joins onto is the peer's to name.
    """
    import asyncio

    from wattracker_connector.handlers import ConnectorConfig, build_handlers

    home = zwift_home.parent
    redirect_home(monkeypatch, str(home))
    ssh = home / ".ssh"
    ssh.mkdir()
    key = ssh / "authorized_keys"
    key.write_text("ssh-ed25519 AAAA rider@zwift")
    taxes_dir = home / "Documents" / "taxes"
    taxes_dir.mkdir(parents=True)
    taxes = taxes_dir / "2025.pdf"
    taxes.write_bytes(b"%PDF-1.7")
    zshrc = home / ".zshrc"
    zshrc.write_text("export PATH=...")
    # A .zwo outside the Zwift folders, so the refusal cannot be the extension
    # guard standing in for the folder rule.
    autostart = home / ".config" / "autostart"
    autostart.mkdir(parents=True)
    planted = autostart / "pwn.zwo"
    planted.write_text("<workout_file/>")

    handlers = build_handlers(ConnectorConfig())
    for folder, name in (
        (ssh, "authorized_keys"),
        (taxes_dir, "2025.pdf"),
        (home, ".zshrc"),
        (autostart, "pwn.zwo"),
    ):
        result = asyncio.run(handlers["workouts.sync"](
            resolution="direct", override=str(folder), remove=[name],
        ))
        assert result["status"] == "blocked", folder
        assert result["removed"] == 0, folder
        assert result["directory"] is None, folder
    assert key.exists() and taxes.exists() and zshrc.exists() and planted.exists()

    # The settings page resolves through the same rule, so it cannot report a
    # folder the sync would refuse.
    assert asyncio.run(handlers["paths.resolve_export_dir"](
        override=str(ssh)
    )) == {"directory": None, "reason": "blocked"}


def test_a_server_named_folder_cannot_be_created_outside_the_zwift_folders(
    zwift_home, monkeypatch
):
    """The write half creates its target, so it reaches folders that do not exist.

    ``write_plan_to_zwift`` makedirs() and the override is confined with
    ``must_exist=False``, which made a server-named folder a way to plant a
    file in ``~/.config/autostart`` or ``~/Library/LaunchAgents`` - directories
    the rider does not have and the peer brings into being.
    """
    import asyncio

    from wattracker_connector.handlers import ConnectorConfig, build_handlers

    home = zwift_home.parent
    redirect_home(monkeypatch, str(home))
    handlers = build_handlers(ConnectorConfig())
    workout = {"date": "2026-07-07", "name": "x", "zwo": "<workout_file/>"}

    for folder in (
        home / ".config" / "autostart",
        home / "Library" / "LaunchAgents" / "made" / "up" / "tree",
        home / ".ssh",
    ):
        result = asyncio.run(handlers["workouts.sync"](
            resolution="direct", override=str(folder), write=[workout],
        ))
        assert result["status"] == "blocked", folder
        assert result["exported"] == 0, folder
        assert not folder.exists(), f"{folder} was created by the refusal"


def test_a_zwift_workouts_folder_the_server_names_is_still_honoured(
    zwift_home, monkeypatch
):
    """The guard is a filter, not a wall.

    Choosing between player folders in the web UI is a real thing riders do,
    and it is the reason the override travels at all. What changes is that the
    choice is between Zwift Workouts folders rather than between every folder
    on the machine.
    """
    import asyncio

    from wattracker_connector.handlers import ConnectorConfig, build_handlers

    home = zwift_home.parent
    redirect_home(monkeypatch, str(home))
    player = pathlib.Path(paths.export_workouts_roots()[0]) / "1234567"
    player.mkdir(parents=True)

    handlers = build_handlers(ConnectorConfig())
    result = asyncio.run(handlers["workouts.sync"](
        resolution="direct", override=str(player),
        write=[{"date": "2026-07-07", "name": "VO2 5x4", "zwo": "<workout_file/>"}],
    ))
    assert result["status"] == "ok"
    assert result["exported"] == 1
    assert result["directory"] == str(player)
    assert (player / "2026-07-07 VO2 5x4.zwo").exists()
    assert asyncio.run(handlers["paths.resolve_export_dir"](
        override=str(player)
    ))["directory"] == str(player)


@pytest.mark.skipif(os.name != "posix", reason="POSIX symlinks")
def test_a_relocated_player_folder_is_still_a_zwift_workouts_folder(
    zwift_home, monkeypatch
):
    """``mklink /J`` onto another drive is a supported Zwift layout.

    So the scope check resolves ancestors and stops at the leaf, exactly as
    ``confine_detected_dir`` does. Resolving the final component would refuse
    the rider's own folder - the failure that made a resolver and a writer
    disagree and reach two routes as a 500.
    """
    import asyncio

    from wattracker_connector.handlers import ConnectorConfig, build_handlers

    home = zwift_home.parent
    redirect_home(monkeypatch, str(home))
    root = pathlib.Path(paths.export_workouts_roots()[0])
    elsewhere = home / "zwift-on-the-big-disk"
    elsewhere.mkdir()
    (root / "1234567").symlink_to(elsewhere)

    handlers = build_handlers(ConnectorConfig())
    result = asyncio.run(handlers["workouts.sync"](
        resolution="direct", override=str(root / "1234567"),
        write=[{"date": "2026-07-07", "name": "VO2 5x4", "zwo": "<workout_file/>"}],
    ))
    assert result["status"] == "ok"
    assert (elsewhere / "2026-07-07 VO2 5x4.zwo").exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX symlinks")
def test_a_link_into_the_workouts_root_is_still_a_zwift_workouts_folder(
    zwift_home, monkeypatch
):
    """The other half of ``within_workouts_roots``' probe pair (issue #87).

    The candidate is judged twice: once fully resolved, and once with its
    ancestors resolved but the leaf left alone. The test above covers the
    leaf-unresolved probe - a player folder junctioned OUT of the root, whose
    real path is elsewhere. This covers the fully-resolved one: a link from
    somewhere else INTO the root, whose real path is the player folder and
    whose ancestors are not under any root at all. Only the fully resolved
    probe can see it, so dropping that probe leaves this refused - and nothing
    noticed, because the two probes were only ever tested as a pair.

    It widens the predicate rather than narrowing it, which is why the check
    is safe: the connector still acts on the RESOLVED folder, so a link the
    server names buys it nothing it could not have had by naming the target.
    """
    import asyncio

    from wattracker_connector.handlers import ConnectorConfig, build_handlers

    home = zwift_home.parent
    redirect_home(monkeypatch, str(home))
    root = pathlib.Path(paths.export_workouts_roots()[0])
    player = root / "1234567"
    player.mkdir(parents=True)
    shortcut = home / "my-zwift-workouts"
    shortcut.symlink_to(player)

    # The leaf-unresolved probe is $HOME/my-zwift-workouts, which is under no
    # Workouts root - so this is the fully-resolved probe's answer alone.
    assert not paths.within_workouts_roots(str(home / "some-other-folder"))
    assert paths.within_workouts_roots(str(shortcut))

    handlers = build_handlers(ConnectorConfig())
    result = asyncio.run(handlers["workouts.sync"](
        resolution="direct", override=str(shortcut),
        write=[{"date": "2026-07-07", "name": "VO2 5x4", "zwo": "<workout_file/>"}],
    ))
    assert result["status"] == "ok"
    assert result["exported"] == 1
    # Acted on where the link really points, not on the name it was given.
    assert (player / "2026-07-07 VO2 5x4.zwo").exists()


def test_a_remove_entry_must_be_a_workout_this_sync_could_have_written(
    zwift_home, monkeypatch
):
    """Inside the Workouts folder, the extension is the remaining guard.

    A bare filename is not enough on its own: the folder it lands in is the
    rider's own Zwift data, and a peer that can name any bare filename in it
    can empty it of everything that is not a .zwo.
    """
    import asyncio

    from wattracker_connector.handlers import ConnectorConfig, build_handlers

    home = zwift_home.parent
    redirect_home(monkeypatch, str(home))
    workouts = zwift_home / "Workouts"
    notes = workouts / "notes.txt"
    notes.write_text("my intervals")
    ride = workouts / "backup.fit"
    ride.write_bytes(b"a real ride")
    (workouts / "2026-07-07 Real.zwo").write_text("<workout_file/>")

    handlers = build_handlers(ConnectorConfig(workouts_dir=str(workouts)))
    result = asyncio.run(handlers["workouts.sync"](
        resolution="direct",
        # The last two are not filenames at all: one bad entry is an entry to
        # skip, not a sync that dies and leaves the folder half-synced.
        remove=["notes.txt", "backup.fit", "2026-07-07 Real", None, {"x": 1}],
    ))
    assert result["status"] == "ok"
    assert result["removed"] == 0
    assert notes.exists() and ride.exists()

    # The workouts it did write are still removable, in either case.
    result = asyncio.run(handlers["workouts.sync"](
        resolution="direct", remove=["2026-07-07 Real.zwo"],
    ))
    assert result["removed"] == 1


def test_activities_cannot_be_pointed_at_a_folder_the_connector_did_not_choose(
    zwift_home, monkeypatch
):
    """The read side of the same bug: the FOLDER was the peer's to name.

    ``_in_scope`` constrains the file within a folder, so with $HOME as the
    folder rule any .fit anywhere under the rider's home was reachable by
    naming its parent - and ``activities.list`` answered as a
    directory-existence and symlink-target oracle for everything else.
    """
    import asyncio

    from wattracker_connector.handlers import ConnectorConfig, build_handlers

    home = zwift_home.parent
    redirect_home(monkeypatch, str(home))
    activities = zwift_home / "Activities"
    (activities / "ride.fit").write_bytes(b"dummy")
    secret = home / "secret.fit"
    secret.write_bytes(b"a ride the rider did not put in Zwift")

    handlers = build_handlers(ConnectorConfig(activities_dir=str(activities)))
    with pytest.raises(ValueError):
        asyncio.run(handlers["activities.read"](
            path=str(secret), directory=str(home)
        ))

    # A folder that exists and one that does not answer identically, so the
    # listing cannot be used to probe for either.
    answers = [
        asyncio.run(handlers["activities.list"](directory=str(probe)))
        for probe in (home, home / "does-not-exist")
    ]
    for answer in answers:
        assert answer["exists"] is False
        assert answer["files"] == []
        assert answer["skipped"] == 0

    # The connector's own folder still works, so this is scope and not refusal.
    assert [f["name"] for f in asyncio.run(handlers["activities.list"]())["files"]] \
        == ["ride.fit"]


def test_a_second_zwift_install_is_still_selectable(zwift_home, monkeypatch):
    """What the server MAY name: one of this machine's own Activities folders.

    Two installs on one PC is the case the override exists for, and the
    candidates come from this machine's own discovery, so honouring them adds
    nothing the server can reach on its own.
    """
    import asyncio

    from wattracker_connector.handlers import ConnectorConfig, build_handlers

    home = zwift_home.parent
    redirect_home(monkeypatch, str(home))
    other = home / "Documents" / "Zwift" / "Activities"
    other.mkdir(parents=True)
    (other / "other.fit").write_bytes(b"dummy")

    handlers = build_handlers(
        ConnectorConfig(activities_dir=str(zwift_home / "Activities"))
    )
    listing = asyncio.run(handlers["activities.list"](directory=str(other)))
    assert listing["directory"] == str(other)
    assert [f["name"] for f in listing["files"]] == ["other.fit"]
    assert asyncio.run(handlers["activities.read"](
        path=str(other / "other.fit"), directory=str(other)
    ))["name"] == "other.fit"


def test_the_web_ui_refuses_a_folder_the_connector_will_not_serve(
    client, zwift_home, monkeypatch
):
    """A setting that is accepted and then ignored is worse than a refusal.

    The connector answers the settings form with the rule it will enforce when
    the folder is used, so a rider whose Zwift install is somewhere unusual is
    told to configure it on the machine that holds it - which is where, under
    this trust model, it can be configured at all.
    """
    monkeypatch.setenv("WATTRACKER_MODE", "server")
    home = zwift_home.parent
    redirect_home(monkeypatch, str(home))
    elsewhere = home / "elsewhere"
    elsewhere.mkdir()

    uid = _register(client)
    attached, _config = attach_connector(client, uid, zwift_home)
    with attached:
        rescan = client.post(
            "/activities/rescan", data={"activities_dir": str(elsewhere)}
        )
        assert rescan.status_code == 400
        assert "connector" in rescan.json()["error"]

        saved = client.post(
            "/settings",
            data={"activities_dir": str(elsewhere), "workouts_dir": str(home)},
        )
        assert saved.status_code == 200
        assert "connector" in saved.text

    settings = db.get_user_settings(uid)
    assert not settings.get("activities_dir")
    assert not settings.get("workouts_dir")


@pytest.mark.skipif(os.name != "posix", reason="POSIX symlinks")
def test_a_file_the_connector_could_not_offer_is_not_reported_as_a_duplicate(
    client, zwift_home, monkeypatch, caplog
):
    """The skip is deliberate; calling it a duplicate is not.

    ``skipped`` travels to the server as a bare number and the page rendered it
    as "N duplicate(s) skipped", so a rider whose Activities folder is full of
    symlinks was told every one of their rides was already imported. The count
    now travels on its own, and the only machine that knows WHICH file names it
    in its log.
    """
    monkeypatch.setenv("WATTRACKER_MODE", "server")
    redirect_home(monkeypatch, str(zwift_home.parent))
    activities = zwift_home / "Activities"
    secret = zwift_home.parent / "id_ed25519"
    secret.write_text("PRIVATE KEY MATERIAL")
    (activities / "sneaky.fit").symlink_to(secret)

    uid = _register(client)
    attached, _config = attach_connector(client, uid, zwift_home)
    with attached, caplog.at_level("WARNING"):
        assert client.post("/activities/rescan").status_code == 202
        status = _wait_done(client)

    assert status["found"] == 1
    assert status["imported"] == 0
    assert status["skipped"] == 1
    assert status["not_offered"] == 1, "a skipped file read as a duplicate"
    assert len(db.list_activities(uid)) == 0
    assert any("sneaky.fit" in r.message for r in caplog.records), \
        "the machine that skipped it is the only one that can name it"
