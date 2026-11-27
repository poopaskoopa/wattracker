"""Type-aware perceived-exertion (RPE) rating for imported activities.

Covers the activity-scoped subjective-rating endpoint and
the type-aware linkage exposed by the activity-detail API (a ride matched to a
verified plan/standalone workout drives that workout's rating; an unmatched
ride carries its own subjective rating).
"""
import sqlite3
import datetime as dt

import pytest

from wattracker import auth, db
from wattracker.ingest import importer

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


def _insert(user_id, seconds=1800, start="2026-06-01T10:00:00", **over):
    rec = {
        "dedup_hash": over.get("dedup_hash", f"h-{user_id}-{seconds}-{start}"),
        "filename": "ride.fit", "start_time": start,
        "duration_s": seconds, "distance_m": 1000.0, "avg_power": 200.0,
        "avg_hr": 140.0, "np": 205.0, "if_": 0.8, "tss": 50.0,
        "streams": {"time": [None] * seconds, "power": [200.0] * seconds},
    }
    return db.insert_activity(user_id, rec)


# ------------------------------------------------------------------ schema
def test_fresh_schema_has_activities_rpe():
    db.init_db()
    conn = db.connect()
    try:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(activities)")}
        plan_cols = {r["name"] for r in conn.execute("PRAGMA table_info(plan_workouts)")}
        standalone_cols = {
            r["name"] for r in conn.execute("PRAGMA table_info(standalone_workouts)")
        }
        edit_cols = {
            r["name"] for r in conn.execute(
                "PRAGMA table_info(cloud_applied_rpe_edits)"
            )
        }
        has_applied_edit_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='cloud_applied_rpe_edits'"
        ).fetchone() is not None
    finally:
        conn.close()
    assert "rpe" in cols
    assert "rpe_source" not in plan_cols
    assert "rpe_source" not in standalone_cols
    assert has_applied_edit_table
    assert {"edit_id", "feedback_pending"} <= edit_cols


def test_migration_adds_activities_rpe(tmp_path):
    """An older DB (no activities.rpe) gains the column on init_db()."""
    path = str(tmp_path / "old.db")
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE activities (id INTEGER PRIMARY KEY, user_id INTEGER, "
        "dedup_hash TEXT, start_time TEXT, tss REAL);"
        "CREATE TABLE plan_workouts (id INTEGER PRIMARY KEY, user_id INTEGER, "
        "date TEXT, completed_activity_id INTEGER);"
        "CREATE TABLE standalone_workouts (id INTEGER PRIMARY KEY, "
        "user_id INTEGER, scheduled_date TEXT, completed_activity_id INTEGER);"
    )
    conn.execute("PRAGMA user_version = 17")
    conn.commit()
    conn.close()

    db.init_db(path)

    conn = sqlite3.connect(path)
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(activities)")}
        ver = conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()
    assert "rpe" in cols
    assert ver == db.SCHEMA_VERSION


def test_v38_migration_creates_applied_edit_table_and_preserves_ratings(tmp_path):
    path = str(tmp_path / "v38.db")
    db.init_db(path)
    uid = db.create_user("v38-rider", "unused-password", path=path)
    plan = db.create_plan(uid, "Base", "2026-06-01", 1, path=path)
    plan_workout = db.add_plan_workout(
        plan, uid, "2026-06-01", "Threshold", "threshold", 3600, 50,
        "<workout/>", path=path,
    )
    standalone_workout = db.add_standalone_workout(
        uid, "v38", "2026-06-01", "Threshold", "threshold", 3600, 50,
        "<workout/>", 200, path=path,
    )
    conn = db.connect(path)
    try:
        conn.execute(
            "UPDATE plan_workouts SET rpe=6 WHERE user_id=? AND id=?",
            (uid, plan_workout),
        )
        conn.execute(
            "UPDATE standalone_workouts SET rpe=7 WHERE user_id=? AND id=?",
            (uid, standalone_workout),
        )
        conn.execute("DROP TABLE cloud_applied_rpe_edits")
        conn.execute("PRAGMA user_version=38")
        conn.commit()
    finally:
        conn.close()

    db.init_db(path)

    conn = db.connect(path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 39
        assert conn.execute(
            "SELECT rpe FROM plan_workouts WHERE id=?", (plan_workout,),
        ).fetchone()[0] == 6
        assert conn.execute(
            "SELECT rpe FROM standalone_workouts WHERE id=?",
            (standalone_workout,),
        ).fetchone()[0] == 7
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='cloud_applied_rpe_edits'"
        ).fetchone() is not None
        edit_cols = {
            row[1] for row in conn.execute(
                "PRAGMA table_info(cloud_applied_rpe_edits)"
            )
        }
        assert "feedback_pending" in edit_cols
    finally:
        conn.close()


# ------------------------------------------------ activity rpe endpoint
def test_activity_rpe_stores_and_reflects(client):
    uid = _register(client)
    aid = _insert(uid)
    r = client.post(f"/api/activity/{aid}/rpe", json={"rpe": 7})
    assert r.status_code == 200
    assert r.json() == {"id": aid, "rpe": 7}
    assert db.get_activity(uid, aid)["rpe"] == 7
    # Detail API surfaces the subjective rating for an unlinked ride.
    d = client.get(f"/api/activity/{aid}").json()
    assert d["rpe"] == 7
    assert "linked_workout" not in d


@pytest.mark.parametrize("bad", [0, 11, -1, 100])
def test_activity_rpe_rejects_out_of_range(client, bad):
    uid = _register(client)
    aid = _insert(uid)
    r = client.post(f"/api/activity/{aid}/rpe", json={"rpe": bad})
    assert r.status_code == 400
    assert db.get_activity(uid, aid)["rpe"] is None


def test_activity_rpe_404_for_missing(client):
    _register(client)
    r = client.post("/api/activity/999999/rpe", json={"rpe": 5})
    assert r.status_code == 404


def test_activity_rpe_user_scoped(client):
    alice = _register(client, "alice")
    aid = _insert(alice)
    client.post("/logout")
    _register(client, "bob")
    r = client.post(f"/api/activity/{aid}/rpe", json={"rpe": 5})
    assert r.status_code == 404
    assert db.get_activity(alice, aid)["rpe"] is None


def test_cloud_rpe_edit_compare_and_set_and_dedupe_redelivery(client, monkeypatch):
    uid = _register(client)
    aid = _insert(uid, start="2026-06-10T10:00:00")
    now = dt.datetime(2026, 6, 15, tzinfo=dt.timezone.utc)

    assert importer.apply_cloud_rpe_edit(uid, aid, 6, None, edit_id="a" * 32, now=now) == "applied"
    assert db.get_activity(uid, aid)["rpe"] == 6

    def unexpected_write(*_args, **_kwargs):
        pytest.fail("an already-current cloud rating must not write")

    monkeypatch.setattr(db, "set_activity_rpe", unexpected_write)
    assert importer.apply_cloud_rpe_edit(uid, aid, 6, None, edit_id="a" * 32, now=now) == "applied"
    assert importer.apply_cloud_rpe_edit(uid, aid, 8, None, edit_id="b" * 32, now=now) == "conflict"
    assert db.get_activity(uid, aid)["rpe"] == 6


@pytest.mark.parametrize("rpe,base_rpe", [(True, None), (11, None), (6, True), (6, 6)])
def test_cloud_rpe_edit_rejects_invalid_wire_values(client, rpe, base_rpe):
    uid = _register(client)
    aid = _insert(uid, start="2026-06-10T10:00:00")
    assert importer.apply_cloud_rpe_edit(
        uid, aid, rpe, base_rpe, edit_id="c" * 32,
        now=dt.datetime(2026, 6, 15, tzinfo=dt.timezone.utc),
    ) == "rejected"
    assert db.get_activity(uid, aid)["rpe"] is None


def test_cloud_rpe_edit_rejects_missing_duplicate_old_and_other_user_targets(client):
    alice = _register(client, "alice")
    recent = _insert(alice, start="2026-06-10T10:00:00")
    duplicate = _insert(alice, start="2026-06-11T10:00:00")
    assert db.set_duplicate_of(alice, duplicate, recent)
    bob = db.create_user("bob", "not-a-password")
    bob_activity = _insert(bob, start="2026-06-10T10:00:00")
    old = _insert(alice, start="2026-04-01T10:00:00")
    now = dt.datetime(2026, 6, 15, tzinfo=dt.timezone.utc)

    assert importer.apply_cloud_rpe_edit(alice, 999999, 6, None, edit_id="a" * 32, now=now) == "rejected"
    assert importer.apply_cloud_rpe_edit(alice, duplicate, 6, None, edit_id="b" * 32, now=now) == "rejected"
    assert importer.apply_cloud_rpe_edit(alice, bob_activity, 6, None, edit_id="c" * 32, now=now) == "rejected"
    assert importer.apply_cloud_rpe_edit(alice, old, 6, None, edit_id="d" * 32, now=now) == "rejected"
    assert db.get_activity(bob, bob_activity)["rpe"] is None


def test_cloud_rpe_edit_updates_verified_workout_through_shared_resolver(client, monkeypatch):
    uid = _register(client)
    aid = _insert(uid, seconds=3600, start="2026-06-10T10:00:00")
    plan_id = db.create_plan(uid, "Base", "2026-06-01", 4)
    workout_id = db.add_plan_workout(
        plan_id, uid, "2026-06-10", "Endurance", "endurance", 3600, 50.0, "<>"
    )
    assert db.mark_plan_workout_completed(uid, workout_id, aid, "2026-06-10")
    save_calls = []
    original_save = importer.save_workout_rpe

    def save_workout(user_id, kind, workout_id, rpe, now=None, **kwargs):
        save_calls.append((
            user_id, kind, workout_id, rpe,
            kwargs.get("conn") is not None,
        ))
        return original_save(user_id, kind, workout_id, rpe, now, **kwargs)
    monkeypatch.setattr(importer, "save_workout_rpe", save_workout)
    feedback_calls = []
    monkeypatch.setattr(
        importer, "apply_rpe_ftp_feedback",
        lambda *args, **kwargs: feedback_calls.append((args, kwargs)),
    )

    assert importer.apply_cloud_rpe_edit(
        uid, aid, 7, None, edit_id="e" * 32,
        now=dt.datetime(2026, 6, 15, tzinfo=dt.timezone.utc),
    ) == "applied"
    assert db.get_plan_workout(uid, workout_id)["rpe"] == 7
    assert db.get_activity(uid, aid)["rpe"] is None
    assert save_calls == [(uid, "plan", workout_id, 7, True)]
    assert len(feedback_calls) == 1
    # Re-delivery of the same id must not call the desktop write path again.
    assert importer.apply_cloud_rpe_edit(
        uid, aid, 7, None, edit_id="e" * 32,
        now=dt.datetime(2026, 6, 15, tzinfo=dt.timezone.utc),
    ) == "applied"
    assert save_calls == [(uid, "plan", workout_id, 7, True)]
    assert len(feedback_calls) == 1

    # A stale edit conflicts even when its value now matches the desktop's.
    assert importer.apply_cloud_rpe_edit(
        uid, aid, 7, 6, edit_id="9" * 32,
        now=dt.datetime(2026, 6, 15, tzinfo=dt.timezone.utc),
    ) == "conflict"
    assert save_calls == [(uid, "plan", workout_id, 7, True)]
    assert len(feedback_calls) == 1


def test_cloud_edit_redelivery_does_not_overwrite_later_desktop_rating(client):
    uid = _register(client)
    aid = _insert(uid, seconds=3600, start="2026-06-10T10:00:00")
    plan_id = db.create_plan(uid, "Base", "2026-06-01", 4)
    workout_id = db.add_plan_workout(
        plan_id, uid, "2026-06-10", "Endurance", "endurance", 3600, 50.0, "<>"
    )
    assert db.mark_plan_workout_completed(uid, workout_id, aid, "2026-06-10")
    assert db.set_plan_workout_rpe(uid, workout_id, 4)
    now = dt.datetime(2026, 6, 15, tzinfo=dt.timezone.utc)

    assert importer.apply_cloud_rpe_edit(uid, aid, 7, 4, edit_id="f" * 32, now=now) == "applied"
    with db.connect() as conn:
        assert conn.execute(
            "SELECT count(*) FROM cloud_applied_rpe_edits WHERE user_id=? AND edit_id=?",
            (uid, "f" * 32),
        ).fetchone()[0] == 1
    assert importer.save_workout_rpe(uid, "plan", workout_id, 4, now)
    assert importer.apply_cloud_rpe_edit(uid, aid, 7, 4, edit_id="f" * 32, now=now) == "applied"
    assert db.get_plan_workout(uid, workout_id)["rpe"] == 4


def test_cloud_edit_redelivery_retries_failed_feedback_without_rewriting(client, monkeypatch):
    uid = _register(client)
    aid = _insert(uid, seconds=3600, start="2026-06-10T10:00:00")
    plan_id = db.create_plan(uid, "Base", "2026-06-01", 4)
    workout_id = db.add_plan_workout(
        plan_id, uid, "2026-06-10", "Endurance", "endurance", 3600, 50.0, "<>"
    )
    assert db.mark_plan_workout_completed(uid, workout_id, aid, "2026-06-10")
    save_calls = []
    feedback_calls = []
    original_save = importer.save_workout_rpe

    def save_workout(*args, **kwargs):
        save_calls.append(1)
        return original_save(*args, **kwargs)

    def feedback(*_args, **_kwargs):
        feedback_calls.append(1)
        if len(feedback_calls) == 1:
            raise RuntimeError("temporary FTP feedback failure")

    monkeypatch.setattr(importer, "save_workout_rpe", save_workout)
    monkeypatch.setattr(importer, "apply_rpe_ftp_feedback", feedback)
    now = dt.datetime(2026, 6, 15, tzinfo=dt.timezone.utc)

    with pytest.raises(RuntimeError, match="temporary FTP feedback failure"):
        importer.apply_cloud_rpe_edit(
            uid, aid, 7, None, edit_id="8" * 32, now=now,
        )
    assert db.get_plan_workout(uid, workout_id)["rpe"] == 7
    with db.connect() as conn:
        assert conn.execute(
            "SELECT feedback_pending FROM cloud_applied_rpe_edits "
            "WHERE user_id=? AND edit_id=?", (uid, "8" * 32),
        ).fetchone()[0] == 1

    assert importer.apply_cloud_rpe_edit(
        uid, aid, 7, None, edit_id="8" * 32, now=now,
    ) == "applied"
    assert db.get_plan_workout(uid, workout_id)["rpe"] == 7
    assert save_calls == [1]
    assert feedback_calls == [1, 1]
    with db.connect() as conn:
        assert conn.execute(
            "SELECT feedback_pending FROM cloud_applied_rpe_edits "
            "WHERE user_id=? AND edit_id=?", (uid, "8" * 32),
        ).fetchone()[0] == 0


def test_unlinked_cloud_rating_matches_desktop_passive_estimate(client):
    cloud_uid = _register(client, "cloud-rider")
    cloud_activity = _insert(
        cloud_uid, seconds=1800, start="2026-06-10T10:00:00",
    )
    desktop_uid = db.create_user("desktop-rider", "unused-password")
    desktop_activity = _insert(
        desktop_uid, seconds=1800, start="2026-06-10T10:00:00",
    )
    now = dt.datetime(2026, 6, 15, tzinfo=dt.timezone.utc)

    assert importer.apply_cloud_rpe_edit(
        cloud_uid, cloud_activity, 1, None, edit_id="2" * 32, now=now,
    ) == "applied"
    assert db.set_activity_rpe(desktop_uid, desktop_activity, 1)
    estimate_now = dt.datetime(2026, 6, 15)
    cloud_estimate = importer._current_estimate(
        importer.passive_ftp_evidence(cloud_uid), estimate_now,
    )
    desktop_estimate = importer._current_estimate(
        importer.passive_ftp_evidence(desktop_uid), estimate_now,
    )
    assert cloud_estimate == pytest.approx(desktop_estimate)


def test_plan_desktop_same_value_rerate_after_cloud_edit_feeds_ftp(
    client, monkeypatch,
):
    uid = _register(client)
    now = dt.datetime(2026, 6, 15, tzinfo=dt.timezone.utc)
    monkeypatch.setattr(importer, "utc_now", lambda: now)
    db.add_ftp_entry(uid, "2026-06-15", 200, "estimated")

    plan_id = db.create_plan(uid, "Base", "2026-06-01", 4)
    target_activity = _insert(
        uid, seconds=3600, start="2026-06-10T10:00:00",
    )
    peer_activity = _insert(
        uid, seconds=3600, start="2026-06-11T10:00:00",
    )
    target_id = db.add_plan_workout(
        plan_id, uid, "2026-06-10", "Threshold", "threshold", 3600,
        50.0, "<>",
    )
    peer_id = db.add_plan_workout(
        plan_id, uid, "2026-06-11", "Threshold", "threshold", 3600,
        50.0, "<>",
    )
    assert db.mark_plan_workout_completed(
        uid, target_id, target_activity, "2026-06-10", .95, 230.0,
    )
    assert db.mark_plan_workout_completed(
        uid, peer_id, peer_activity, "2026-06-11", .95, 230.0,
    )
    assert db.set_plan_workout_rpe(uid, peer_id, 6)

    assert importer.apply_cloud_rpe_edit(
        uid, target_activity, 6, None, edit_id="1" * 32, now=now,
    ) == "applied"
    assert db.latest_ftp(uid)["ftp_watts"] == 210.0

    response = client.post(
        f"/api/plan/workout/{target_id}/rpe", json={"rpe": 6},
    )
    assert response.status_code == 200
    assert response.json()["ftp_feedback_applied"] is True
    assert db.latest_ftp(uid)["ftp_watts"] == 210.0
    assert db.get_plan_workout(uid, target_id)["feedback_applied"]


# ----------------------------------------------- type-aware linkage
def test_detail_exposes_verified_plan_link(client):
    uid = _register(client)
    aid = _insert(uid, seconds=3600, start="2026-06-02T09:00:00")
    plan_id = db.create_plan(uid, "Base", "2026-06-01", 4)
    # Trivial prescription (no objective power profile) => completion verified
    # by legacy duration/TSS semantics once linked.
    wid = db.add_plan_workout(
        plan_id, uid, "2026-06-02", "Endurance", "endurance", 3600, 50.0, "<>"
    )
    assert db.mark_plan_workout_completed(uid, wid, aid, "2026-06-02")

    d = client.get(f"/api/activity/{aid}").json()
    link = d["linked_workout"]
    assert link["kind"] == "plan"
    assert link["id"] == wid
    assert link["name"] == "Endurance"
    assert link["rpe_eligible"] is True

    # Rating a matched ride goes through the workout endpoint and feeds it.
    r = client.post(f"/api/plan/workout/{wid}/rpe", json={"rpe": 6})
    assert r.status_code == 200
    assert db.get_plan_workout(uid, wid)["rpe"] == 6
    # The activity's own subjective column stays untouched.
    assert db.get_activity(uid, aid)["rpe"] is None


def test_detail_exposes_verified_standalone_link(client):
    uid = _register(client)
    aid = _insert(uid, seconds=3600, start="2026-06-03T09:00:00")
    wid = db.add_standalone_workout(
        uid, "key-1", "2026-06-03", "Sweet Spot", "sweet_spot",
        3600, 60.0, "<workout/>", 250.0
    )
    assert db.mark_standalone_completed(uid, wid, aid, "2026-06-03", None, None)

    d = client.get(f"/api/activity/{aid}").json()
    link = d["linked_workout"]
    assert link["kind"] == "standalone"
    assert link["id"] == wid
    assert link["rpe_eligible"] is True


def test_unlinked_activity_has_no_link(client):
    uid = _register(client)
    aid = _insert(uid)
    d = client.get(f"/api/activity/{aid}").json()
    assert "linked_workout" not in d
    assert d["rpe"] is None


def test_activities_page_shows_linked_and_activity_rpe(client):
    uid = _register(client)
    plan_activity = _insert(uid, seconds=3600, start="2026-06-02T09:00:00")
    standalone_activity = _insert(uid, seconds=3600, start="2026-06-03T09:00:00")
    unlinked_activity = _insert(uid, start="2026-06-04T09:00:00")
    _insert(uid, start="2026-06-05T09:00:00")

    plan_id = db.create_plan(uid, "Base", "2026-06-01", 4)
    plan_workout = db.add_plan_workout(
        plan_id, uid, "2026-06-02", "Endurance", "endurance", 3600, 50.0, "<>"
    )
    assert db.mark_plan_workout_completed(uid, plan_workout, plan_activity, "2026-06-02")
    assert db.set_plan_workout_rpe(uid, plan_workout, 6)

    standalone_workout = db.add_standalone_workout(
        uid, "key-1", "2026-06-03", "Sweet Spot", "sweet_spot",
        3600, 60.0, "<workout/>", 250.0,
    )
    assert db.mark_standalone_completed(
        uid, standalone_workout, standalone_activity, "2026-06-03", None, None
    )
    assert db.set_standalone_rpe(uid, standalone_workout, 7)
    assert db.set_activity_rpe(uid, unlinked_activity, 4)

    response = client.get("/activities")
    assert response.status_code == 200
    assert response.text.count("RPE 6") == 1
    assert response.text.count("RPE 7") == 1
    assert response.text.count("RPE 4") == 1
    assert response.text.count(">Rate</a>") == 1


@pytest.mark.parametrize("kind", ["verified_plan", "unverified_plan", "standalone", "unlinked"])
@pytest.mark.parametrize("workout_rpe", [4, None])
def test_shared_rpe_target_matches_snapshot_and_setter(client, monkeypatch, kind, workout_rpe):
    from wattracker import config
    from wattracker.cloud.snapshot import snapshot_objects
    from wattracker.ingest import importer

    uid = _register(client)
    aid = _insert(uid, seconds=600)
    assert db.set_activity_rpe(uid, aid, 7)
    wid = None
    if kind.endswith("plan"):
        plan = db.create_plan(uid, "RPE", "2026-06-01", 1)
        xml = '<workout_file><workout><SteadyState Duration="600" Power="0.8"/></workout></workout_file>'
        wid = db.add_plan_workout(plan, uid, "2026-06-01", "Endurance", "endurance", 600, 20, xml)
        completed = "2026-06-01" if kind == "verified_plan" else "2026-05-01"
        assert db.mark_plan_workout_completed(uid, wid, aid, completed)
        if workout_rpe is not None:
            assert db.set_plan_workout_rpe(uid, wid, workout_rpe)
    elif kind == "standalone":
        wid = db.add_standalone_workout(uid, "rpe", "2026-06-01", "Endurance", "endurance", 600, 20, "{}", 250)
        assert db.mark_standalone_completed(uid, wid, aid, "2026-06-01", None, None)
        if workout_rpe is not None:
            assert db.set_standalone_rpe(uid, wid, workout_rpe)
    expected_kind = {"verified_plan": "plan", "standalone": "standalone"}.get(kind, "activity")
    target = importer.resolve_rpe_target(uid, aid)
    assert target == importer.RpeTarget(expected_kind, wid if expected_kind != "activity" else aid, 7 if expected_kind == "activity" else workout_rpe, expected_kind != "activity")
    # Snapshot must use its own connection, even when verifying a power profile.
    with monkeypatch.context() as scoped:
        scoped.setattr(importer, "db_path", lambda: pytest.fail("default DB path accessed"))
        scoped.setattr(db, "db_path", lambda: pytest.fail("default DB path accessed"))
        activity = next(o for o in snapshot_objects(config.db_path(), uid) if o.object_id == f"activity-{aid}")
    assert activity.data.get("rpe") == target.current
    assert ("rpe" in activity.data) is (target.current is not None)
    assert activity.data.get("rpe_tunes_ftp", False) is target.tunes_ftp
    assert client.get(f"/api/activity/{aid}").json().get("linked_workout", {}).get("rpe_eligible", False) is target.tunes_ftp

    calls = []
    original_save = importer.save_workout_rpe
    def save(*args, **kwargs):
        calls.append(args)
        return original_save(*args, **kwargs)
    monkeypatch.setattr(importer, "save_workout_rpe", save)
    response = client.post(f"/api/activity/{aid}/rpe", json={"rpe": 6})
    assert response.status_code == 200
    assert response.json() == {"id": aid, "rpe": 6}
    assert importer.resolve_rpe_target(uid, aid).current == 6
    if target.tunes_ftp:
        assert calls == [(uid, expected_kind, wid, 6)]
        assert db.get_activity(uid, aid)["rpe"] == 7
    else:
        assert calls == []
        assert db.get_activity(uid, aid)["rpe"] == 6


@pytest.mark.parametrize("bad", [True, False, "5", 5.0, None])
def test_activity_rpe_rejects_non_integer(client, bad):
    uid = _register(client)
    aid = _insert(uid)
    assert client.post(f"/api/activity/{aid}/rpe", json={"rpe": bad}).status_code == 400
    assert db.get_activity(uid, aid)["rpe"] is None


def test_resolver_and_setter_refuse_missing_duplicate_and_other_user(client):
    from wattracker.ingest import importer

    uid = _register(client)
    primary = _insert(uid)
    duplicate = _insert(uid, seconds=900)
    with db.connect() as conn:
        conn.execute("UPDATE activities SET duplicate_of=? WHERE id=?", (primary, duplicate))
    other = db.create_user("other", "unused-password")
    for user, activity in [(uid, 999999), (uid, duplicate), (other, primary)]:
        assert importer.resolve_rpe_target(user, activity) is None
        with pytest.raises(LookupError):
            importer.set_ride_rpe(user, activity, 5)
    assert client.post(f"/api/activity/{duplicate}/rpe", json={"rpe": 5}).status_code == 404


@pytest.mark.parametrize("bad", [0, 11, True, "5", 5.0, None])
def test_shared_setter_never_coerces_or_clears(client, bad):
    from wattracker.ingest import importer

    uid = _register(client)
    aid = _insert(uid)
    db.set_activity_rpe(uid, aid, 7)
    with pytest.raises(ValueError):
        importer.set_ride_rpe(uid, aid, bad)
    assert db.get_activity(uid, aid)["rpe"] == 7


def test_resolver_tolerates_missing_workout_tables_and_columns():
    from wattracker.ingest import importer

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript("""
            CREATE TABLE activities (id INTEGER, user_id INTEGER, rpe INTEGER);
            INSERT INTO activities VALUES (1, 2, 7);
            CREATE TABLE plan_workouts (id INTEGER, user_id INTEGER);
        """)
        assert importer.resolve_rpe_target(2, 1, conn=conn) == importer.RpeTarget("activity", 1, 7, False)
    finally:
        conn.close()


def test_connection_verification_uses_timezone_and_corrected_power(client):
    from wattracker.ingest import importer

    uid = _register(client)
    aid = _insert(uid, seconds=600, start="2026-06-02T01:00:00")
    db.save_user_settings(uid, {"timezone": "America/New_York"})
    plan = db.create_plan(uid, "Timezone", "2026-06-01", 1)
    xml = '<workout_file><workout><SteadyState Duration="600" Power="0.8"/></workout></workout_file>'
    wid = db.add_plan_workout(plan, uid, "2026-06-01", "Endurance", "endurance", 600, 20, xml)
    assert db.mark_plan_workout_completed(uid, wid, aid, "2026-06-01")
    workout = db.get_plan_workout(uid, wid)
    assert importer.plan_workout_completion_verified(uid, workout)
    assert importer.resolve_rpe_target(uid, aid).kind == "plan"
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO power_sample_corrections (user_id, activity_id, start_index, end_index, ftp_basis, created) "
            "VALUES (?, ?, 0, 599, 250, '2026-06-03')", (uid, aid),
        )
    assert not importer.plan_workout_completion_verified(uid, workout)
    assert importer.resolve_rpe_target(uid, aid).kind == "activity"
