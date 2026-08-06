"""Issue #60: an activity must never be scored against an implausible FTP.

Background (the reproduction these tests encode):

``estimate_ftp`` is anchored at wall-clock *now* and decays every past effort by
the inactivity between it and that anchor. When a rider imports a backlog of old
rides, the newest ride in the database is years old, so the estimate is decayed
across that whole gap - it correctly answers "what could they hold today after
three years off", which as a *wattage* is a fraction of a watt. The importer then
used that number as the scoring basis for the historical rides themselves.

Because TSS is quadratic in 1/FTP, a 0.64 W basis turned a normal 5,452-second
ride at 202 W NP into a stored TSS of 16,136,334. Worse, ``evaluate_ftp``
persisted the failed estimate to ``ftp_history``, from where ``current_ftp``
read it back as authoritative for every subsequent import.
"""
import datetime as dt

import pytest

import wattracker.ingest.importer as importer
from wattracker import db
from wattracker.metrics.power import FTP_PLAUSIBLE_MIN_WATTS, is_plausible_ftp
from wattracker.timeutil import utc_now


def _parsed(start_time, seconds=3600, watts=200.0):
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


def _user(username="rider"):
    db.init_db()
    db.create_user(username, "password123")
    return db.get_user_by_username(username)["id"]


def _stale_backlog(directory, monkeypatch, count, first_index=0, days_ago=1100):
    """Plant ``count`` weekly 200 W hours starting ~3 years before now.

    Returns nothing; re-patches ``parse_fit`` so every planted file parses to its
    own timestamp. Three years is the shape of the real data: the affected users'
    imports ran in 2026 over rides recorded from 2023 on.
    """
    base = utc_now() - dt.timedelta(days=days_ago)
    stamps = getattr(_stale_backlog, "_stamps", None)
    if stamps is None or first_index == 0:
        stamps = {}
        _stale_backlog._stamps = stamps
    for i in range(first_index, first_index + count):
        when = (base + dt.timedelta(days=7 * i)).replace(microsecond=0)
        path = directory / f"ride{i:03d}.fit"
        path.write_bytes(b"fit")
        stamps[str(path)] = when.isoformat(timespec="seconds")
    monkeypatch.setattr(
        importer, "parse_fit", lambda path: _parsed(stamps[str(path)])
    )


# --------------------------------------------------------------- the guarantee

def test_backlog_import_never_scores_against_decayed_estimate(tmp_path, monkeypatch):
    """The end-to-end reproduction, driven through ``scan_activities``.

    FAILS without the fix: the second scan stored IF ~55 and TSS ~308,642.
    """
    uid = _user()
    act = tmp_path / "Activities"
    act.mkdir()

    _stale_backlog(act, monkeypatch, 20)
    importer.scan_activities(uid, str(act))
    # The rest of the same-era backlog arrives on a later scan - the point at
    # which the first scan's evaluation has become the scoring basis.
    _stale_backlog(act, monkeypatch, 20, first_index=20)
    importer.scan_activities(uid, str(act))

    rows = db.list_activities(uid)
    assert len(rows) == 40
    for row in rows:
        assert row["np"] and row["np"] > 0, "NP is a measurement and must survive"
        implied = row["np"] / row["if_"] if row["if_"] else None
        assert implied is None or implied >= FTP_PLAUSIBLE_MIN_WATTS, (
            f"ride {row['start_time']} scored against {implied:.3f} W"
        )
        assert (row["tss"] or 0.0) < 1000.0, (
            f"ride {row['start_time']} stored an impossible TSS: {row['tss']}"
        )


def test_stale_backlog_estimate_is_never_written_to_ftp_history(tmp_path, monkeypatch):
    """FAILS without the fix: ftp_history gained a 3.6 W 'estimated' row."""
    uid = _user()
    act = tmp_path / "Activities"
    act.mkdir()
    _stale_backlog(act, monkeypatch, 20)

    importer.scan_activities(uid, str(act))

    latest = db.latest_ftp(uid)
    assert latest is None or is_plausible_ftp(latest["ftp_watts"]), (
        f"implausible FTP persisted to history: {latest}"
    )


def test_evaluate_ftp_refuses_an_implausible_estimate(tmp_path, monkeypatch):
    """FAILS without the fix: returned True and appended the failed estimate."""
    uid = _user()
    act = tmp_path / "Activities"
    act.mkdir()
    _stale_backlog(act, monkeypatch, 20)
    importer.scan_activities(uid, str(act))

    assert importer.evaluate_ftp(uid) is False
    assert db.latest_ftp(uid) is None


def test_current_ftp_never_returns_an_implausible_estimate(tmp_path, monkeypatch):
    """FAILS without the fix: current_ftp returned 3.6 W."""
    uid = _user()
    act = tmp_path / "Activities"
    act.mkdir()
    _stale_backlog(act, monkeypatch, 20)
    importer.scan_activities(uid, str(act))

    resolved = importer.current_ftp(uid)
    assert is_plausible_ftp(resolved), f"current_ftp returned {resolved} W"
    assert resolved == importer.DEFAULT_FTP


def test_legacy_implausible_history_row_is_not_used_as_a_basis():
    """A pre-fix ftp_history row is ignored rather than trusted.

    FAILS without the fix: current_ftp returned the stored 0.64 W verbatim, so
    every later import of an already-damaged database kept compounding.
    """
    uid = _user()
    db.add_ftp_entry(uid, utc_now().date().isoformat(), 0.64, "estimated")

    resolved = importer.current_ftp(uid)
    assert is_plausible_ftp(resolved), f"current_ftp returned {resolved} W"


def test_activity_with_implausible_basis_is_stored_unscored(tmp_path, monkeypatch):
    """The last rail: a caller that resolves the FTP itself still cannot score.

    FAILS without the fix: stored IF 315.8 and TSS 5,955,000. The row is left
    identifiable as never-scored (NP present, IF/TSS zero) rather than carrying
    a number that is wrong by five orders of magnitude - that is the state
    issue #62's repair pass looks for.
    """
    uid = _user()
    path = tmp_path / "ride.fit"
    path.write_bytes(b"fit")
    monkeypatch.setattr(
        importer, "parse_fit", lambda p: _parsed("2023-09-08T10:00:00", 5452, 202.4)
    )

    new_id = importer.ingest_file(uid, str(path), ftp=0.64)
    assert new_id is not None

    row = db.list_activities(uid)[0]
    assert row["np"] and row["np"] > 0
    assert (row["if_"] or 0.0) == 0.0
    assert (row["tss"] or 0.0) == 0.0


# ------------------------------------------------------------------- the floor

@pytest.mark.parametrize(
    "watts, expected",
    [
        (None, False), (0, False), (-10.0, False), (0.64, False), (3.7, False),
        (32.1, False), (41.5, False), (49.9, False),
        (50.0, True), (60.0, True), (184.9, True), (400.0, True),
        (float("nan"), False), (float("inf"), False), ("abc", False),
    ],
)
def test_is_plausible_ftp(watts, expected):
    """Unit coverage of the new admission test.

    Guard, not proof: ``is_plausible_ftp`` did not exist before the fix, so this
    cannot fail against the unfixed code in a meaningful way (it errors on
    import). It pins the boundary against future drift.
    """
    assert is_plausible_ftp(watts) is expected


def test_manual_override_below_the_floor_is_still_honoured():
    """The floor must not silently rewrite what the rider typed.

    Guard, not proof: passes either way. It exists because the obvious
    implementation of the fix - clamping every FTP in ``current_ftp`` - would
    replace a rider's own stated value with 200 W without telling them, which is
    the same silent-substitution defect in a new place. A manual value is an
    assertion, not an estimate; only estimates are filtered.
    """
    uid = _user()
    db.save_user_settings(uid, {"ftp": 40.0})
    assert importer.current_ftp(uid) == 40.0
