import datetime as dt
import sqlite3

import pytest

pytest.importorskip("httpx")
from fastapi.testclient import TestClient

from wattracker import auth, db
from wattracker.ingest import importer
from wattracker.prescribe import zwo
from wattracker.prescribe.planner import build_workout
from wattracker.server import create_app
import wattracker.server as servermod


DATE = "2026-07-19"


def _workout(user_id, key="one", date=DATE, kind="threshold", ftp=200.0):
    session = build_workout(kind, 60)
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
              date=DATE, kind="threshold"):
    workout_id, _ = _workout(user_id, key, date, kind)
    assert db.mark_standalone_completed(
        user_id, workout_id, 10_000 + workout_id, date, compliance, effective
    )
    assert db.set_standalone_rpe(user_id, workout_id, rpe)
    return workout_id


def _plan_workout(user_id, key="plan", kind="threshold", date=DATE):
    session = build_workout(kind, 60)
    plan_id = db.create_plan(user_id, key, date, 1)
    workout_id = db.add_plan_workout(
        plan_id, user_id, date, session.name, kind, session.total_duration(),
        session.estimated_tss, zwo.zwo_string(session),
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
    monkeypatch.setattr(servermod, "plan_workout", lambda state, minutes: session)
    monkeypatch.setattr(servermod.llm, "shape_session", lambda value, state: value)
    with TestClient(app) as client:
        client.post(
            "/login", data={"username": "tester", "password": "password123"},
            follow_redirects=False,
        )
        generated = client.post("/generate", data={"duration_min": 60})
        assert f'name="scheduled_date" value="{dt.date.today().isoformat()}"' in generated.text
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
