"""The connector noticing a finished ride, and the server acting on it.

Three separable pieces, tested apart because they fail apart: the watcher's
settle-and-report rule (pure, no loop), the client holding the news across a
disconnect, and the server turning the event into a scan.
"""
import os
import sys
import time

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(__file__))

from wattracker import connectorhub, db  # noqa: E402
from wattracker.ingest import importer  # noqa: E402
from wattracker import server as servermod  # noqa: E402
from wattracker.server import create_app  # noqa: E402
from wattracker_connector import watcher as watchmod  # noqa: E402
from wattracker_connector.handlers import ConnectorConfig  # noqa: E402

from conftest import redirect_home  # noqa: E402
from conftest_connector import attach_connector  # noqa: E402


@pytest.fixture()
def activities(tmp_path):
    folder = tmp_path / "zwift" / "Activities"
    folder.mkdir(parents=True)
    return folder


def _watcher(activities):
    return watchmod.ActivityWatcher(
        ConnectorConfig(activities_dir=str(activities), workouts_dir=None)
    )


def _write(path, size=64):
    path.write_bytes(b"x" * size)
    return path


# --------------------------------------------------------- the settle rule
def test_the_first_pass_always_reports(activities):
    """A connector that just started has no memory, so everything is news.

    This is what makes starting the connector the cold-start trigger: a ride
    ridden while it was down reaches the server without the server needing a
    second mechanism to notice the attach.
    """
    watch = _watcher(activities)
    assert watch.poll() is True
    # And having reported, an unchanged folder is not news again.
    assert watch.poll() is False


def test_a_new_file_is_not_reported_until_it_stops_changing(activities):
    """Zwift writes a .fit over several seconds; half of one will not parse."""
    watch = _watcher(activities)
    watch.poll()  # cold start, folder empty

    ride = _write(activities / "ride.fit", size=10)
    assert watch.poll() is False, "reported a file first seen this pass"

    _write(ride, size=200)
    assert watch.poll() is False, "reported a file that was still growing"

    # Unchanged between two passes: now it counts.
    assert watch.poll() is True
    # Reported once, not once per pass thereafter.
    assert watch.poll() is False


def test_zwifts_in_progress_buffer_is_never_reported(activities):
    """It changes every second of every ride, and is never offered anyway."""
    watch = _watcher(activities)
    watch.poll()

    buffer = activities / "inprogressactivity.fit"
    for size in (10, 20, 20, 30, 30):
        _write(buffer, size=size)
        assert watch.poll() is False, "the live recording buffer was reported"


def test_files_the_listing_would_not_offer_are_never_reported(activities):
    """The watcher's predicate has to be the listing's, or it cries wolf.

    A file the listing refuses to hand over cannot be imported however often
    the server is told to look - so reporting one would mean a scan that
    imports nothing, on every single pass, forever.
    """
    watch = _watcher(activities)
    watch.poll()

    for name in ("notes.txt", "id_ed25519", "connector.json"):
        _write(activities / name)
    assert watch.poll() is False
    assert watch.poll() is False


def test_a_symlink_pointing_out_of_the_folder_is_never_reported(
    activities, tmp_path
):
    """The name is only half the listing's predicate, and half is not enough.

    The listing also resolves the path and requires the target to sit directly
    in the folder (handlers._in_scope), so a link to a .fit kept elsewhere is
    skipped there - while a watcher testing the name alone reports it on every
    settle. That is the cries-wolf case reached through the half that was not
    shared. handlers.py documents a link-filled Activities folder as
    supported-but-degraded, so it is reachable rather than theoretical.
    """
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    target = _write(outside / "real.fit")
    try:
        os.symlink(target, activities / "linked.fit")
    except (OSError, NotImplementedError):
        pytest.skip("this platform will not create symlinks unprivileged")

    watch = _watcher(activities)
    watch.poll()  # cold pass; always reports

    # Settle the target repeatedly. None of it is news, because none of it
    # could ever be imported.
    for size in (128, 256, 512):
        _write(target, size=size)
        watch.poll()                 # sees the change, not settled yet
        assert watch.poll() is False, \
            "a symlink out of the folder was reported as news"

    # And the rule is not simply "never report": a real file still is.
    _write(activities / "ride.fit")
    watch.poll()
    assert watch.poll() is True


def test_the_watcher_asks_the_listing_whether_a_file_is_in_scope(
    activities, monkeypatch
):
    """The containment half, asserted without needing symlink privileges.

    Windows will not create a symlink unprivileged, so the test above skips on
    the machine this feature actually runs on. This one states the same
    property directly: whatever _in_scope refuses, the watcher does not report.
    """
    _write(activities / "ride.fit")
    watch = _watcher(activities)
    watch.poll()  # cold pass

    asked = []

    def refuse(directory, path):
        asked.append(path)
        return False

    monkeypatch.setattr(watchmod, "_in_scope", refuse)
    _write(activities / "ride2.fit")
    assert watch.poll() is False
    assert watch.poll() is False
    assert asked, "the watcher never consulted the listing's scope test"


def test_a_removed_file_is_not_news_but_is_forgotten(activities):
    """Nothing to import in a deletion - but the name must be reusable."""
    watch = _watcher(activities)
    watch.poll()
    ride = _write(activities / "ride.fit", size=64)
    watch.poll()
    assert watch.poll() is True  # settled and reported

    ride.unlink()
    assert watch.poll() is False, "a deletion asked the server to scan"
    assert watch.poll() is False

    # Same name, same size, back again: news a second time, because the
    # bookkeeping let go of it when it went away.
    _write(ride, size=64)
    watch.poll()
    assert watch.poll() is True


def test_a_folder_that_is_not_there_yet_is_not_an_error(tmp_path):
    """Zwift may never have run on this machine. Poll must not raise."""
    missing = tmp_path / "nope" / "Activities"
    watch = watchmod.ActivityWatcher(
        ConnectorConfig(activities_dir=str(missing), workouts_dir=None)
    )
    assert watch.poll() is True   # cold start
    assert watch.poll() is False  # and nothing thereafter


def test_the_same_folder_named_twice_is_watched_once(activities, monkeypatch):
    """activities_scope can repeat a folder; the log should not."""
    monkeypatch.setattr(
        watchmod, "activities_scope",
        lambda config: [str(activities), str(activities), str(activities)],
    )
    watch = _watcher(activities)
    assert watch.folders() == [str(activities)]


# ------------------------------------------------------------- the interval
@pytest.mark.parametrize(
    "given,expected",
    [
        (None, watchmod.DEFAULT_INTERVAL_S),
        (30.0, 30.0),
        (0, 0.0),            # explicitly off
        (-5, 0.0),           # so is anything below it
        (0.01, watchmod.MIN_INTERVAL_S),   # a typo, not a request
        ("banana", watchmod.DEFAULT_INTERVAL_S),
        (float("inf"), watchmod.DEFAULT_INTERVAL_S),
    ],
)
def test_the_interval_is_taken_from_config_but_never_trusted(given, expected):
    """The config file is hand-editable, so every value here is possible."""
    assert watchmod.normalize_interval(given) == expected


def test_turning_the_watcher_off_starts_no_task(activities):
    """0 means the daily sweep is the only thing that imports rides."""
    from wattracker_connector.client import Connector

    connector = Connector(
        server_url="http://server.invalid:8000", token="t",
        config=ConnectorConfig(activities_dir=str(activities), workouts_dir=None),
        scan_interval=0,
    )
    assert connector.scan_interval == 0.0
    assert connector._start_activity_watch() is None


# ------------------------------------------- holding the news while offline
def _run(coro):
    import asyncio

    return asyncio.run(coro)


def test_a_change_noticed_while_offline_goes_out_on_the_next_connection(
    activities,
):
    """The whole reason the flag is separate from the poll.

    A ride finished during a server restart is seen by the watcher at the
    time, with no socket to report it on. Dropping it there would mean waiting
    for the next ride - or the daily sweep - to mention it.
    """
    from wattracker_connector.client import Connector

    connector = Connector(
        server_url="http://server.invalid:8000", token="t",
        config=ConnectorConfig(activities_dir=str(activities), workouts_dir=None),
    )
    connector._activities_dirty = True

    # No peer: nothing can be sent, and the flag must survive that.
    _run(connector._flush_activity_signal())
    assert connector._activities_dirty is True

    sent = []

    class _Peer:
        async def send_event(self, event, **fields):
            sent.append((event, fields))

    connector._peer = _Peer()
    _run(connector._flush_activity_signal())
    assert sent == [("activities.changed", {})]
    assert connector._activities_dirty is False

    # And having been told, the server is not told again for nothing.
    _run(connector._flush_activity_signal())
    assert len(sent) == 1


def test_a_send_that_fails_keeps_the_news_for_next_time(activities):
    """An event has no acknowledgement, so a failed send is all we get."""
    from wattracker_connector.client import Connector

    connector = Connector(
        server_url="http://server.invalid:8000", token="t",
        config=ConnectorConfig(activities_dir=str(activities), workouts_dir=None),
    )
    connector._activities_dirty = True

    class _BrokenPeer:
        async def send_event(self, event, **fields):
            raise RuntimeError("the socket went away")

    connector._peer = _BrokenPeer()
    _run(connector._flush_activity_signal())
    assert connector._activities_dirty is True


# ------------------------------------------------- the server acting on it
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


def _distinct_parser():
    """A parse_fit stub giving each filename its own ride.

    Every file returning the same start_time and duration would dedupe against
    the first, so a second import would silently be a no-op and a test meant to
    prove a scan ran would pass whether or not it did.
    """
    seen = {}

    def parse(path):
        name = os.path.basename(str(path))
        index = seen.setdefault(name, len(seen))
        return _fake_parsed(start_time=f"2026-06-{index + 1:02d}T10:00:00")

    return parse


def _wait_for(predicate, timeout=10.0, what="the expected state"):
    """Wait on the OUTCOME, not on the scan's own progress flags.

    A scan runs in its own thread and can be over before the first poll, so
    "wait until running goes false" reads an idle status as a finished one and
    passes whether or not anything ever ran. What each test here actually
    means is "until the ride is in the database".
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what}")


def _wait_idle(client, timeout=5.0):
    """Let the scan thread finish before the test ends.

    The status is cleared to ``running: False`` only after the thread has
    deregistered itself, so this is also what keeps the suite's
    straggler-thread assertion honest.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        status = client.get("/api/scan/status").json() or {}
        if not status.get("running"):
            return status
        time.sleep(0.02)
    raise AssertionError("a scan was still running when the test ended")


def _rider_with_a_ride(client, zwift_home, monkeypatch, filename="ride.fit"):
    monkeypatch.setenv("WATTRACKER_MODE", "server")
    redirect_home(monkeypatch, str(zwift_home.parent))
    monkeypatch.setattr(importer, "parse_fit", _distinct_parser())
    uid = _register(client)
    db.save_user_settings(uid, {"activities_dir": str(zwift_home / "Activities")})
    (zwift_home / "Activities" / filename).write_bytes(b"dummy")
    return uid


def test_the_connectors_event_imports_the_ride_without_anyone_asking(
    client, zwift_home, monkeypatch
):
    """The point of the whole change: finish a ride, and it turns up."""
    uid = _rider_with_a_ride(client, zwift_home, monkeypatch)

    attached, _config = attach_connector(client, uid, zwift_home)
    with attached as connector:
        connector.send_event("activities.changed")
        _wait_for(lambda: len(db.list_activities(uid)) == 1,
                  what="the ride to be imported")
        _wait_idle(client)


def test_a_second_event_straight_away_does_not_start_a_second_scan(
    client, zwift_home, monkeypatch
):
    """A connector in a crash loop reconnects - and flushes - every few seconds.

    The rate limit is what stops that turning into a scan per reconnect. The
    Rescan button stays exempt; see the test below.
    """
    uid = _rider_with_a_ride(client, zwift_home, monkeypatch)

    attached, _config = attach_connector(client, uid, zwift_home)
    with attached as connector:
        connector.send_event("activities.changed")
        _wait_for(lambda: len(db.list_activities(uid)) == 1,
                  what="the first ride to be imported")
        _wait_idle(client)

        # A second ride lands, but the limit has not elapsed. The scan does
        # not RUN now, which is all the limit promises. The news is not lost
        # either - see the test below for that half.
        (zwift_home / "Activities" / "ride2.fit").write_bytes(b"dummy2")
        connector.send_event("activities.changed")
        time.sleep(0.3)
        assert len(db.list_activities(uid)) == 1, \
            "a second scan ran inside the rate limit"
        _wait_idle(client)


def test_an_event_the_limit_refused_still_gets_its_scan(
    client, zwift_home, monkeypatch
):
    """The limit bounds how often a scan RUNS, not whether the news survives.

    The connector names a finished file exactly once - the watcher records it
    in _reported and never raises it again - and an event is fire-and-forget,
    so a refusal reaches nobody who could resend it. Drop the request and that
    ride waits for the daily sweep, which is the failure this whole feature
    exists to remove.

    Nor is it a rare window. The connect-time flush is off the poll grid, so
    any disconnect/reconnect lands an event seconds after the previous one,
    and under the README's own --scan-interval 30 roughly every second genuine
    event would arrive inside the limit.
    """
    monkeypatch.setattr(servermod, "AUTO_SCAN_MIN_INTERVAL_S", 0.5)
    uid = _rider_with_a_ride(client, zwift_home, monkeypatch)

    attached, _config = attach_connector(client, uid, zwift_home)
    with attached as connector:
        connector.send_event("activities.changed")
        _wait_for(lambda: len(db.list_activities(uid)) == 1,
                  what="the first ride to be imported")
        _wait_idle(client)

        (zwift_home / "Activities" / "ride2.fit").write_bytes(b"dummy2")
        connector.send_event("activities.changed")

        # No second event and no Rescan: the deferred replay is the only
        # thing that can import this, which is what makes the test honest.
        _wait_for(lambda: len(db.list_activities(uid)) == 2,
                  what="the deferred scan to import the second ride")
        _wait_idle(client)


def test_news_arriving_during_a_running_scan_is_owed_not_lost(monkeypatch):
    """The slot is spent before we discover a scan is already running.

    Without deferring, this news is destroyed exactly as a rate-limited event
    is - and the scan already running was started before this file landed, so
    it is not the one that is going to find it.
    """
    servermod.reset_auto_work_limits()
    monkeypatch.setattr(servermod, "_start_user_scan", lambda *a, **k: None)
    try:
        started = servermod._start_auto_scan(
            4242, "the connector reported new files"
        )
        assert started is False
        assert 4242 in servermod._auto_scan_owed, \
            "news arriving during a running scan was dropped"
    finally:
        servermod.reset_auto_work_limits()


def test_the_rescan_button_is_never_rate_limited(
    client, zwift_home, monkeypatch
):
    """A rider who presses it is entitled to a scan, whatever just happened."""
    uid = _rider_with_a_ride(client, zwift_home, monkeypatch)

    attached, _config = attach_connector(client, uid, zwift_home)
    with attached as connector:
        connector.send_event("activities.changed")
        _wait_for(lambda: len(db.list_activities(uid)) == 1,
                  what="the automatic scan to import the first ride")
        _wait_idle(client)

        # Straight after the auto scan, well inside the limit.
        (zwift_home / "Activities" / "ride2.fit").write_bytes(b"dummy2")
        assert client.post("/activities/rescan").status_code == 202
        _wait_for(lambda: len(db.list_activities(uid)) == 2,
                  what="the button's scan to import the second ride")
        _wait_idle(client)


def test_an_unknown_event_neither_scans_nor_drops_the_connection(
    client, zwift_home, monkeypatch
):
    """Forward compatibility: an older server must tolerate a newer connector.

    An event it has never heard of has to be ignored rather than treated as a
    protocol error - and ignored means ignored, not "scan just in case".
    """
    uid = _rider_with_a_ride(client, zwift_home, monkeypatch)

    attached, _config = attach_connector(client, uid, zwift_home)
    with attached as connector:
        connector.send_event("activities.teleported", why="who knows")
        time.sleep(0.3)
        assert db.list_activities(uid) == [],             "an unrecognized event started a scan"
        assert connectorhub.is_attached(uid),             "an unrecognized event dropped the connection"

        # And the socket still carries the event it does know.
        connector.send_event("activities.changed")
        _wait_for(lambda: len(db.list_activities(uid)) == 1,
                  what="the known event to still work")
        _wait_idle(client)



# ---------------------------------------------- the sweep as a backstop
def test_the_sweep_says_so_when_it_could_not_look(monkeypatch, caplog):
    """"Imported 0" and "could not look" are different facts.

    The sweep used to report them identically - a bare `except: pass` - so a
    connector that happened to be down at 3am was indistinguishable from a
    Zwift folder with nothing new in it, for a further 24 hours.
    """
    from wattracker.rpc import ConnectorUnavailable

    db.init_db()
    uid = db.create_user("sweeper", "x")

    def _offline(user_id, **kwargs):
        raise ConnectorUnavailable("no connector attached")

    monkeypatch.setattr(importer, "scan_activities", _offline)
    with caplog.at_level("INFO", logger="wattracker.ingest.importer"):
        totals = importer.run_auto_scan()

    assert totals["unreachable"] == 1
    assert totals["imported"] == 0
    assert totals["users"] == 1, "the sweep stopped on the unreachable user"
    assert any(
        "no connector attached" in record.getMessage()
        for record in caplog.records
    ), "the sweep passed over an unreachable connector in silence"
    assert uid


# ------------------------------------------- the export sync, on attach
def test_attaching_pushes_the_plan_to_the_zwift_folder(
    client, zwift_home, monkeypatch
):
    """The export has the scan's dependency and had the scan's bug.

    It runs on the daily sweep, it needs a connector attached to write
    anything, and when there was none it reported 'offline' and did not try
    again for 24 hours - so a rider whose connector was down at sweep time
    found tomorrow's workout missing from Zwift. Attaching is the moment it
    becomes possible, so that is when it runs.
    """
    monkeypatch.setenv("WATTRACKER_MODE", "server")
    redirect_home(monkeypatch, str(zwift_home.parent))
    uid = _register(client)

    calls = []
    monkeypatch.setattr(
        servermod.exporter, "sync_plan_exports",
        lambda user_id: calls.append(user_id) or {"status": "empty",
                                                  "exported": 0, "removed": 0},
    )

    attached, _config = attach_connector(client, uid, zwift_home)
    with attached:
        _wait_for(lambda: calls == [uid],
                  what="the export sync to run on attach")


def test_a_reconnecting_connector_does_not_re_export_every_time(
    client, zwift_home, monkeypatch
):
    """Nothing about a reconnect makes the plan newer.

    A flapping connector would otherwise push the whole workout folder over
    the socket every few seconds.
    """
    monkeypatch.setenv("WATTRACKER_MODE", "server")
    redirect_home(monkeypatch, str(zwift_home.parent))
    uid = _register(client)

    calls = []
    monkeypatch.setattr(
        servermod.exporter, "sync_plan_exports",
        lambda user_id: calls.append(user_id) or {"status": "empty",
                                                  "exported": 0, "removed": 0},
    )

    attached, _config = attach_connector(client, uid, zwift_home)
    with attached:
        _wait_for(lambda: calls == [uid], what="the first export sync")

    # Straight back again, well inside the limit.
    again, _config = attach_connector(client, uid, zwift_home)
    with again:
        time.sleep(0.3)
        assert calls == [uid], "a reconnect re-exported the whole plan"
