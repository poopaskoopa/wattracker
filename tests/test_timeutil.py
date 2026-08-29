"""Regression tests: tz-aware activity timestamps must not crash windowing.

FIT timestamps are timezone-aware (UTC); the app's windows use a naive
datetime.now(). Comparing them raised "can't compare offset-naive and
offset-aware datetimes" and 500'd the dashboard. See wattracker/timeutil.py.
"""
import datetime as dt

from wattracker.timeutil import (
    local_today,
    parse_naive,
    to_naive,
    to_user_timezone,
    valid_timezone,
)
from wattracker import db
from wattracker.analysis import pipeline
from wattracker.ingest import importer


def test_parse_naive_strips_tzinfo():
    assert parse_naive("2026-06-25T10:00:00+00:00").tzinfo is None
    assert parse_naive("2026-06-25T10:00:00").tzinfo is None
    assert parse_naive(None) is None
    assert parse_naive("not-a-date") is None
    assert to_naive(None) is None


def test_local_today_uses_user_timezone_with_utc_default():
    instant = dt.datetime(2026, 1, 2, 0, 30)
    assert local_today("America/New_York", instant) == dt.date(2026, 1, 1)
    assert local_today("UTC", instant) == dt.date(2026, 1, 2)
    assert local_today(None, instant) == dt.date(2026, 1, 2)


def test_user_timezone_is_dst_aware():
    winter = to_user_timezone(
        dt.datetime(2026, 1, 15, 12), "America/New_York"
    )
    summer = to_user_timezone(
        dt.datetime(2026, 7, 15, 12), "America/New_York"
    )
    assert winter.utcoffset() == dt.timedelta(hours=-5)
    assert summer.utcoffset() == dt.timedelta(hours=-4)


def test_invalid_timezone_is_rejected_and_falls_back_to_utc():
    instant = dt.datetime(2026, 1, 2, 0, 30)
    assert not valid_timezone("../../etc/passwd")
    assert not valid_timezone("Not/A_Real_Zone")
    assert not valid_timezone("A" * 256)
    assert local_today("Not/A_Real_Zone", instant) == dt.date(2026, 1, 2)
    assert local_today(123, instant) == dt.date(2026, 1, 2)


def test_build_state_with_tz_aware_activity(tmp_path, monkeypatch):
    dbfile = str(tmp_path / "t.db")
    db.init_db(dbfile)
    uid = db.create_user("u", "h", path=dbfile)
    db.insert_activity(
        uid,
        {
            "dedup_hash": "x",
            "filename": "r.fit",
            "start_time": "2026-06-25T10:00:00+00:00",  # tz-aware -> used to crash
            "duration_s": 1300,
            "distance_m": 0,
            "tss": 50,
            "np": 200,
            "if_": 0.8,
            "streams": {"power": [200] * 1300, "time": list(range(1300))},
        },
        path=dbfile,
    )
    # Two seams, both anchored at wall-clock now, both needed:
    #  - db's, because build_state windows through recent_power_streams(90) /
    #    recent_full_activities, which is what keeps the ride above in range;
    #  - importer's, because current_ftp() anchors the detraining decay there,
    #    and once the ride is far enough back the estimate decays into
    #    implausibility, gets discarded, and DEFAULT_FTP (200) is returned.
    frozen = dt.datetime(2026, 6, 26, 12)
    monkeypatch.setattr(db, "utc_now", lambda: frozen)
    monkeypatch.setattr(importer, "utc_now", lambda: frozen)
    # build_state used to raise TypeError in _window_power / estimate_ftp.
    state = pipeline.build_state(uid, path=dbfile) if _accepts_path() else None
    if state is None:
        # build_state resolves its own DB path; point it there via env.
        import os

        os.environ["WATTRACKER_DB"] = dbfile
        state = pipeline.build_state(uid)
    # The estimate is anchored at wall-clock now() and detraining-decayed, so
    # the value drifts with the real date. Left unpinned that drift is not
    # merely cosmetic: on 2027-05-25 the 2026-06-25 ride falls out of the FTP
    # window entirely, the estimate stops being derived from it at all, and
    # `<= 190.0` fails against the undecayed 200. Pin the clock just after the
    # ride so the window is a fixed one. Best-20 of 200W -> 190 undecayed, and
    # decay only reduces it, so a positive value not exceeding 190 is the
    # regression being guarded: a tz-aware start_time no longer crashes.
    assert 0.0 < state.ftp <= 190.0


def _accepts_path() -> bool:
    import inspect

    return "path" in inspect.signature(pipeline.build_state).parameters
