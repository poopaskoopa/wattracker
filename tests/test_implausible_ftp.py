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

Two things the guard is NOT allowed to do, both covered below: it must not
filter an FTP the rider ASSERTED (however low), and it must not back-fill a
score onto a row that was deliberately left unscored. Both are silent data
defects in the opposite direction.
"""
import datetime as dt

import pytest

import wattracker.ingest.importer as importer
from wattracker import db, power_corrections
from wattracker.analysis import activity_cache
from wattracker.ftp_provenance import is_asserted_source
from wattracker.metrics import profile_store
from wattracker.metrics.power import (
    FTP_PLAUSIBLE_MIN_WATTS,
    asserted_ftp,
    intensity_factor,
    is_plausible_ftp,
    training_stress_score,
)
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


# ------------------------------------------------- provenance: asserted vs. estimated

def test_sub_floor_manual_override_still_scores_rides(tmp_path, monkeypatch):
    """A rider who asserts 40 W must accrue real training load, end to end.

    FAILS against 595cb71: the plausibility rail in ``_build_record`` looked
    only at the NUMBER, so honouring the override in ``current_ftp`` and then
    refusing it as a basis stored np=200, if_=0, tss=0 - the rider's asserted
    FTP accepted as a number and discarded as a basis, leaving CTL/ATL/TSB
    reading as untrained with only a log line to show for it.
    """
    uid = _user()
    db.save_user_settings(uid, {"ftp": 40.0})
    act = tmp_path / "Activities"
    act.mkdir()
    path = act / "ride.fit"
    path.write_bytes(b"fit")
    monkeypatch.setattr(
        importer, "parse_fit", lambda p: _parsed("2026-07-01T10:00:00", 3600, 200.0)
    )

    assert importer.scan_activities(uid, str(act))["imported"] == 1

    row = db.list_activities(uid)[0]
    assert row["np"] == pytest.approx(200.0)
    # One hour at 5x the asserted FTP: IF 5, TSS 2500. Absurd as training load,
    # but it is the rider's own number - the app's job is to honour it, not to
    # quietly zero it.
    assert row["if_"] == pytest.approx(5.0)
    assert row["tss"] == pytest.approx(2500.0)


def test_asserted_ftp_passes_the_floor_and_a_bare_float_does_not():
    """Guard: provenance, not magnitude, is what the admission test reads."""
    assert is_plausible_ftp(40.0) is False  # nothing in this DB asserts 40 W
    assert is_plausible_ftp(asserted_ftp(40.0)) is True
    # An AssertedFTP behaves as its own number everywhere else.
    assert asserted_ftp(40.0) == 40.0
    assert float(asserted_ftp(40.0)) == 40.0
    assert round(asserted_ftp(40.049), 1) == 40.0
    # Not a licence for nonsense: zero and negatives are still not an FTP.
    assert asserted_ftp(0) is None
    assert asserted_ftp(-5) is None
    assert asserted_ftp(None) is None


# --------------------------------------------- the rail no rescorer can bypass

def test_scorers_refuse_an_implausible_basis():
    """The guard lives in the scorers themselves, not only in the importer.

    FAILS against 595cb71: both functions were guarded by ``ftp <= 0`` alone and
    happily returned IF 313 / TSS 14,877,347.

    This placement is deliberate. ``intensity_factor`` and
    ``training_stress_score`` are the only two ways IF and TSS are ever
    computed, so every present and future scorer inherits the rail without
    having to remember it - including PR #59's ``ftp_rescore.score_activity``,
    which resolves its own FTP from ftp_history, never goes through
    ``_build_record``, and would otherwise re-score exactly the rows this fix
    leaves unscored using the legacy sub-floor values that caused #60.
    """
    assert intensity_factor(202.4, 0.6378) == 0.0
    assert training_stress_score(5452, 202.4, 0.6378) == 0.0
    assert intensity_factor(200.0, 49.9) == 0.0
    assert training_stress_score(3600, 200.0, 49.9) == 0.0
    # Plausible and asserted bases are untouched.
    assert intensity_factor(200.0, 250.0) == pytest.approx(0.8)
    assert training_stress_score(3600, 250.0, 250.0) == pytest.approx(100.0)
    assert intensity_factor(200.0, asserted_ftp(40.0)) == pytest.approx(5.0)
    assert training_stress_score(3600, 200.0, asserted_ftp(40.0)) == pytest.approx(2500.0)


# ------------------------------------------------------- power-sample corrections

@pytest.fixture
def _cheap_correction_refresh(monkeypatch):
    """``power_corrections.apply`` re-evaluates FTP and the profile; skip both."""
    monkeypatch.setattr(profile_store, "refresh", lambda uid: None)
    activity_cache.invalidate()
    yield
    activity_cache.invalidate()


def _stored_activity(uid, *, np_value, if_value, tss_value, watts=200.0, n=600):
    power = [watts] * n
    return db.insert_activity(
        uid,
        {
            "dedup_hash": f"{uid}-corr",
            "filename": "legacy.fit",
            "start_time": "2023-09-08T10:00:00",
            "duration_s": n,
            "distance_m": 1000.0,
            "avg_power": watts,
            "avg_hr": 140.0,
            "np": np_value,
            "if_": if_value,
            "tss": tss_value,
            "streams": {
                "time": [None] * n,
                "power": power,
                "heartrate": [140.0] * n,
                "cadence": [90.0] * n,
                "distance": [float(i) for i in range(n)],
            },
        },
    )


def test_correction_never_writes_an_implausible_basis(_cheap_correction_refresh):
    """A correction must not mint a fresh score at a basis the app rejects.

    FAILS against 595cb71: ``power_corrections`` writes if_/tss itself, without
    ``_build_record``, ``is_plausible_ftp`` or ``current_ftp``. It back-solves
    the basis from the stored row (``np / if_``) and re-scores against it, so
    correcting a legacy row wrote - today - if_ 313.427 and tss 14,877,347.5 at
    an implied basis of 0.6378 W.

    Preserving the row's original basis is that module's deliberate design and
    it survives here; what it may no longer do is propagate a basis the app has
    just declared impossible. The corrected row keeps its measurements (NP,
    average power) and drops to the never-scored state instead.
    """
    uid = _user()
    # A row as the bug left it: 202.4 W NP scored against 0.6378 W.
    aid = _stored_activity(
        uid, np_value=202.4, if_value=317.399, tss_value=16_136_334.0
    )

    power_corrections.apply(uid, aid, 10, 12, "spike")

    row = db.list_activities(uid)[0]
    assert row["np"] and row["np"] > 0, "NP is a measurement and must survive"
    assert (row["if_"] or 0.0) == 0.0
    assert (row["tss"] or 0.0) == 0.0
    stored_basis = db.list_power_corrections(uid, active_only=True)[0]["ftp_basis"]
    assert stored_basis == pytest.approx(202.4 / 317.399, rel=1e-6), (
        "the audit column still records the basis the row was scored against"
    )


def test_correction_preserves_the_never_scored_marker(_cheap_correction_refresh):
    """np > 0 with if_ == 0 is issue #62's marker; a correction must not erase it.

    FAILS against 595cb71: with nothing to back-solve from, ``_recovered_ftp``
    fell through to ``current_ftp`` and the correction stamped if_ 0.999 and
    tss 99.8 onto the row - back-filling a 200 W fiction and destroying the only
    signal that says "this ride has never been scored".
    """
    uid = _user()
    aid = _stored_activity(uid, np_value=200.0, if_value=0.0, tss_value=0.0)

    power_corrections.apply(uid, aid, 10, 12, "spike")

    row = db.list_activities(uid)[0]
    assert row["np"] and row["np"] > 0
    assert (row["if_"] or 0.0) == 0.0, "the never-scored marker was overwritten"
    assert (row["tss"] or 0.0) == 0.0


def test_correction_still_scores_against_an_asserted_sub_floor_basis(
    _cheap_correction_refresh,
):
    """Guard: the correction rail reads provenance too, not just the number.

    Guard, not proof: it cannot fail against 595cb71, which had no rail here at
    all. It pins the other half of the rule - a rider who asserted 40 W has rows
    legitimately scored at IF 5, and correcting one must re-score it, not
    silently zero it the way a magnitude-only test would.
    """
    uid = _user()
    db.save_user_settings(uid, {"ftp": 40.0})
    aid = _stored_activity(uid, np_value=200.0, if_value=5.0, tss_value=2500.0)

    power_corrections.apply(uid, aid, 10, 12, "spike")

    row = db.list_activities(uid)[0]
    # Masking three samples nudges NP down a touch; IF stays ~NP/40.
    assert row["if_"] == pytest.approx(5.0, rel=0.01)
    assert row["if_"] == pytest.approx(row["np"] / 40.0, rel=1e-3)
    assert row["tss"] and row["tss"] > 0


# ------------------------------------------- provenance that survives SQLite

def _basis_as_a_rescorer_reads_it(uid, start_time):
    """The FTP basis for a ride, resolved the way PR #59's rescore resolves it.

    ``ftp_rescore.score_activity`` never calls ``current_ftp``: it takes the FTP
    effective on the ride's own date straight out of ``ftp_history`` and scores
    from ``float(row["ftp_watts"])``. This is that SQL, so these tests exercise
    the read path an offline rescorer uses whether or not #59 is merged into the
    tree they run in - the point being that a rescorer sees a BARE FLOAT, with
    no ``AssertedFTP`` marker anywhere in sight.
    """
    conn = db.connect()
    try:
        row = conn.execute(
            """
            SELECT COALESCE(
              (SELECT h.ftp_watts FROM ftp_history h
                WHERE h.user_id = ? AND h.date <= ? ORDER BY h.date DESC LIMIT 1),
              (SELECT h.ftp_watts FROM ftp_history h
                WHERE h.user_id = ? ORDER BY h.date ASC LIMIT 1)
            ) AS ftp_watts
            """,
            (uid, start_time[:10], uid),
        ).fetchone()
    finally:
        conn.close()
    return None if row is None or row["ftp_watts"] is None else float(row["ftp_watts"])


def test_asserted_sub_floor_ftp_survives_a_database_round_trip(tmp_path, monkeypatch):
    """The rider's assertion must still be an assertion after a SQLite round-trip.

    FAILS against 1e27b9f, twice over - this is the design flaw the round is for.
    ``AssertedFTP`` is an in-memory marker, so provenance existed only inside the
    importer's own call stack:

    1. the assertion was never recorded durably at all. The rider's 40 W lives in
       ``user_settings``; ``evaluate_ftp`` then wrote a ~182 W *estimated* row to
       ``ftp_history``, which is the only place a rescorer looks - so the DB's
       answer to "what is this rider's FTP" contradicted the one the importer
       scored with; and
    2. the basis comes back out of SQLite as a bare 40.0, which the admission
       test rejected, so a rescorer re-scores the ride to IF 0 / TSS 0 and wipes
       a rehab rider's entire training load.

    Both halves are checked here: what the database says, and what a scorer that
    reads it computes.
    """
    uid = _user()
    db.save_user_settings(uid, {"ftp": 40.0})
    act = tmp_path / "Activities"
    act.mkdir()
    (act / "ride.fit").write_bytes(b"fit")
    start = "2026-07-01T10:00:00"
    monkeypatch.setattr(importer, "parse_fit", lambda p: _parsed(start, 3600, 200.0))
    assert importer.scan_activities(uid, str(act))["imported"] == 1

    row = db.list_activities(uid)[0]
    assert row["if_"] == pytest.approx(5.0)

    # 1. the durable record agrees with what the importer scored against.
    basis = _basis_as_a_rescorer_reads_it(uid, start)
    assert basis == pytest.approx(40.0), (
        f"a rescorer would rebase this rider's rides onto {basis} W"
    )
    assert is_asserted_source(db.latest_ftp(uid)["source"])

    # 2. and a scorer handed that bare float reaches the importer's answer.
    assert is_plausible_ftp(basis) is True
    assert intensity_factor(row["np"], basis) == pytest.approx(row["if_"])
    assert training_stress_score(3600, row["np"], basis) == pytest.approx(row["tss"])


def test_manual_history_row_is_asserted_after_a_round_trip():
    """A source='manual' row is an assertion no matter which process reads it.

    FAILS against 1e27b9f: ``is_plausible_ftp(40.0)`` was False for any bare
    float, so an offline rescorer reading this rider's own 40 W back out of
    ftp_history scored every ride of theirs at zero.
    """
    uid = _user()
    db.add_ftp_entry(uid, "2026-06-01", 40.0, "manual")

    assert is_plausible_ftp(40.0) is True
    assert intensity_factor(200.0, 40.0) == pytest.approx(5.0)
    assert training_stress_score(3600, 200.0, 40.0) == pytest.approx(2500.0)
    # Only that value, and only because the rider stated it.
    assert is_plausible_ftp(41.0) is False
    assert importer.current_ftp(uid) == pytest.approx(40.0)


def test_an_unknown_history_source_is_not_a_rider_assertion():
    """FAILS against 1e27b9f: the predicate was ``source != "estimated"``.

    Only 'manual' and 'estimated' are written today, so this was latent - but it
    is fail-OPEN: the first ``add_ftp_entry(..., "ramp_test")`` anyone adds would
    have its output honoured as an unbounded rider assertion and walk straight
    back into #60. An unrecognised source is our number, not the rider's.
    """
    uid = _user()
    db.add_ftp_entry(uid, "2026-06-01", 3.7, "ramp_test")

    assert is_asserted_source("ramp_test") is False
    assert is_asserted_source("manual") is True
    assert is_asserted_source("MANUAL ") is True
    assert is_asserted_source(None) is False
    assert is_plausible_ftp(3.7) is False
    assert importer.current_ftp(uid) == importer.DEFAULT_FTP


# ------------------------------------------------------- bounds on an assertion

def test_an_assertion_is_bounded_by_the_human_range():
    """An assertion is honoured because it is a claim about a body.

    FAILS against 1e27b9f: ``asserted_ftp`` admitted any positive wattage, so a
    stated 0.64 W was honoured as a scoring basis. /settings validates nothing
    at all (issue #64, not fixed here), which is exactly why the SCORING BASIS
    has to bound itself: a fat-fingered 2500 stores a ride at a sixth of
    its true TSS forever, and a 1 W row from a bad migration stores 4,000,000.
    The rehab case the provenance path exists for must keep working.
    """
    assert asserted_ftp(40.0) == 40.0        # a rider mid-rehab: honoured
    assert asserted_ftp(20.0) == 20.0        # bottom of clinical ergometry
    assert asserted_ftp(700.0) == 700.0
    assert asserted_ftp(19.9) is None
    assert asserted_ftp(0.64) is None        # the #60 estimate, typed in
    assert asserted_ftp(1.0) is None
    assert asserted_ftp(2500.0) is None      # a typo, not a cyclist
    assert is_plausible_ftp(asserted_ftp(40.0)) is True
    # ...and the bound holds for an estimate too: nothing may score at 2500 W.
    assert is_plausible_ftp(2500.0) is False
    assert training_stress_score(3600, 200.0, 2500.0) == 0.0


def test_an_out_of_range_override_is_not_used_as_a_basis():
    """FAILS against 1e27b9f: ``current_ftp`` returned the stated 0.64 W as an
    ``AssertedFTP``, which the scorers then honoured - a rider (or a bad
    migration) could put #60 back by typing it into an unvalidated form."""
    uid = _user()
    db.save_user_settings(uid, {"ftp": 0.64})

    resolved = importer.current_ftp(uid)
    assert is_plausible_ftp(resolved)
    assert resolved == importer.DEFAULT_FTP


def test_a_settings_value_cannot_re_bless_a_corrupt_legacy_basis(
    _cheap_correction_refresh,
):
    """The 2% basis-match tolerance was an exploit; it is gone.

    FAILS against 1e27b9f: correcting a legacy row back-solves its basis
    (202.4 / 317.399 = 0.6378 W). That release re-marked a sub-floor basis as
    asserted whenever it came within 2% of the rider's current assertion, so
    setting the FTP to 0.64 made the corrupt basis "the rider's own number" and
    re-scored the ride at IF 313.064 / TSS 1,633,482 - the round-one exploit
    reached through the assertion channel instead.

    Two things close it and both are checked: an assertion is bounded by the
    human range (0.64 W is not one), and the correction rail no longer compares
    a recovered basis against today's FTP at all.
    """
    uid = _user()
    db.save_user_settings(uid, {"ftp": 0.64})
    aid = _stored_activity(
        uid, np_value=202.4, if_value=317.399, tss_value=16_136_334.0
    )

    power_corrections.apply(uid, aid, 10, 12, "spike")

    row = db.list_activities(uid)[0]
    assert row["np"] and row["np"] > 0, "NP is a measurement and must survive"
    assert (row["if_"] or 0.0) == 0.0, "a corrupt basis was re-blessed"
    assert (row["tss"] or 0.0) == 0.0
    # The value the rider stated never became admissible either.
    assert is_plausible_ftp(0.6378) is False
    assert is_plausible_ftp(0.64) is False


def test_correction_keeps_a_valid_score_after_the_rider_updates_their_ftp(
    _cheap_correction_refresh,
):
    """A correction may change what was measured, never destroy what was scored.

    FAILS against 1e27b9f: the sub-floor basis of a legitimately scored row was
    honoured only while it still matched the rider's CURRENT FTP. A rehab rider
    who stated 40 W, accrued real rides at IF 5, recovered and updated to 250 W
    then corrected one of those rides had it rewritten to np > 0, if_ 0, tss 0 -
    silently deleting a valid score AND minting a fake "never scored" marker on
    a row that plainly was, which is the very signal issue #62's repair pass
    keys on. A full undo restored it; a ranged one did not.
    """
    uid = _user()
    db.save_user_settings(uid, {"ftp": 40.0})
    aid = _stored_activity(uid, np_value=200.0, if_value=5.0, tss_value=2500.0)
    # The rider recovers and updates their FTP. The old row's score stands: it
    # was scored against the FTP the rider had at the time.
    db.save_user_settings(uid, {"ftp": 250.0})

    power_corrections.apply(uid, aid, 10, 12, "spike")

    row = db.list_activities(uid)[0]
    assert row["if_"] == pytest.approx(row["np"] / 40.0, rel=1e-3), (
        "the row was re-scored against something other than its own basis"
    )
    assert (row["if_"] or 0.0) > 0.0, "a valid score was destroyed"
    assert row["tss"] and row["tss"] > 0
    assert not ((row["np"] or 0) > 0 and (row["if_"] or 0) == 0), (
        "a false never-scored marker was minted on a row that was scored"
    )

    # And the same for a ranged (partial) undo, which re-scores rather than
    # restoring the stored original.
    correction_id = db.list_power_corrections(uid, active_only=True)[0]["id"]
    power_corrections.undo(uid, correction_id)
    restored = db.list_activities(uid)[0]
    assert restored["if_"] == pytest.approx(5.0, rel=1e-3)
    assert restored["tss"] and restored["tss"] > 0
