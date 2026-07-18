"""Regression tests: tz-aware activity timestamps must not crash windowing.

FIT timestamps are timezone-aware (UTC); the app's windows use a naive
datetime.now(). Comparing them raised "can't compare offset-naive and
offset-aware datetimes" and 500'd the dashboard. See wattracker/timeutil.py.
"""
import tempfile

from wattracker.timeutil import parse_naive, to_naive
from wattracker import db
from wattracker.analysis import pipeline


def test_parse_naive_strips_tzinfo():
    assert parse_naive("2026-06-25T10:00:00+00:00").tzinfo is None
    assert parse_naive("2026-06-25T10:00:00").tzinfo is None
    assert parse_naive(None) is None
    assert parse_naive("not-a-date") is None
    assert to_naive(None) is None


def test_build_state_with_tz_aware_activity(tmp_path):
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
    # build_state used to raise TypeError in _window_power / estimate_ftp.
    state = pipeline.build_state(uid, path=dbfile) if _accepts_path() else None
    if state is None:
        # build_state resolves its own DB path; point it there via env.
        import os

        os.environ["WATTRACKER_DB"] = dbfile
        state = pipeline.build_state(uid)
    # The estimate is now anchored at wall-clock now() and detraining-decayed,
    # so the exact value drifts with the real date; the regression here is that
    # a tz-aware start_time no longer crashes. Best-20 of 200W -> 190 undecayed,
    # decay only reduces it, so assert a positive value not exceeding 190.
    assert 0.0 < state.ftp <= 190.0


def _accepts_path() -> bool:
    import inspect

    return "path" in inspect.signature(pipeline.build_state).parameters
