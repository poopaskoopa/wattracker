import datetime as dt
import sqlite3

import pytest

pytest.importorskip("httpx")
from fastapi.testclient import TestClient

from wattracker import auth, db
from wattracker.ingest import importer
from wattracker.prescribe import plan, zwo
from wattracker.prescribe.planner import build_workout
from wattracker.server import create_app
import wattracker.server as servermod
from wattracker.timeutil import utc_today


DATE = "2026-07-19"


def _workout(user_id, key="one", date=DATE, kind="threshold", ftp=200.0,
             minutes=60):
    session = build_workout(kind, minutes)
    xml = zwo.zwo_string(session)
    workout_id = db.add_standalone_workout(
        user_id, key, date, session.name, kind, session.total_duration(),
        session.estimated_tss, xml, ftp,
    )
    return workout_id, xml


def _activity(user_id, date, power, duration=None, suffix="a"):
    duration = duration if duration is not None else len(power)
    return db.insert_activity(user_id, {
        "dedup_hash": f"{user_id}-{date}-{suffix}",
        "filename": f"{suffix}.fit",
        "start_time": f"{date}T10:00:00",
        "duration_s": duration,
        "distance_m": 0,
        "avg_power": sum(power) / len(power) if power else None,
        "avg_hr": None,
        "np": None,
        "if_": None,
        "tss": 999,
        "streams": {"power": power},
    })


def _complete(user_id, key, rpe, compliance=.95, effective=220.0,
              date=DATE, kind="threshold", minutes=60):
    workout_id, _ = _workout(user_id, key, date, kind, minutes=minutes)
    assert db.mark_standalone_completed(
        user_id, workout_id, 10_000 + workout_id, date, compliance, effective
    )
    assert db.set_standalone_rpe(user_id, workout_id, rpe)
    return workout_id


def _plan_workout(user_id, key="plan", kind="threshold", date=DATE,
                  export_ftp=None):
    session = build_workout(kind, 60)
    plan_id = db.create_plan(user_id, key, date, 1)
    workout_id = db.add_plan_workout(
        plan_id, user_id, date, session.name, kind, session.total_duration(),
        session.estimated_tss, zwo.zwo_string(session), export_ftp=export_ftp,
    )
    return workout_id, zwo.zwo_string(session)


def _complete_plan(user_id, key, rpe, kind="threshold", effective=220.0):
    workout_id, xml = _plan_workout(user_id, key, kind)
    profile = importer._zwo_fraction_profile(xml)
    activity_id = _activity(
        user_id, DATE, [p * effective for p in profile], suffix=key
    )
    assert db.mark_plan_workout_completed(
        user_id, workout_id, activity_id, DATE, .95, effective
    )
    assert importer.save_workout_rpe(user_id, "plan", workout_id, rpe,
                                     dt.datetime.fromisoformat(f"{DATE}T20:00:00"))
    return workout_id


def test_v14_migration_preserves_data_and_adds_standalone_table(tmp_path):
    path = str(tmp_path / "migration.db")
    db.init_db(path)
    uid = db.create_user("kept", "hash", path)
    conn = sqlite3.connect(path)
    conn.execute("DROP TABLE standalone_workouts")
    conn.execute("PRAGMA user_version=14")
    conn.commit()
    conn.close()

    db.init_db(path)

    conn = sqlite3.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    assert conn.execute("SELECT username FROM users WHERE id=?", (uid,)).fetchone()[0] == "kept"
    assert conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='standalone_workouts'"
    ).fetchone()
    assert conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='ftp_feedback_batches'"
    ).fetchone()
    columns = {row[1] for row in conn.execute("PRAGMA table_info(plan_workouts)")}
    assert {"compliance", "effective_ftp", "feedback_batch_id"} <= columns
    conn.close()


def test_standalone_persistence_is_idempotent_and_user_scoped(user_id):
    first, _ = _workout(user_id)
    second, _ = _workout(user_id)
    other = db.create_user("other", auth.hash_password("password123"))
    third, _ = _workout(other)

    assert first == second
    assert third != first
    assert len(db.standalone_workouts_on_date(user_id, DATE)) == 1
    assert len(db.standalone_workouts_on_date(other, DATE)) == 1


def test_export_route_persists_only_after_success(user_id, monkeypatch, tmp_path):
    app = create_app()
    session = build_workout("threshold", 60)
    xml = zwo.zwo_string(session)
    app.state.last[user_id] = {
        "zwo": xml, "name": session.name, "type": "threshold",
        "duration_s": session.total_duration(), "tss": session.estimated_tss,
        "export_ftp": 200,
    }
    output = tmp_path / "dated.zwo"
    monkeypatch.setattr(
        servermod.zwo, "write_plan_to_zwift",
        lambda *args, **kwargs: {"paths": [str(output)]},
    )
    monkeypatch.setattr(servermod.pipeline, "build_state", lambda uid: type("S", (), {"ftp": 200})())
    monkeypatch.setattr(servermod, "plan_workout",
                        lambda state, minutes, profile=None: session)
    monkeypatch.setattr(servermod.llm, "shape_session", lambda value, state: value)
    with TestClient(app) as client:
        client.post(
            "/login", data={"username": "tester", "password": "password123"},
            follow_redirects=False,
        )
        generated = client.post("/generate", data={"duration_min": 60})
        assert f'name="scheduled_date" value="{utc_today().isoformat()}"' in generated.text
        response = client.post("/generate/export", data={"scheduled_date": DATE})
        assert response.status_code == 200
        assert str(output) in response.text
        client.post("/generate/export", data={"scheduled_date": DATE})
    assert len(db.standalone_workouts_on_date(user_id, DATE)) == 1


def test_failed_export_does_not_persist(user_id, monkeypatch):
    app = create_app()
    session = build_workout("threshold", 60)
    app.state.last[user_id] = {
        "zwo": zwo.zwo_string(session), "name": session.name,
        "type": "threshold", "duration_s": session.total_duration(),
        "tss": session.estimated_tss, "export_ftp": 200,
    }

    def fail(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(servermod.zwo, "write_plan_to_zwift", fail)
    with TestClient(app, raise_server_exceptions=False) as client:
        client.post("/login", data={"username": "tester", "password": "password123"})
        assert client.post(
            "/generate/export", data={"scheduled_date": DATE}
        ).status_code == 500
    assert db.standalone_workouts_on_date(user_id, DATE) == []


def test_profile_matching_requires_date_user_and_target_similarity(user_id):
    workout_id, xml = _workout(user_id)
    target = importer._zwo_fraction_profile(xml)
    matching = [fraction * 210 for fraction in target]
    activity_id = _activity(user_id, DATE, matching, suffix="match")

    assert importer.match_standalone_completions(
        user_id, dt.datetime.fromisoformat(f"{DATE}T20:00:00")
    ) == 1
    stored = db.get_standalone_workout(user_id, workout_id)
    assert stored["completed_activity_id"] == activity_id
    assert stored["compliance"] >= importer.PROFILE_MIN_COMPLIANCE
    assert stored["effective_ftp"] == pytest.approx(210, rel=.02)


def test_threshold_does_not_match_same_duration_endurance_profile(user_id):
    """Structure separates these; neither average power nor scale can.

    A right-sized threshold hour is mostly one long near-steady effort, so a
    Zone 2 hour fits its shape at 0.92 compliance - over the gate - at a scale
    of 151W against a 200W export, which is a wattage a genuine completion can
    also legitimately land on. What gives it away is that the ride never dips
    for the recoveries. See PROFILE_MIN_STRUCTURE_RATIO.
    """
    workout_id, _ = _workout(user_id, key="threshold-vs-endurance")
    endurance = zwo.zwo_string(build_workout("endurance", 60))
    profile = importer._zwo_fraction_profile(endurance)
    _activity(user_id, DATE, [fraction * 200 for fraction in profile],
              suffix="endurance-profile")

    assert importer.match_standalone_completions(
        user_id, dt.datetime.fromisoformat(f"{DATE}T20:00:00")
    ) == 0
    assert db.get_standalone_workout(user_id, workout_id)["completed_activity_id"] is None


def test_slightly_noisy_genuine_threshold_profile_still_matches(user_id):
    workout_id, xml = _workout(user_id, key="noisy-threshold")
    target = importer._zwo_fraction_profile(xml)
    noisy = [fraction * 205 * (0.97 if second % 2 else 1.03)
             for second, fraction in enumerate(target)]
    activity_id = _activity(user_id, DATE, noisy, suffix="noisy-threshold")

    assert importer.match_standalone_completions(
        user_id, dt.datetime.fromisoformat(f"{DATE}T20:00:00")
    ) == 1
    stored = db.get_standalone_workout(user_id, workout_id)
    assert stored["completed_activity_id"] == activity_id
    assert stored["compliance"] >= importer.PROFILE_MIN_COMPLIANCE


def test_genuine_completion_below_the_export_ftp_still_matches(user_id):
    """The scale gate must not reject a rider whose FTP moved since export.

    175W against a workout exported at 200W is a real completion by someone
    whose trainer FTP is set 12% lower - it has to survive the same check that
    rejects the 150W endurance ride.
    """
    workout_id, xml = _workout(user_id, key="lower-ftp")
    target = importer._zwo_fraction_profile(xml)
    activity_id = _activity(user_id, DATE, [fraction * 175 for fraction in target],
                            suffix="lower-ftp")

    assert importer.match_standalone_completions(
        user_id, dt.datetime.fromisoformat(f"{DATE}T20:00:00")
    ) == 1
    stored = db.get_standalone_workout(user_id, workout_id)
    assert stored["completed_activity_id"] == activity_id
    assert stored["effective_ftp"] == pytest.approx(175, rel=.02)


def test_scheduled_plan_wins_activity_conflict_and_keeps_profile_evidence(user_id):
    plan_id, xml = _plan_workout(user_id)
    standalone_id, _ = _workout(user_id, key="standalone-conflict")
    target = importer._zwo_fraction_profile(xml)
    activity_id = _activity(user_id, DATE, [p * 210 for p in target], suffix="conflict")

    assert importer.match_plan_completions(
        user_id, dt.datetime.fromisoformat(f"{DATE}T20:00:00")
    ) == 1
    plan = db.get_plan_workout(user_id, plan_id)
    assert plan["completed_activity_id"] == activity_id
    assert plan["compliance"] >= importer.PROFILE_MIN_COMPLIANCE
    assert plan["effective_ftp"] == pytest.approx(210, rel=.02)
    assert db.get_standalone_workout(user_id, standalone_id)["completed_activity_id"] is None


def test_plan_profile_match_is_preferred_over_legacy_duration_fallback(user_id):
    plan_id, xml = _plan_workout(user_id)
    target = importer._zwo_fraction_profile(xml)
    _activity(user_id, DATE, [], duration=len(target), suffix="legacy-exact")
    profile_id = _activity(
        user_id, DATE, [p * 210 for p in target],
        duration=round(len(target) * 1.05), suffix="profile-preferred",
    )

    assert importer.match_plan_completions(
        user_id, dt.datetime.fromisoformat(f"{DATE}T20:00:00")
    ) == 1
    assert db.get_plan_workout(user_id, plan_id)["completed_activity_id"] == profile_id


@pytest.mark.parametrize("failure", ["date", "user", "profile"])
def test_standalone_matching_rejects_false_matches(user_id, failure):
    _, xml = _workout(user_id)
    target = importer._zwo_fraction_profile(xml)
    owner = user_id
    date = DATE
    power = [fraction * 210 for fraction in target]
    if failure == "date":
        date = "2026-07-18"
    elif failure == "user":
        owner = db.create_user("other", auth.hash_password("password123"))
    else:
        power = [0.0] * len(target)
    _activity(owner, date, power, suffix=failure)

    assert importer.match_standalone_completions(
        user_id, dt.datetime.fromisoformat(f"{DATE}T20:00:00")
    ) == 0


def test_profile_unavailable_never_matches_by_duration_alone(user_id):
    _, xml = _workout(user_id)
    duration = len(importer._zwo_fraction_profile(xml))
    _activity(user_id, DATE, [], duration=duration, suffix="fallback")

    assert importer.match_standalone_completions(
        user_id, dt.datetime.fromisoformat(f"{DATE}T20:00:00")
    ) == 0


def test_malformed_standalone_profile_never_matches(user_id):
    workout_id = db.add_standalone_workout(
        user_id, "bad-xml", DATE, "Broken", "threshold", 3600, 100,
        "<not-zwo", 200,
    )
    _activity(user_id, DATE, [200] * 3600, suffix="malformed")
    assert importer.match_standalone_completions(
        user_id, dt.datetime.fromisoformat(f"{DATE}T20:00:00")
    ) == 0
    assert db.get_standalone_workout(user_id, workout_id)["completed_activity_id"] is None


def test_pending_queue_rating_boundaries_correction_and_user_scope(user_id):
    workout_id, _ = _workout(user_id)
    db.mark_standalone_completed(user_id, workout_id, 123, DATE, .95, 210)
    other = db.create_user("other", auth.hash_password("password123"))

    assert [item["id"] for item in db.pending_ratings(user_id)] == [workout_id]
    assert not db.set_standalone_rpe(other, workout_id, 1)
    assert db.set_standalone_rpe(user_id, workout_id, 1)
    assert db.set_standalone_rpe(user_id, workout_id, 10)
    assert db.get_standalone_workout(user_id, workout_id)["rpe"] == 10
    assert db.pending_ratings(user_id) == []


def test_rating_api_validates_boundary_and_queue_is_on_dashboard_and_calendar(user_id):
    workout_id, _ = _workout(user_id)
    db.mark_standalone_completed(user_id, workout_id, 123, DATE, .95, 210)
    app = create_app()
    with TestClient(app) as client:
        client.post("/login", data={"username": "tester", "password": "password123"})
        assert "Rate completed workouts" in client.get("/").text
        assert "Rate completed workouts" in client.get("/calendar?year=2026&month=7").text
        assert client.post(
            f"/api/standalone-workout/{workout_id}/rpe", json={"rpe": 0}
        ).status_code == 400
        assert client.post(
            f"/api/standalone-workout/{workout_id}/rpe", json={"rpe": 10}
        ).status_code == 200
        assert "Rate completed workouts" not in client.get("/").text


def test_feedback_raises_and_caps_then_is_idempotent(user_id):
    db.add_ftp_entry(user_id, DATE, 200, "estimated")
    ids = [_complete(user_id, f"up-{n}", 6, effective=230) for n in range(2)]

    assert importer.apply_rpe_ftp_feedback(
        user_id, dt.datetime.fromisoformat(f"{DATE}T20:00:00")
    ) == 210.0
    assert db.latest_ftp(user_id)["ftp_watts"] == 210.0
    assert importer.apply_rpe_ftp_feedback(
        user_id, dt.datetime.fromisoformat(f"{DATE}T20:00:00")
    ) is None
    assert all(db.get_standalone_workout(user_id, item)["feedback_applied"] for item in ids)


def test_feedback_lowers_caps_and_ignores_insufficient_or_bad_evidence(user_id):
    db.add_ftp_entry(user_id, DATE, 200, "estimated")
    _complete(user_id, "only", 9)
    _complete(user_id, "easy", 9, kind="endurance")
    _complete(user_id, "bad", 9, compliance=.5)
    assert importer.apply_rpe_ftp_feedback(
        user_id, dt.datetime.fromisoformat(f"{DATE}T20:00:00")
    ) is None
    _complete(user_id, "second", 10)
    assert importer.apply_rpe_ftp_feedback(
        user_id, dt.datetime.fromisoformat(f"{DATE}T20:00:00")
    ) == 192.5


def test_feedback_never_changes_manual_ftp(user_id):
    db.add_ftp_entry(user_id, DATE, 200, "estimated")
    _complete(user_id, "manual-1", 5, effective=230)
    _complete(user_id, "manual-2", 5, effective=230)
    db.save_user_settings(user_id, {"ftp": 250})

    assert importer.apply_rpe_ftp_feedback(
        user_id, dt.datetime.fromisoformat(f"{DATE}T20:00:00")
    ) is None
    assert db.latest_ftp(user_id)["ftp_watts"] == 200


@pytest.mark.parametrize(("kind", "rpe", "expected"), [
    ("sweet_spot", 7, 210.0),
    ("sweet_spot", 8, None),
    ("sweet_spot", 9, 195.0),
    ("threshold", 7, 210.0),
    ("threshold", 8, None),
    ("threshold", 9, 195.0),
    ("vo2max", 8, 210.0),
    ("vo2max", 9, None),
    ("vo2max", 10, 195.0),
])
def test_feedback_expected_rpe_bands_are_type_aware(user_id, kind, rpe, expected):
    db.add_ftp_entry(user_id, DATE, 200, "estimated")
    _complete(user_id, f"{kind}-1", rpe, kind=kind, effective=230)
    _complete(user_id, f"{kind}-2", rpe, kind=kind, effective=230)
    result = importer.apply_rpe_ftp_feedback(
        user_id, dt.datetime.fromisoformat(f"{DATE}T20:00:00")
    )
    assert result == expected
    assert db.latest_ftp(user_id)["ftp_watts"] == (expected or 200)


def test_rating_correction_rolls_back_batch_and_is_idempotent(user_id):
    db.add_ftp_entry(user_id, DATE, 200, "estimated")
    first = _complete(user_id, "correct-1", 6, effective=230)
    _complete(user_id, "correct-2", 6, effective=230)
    assert importer.apply_rpe_ftp_feedback(
        user_id, dt.datetime.fromisoformat(f"{DATE}T20:00:00")
    ) == 210.0

    now = dt.datetime.fromisoformat(f"{DATE}T20:00:00")
    assert importer.save_workout_rpe(user_id, "standalone", first, 9, now)
    assert db.latest_ftp(user_id)["ftp_watts"] == 200.0
    assert importer.save_workout_rpe(user_id, "standalone", first, 9, now)
    assert db.latest_ftp(user_id)["ftp_watts"] == 200.0


def test_plan_ratings_share_feedback_policy(user_id):
    db.add_ftp_entry(user_id, DATE, 200, "estimated")
    first = _complete_plan(user_id, "plan-feedback-1", 6, effective=230)
    assert db.latest_ftp(user_id)["ftp_watts"] == 200
    second = _complete_plan(user_id, "plan-feedback-2", 6, effective=230)
    assert db.latest_ftp(user_id)["ftp_watts"] == 210.0
    assert db.get_plan_workout(user_id, first)["feedback_applied"]
    assert db.get_plan_workout(user_id, second)["feedback_applied"]


def test_valid_plan_rpe_ten_participates_in_ftp_feedback(user_id):
    db.add_ftp_entry(user_id, DATE, 200, "estimated")
    first = _complete_plan(user_id, "plan-too-hard-1", 10)
    assert db.latest_ftp(user_id)["ftp_watts"] == 200
    second = _complete_plan(user_id, "plan-too-hard-2", 10)

    # threshold RPE 10 -> 2 points over neutral (8) -> 2 * 0.025 = 5% drop.
    assert db.latest_ftp(user_id)["ftp_watts"] == 190.0
    assert db.get_plan_workout(user_id, first)["feedback_applied"]
    assert db.get_plan_workout(user_id, second)["feedback_applied"]


def test_invalid_plan_evidence_cannot_change_estimated_ftp(user_id):
    db.add_ftp_entry(user_id, DATE, 200, "estimated")

    wrong_date, xml = _plan_workout(user_id, "wrong-date-feedback")
    profile = importer._zwo_fraction_profile(xml)
    wrong_date_activity = _activity(
        user_id,
        "2026-07-18",
        [p * 220 for p in profile],
        suffix="wrong-date-feedback",
    )
    assert db.mark_plan_workout_completed(
        user_id, wrong_date, wrong_date_activity, DATE, .95, 220
    )
    assert db.set_plan_workout_rpe(user_id, wrong_date, 10)

    missing_activity, _ = _plan_workout(user_id, "missing-feedback")
    assert db.mark_plan_workout_completed(
        user_id, missing_activity, 999_999, DATE, .95, 220
    )
    assert db.set_plan_workout_rpe(user_id, missing_activity, 10)

    assert not importer.plan_workout_completion_verified(
        user_id, db.get_plan_workout(user_id, wrong_date)
    )
    assert not importer.plan_workout_completion_verified(
        user_id, db.get_plan_workout(user_id, missing_activity)
    )
    assert importer.apply_rpe_ftp_feedback(
        user_id, dt.datetime.fromisoformat(f"{DATE}T20:00:00")
    ) is None
    assert db.latest_ftp(user_id)["ftp_watts"] == 200
    assert not db.get_plan_workout(user_id, wrong_date)["feedback_applied"]
    assert not db.get_plan_workout(user_id, missing_activity)["feedback_applied"]


def test_mixed_plan_standalone_batch_rolls_back_across_kinds(user_id):
    db.add_ftp_entry(user_id, DATE, 200, "estimated")
    standalone = _complete(user_id, "mixed-standalone", 6, effective=230)
    plan = _complete_plan(user_id, "mixed-plan", 6, effective=230)
    assert db.latest_ftp(user_id)["ftp_watts"] == 210.0

    now = dt.datetime.fromisoformat(f"{DATE}T20:00:00")
    assert importer.save_workout_rpe(user_id, "plan", plan, 9, now)
    assert db.latest_ftp(user_id)["ftp_watts"] == 200.0
    assert not db.get_standalone_workout(user_id, standalone)["feedback_applied"]


# ------------------------------------------------------------------ sweeps
# These pin the boundary the profile matcher is actually asked to police: a
# Zone 2 endurance hour must never be recorded as a completed hard session (its
# fabricated effective_ftp would feed the RPE -> FTP loop), while a genuine
# completion must survive across every wattage a real rider lands on. The
# metric is scale-free, so a single wattage proves nothing - both sides are
# swept. See PROFILE_MIN_STRUCTURE_RATIO in importer.py.

# The three kinds that clear PROFILE_MIN_HARD_SECONDS, i.e. the only ones that
# can mint an effective_ftp.
_HARD_KINDS = ("threshold", "sweet_spot", "vo2max")


def _prescription(kind, export_ftp=200.0, minutes=60):
    session = build_workout(kind, minutes)
    return {"zwo": zwo.zwo_string(session), "export_ftp": export_ftp}


def _matches(workout, power):
    """The accept/reject decision every call site makes, on one power stream."""
    evidence = importer._profile_evidence({"streams": {"power": power}}, workout)
    if evidence is None:
        return False, 0.0
    return evidence[0] >= importer.PROFILE_MIN_COMPLIANCE, evidence[0]


def test_endurance_hours_never_match_a_structured_prescription():
    """No flat Zone 2 hour from 150W to 400W may complete a hard session."""
    z2_shape = importer._zwo_fraction_profile(
        zwo.zwo_string(build_workout("endurance", 60))
    )
    matched = []
    for kind in _HARD_KINDS:
        workout = _prescription(kind)
        for watts in range(150, 401, 5):
            for label, power in (
                ("flat", [float(watts)] * 3600),
                # The harder look-alike: a real Zone 2 ride, with its own
                # warmup ramp and cooldown, so it is not literally constant.
                ("shaped", [f * watts for f in z2_shape]),
            ):
                ok, compliance = _matches(workout, power)
                if ok:
                    matched.append((kind, label, watts, round(compliance, 3)))
    assert matched == []


def test_genuine_completions_match_across_the_realistic_wattage_band():
    """171W-260W realized against a 200W export, plus an imperfect ride."""
    missed = []
    for kind in _HARD_KINDS:
        workout = _prescription(kind)
        target = importer._zwo_fraction_profile(workout["zwo"])
        for watts in range(171, 261):
            ok, compliance = _matches(workout, [f * watts for f in target])
            if not ok:
                missed.append((kind, watts, round(compliance, 3)))
        # Imperfect completions: second-by-second jitter, a mid-ride pause, and
        # a rider who skipped the warmup ramp and rode straight into the work.
        imperfect = {
            "jitter": [f * 205 * (1.09 if s % 2 else 0.91)
                       for s, f in enumerate(target)],
            # A two-minute dead stop. Longer stops are rejected on compliance,
            # which predates the structure check and is a separate judgement.
            "paused": ([f * 205 for f in target[:1800]] + [0.0] * 120
                       + [f * 205 for f in target[1800:]]),
            "no_warmup": [max(f, 0.85) * 205 if s < 600 else f * 205
                          for s, f in enumerate(target)],
        }
        for label, power in imperfect.items():
            ok, compliance = _matches(workout, power)
            if not ok:
                missed.append((kind, label, round(compliance, 3)))
    assert missed == []


def test_endurance_prescription_still_accepts_a_steady_ride(user_id):
    """A near-flat prescription has no structure to demand of the rider."""
    workout_id, _ = _workout(user_id, key="steady-z2", kind="endurance")
    activity_id = _activity(user_id, DATE, [145.0] * 3600, suffix="steady-z2")

    assert importer.match_standalone_completions(
        user_id, dt.datetime.fromisoformat(f"{DATE}T20:00:00")
    ) == 1
    stored = db.get_standalone_workout(user_id, workout_id)
    assert stored["completed_activity_id"] == activity_id
    # No block at >= 85% FTP, so no FTP evidence is minted from it.
    assert stored["effective_ftp"] is None


# ------------------------------------------------- plan-workout export FTP

def test_plan_completion_matches_with_and_without_stored_export_ftp(user_id):
    """Plan rows now carry the FTP they were generated at; legacy rows are NULL.

    Both must still complete end-to-end - the new column is a sanity rail on
    the fitted wattage, not a new requirement.
    """
    now = dt.datetime.fromisoformat(f"{DATE}T20:00:00")
    for suffix, export_ftp in (("with-ftp", 200.0), ("legacy", None)):
        uid = db.create_user(f"rider-{suffix}", auth.hash_password("password123"))
        workout_id, xml = _plan_workout(uid, key=suffix, export_ftp=export_ftp)
        assert db.get_plan_workout(uid, workout_id)["export_ftp"] == export_ftp
        target = importer._zwo_fraction_profile(xml)
        activity_id = _activity(uid, DATE, [f * 205 for f in target],
                                suffix=suffix)

        assert importer.match_plan_completions(uid, now) == 1
        stored = db.get_plan_workout(uid, workout_id)
        assert stored["completed_activity_id"] == activity_id
        assert stored["effective_ftp"] == pytest.approx(205, rel=.02)


def test_stored_export_ftp_rejects_an_implausible_plan_wattage(user_id):
    """The rail only bites once the column is populated (legacy rows keep NULL)."""
    now = dt.datetime.fromisoformat(f"{DATE}T20:00:00")
    workout_id, xml = _plan_workout(user_id, key="rail", export_ftp=200.0)
    target = importer._zwo_fraction_profile(xml)
    # 400W against a 200W export is twice the prescription: the shape fits, the
    # wattage cannot be a completion of this workout.
    _activity(user_id, DATE, [f * 400 for f in target], suffix="rail")

    assert importer.match_plan_completions(user_id, now) == 0
    assert db.get_plan_workout(user_id, workout_id)["completed_activity_id"] is None


def test_v26_migration_adds_plan_export_ftp_without_losing_rows(tmp_path):
    path = str(tmp_path / "v26.db")
    db.init_db(path)
    uid = db.create_user("kept", "hash", path)
    plan_id = db.create_plan(uid, "Base", DATE, 1, path=path)
    workout_id = db.add_plan_workout(
        plan_id, uid, DATE, "Threshold", "threshold", 3600, 65.0, "<x/>",
        export_ftp=210.0, path=path,
    )
    conn = sqlite3.connect(path)
    conn.execute("ALTER TABLE plan_workouts DROP COLUMN export_ftp")
    conn.execute("PRAGMA user_version = 26")
    conn.commit()
    conn.close()

    db.init_db(path)

    conn = sqlite3.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    assert "export_ftp" in {
        row[1] for row in conn.execute("PRAGMA table_info(plan_workouts)")
    }
    conn.close()
    # The row survived, and a pre-existing workout keeps the loose fallback
    # rather than being retro-rejected against a guessed FTP.
    stored = db.get_plan_workout(uid, workout_id, path=path)
    assert stored["name"] == "Threshold"
    assert stored["export_ftp"] is None


# ------------------------------------------- dose-aware expected effort (RPE)
def test_zwo_hard_seconds_matches_plan_hard_seconds_for_every_prescription():
    """The stored-XML reader and plan.hard_seconds are one definition.

    ``hard_seconds`` needs a planner Session, which a completed workout no
    longer has - only the .zwo it was exported as. This pins the two to the same
    number for every workout the planner can build.
    """
    for kind in ("threshold", "sweet_spot", "vo2max", "endurance"):
        for minutes in (45, 60, 75, 90):
            session = build_workout(kind, minutes)
            assert importer._zwo_hard_seconds(zwo.zwo_string(session)) == \
                plan.hard_seconds(session), (kind, minutes)


def test_zwo_hard_seconds_survives_junk():
    assert importer._zwo_hard_seconds(None) == 0
    assert importer._zwo_hard_seconds("not xml") == 0
    assert importer._zwo_hard_seconds("<workout_file></workout_file>") == 0


def test_neutral_rpe_scales_down_with_a_short_dose_but_never_up():
    full_threshold = plan.hard_seconds(build_workout("threshold", 60))
    assert full_threshold == 2160  # 36 min in zone, the reference dose
    # A full dose keeps the type's long-standing neutral exactly.
    assert importer._neutral_rpe("threshold", full_threshold) == 8
    assert importer._neutral_rpe("sweet_spot",
                                 plan.hard_seconds(build_workout("sweet_spot", 60))) == 8
    assert importer._neutral_rpe("vo2max",
                                 plan.hard_seconds(build_workout("vo2max", 60))) == 9
    # The owner's session: a 2x13 threshold, 26 of the reference 36 minutes.
    assert plan.hard_seconds(build_workout("threshold", 45)) == 1560
    assert importer._neutral_rpe("threshold", 1560) == 6
    # More time in zone than the reference is a different stimulus, not proof
    # that an 8 means the FTP is low - the neutral is never raised.
    assert importer._neutral_rpe("threshold", 4 * full_threshold) == 8
    # A tiny dose floors out rather than sliding to 1, which would turn any
    # ordinary rating into "too hard, drop the FTP".
    assert importer._neutral_rpe("threshold", 60) == 5
    assert importer._neutral_rpe("vo2max", 60) == 6
    # Unknown dose (unparseable or absent prescription) = today's flat neutral.
    assert importer._neutral_rpe("threshold", 0) == 8
    assert importer._neutral_rpe("threshold") == 8
    assert importer._neutral_rpe("endurance", 1) == 8


def test_truncated_session_honest_low_rating_no_longer_inflates_ftp(user_id):
    """26 of 36 minutes in zone rated 6/10 is exactly what should happen."""
    db.add_ftp_entry(user_id, DATE, 200, "estimated")
    for n in range(2):
        _complete(user_id, f"short-{n}", 6, effective=230, minutes=45)

    assert importer.apply_rpe_ftp_feedback(
        user_id, dt.datetime.fromisoformat(f"{DATE}T20:00:00")
    ) is None
    assert db.latest_ftp(user_id)["ftp_watts"] == 200


def test_full_volume_session_low_rating_still_raises_ftp(user_id):
    """The full-dose path is untouched: 36 min in zone at 6/10 still moves."""
    db.add_ftp_entry(user_id, DATE, 200, "estimated")
    for n in range(2):
        _complete(user_id, f"full-{n}", 6, effective=230, minutes=60)

    assert importer.apply_rpe_ftp_feedback(
        user_id, dt.datetime.fromisoformat(f"{DATE}T20:00:00")
    ) == 210.0


def test_truncated_session_can_still_move_ftp_when_the_rating_is_low_enough(user_id):
    """Volume-awareness lowers the bar, it does not disconnect the loop."""
    db.add_ftp_entry(user_id, DATE, 200, "estimated")
    for n in range(2):
        _complete(user_id, f"very-easy-{n}", 4, effective=230, minutes=45)

    # Neutral 6, rated 4 -> 2 points under -> the same 5% step a full session
    # rated 6 would have produced.
    assert importer.apply_rpe_ftp_feedback(
        user_id, dt.datetime.fromisoformat(f"{DATE}T20:00:00")
    ) == 210.0


# ----------------------------------------------- suggestions under manual FTP
def _manual_evidence(user_id, manual=240, rpe=6, effective=230, minutes=60):
    db.add_ftp_entry(user_id, DATE, 200, "estimated")
    ids = [_complete(user_id, f"sugg-{n}", rpe, effective=effective,
                     minutes=minutes) for n in range(2)]
    db.set_user_ftp_override(user_id, manual)
    return ids


def test_manual_ftp_never_moves_but_the_evidence_becomes_a_suggestion(user_id):
    ids = _manual_evidence(user_id)

    assert importer.apply_rpe_ftp_feedback(
        user_id, dt.datetime.fromisoformat(f"{DATE}T20:00:00")
    ) is None
    # Nothing moved: not the manual value, not the estimated history row.
    assert db.get_user_settings(user_id)["ftp"] == 240
    assert db.latest_ftp(user_id)["ftp_watts"] == 200

    suggestion = db.pending_ftp_suggestion(user_id)
    assert suggestion is not None
    # Judged against the FTP the rider actually trained at (240), capped at 5%.
    assert suggestion["current_ftp"] == 240.0
    assert suggestion["suggested_ftp"] == 252.0
    assert suggestion["workouts"] == 2
    assert [item["rpe"] for item in suggestion["evidence"]] == [6, 6]
    assert {item["type"] for item in suggestion["evidence"]} == {"threshold"}
    assert all(item["hard_minutes"] == 36 for item in suggestion["evidence"])
    # The evidence is consumed exactly as the applied path consumes it, so the
    # same workouts cannot produce a second suggestion.
    assert all(db.get_standalone_workout(user_id, i)["feedback_applied"] for i in ids)
    assert importer.apply_rpe_ftp_feedback(
        user_id, dt.datetime.fromisoformat(f"{DATE}T20:00:00")
    ) is None
    assert db.pending_ftp_suggestion(user_id)["id"] == suggestion["id"]


def test_suggestion_survives_a_restart(user_id, tmp_path):
    """It is a row, not process state: a fresh connection still sees it."""
    _manual_evidence(user_id)
    importer.apply_rpe_ftp_feedback(
        user_id, dt.datetime.fromisoformat(f"{DATE}T20:00:00")
    )
    conn = sqlite3.connect(db.db_path())
    try:
        row = conn.execute(
            "SELECT suggested_ftp,status FROM ftp_suggestions WHERE user_id=?",
            (user_id,),
        ).fetchone()
    finally:
        conn.close()
    assert row == (252.0, "pending")


def test_accepting_a_suggestion_writes_it_as_the_manual_value(user_id):
    _manual_evidence(user_id)
    importer.apply_rpe_ftp_feedback(
        user_id, dt.datetime.fromisoformat(f"{DATE}T20:00:00")
    )
    suggestion = db.pending_ftp_suggestion(user_id)

    app = create_app()
    with TestClient(app) as client:
        client.post("/login", data={"username": "tester", "password": "password123"})
        assert "Suggested Training FTP" in client.get("/settings").text
        assert "Suggested Training FTP" in client.get("/profile").text
        response = client.post("/ftp-suggestion", data={
            "suggestion_id": suggestion["id"], "action": "use",
            "next_path": "/settings",
        })
        assert response.status_code == 200  # followed the 303
        assert "Suggested Training FTP" not in response.text

    assert db.get_user_settings(user_id)["ftp"] == 252
    assert importer.current_ftp(user_id) == 252.0
    assert db.pending_ftp_suggestion(user_id) is None
    # Still nothing automatic: the estimated history row was never touched.
    assert db.latest_ftp(user_id)["ftp_watts"] == 200


def test_dismissing_a_suggestion_changes_nothing_and_it_does_not_return(user_id):
    _manual_evidence(user_id)
    importer.apply_rpe_ftp_feedback(
        user_id, dt.datetime.fromisoformat(f"{DATE}T20:00:00")
    )
    suggestion = db.pending_ftp_suggestion(user_id)

    app = create_app()
    with TestClient(app) as client:
        client.post("/login", data={"username": "tester", "password": "password123"})
        response = client.post("/ftp-suggestion", data={
            "suggestion_id": suggestion["id"], "action": "dismiss",
            "next_path": "/profile",
        })
        assert response.status_code == 200
        assert "Suggested Training FTP" not in response.text

    assert db.get_user_settings(user_id)["ftp"] == 240
    assert db.latest_ftp(user_id)["ftp_watts"] == 200
    assert db.pending_ftp_suggestion(user_id) is None
    # The same evidence must not immediately produce it again.
    assert importer.apply_rpe_ftp_feedback(
        user_id, dt.datetime.fromisoformat(f"{DATE}T20:00:00")
    ) is None
    assert db.pending_ftp_suggestion(user_id) is None


def test_a_suggestion_belongs_to_its_owner(user_id):
    _manual_evidence(user_id)
    importer.apply_rpe_ftp_feedback(
        user_id, dt.datetime.fromisoformat(f"{DATE}T20:00:00")
    )
    suggestion = db.pending_ftp_suggestion(user_id)
    other = db.create_user("intruder", auth.hash_password("password123"))

    assert db.resolve_ftp_suggestion(other, suggestion["id"], "accepted") is None
    assert db.pending_ftp_suggestion(user_id)["id"] == suggestion["id"]
    assert db.get_user_settings(other).get("ftp") in (None, 0)


def test_evidence_implying_no_change_is_consumed_without_a_suggestion(user_id):
    """A manual FTP the evidence agrees with produces no banner, and the
    evidence is still spent rather than re-examined forever."""
    db.add_ftp_entry(user_id, DATE, 200, "estimated")
    ids = [_complete(user_id, f"agree-{n}", 8, effective=200) for n in range(2)]
    db.set_user_ftp_override(user_id, 240)

    assert importer.apply_rpe_ftp_feedback(
        user_id, dt.datetime.fromisoformat(f"{DATE}T20:00:00")
    ) is None
    # RPE 8 on a full threshold session is neutral: no band, nothing consumed.
    assert not any(db.get_standalone_workout(user_id, i)["feedback_applied"] for i in ids)
    assert db.pending_ftp_suggestion(user_id) is None


def test_correcting_a_rating_retracts_the_suggestion_it_produced(user_id):
    ids = _manual_evidence(user_id)
    importer.apply_rpe_ftp_feedback(
        user_id, dt.datetime.fromisoformat(f"{DATE}T20:00:00")
    )
    assert db.pending_ftp_suggestion(user_id)["suggested_ftp"] == 252.0

    now = dt.datetime.fromisoformat(f"{DATE}T20:00:00")
    # Re-rating one of them releases the batch; the remaining single workout is
    # no longer enough evidence, so the proposal is withdrawn.
    assert importer.save_workout_rpe(user_id, "standalone", ids[0], 8, now)
    assert db.pending_ftp_suggestion(user_id) is None
    assert db.get_user_settings(user_id)["ftp"] == 240
    assert db.latest_ftp(user_id)["ftp_watts"] == 200


def test_suggestion_is_recomputed_from_the_manual_value_after_it_changes(user_id):
    _manual_evidence(user_id)
    importer.apply_rpe_ftp_feedback(
        user_id, dt.datetime.fromisoformat(f"{DATE}T20:00:00")
    )
    first = db.pending_ftp_suggestion(user_id)
    # New evidence against a new manual value supersedes the old proposal.
    db.set_user_ftp_override(user_id, 200)
    for n in range(2):
        _complete(user_id, f"again-{n}", 6, effective=230)
    importer.apply_rpe_ftp_feedback(
        user_id, dt.datetime.fromisoformat(f"{DATE}T20:00:00")
    )
    live = db.pending_ftp_suggestion(user_id)
    assert live["id"] != first["id"]
    # Recomputed against the NEW manual value (200), not the old one, and still
    # capped at one 5% step even though the wattage demonstrated 230.
    assert live["current_ftp"] == 200.0
    assert live["suggested_ftp"] == 210.0
    assert db.get_user_settings(user_id)["ftp"] == 200


def test_the_sweep_files_suggestions_for_a_backlog_of_old_ratings(user_id):
    """Ratings given before this existed were discarded, not consumed; the
    daily sweep is what finally gives that backlog a voice."""
    _manual_evidence(user_id)
    importer.run_auto_scan(dt.datetime.fromisoformat(f"{DATE}T20:00:00"))
    assert db.pending_ftp_suggestion(user_id) is not None
    assert db.get_user_settings(user_id)["ftp"] == 240


def test_v27_migration_adds_ftp_suggestions_without_losing_rows(tmp_path):
    path = str(tmp_path / "v27.db")
    db.init_db(path)
    uid = db.create_user("kept", "hash", path)
    conn = sqlite3.connect(path)
    conn.execute("DROP TABLE ftp_suggestions")
    conn.execute("PRAGMA user_version = 27")
    conn.commit()
    conn.close()

    db.init_db(path)

    conn = sqlite3.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    assert conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='ftp_suggestions'"
    ).fetchone()
    assert conn.execute(
        "SELECT username FROM users WHERE id=?", (uid,)
    ).fetchone()[0] == "kept"
    conn.close()
