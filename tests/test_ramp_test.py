"""The declared FTP ramp test: its shape, its result, and what accepting writes.

The test is identified by the rider SELECTING it, never by recognizing its
shape, so every assertion here works from the prescribed structure and treats
``ramp_test_ftp_candidate`` purely as a cross-check.
"""
import pytest

from wattracker import db, ramp_test
from wattracker.ble.runner import RideController
from wattracker.ftp_provenance import is_asserted_source
from wattracker.ingest import importer
from wattracker.metrics import power as powermod
from wattracker.timeutil import utc_now
from wattracker.prescribe.planner import (
    RAMP_TEST_NAME,
    RAMP_TEST_SLOPE_FRACTION,
    RAMP_TEST_START_FRACTION,
    RAMP_TEST_STEPS,
    RAMP_TEST_STEP_S,
    RAMP_TEST_WARMUP_S,
    build_workout,
    ramp_test_window,
    ramp_test_prescribed_window,
)

pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from wattracker.server import create_app  # noqa: E402


@pytest.fixture()
def client():
    app = create_app()
    with TestClient(app) as c:
        yield c


def _register(client, username="rider"):
    client.post("/register", data={"username": username, "password": "password123"})
    return db.get_user_by_username(username)["id"]


def _ridden_stream(ftp, steps_completed, extra_seconds=0, dt=1.0):
    """See below; ``dt`` records at a cadence other than one sample a second."""
    """The power a rider records who holds every target, then fails.

    Built from the real prescription through the real flattening, so the
    stream is what the trainer would actually have held. ``steps_completed``
    steps are ridden in full and ``extra_seconds`` of the next one before the
    rider stops. Only positive-power seconds appear, because that is all the
    controller records.
    """
    session = build_workout("ramp_test", 30)
    controller = RideController(session, ftp, autosave=False)
    end = RAMP_TEST_WARMUP_S + steps_completed * RAMP_TEST_STEP_S + extra_seconds
    count = int(round(end / dt))
    return [controller.target_watts(i * dt) for i in range(count)]


# --------------------------------------------------------------- the builder
def test_ramp_test_is_discrete_one_minute_steps():
    s = build_workout("ramp_test", 30)
    assert s.workout_type == "ramp_test"
    assert s.name == RAMP_TEST_NAME
    steps = [seg for seg in s.segments if seg.kind == "steadystate"]
    assert len(steps) == RAMP_TEST_STEPS
    # Discrete steps, NOT a `ramp` segment: the runner interpolates those into
    # a smooth rise, and a smooth rise has no one-minute step to take 75% of.
    assert all(seg.duration == RAMP_TEST_STEP_S for seg in steps)
    assert steps[0].power == pytest.approx(RAMP_TEST_START_FRACTION)
    for a, b in zip(steps, steps[1:]):
        assert b.power - a.power == pytest.approx(RAMP_TEST_SLOPE_FRACTION)
    # A warm-up before and a cooldown after, and nothing else.
    assert s.segments[0].kind == "warmup"
    assert s.segments[-1].kind == "cooldown"


@pytest.mark.parametrize("ftp,expected_step_w", [(209, 10.45), (380, 19.0)])
def test_step_size_scales_with_the_riders_ftp(ftp, expected_step_w):
    """A %FTP slope holds the ramp near 20 minutes at any fitness.

    A fixed 10 W/min would ramp a 380 W rider for ~41 minutes, long enough for
    the test to measure endurance instead of aerobic power and under-read.
    """
    session = build_workout("ramp_test", 30)
    controller = RideController(session, ftp, autosave=False)
    start, end = ramp_test_window(session)
    watts = [controller.target_watts(t) for t in range(start, end, RAMP_TEST_STEP_S)]
    assert watts[0] == round(RAMP_TEST_START_FRACTION * ftp)
    rises = [b - a for a, b in zip(watts, watts[1:])]
    assert all(r == pytest.approx(expected_step_w, abs=1.0) for r in rises)
    assert (end - start) == RAMP_TEST_STEPS * RAMP_TEST_STEP_S
    # The ceiling must sit above the MAP of a rider whose recorded FTP is
    # stale-LOW, since that is the rider being tested: MAP is ~1.33x TRUE FTP,
    # so a stored FTP 30% low needs ~1.73x the stored number before the ramp
    # runs out of steps.
    assert watts[-1] >= round(1.73 * ftp)
    # ...and the whole prescription still fits inside the 45-minute stream cap
    # ramp_test_ftp_candidate applies, so the cross-check stays available.
    assert session.total_duration() < 45 * 60


def test_the_requested_duration_is_ignored():
    """A ramp test ends when the rider fails, not when a menu says it does.

    Truncating the prescription to a shorter menu choice would lower the
    ceiling, and a lowered ceiling is what turns a measurement into a floor.
    """
    lengths = {build_workout("ramp_test", m).total_duration()
               for m in (15, 30, 60, 240)}
    assert lengths == {RAMP_TEST_WARMUP_S + RAMP_TEST_STEPS * RAMP_TEST_STEP_S
                       + build_workout("ramp_test", 30).segments[-1].duration}


def test_ramp_window_is_the_run_of_steps_and_is_none_for_other_kinds():
    session = build_workout("ramp_test", 30)
    assert ramp_test_window(session) == (
        RAMP_TEST_WARMUP_S, RAMP_TEST_WARMUP_S + RAMP_TEST_STEPS * RAMP_TEST_STEP_S
    )
    assert ramp_test_window(build_workout("threshold", 60)) is None


# ---------------------------------------------------- ending on rider failure
def test_a_rider_who_blows_up_finishes_the_test_with_their_data():
    """Blowing up IS the result, so the ride finishes rather than pausing."""
    session = build_workout("ramp_test", 30)
    c = RideController(session, 209, autosave=False)
    for t in range(RAMP_TEST_WARMUP_S + 6 * RAMP_TEST_STEP_S):
        c.tick(power=c.target_watts(t))
    assert c.status == "running"
    ridden = c.recorded_power()
    # Deep into the ramp, not still warming up.
    assert c.elapsed > RAMP_TEST_WARMUP_S + 5 * RAMP_TEST_STEP_S
    assert len(ridden) == int(c.elapsed)
    for _ in range(3):
        c.tick(power=0)
    assert c.status == "finished"
    # ...with everything ridden up to the failure still recorded, and no
    # zero-power seconds appended after it.
    assert c.recorded_power() == ridden


def test_stopping_during_the_warm_up_still_only_pauses():
    """Before the ramp there is no measurement to end, so nothing is ended."""
    c = RideController(build_workout("ramp_test", 30), 209, autosave=False)
    for _ in range(60):
        c.tick(power=70)
    for _ in range(3):
        c.tick(power=0)
    assert c.status == "paused"
    assert c.tick(power=70)["status"] == "running"


def test_an_ordinary_workout_is_unchanged_by_the_failure_rule():
    c = RideController(build_workout("endurance", 45), 209, autosave=False)
    assert c.failure_window is None
    for _ in range(60):
        c.tick(power=150)
    for _ in range(5):
        c.tick(power=0)
    assert c.status == "paused"


# ----------------------------------------------------------- the computation
def test_result_is_the_best_actual_minute_of_the_known_window():
    """A rider who fails part-way into a step keeps what they actually rode.

    The best minute straddles the end of step 13 and the start of step 14,
    which is precisely why the measurement uses ACTUAL power over the known
    window rather than the prescribed target of the last completed step.
    """
    stream = _ridden_stream(209, steps_completed=13, extra_seconds=25)
    window = ramp_test_window(build_workout("ramp_test", 30))
    result = ramp_test.evaluate(stream, window, 209, len(stream))

    step13 = round(1.10 * 209)
    step14 = round(1.15 * 209)
    expected_best = (35 * step13 + 25 * step14) / 60.0
    assert result["best_minute_watts"] == pytest.approx(expected_best, abs=0.5)
    assert result["ftp"] == pytest.approx(expected_best * 0.75, abs=0.5)
    assert result["ftp"] > round(step13 * 0.75, 1)  # more than the last full step
    assert result["offer"] is True


def test_the_detector_agrees_on_a_clean_test():
    stream = _ridden_stream(209, steps_completed=13)
    window = ramp_test_window(build_workout("ramp_test", 30))
    result = ramp_test.evaluate(stream, window, 209, len(stream))
    assert result["cross_check_status"] == ramp_test.AGREES
    assert result["disagreement"] is False
    assert result["cross_check_ftp"] == pytest.approx(result["ftp"], rel=0.05)
    # The cross-check really ran; it is not agreeing by returning nothing.
    assert powermod.ramp_test_ftp_candidate(stream) > 0


def test_a_low_ftp_rider_is_out_of_the_detectors_range_not_in_disagreement():
    """5% of 120 W is 6 W/min, below the 8-35 W/min band the detector knows.

    The cross-check returns 0.0 on a perfectly valid test. That is the check
    being out of range; reporting it as a discrepancy would tell the rider
    their good measurement is suspect.
    """
    stream = _ridden_stream(120, steps_completed=13)
    window = ramp_test_window(build_workout("ramp_test", 30))
    result = ramp_test.evaluate(stream, window, 120, len(stream))

    assert powermod.ramp_test_ftp_candidate(stream) == 0.0
    assert result["cross_check_status"] == ramp_test.OUT_OF_RANGE
    assert result["disagreement"] is False
    assert result["cross_check_ftp"] is None
    assert result["cross_check_slope_w_per_min"] == pytest.approx(6.0)
    assert result["offer"] is True
    assert result["ftp"] == pytest.approx(round(1.10 * 120) * 0.75, abs=0.5)
    assert "out of range" in result["message"]


def test_a_material_disagreement_is_reported():
    """A rider who empties themselves into the last step splits the two methods.

    The structural measure takes the best rolling minute, which straddles the
    step boundary and catches the surge; the detector only ever compares whole
    aligned blocks and reports the last complete step. That is a real
    difference about a real ride, and the rider is told rather than having one
    of the two picked for them.
    """
    stream = _ridden_stream(209, steps_completed=13)
    surge = round(1.6 * stream[-1])
    stream = stream + [surge] * 45
    window = ramp_test_window(build_workout("ramp_test", 30))
    result = ramp_test.evaluate(stream, window, 209, len(stream))
    assert result["completed_ramp"] is False
    assert result["cross_check_status"] == ramp_test.DIFFERS
    assert result["disagreement"] is True
    assert "disagree" in result["message"]


def test_a_test_with_no_full_minute_is_never_offered():
    stream = _ridden_stream(209, steps_completed=0, extra_seconds=20)
    window = ramp_test_window(build_workout("ramp_test", 30))
    result = ramp_test.evaluate(stream, window, 209, len(stream))
    assert result["ftp"] == 0.0
    assert result["offer"] is False


def test_an_implausible_result_is_never_offered():
    """A 40 W best minute reads 30 W, below the floor a basis may take."""
    window = (0, 120)
    result = ramp_test.evaluate([40] * 120, window, 209, 120)
    assert result["ftp"] == 30.0
    assert result["offer"] is False
    assert "Nothing has been saved" in result["message"]


def test_the_recorded_window_matches_the_prescribed_one():
    """The accept route re-derives the window without the Session in hand."""
    stream = _ridden_stream(209, steps_completed=13)
    prescribed = ramp_test_window(build_workout("ramp_test", 30))
    recorded = ramp_test_prescribed_window()
    assert recorded[0] == prescribed[0]
    assert ramp_test.evaluate(stream, recorded, 209, len(stream))["ftp"] == \
        ramp_test.evaluate(stream, prescribed, 209, len(stream))["ftp"]


# ------------------------------------------------------------- the opt-in
def test_ramp_test_is_an_asserted_source():
    assert is_asserted_source("ramp_test") is True


# ------------------------------------------------------------- persistence
def _ramp_rows(uid):
    """Every ftp_history row this feature could have written, oldest first."""
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT date, ftp_watts, source FROM ftp_history "
            "WHERE user_id = ? AND source = 'ramp_test' ORDER BY date",
            (uid,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]

def _test_day():
    """The ride is filed under its UTC date, and so is its result."""
    return utc_now().date()


def _record_ramp_test(uid, ftp=209, steps_completed=13, name=RAMP_TEST_NAME):
    import datetime as dt

    stream = _ridden_stream(ftp, steps_completed=steps_completed)
    activity_id, _record = importer.save_ride_record(
        user_id=uid,
        started_at=dt.datetime.combine(_test_day(), dt.time(10, 0, 0)),
        duration_s=len(stream),
        samples={"power": stream, "cadence": [], "heartrate": []},
        session_name=name,
        ftp=ftp,
    )
    return activity_id, stream


def _all_ftp_rows(uid):
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT date, ftp_watts, source FROM ftp_history "
            "WHERE user_id = ? ORDER BY date",
            (uid,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def test_nothing_is_written_before_the_rider_accepts(client):
    """Not just no ramp_test row - no NEW row of any source, and no change in
    what the app is prescribing from.

    ``save_ride_record`` calls ``evaluate_ftp``, and ``estimate_ftp`` runs the
    ramp detector over every stream it is given, so before the passive
    estimator learned to skip a declared test, finishing one wrote its own
    result back as an 'estimated' row seconds later and ``current_ftp`` moved
    to it - defeating the confirmation gate entirely while a query for
    ``source='ramp_test'`` still came back empty.
    """
    uid = _register(client, "rider_offer")
    db.save_user_settings(uid, {"ftp": None})
    db.add_ftp_entry(uid, "2026-08-01", 209.0, "manual")
    before_rows = _all_ftp_rows(uid)
    before_ftp = importer.current_ftp(uid)
    assert before_ftp == pytest.approx(209.0)

    _record_ramp_test(uid)

    assert _all_ftp_rows(uid) == before_rows
    assert importer.current_ftp(uid) == pytest.approx(before_ftp)


def test_an_ordinary_ride_still_feeds_the_passive_estimator(client):
    """The exclusion is scoped to the declared test, not to in-app rides."""
    import datetime as dt

    uid = _register(client, "rider_passive")
    db.save_user_settings(uid, {"ftp": None})
    importer.save_ride_record(
        user_id=uid,
        started_at=dt.datetime.combine(_test_day(), dt.time(9, 0, 0)),
        duration_s=3600,
        samples={"power": [150] * 3600, "cadence": [], "heartrate": []},
        session_name="Endurance",
        ftp=209,
    )
    rows = _all_ftp_rows(uid)
    assert [r["source"] for r in rows] == ["estimated"]
    assert rows[0]["ftp_watts"] == pytest.approx(150.0, abs=1.0)


def test_an_undeclared_ramp_in_an_imported_ride_is_still_recognized():
    """Only the DECLARED test is skipped; #138/#140's behaviour is untouched."""
    stream = _ridden_stream(209, steps_completed=13)
    assert powermod.ramp_test_ftp_candidate(stream) > 0
    assert ramp_test.is_declared_ftp_test({"filename": "Ride 2026-08-26 Ramp Test"})
    assert not ramp_test.is_declared_ftp_test({"filename": "zwift_ramp.fit"})


def test_accepting_writes_a_ramp_test_row_dated_the_test_day(client):
    uid = _register(client, "rider_accept")
    db.save_user_settings(uid, {"ftp": None})
    db.add_ftp_entry(uid, "2026-08-01", 209.0, "manual")
    activity_id, stream = _record_ramp_test(uid)

    r = client.post("/api/ftp/ramp-test/accept", json={"activity_id": activity_id})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["source"] == "ramp_test"
    assert body["date"] == _test_day().isoformat()  # the day it was ridden
    expected = round(round(1.10 * 209) * 0.75, 1)
    assert body["ftp"] == pytest.approx(expected, abs=0.5)

    rows = _ramp_rows(uid)
    assert [r["date"] for r in rows] == [_test_day().isoformat()]
    assert float(rows[0]["ftp_watts"]) == pytest.approx(expected, abs=0.5)
    # ...and it is now the FTP everything else is prescribed from.
    assert importer.current_ftp(uid) == pytest.approx(expected, abs=0.5)


def test_accepting_replaces_an_entry_already_dated_that_day(client):
    """ftp_backfill fills every date, so an INSERT OR IGNORE would do nothing."""
    uid = _register(client, "rider_replace")
    db.save_user_settings(uid, {"ftp": None})
    db.add_ftp_entry(uid, _test_day().isoformat(), 190.0, "estimated")
    activity_id, _ = _record_ramp_test(uid)
    assert client.post(
        "/api/ftp/ramp-test/accept", json={"activity_id": activity_id}
    ).status_code == 200
    assert db.latest_ftp(uid)["source"] == "ramp_test"


def test_accepting_reads_the_ride_at_the_rate_it_was_recorded(client):
    """The live loop floors its poll at a second, so samples < seconds is NORMAL.

    Deriving the ramp window from the SAMPLE COUNT therefore read the wrong
    part of every real ride: at a 1.25s average tick the accept route wrote
    144 W after the ride screen had just shown the rider 172 W, silently, with
    a 200 and a row in ftp_history. The window is workout seconds; the stream's
    own rate is what maps them onto samples.
    """
    import datetime as dt

    ftp, steps = 209, 13
    stream = _ridden_stream(ftp, steps_completed=steps)
    at_one_hz = ramp_test.evaluate(
        stream, ramp_test_prescribed_window(), ftp, len(stream),
    )
    assert at_one_hz["offer"]

    for tick in (1.02, 1.10, 1.25, 2.0):
        uid = _register(client, f"rider_tick_{int(tick * 100)}")
        db.save_user_settings(uid, {"ftp": None})
        # Same ride, same wall clock, fewer samples: one per tick spread across
        # the WHOLE ride. Truncating instead would drop the hardest steps and
        # lower the result honestly, which is not the bug under test.
        sampled = [
            stream[min(len(stream) - 1, int(i * tick))]
            for i in range(int(len(stream) / tick))
        ]
        activity_id, _ = importer.save_ride_record(
            user_id=uid,
            started_at=dt.datetime.combine(_test_day(), dt.time(10, 0, 0)),
            duration_s=len(stream),          # the ride really lasted this long
            samples={"power": sampled, "cadence": [], "heartrate": []},
            session_name=RAMP_TEST_NAME,
            ftp=ftp,
        )
        r = client.post(
            "/api/ftp/ramp-test/accept", json={"activity_id": activity_id}
        )
        assert r.status_code in (200, 400), (tick, r.text)
        if r.status_code == 400:
            continue                          # refused is fine; silently wrong is not
        body = r.json()
        assert body["ftp"] == pytest.approx(at_one_hz["ftp"], rel=0.02), (
            tick, body["ftp"], at_one_hz["ftp"]
        )


def test_the_accept_route_knows_a_rider_failed_before_the_last_step(client):
    """`completed_ramp` was trivially True: the window end was clamped to the
    recording, so every ride had "ridden every step there was"."""
    uid = _register(client, "rider_not_completed")
    db.save_user_settings(uid, {"ftp": None})
    activity_id, _ = _record_ramp_test(uid, steps_completed=13)
    body = client.post(
        "/api/ftp/ramp-test/accept", json={"activity_id": activity_id}
    ).json()
    assert body["completed_ramp"] is False


def test_an_accepted_result_survives_the_next_evaluation_window(client):
    """21 days later the update clock fires; the measurement must still stand.

    `current_ftp` reads the LATEST row whatever its source, so an appended
    'estimated' row would silently put an ERG-only rider back on the stale-low
    passive number three weeks after they measured themselves - undoing the
    one thing this feature exists to do.
    """
    import datetime as dt

    uid = _register(client, "rider_durable")
    db.save_user_settings(uid, {"ftp": None})
    # An ordinary ride to estimate FROM: without one the passive estimator has
    # no evidence at all (it excludes the test), returns 0 and appends nothing,
    # and the test would pass whether or not the measurement is protected.
    importer.save_ride_record(
        user_id=uid,
        started_at=dt.datetime.combine(_test_day(), dt.time(7, 0, 0)),
        duration_s=3600,
        samples={"power": [150] * 3600, "cadence": [], "heartrate": []},
        session_name="Endurance",
        ftp=209,
    )
    activity_id, _ = _record_ramp_test(uid)
    body = client.post(
        "/api/ftp/ramp-test/accept", json={"activity_id": activity_id}
    ).json()
    measured = body["ftp"]

    later = utc_now() + dt.timedelta(days=22)
    importer.evaluate_ftp(uid, now=later)

    assert db.latest_ftp(uid)["source"] == "ramp_test"
    assert importer.current_ftp(uid, now=later) == pytest.approx(measured, abs=0.5)


def test_accepting_clears_a_typed_settings_override(client):
    """user_settings.ftp outranks ftp_history, so leaving it would change nothing.

    A rider who once typed an FTP into Settings would otherwise accept a
    measured result and see every prescription carry on from the old number.
    Accepting a measurement IS the rider replacing their own earlier
    statement, so the statement goes and the response says it went.
    """
    uid = _register(client, "rider_override")
    db.save_user_settings(uid, {"ftp": 209.0})
    activity_id, _ = _record_ramp_test(uid)
    assert db.get_user_settings(uid).get("ftp") == pytest.approx(209.0)

    r = client.post("/api/ftp/ramp-test/accept", json={"activity_id": activity_id})
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["cleared_override"] == pytest.approx(209.0)
    assert db.get_user_settings(uid).get("ftp") is None
    # The tested value is what everything is prescribed from now, not the 209.
    assert importer.current_ftp(uid) == pytest.approx(body["ftp"], abs=0.5)
    assert body["effective_ftp"] == pytest.approx(body["ftp"], abs=0.5)


def test_accepting_reports_the_entry_it_replaced(client):
    """Replacing a rider's own manual entry is disclosed, never silent."""
    uid = _register(client, "rider_replace_manual")
    db.save_user_settings(uid, {"ftp": None})
    db.add_ftp_entry(uid, _test_day().isoformat(), 250.0, "manual")
    activity_id, _ = _record_ramp_test(uid)

    body = client.post(
        "/api/ftp/ramp-test/accept", json={"activity_id": activity_id}
    ).json()
    assert body["replaced"] is not None
    assert body["replaced"]["source"] == "manual"
    assert float(body["replaced"]["ftp_watts"]) == pytest.approx(250.0)


def test_accepting_with_nothing_to_replace_says_so(client):
    uid = _register(client, "rider_replace_none")
    db.save_user_settings(uid, {"ftp": None})
    activity_id, _ = _record_ramp_test(uid)
    body = client.post(
        "/api/ftp/ramp-test/accept", json={"activity_id": activity_id}
    ).json()
    assert body["replaced"] is None
    assert body["cleared_override"] is None


def test_a_ride_that_was_not_a_ramp_test_is_refused(client):
    uid = _register(client, "rider_notatest")
    activity_id, _ = _record_ramp_test(uid, name="Threshold Intervals")
    r = client.post("/api/ftp/ramp-test/accept", json={"activity_id": activity_id})
    assert r.status_code == 400
    assert "not a ramp test" in r.json()["error"]
    assert _ramp_rows(uid) == []


def test_an_implausible_result_is_refused_by_the_route(client):
    uid = _register(client, "rider_implausible")
    activity_id, _ = _record_ramp_test(uid, ftp=30, steps_completed=13)
    r = client.post("/api/ftp/ramp-test/accept", json={"activity_id": activity_id})
    assert r.status_code == 400
    assert _ramp_rows(uid) == []


def test_accepting_does_not_rescore_prior_activities(client, monkeypatch):
    """Accepting a test dates a new FTP; it does not rewrite what came before.

    ftp_history is dated, so a row dated the test day changes nothing that
    preceded it. Rescoring is a separate, explicit action and must never run
    as a side effect of a rider confirming a number.
    """
    import datetime as dt

    from wattracker import ftp_rescore

    uid = _register(client, "rider_norescore")
    db.save_user_settings(uid, {"ftp": None})
    db.add_ftp_entry(uid, "2026-08-01", 209.0, "manual")

    prior_id, _ = importer.save_ride_record(
        user_id=uid,
        started_at=dt.datetime(2026, 8, 10, 9, 0, 0),
        duration_s=1800,
        samples={"power": [180] * 1800, "cadence": [], "heartrate": []},
        session_name="Endurance",
        ftp=209,
    )
    prior = db.get_activity(uid, prior_id)
    before = (prior["if_"], prior["tss"])
    assert before[0] and before[1]

    def refuse(*args, **kwargs):  # pragma: no cover - the point is it is unused
        raise AssertionError("accepting a ramp test must not rescore")

    monkeypatch.setattr(ftp_rescore, "rescore_imported_activities", refuse)
    monkeypatch.setattr(importer, "rescore_imported_activities", refuse)

    activity_id, _ = _record_ramp_test(uid)
    assert client.post(
        "/api/ftp/ramp-test/accept", json={"activity_id": activity_id}
    ).status_code == 200

    after = db.get_activity(uid, prior_id)
    assert (after["if_"], after["tss"]) == before


# ------------------------------------------------------------------- the page
def test_ride_page_offers_the_result_and_writes_only_on_acceptance(client):
    _register(client, "rider_page")
    text = client.get("/ride").text
    # The type is selectable in the picker, alongside Recovery and the rest.
    assert 'value="ramp_test"' in text
    assert RAMP_TEST_NAME in text
    # The prompt exists, and the only thing that writes is the accept button.
    assert 'id="rampTestDialog"' in text
    assert 'id="rampTestFtp"' in text
    assert "/api/ftp/ramp-test/accept" in text
    assert "showRampTestResult(st.ramp_test)" in text
    assert "innerHTML" not in text


def test_the_picker_states_the_ramp_rather_than_a_band(client):
    uid = _register(client, "rider_note")
    db.save_user_settings(uid, {"ftp": 209})
    data = client.get("/ride/workout/preview?type=ramp_test&minutes=30").json()
    info = data["type_info"]
    # No band watts to show, because there is no band - but the picker still
    # says what the session actually does instead of "no target".
    assert info["high_watts"] is None and info["work_watts"] is None
    assert "rising" in info["target_note"]
    assert data["duration_s"] == build_workout("ramp_test", 30).total_duration()


def test_variant_profiles_are_keyed_by_the_duration_actually_served(client):
    """The picker looks these up by duration_s / 60, so the keys must match.

    Keyed by the REQUESTED minutes instead, a session emitted at its own
    length (any measurement protocol) renders no shape card at all: the client
    asks for "29" and the payload only holds "60".
    """
    uid = _register(client, "rider_keys")
    db.save_user_settings(uid, {"ftp": 209})
    for kind in ("ramp_test", "endurance"):
        data = client.get(
            f"/ride/workout/preview?type={kind}&minutes=60"
        ).json()
        for variant, by_duration in data["variant_profiles"].items():
            for key, entry in by_duration.items():
                assert key == str(round(entry["duration_s"] / 60)), (kind, variant)


# ------------------------------------------------- the ceiling and the floor
def test_completing_every_step_is_reported_as_a_floor_not_a_measurement():
    """A rider who never failed did not find their limit; the ramp ran out.

    Reporting a cross-check "agreement" here would read as confirmation of a
    measurement that never happened.
    """
    stream = _ridden_stream(209, steps_completed=RAMP_TEST_STEPS)
    window = ramp_test_window(build_workout("ramp_test", 30))
    result = ramp_test.evaluate(stream, window, 209, len(stream))
    assert result["completed_ramp"] is True
    assert result["offer"] is True          # a floor is still worth accepting
    assert "FLOOR" in result["message"]
    assert "agrees at" not in result["message"]


def test_a_rider_who_fails_has_not_completed_the_ramp():
    stream = _ridden_stream(209, steps_completed=13)
    window = ramp_test_window(build_workout("ramp_test", 30))
    assert ramp_test.evaluate(
        stream, window, 209, len(stream)
    )["completed_ramp"] is False


def test_the_ceiling_clears_the_map_of_a_stale_low_ftp():
    """The arithmetic the step count exists to satisfy, stated once.

    MAP is about 1.33x a rider's TRUE FTP. The test is for a rider whose
    RECORDED FTP has gone stale-low, so the ramp has to keep climbing past
    1.33x the truth while it is anchored to the stale number. At 1.45x (20
    steps) the owner's own case - stored 209, true 215-225 - reaches the
    ceiling and gets a floor instead of a result.
    """
    session = build_workout("ramp_test", 30)
    steps = [seg for seg in session.segments if seg.kind == "steadystate"]
    top = steps[-1].power
    for staleness in (1.00, 1.10, 1.20, 1.30):
        map_fraction = 1.33 * staleness
        assert top > map_fraction, (staleness, top, map_fraction)


# ------------------------------------------------ the one-sample-per-second law
def test_a_stream_recorded_faster_than_one_hz_is_not_silently_misread():
    """index == second holds only while dt == 1, and nothing guaranteed that.

    Driving the controller at dt=0.5 stores twice as many samples as workout
    seconds. Read as 1 Hz, the "window" covers the first half of the ride and
    reports 133.5 W instead of 227.2 - a 41% under-report with offer=True. The
    rate is measured from the ride's own duration and sample count instead.
    """
    fast = _ridden_stream(209, steps_completed=13, dt=0.5)
    honest = _ridden_stream(209, steps_completed=13)
    window = ramp_test_window(build_workout("ramp_test", 30))
    duration_s = RAMP_TEST_WARMUP_S + 13 * RAMP_TEST_STEP_S
    assert len(fast) == 2 * duration_s

    result = ramp_test.evaluate(fast, window, 209, duration_s)
    reference = ramp_test.evaluate(honest, window, 209, duration_s)
    assert result["sample_rate"] == pytest.approx(2.0)
    assert result["ftp"] == pytest.approx(reference["ftp"], abs=0.5)
    # The 1 Hz shape detector cannot read 2 Hz blocks as minutes, so it is
    # reported unavailable rather than treated as a rival answer.
    assert result["cross_check_status"] == ramp_test.NOT_ONE_HZ
    assert result["disagreement"] is False


def test_a_stream_at_a_hopeless_sample_rate_is_refused_not_guessed():
    stream = _ridden_stream(209, steps_completed=13)
    window = ramp_test_window(build_workout("ramp_test", 30))
    # Ten samples a second: far outside any cadence the ride loop produces, so
    # there is no defensible mapping from workout seconds to indices.
    result = ramp_test.evaluate(stream, window, 209, len(stream) / 10.0)
    assert result["offer"] is False
    assert result["ftp"] == 0.0
    assert "not one sample a second" in result["message"]


def test_a_ride_with_no_recorded_duration_is_refused():
    stream = _ridden_stream(209, steps_completed=13)
    window = ramp_test_window(build_workout("ramp_test", 30))
    for duration in (0, None, "nonsense"):
        result = ramp_test.evaluate(stream, window, 209, duration)
        assert result["offer"] is False, duration


def test_mild_wall_clock_drift_still_measures_the_right_part_of_the_ride():
    """The live loop's real cadence is near, not exactly, one sample a second."""
    drifted = _ridden_stream(209, steps_completed=13, dt=1.15)
    honest = _ridden_stream(209, steps_completed=13)
    window = ramp_test_window(build_workout("ramp_test", 30))
    duration_s = RAMP_TEST_WARMUP_S + 13 * RAMP_TEST_STEP_S
    result = ramp_test.evaluate(drifted, window, 209, duration_s)
    reference = ramp_test.evaluate(honest, window, 209, duration_s)
    assert result["offer"] is True
    assert result["ftp"] == pytest.approx(reference["ftp"], rel=0.02)
