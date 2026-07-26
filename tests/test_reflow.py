"""Tests for whole-plan recomputation from the stored recipe, and active plans."""
import datetime as dt
import sqlite3

from wattracker import auth, db
from wattracker.prescribe import plan as planmod
from wattracker.prescribe import planner, reflow, zwo

MONDAY = dt.date(2026, 7, 6)          # plan starts here
NOW = dt.datetime(2026, 7, 15, 9, 0)  # a Wednesday, inside week 2


def _seed_plan(user_id, recipe=None, name="Base", start=MONDAY, weeks=4):
    """Create a plan the way the /generate/plan route does: rows + recipe."""
    recipe = recipe or reflow.build_recipe([0, 2, 4], 6.0, 1)
    generated = planmod.generate_plan(
        name, start, weeks, recipe["days_of_week"], recipe["hours_per_week"],
        recipe["hit_days_per_week"], hard_days=recipe["hard_days"] or None,
        model=recipe["model"],
    )
    plan_id = db.create_plan(
        user_id, name, generated["start_date"], generated["weeks"],
        model=generated["model"], recipe=recipe,
    )
    for w in generated["workouts"]:
        db.add_plan_workout(
            plan_id, user_id, w["date"], w["name"], w["type"], w["duration_s"],
            w["tss"], zwo.zwo_string(w["session"]), variant=w.get("variant"),
            origin=reflow.GENERATED,
        )
    return plan_id


def _set_recipe(user_id, plan_id, recipe):
    """Rewrite a plan's stored recipe (races will do this for real later)."""
    import json

    conn = db.connect()
    try:
        conn.execute(
            "UPDATE plans SET recipe = ? WHERE user_id = ? AND id = ?",
            (json.dumps(recipe) if recipe is not None else None,
             user_id, plan_id),
        )
        conn.commit()
    finally:
        conn.close()


def _sql(statement, *params):
    conn = db.connect()
    try:
        conn.execute(statement, params)
        conn.commit()
    finally:
        conn.close()


def _rows(user_id, plan_id):
    return db.plan_workouts_for_plan(user_id, plan_id, include_zwo=True)


def _future(user_id, plan_id):
    today = NOW.date().isoformat()
    return [r for r in _rows(user_id, plan_id) if r["date"] > today]


# ------------------------------------------------------------- the no-op
def test_reflow_of_an_unmodified_recipe_changes_nothing(user_id):
    """The headline property: recomputing an untouched plan is a perfect no-op."""
    plan_id = _seed_plan(user_id)
    before = _rows(user_id, plan_id)

    result = reflow.reflow_plan(user_id, plan_id, now=NOW)

    assert result == {"status": "ok", "updated": 0, "inserted": 0, "deleted": 0,
                      "skipped_locked": 0, "raced_lost": 0, "failed": 0,
                      "conflicts": 0, "races": [], "race_conflicts": []}
    after = _rows(user_id, plan_id)
    assert after == before  # every field, including the stored .zwo, byte-identical


def test_reflow_is_idempotent_after_a_recipe_change(user_id):
    plan_id = _seed_plan(user_id)
    _set_recipe(user_id, plan_id, reflow.build_recipe([0, 2, 4], 10.0, 1))

    first = reflow.reflow_plan(user_id, plan_id, now=NOW)
    assert first["updated"] > 0
    snapshot = _rows(user_id, plan_id)

    second = reflow.reflow_plan(user_id, plan_id, now=NOW)
    assert (second["updated"], second["inserted"], second["deleted"]) == (0, 0, 0)
    # Past rows still disagree with the recipe and stay locked - the count is
    # stable, not decaying, because nothing about them ever changes.
    assert second["skipped_locked"] == first["skipped_locked"]
    assert _rows(user_id, plan_id) == snapshot


def _harder(kind, duration_min, variant=None):
    """build_workout, but every target 5% higher: same shape, new content.

    Stands in for a real planner change (e.g. making targets profile-aware):
    name, type, variant and duration are untouched, only the segments and the
    TSS move.
    """
    session = planner.build_workout(kind, duration_min, variant)
    for seg in session.segments:
        for attr in ("power", "power_low", "power_high", "on_power", "off_power"):
            value = getattr(seg, attr)
            if value is not None:
                setattr(seg, attr, round(value + 0.05, 4))
    session.compute_tss()
    return session


def test_a_planner_content_change_is_detected_and_repaired(user_id, monkeypatch):
    """The diff covers tss and the stored .zwo, not just the row's labels."""
    plan_id = _seed_plan(user_id)
    today = NOW.date().isoformat()
    before = {r["id"]: r for r in _rows(user_id, plan_id)}
    monkeypatch.setattr(planmod, "build_workout", _harder)

    result = reflow.reflow_plan(user_id, plan_id, now=NOW)

    future = [r for r in _rows(user_id, plan_id) if r["date"] > today]
    assert future
    assert result["updated"] == len(future)
    for row in future:
        old = before[row["id"]]
        # The labels are identical - only content moved, which is exactly the
        # case the old name/type/variant/duration-only diff missed.
        assert (row["name"], row["type"], row["variant"], row["duration_s"]) == (
            old["name"], old["type"], old["variant"], old["duration_s"])
        assert row["tss"] > old["tss"]
        assert row["zwo_or_segments"] != old["zwo_or_segments"]
    # And it converges: a second pass under the same planner is a no-op again.
    second = reflow.reflow_plan(user_id, plan_id, now=NOW)
    assert (second["updated"], second["inserted"], second["deleted"]) == (0, 0, 0)


# ------------------------------------------------------- refusals / locks
def test_legacy_plan_without_a_recipe_is_not_reflowable(user_id):
    plan_id = db.create_plan(user_id, "Legacy", MONDAY.isoformat(), 2)
    db.add_plan_workout(plan_id, user_id, "2026-07-20", "W", "endurance",
                        3600, 60.0, "<x/>")
    before = _rows(user_id, plan_id)

    assert reflow.reflow_plan(user_id, plan_id, now=NOW) == {
        "status": "not_reflowable", "reason": "legacy"
    }
    assert _rows(user_id, plan_id) == before


def test_missing_plan_is_not_reflowable(user_id):
    assert reflow.reflow_plan(user_id, 4242, now=NOW)["status"] == "not_reflowable"


def test_completed_rows_are_never_touched(user_id):
    plan_id = _seed_plan(user_id)
    target = _future(user_id, plan_id)[0]
    db.mark_plan_workout_completed(user_id, target["id"], 999, target["date"])
    _set_recipe(user_id, plan_id, reflow.build_recipe([0, 2, 4], 10.0, 1))

    result = reflow.reflow_plan(user_id, plan_id, now=NOW)

    kept = db.get_plan_workout(user_id, target["id"])
    assert kept["duration_s"] == target["duration_s"]
    assert kept["name"] == target["name"]
    assert result["skipped_locked"] >= 1


def test_past_dated_rows_are_never_touched(user_id):
    plan_id = _seed_plan(user_id)
    today = NOW.date().isoformat()
    past = {r["id"]: r for r in _rows(user_id, plan_id) if r["date"] <= today}
    assert past  # the plan started before NOW, so there is something to protect
    _set_recipe(user_id, plan_id, reflow.build_recipe([0, 2, 4], 10.0, 1))

    reflow.reflow_plan(user_id, plan_id, now=NOW)

    for wid, row in past.items():
        assert db.get_plan_workout(user_id, wid)["duration_s"] == row["duration_s"]


def test_rows_without_generated_origin_are_never_touched(user_id):
    plan_id = _seed_plan(user_id)
    target = _future(user_id, plan_id)[0]
    _sql("UPDATE plan_workouts SET origin = NULL WHERE id = ?", target["id"])
    _set_recipe(user_id, plan_id, reflow.build_recipe([0, 2, 4], 10.0, 1))

    result = reflow.reflow_plan(user_id, plan_id, now=NOW)

    kept = db.get_plan_workout(user_id, target["id"])
    assert kept["duration_s"] == target["duration_s"]
    assert result["skipped_locked"] >= 1


def test_duplicate_rows_on_one_date_are_reported_as_a_conflict(user_id):
    plan_id = _seed_plan(user_id)
    target = _future(user_id, plan_id)[0]
    db.add_plan_workout(plan_id, user_id, target["date"], "Intruder", "endurance",
                        1800, 20.0, "<x/>", origin=reflow.GENERATED)
    _set_recipe(user_id, plan_id, reflow.build_recipe([0, 2, 4], 10.0, 1))

    result = reflow.reflow_plan(user_id, plan_id, now=NOW)

    assert result["conflicts"] == 1
    # Neither row on the conflicted date was rewritten.
    same_date = [r for r in _rows(user_id, plan_id) if r["date"] == target["date"]]
    assert len(same_date) == 2
    assert {r["name"] for r in same_date} == {target["name"], "Intruder"}


# ------------------------------------------------------------- the diff
def test_changed_recipe_rewrites_only_future_rows(user_id):
    plan_id = _seed_plan(user_id)
    today = NOW.date().isoformat()
    before = {r["id"]: r for r in _rows(user_id, plan_id)}
    _set_recipe(user_id, plan_id, reflow.build_recipe([0, 2, 4], 12.0, 1))

    result = reflow.reflow_plan(user_id, plan_id, now=NOW)

    assert result["status"] == "ok"
    assert result["inserted"] == 0 and result["deleted"] == 0
    changed = [r for r in _rows(user_id, plan_id)
               if r["duration_s"] != before[r["id"]]["duration_s"]]
    assert changed and all(r["date"] > today for r in changed)
    assert result["updated"] == len(changed)


def test_adding_and_removing_a_training_day_inserts_and_deletes(user_id):
    plan_id = _seed_plan(user_id)
    today = NOW.date().isoformat()
    # Friday (4) drops out, Saturday (5) comes in.
    _set_recipe(user_id, plan_id, reflow.build_recipe([0, 2, 5], 6.0, 1))

    result = reflow.reflow_plan(user_id, plan_id, now=NOW)

    weekdays = {
        dt.date.fromisoformat(r["date"]).weekday()
        for r in _rows(user_id, plan_id) if r["date"] > today
    }
    assert weekdays == {0, 2, 5}
    assert result["inserted"] > 0 and result["deleted"] > 0
    # Past Fridays survive - they were ridden (or missed) under the old recipe.
    past_weekdays = {
        dt.date.fromisoformat(r["date"]).weekday()
        for r in _rows(user_id, plan_id) if r["date"] <= today
    }
    assert 4 in past_weekdays


def test_reflow_leaves_adapted_rows_alone(user_id):
    """An adaptation is a considered response to the rider's state: an
    unrelated reflow must not discard it. (A race window is the one exception -
    see tests/test_races_plan.py, where the race wins and `adapted` is cleared.)
    """
    plan_id = _seed_plan(user_id)
    target = _future(user_id, plan_id)[0]
    _sql("UPDATE plan_workouts SET adapted = 'recovery', "
         "adapted_at = '2026-07-15T09:00:00' WHERE id = ?", target["id"])
    _set_recipe(user_id, plan_id, reflow.build_recipe([0, 2, 4], 12.0, 1))

    result = reflow.reflow_plan(user_id, plan_id, now=NOW)

    kept = db.get_plan_workout(user_id, target["id"])
    assert kept["duration_s"] == target["duration_s"]
    assert kept["adapted"] == "recovery"
    assert result["skipped_locked"] >= 1


def test_inserted_rows_carry_generated_origin(user_id):
    plan_id = _seed_plan(user_id)
    today = NOW.date().isoformat()
    _set_recipe(user_id, plan_id, reflow.build_recipe([0, 2, 4, 5], 6.0, 1))

    reflow.reflow_plan(user_id, plan_id, now=NOW)

    saturdays = [r for r in _rows(user_id, plan_id)
                 if r["date"] > today
                 and dt.date.fromisoformat(r["date"]).weekday() == 5]
    assert saturdays
    assert all(r["origin"] == "generated" for r in saturdays)
    # A second pass sees them as its own and leaves them alone.
    assert reflow.reflow_plan(user_id, plan_id, now=NOW)["inserted"] == 0


def test_reflow_rewrites_the_zwift_export(user_id, tmp_path):
    out = tmp_path / "zwo"
    out.mkdir()
    db.save_user_settings(user_id, {"workouts_dir": str(out)})
    plan_id = _seed_plan(user_id)
    target = _future(user_id, plan_id)[0]
    old_file = out / zwo.plan_filename(target["date"], target["name"])
    old_file.write_text("<stale/>", encoding="utf-8")
    _set_recipe(user_id, plan_id, reflow.build_recipe([0, 2, 4], 12.0, 1))

    reflow.reflow_plan(user_id, plan_id, now=NOW)

    fresh = db.get_plan_workout(user_id, target["id"])
    new_file = out / zwo.plan_filename(target["date"], fresh["name"])
    assert new_file.exists()
    assert fresh["name"] in new_file.read_text(encoding="utf-8")
    if fresh["name"] != target["name"]:
        assert not old_file.exists()  # the superseded .zwo was pruned


# -------------------------------------------------- races and failures
def test_a_row_completed_between_read_and_write_is_counted_as_raced_lost(
    user_id, monkeypatch
):
    """The DB guard rejecting a row must not make it vanish from the counts."""
    plan_id = _seed_plan(user_id)
    victim = _future(user_id, plan_id)[0]
    _set_recipe(user_id, plan_id, reflow.build_recipe([0, 2, 4], 12.0, 1))
    real_replace = db.replace_plan_workout_content

    def complete_it_first(uid, workout_id, *a, **kw):
        if workout_id == victim["id"]:
            db.mark_plan_workout_completed(uid, workout_id, 999, victim["date"])
        return real_replace(uid, workout_id, *a, **kw)

    monkeypatch.setattr(db, "replace_plan_workout_content", complete_it_first)

    result = reflow.reflow_plan(user_id, plan_id, now=NOW)

    assert result["raced_lost"] == 1
    assert result["failed"] == 0
    kept = db.get_plan_workout(user_id, victim["id"])
    assert kept["duration_s"] == victim["duration_s"]  # the guard held


def test_a_row_whose_write_raises_is_counted_as_failed_and_the_loop_continues(
    user_id, monkeypatch
):
    plan_id = _seed_plan(user_id)
    victim = _future(user_id, plan_id)[0]
    _set_recipe(user_id, plan_id, reflow.build_recipe([0, 2, 4], 12.0, 1))
    real_replace = db.replace_plan_workout_content

    def boom(uid, workout_id, *a, **kw):
        if workout_id == victim["id"]:
            raise sqlite3.OperationalError("database is locked")
        return real_replace(uid, workout_id, *a, **kw)

    monkeypatch.setattr(db, "replace_plan_workout_content", boom)

    result = reflow.reflow_plan(user_id, plan_id, now=NOW)

    assert result["status"] == "ok"  # returned, did not raise
    assert result["failed"] == 1
    assert result["updated"] > 0  # later rows were still applied
    assert db.get_plan_workout(user_id, victim["id"])["duration_s"] == \
        victim["duration_s"]
    # Self-healing: with the fault gone, a re-run repairs the row it skipped.
    monkeypatch.setattr(db, "replace_plan_workout_content", real_replace)
    again = reflow.reflow_plan(user_id, plan_id, now=NOW)
    assert again["failed"] == 0 and again["updated"] == 1
    assert reflow.reflow_plan(user_id, plan_id, now=NOW)["updated"] == 0


# ----------------------------------------------------------- active plan
def test_new_plan_becomes_the_active_plan(user_id):
    first = _seed_plan(user_id, name="First")
    assert db.get_active_plan(user_id)["id"] == first
    second = _seed_plan(user_id, name="Second")
    assert db.get_active_plan(user_id)["id"] == second
    assert [p["active"] for p in db.list_plans(user_id)].count(True) == 1


def test_set_active_plan_moves_the_flag(user_id):
    first = _seed_plan(user_id, name="First")
    _seed_plan(user_id, name="Second")

    assert db.set_active_plan(user_id, first) is True
    assert db.get_active_plan(user_id)["id"] == first
    assert [p["id"] for p in db.list_plans(user_id) if p["active"]] == [first]


def test_set_active_plan_refuses_another_users_plan(user_id):
    other = db.create_user("intruder", auth.hash_password("password123"))
    theirs = _seed_plan(other, name="Theirs")

    assert db.set_active_plan(user_id, theirs) is False
    assert db.get_active_plan(other)["id"] == theirs


def test_deleting_the_active_plan_promotes_the_next_most_recent(user_id):
    first = _seed_plan(user_id, name="First")
    second = _seed_plan(user_id, name="Second")
    assert db.get_active_plan(user_id)["id"] == second

    db.delete_plan(user_id, second)
    assert db.get_active_plan(user_id)["id"] == first

    db.delete_plan(user_id, first)
    assert db.get_active_plan(user_id) is None


def test_deleting_an_inactive_plan_leaves_the_active_one_alone(user_id):
    first = _seed_plan(user_id, name="First")
    second = _seed_plan(user_id, name="Second")

    db.delete_plan(user_id, first)
    assert db.get_active_plan(user_id)["id"] == second


def test_promotion_is_deterministic_when_created_timestamps_tie(user_id):
    """Same `created` on both plans: the higher id wins, every time."""
    first = _seed_plan(user_id, name="First")
    second = _seed_plan(user_id, name="Second")
    third = _seed_plan(user_id, name="Third")
    _sql("UPDATE plans SET created = '2026-07-01T00:00:00' WHERE user_id = ?",
         user_id)

    db.delete_plan(user_id, third)
    assert db.get_active_plan(user_id)["id"] == second

    db.delete_plan(user_id, second)
    assert db.get_active_plan(user_id)["id"] == first


def test_recipe_round_trips_as_a_dict(user_id):
    recipe = reflow.build_recipe([4, 0, 2], 6.0, 1, hard_days=[2], model="polarized")
    plan_id = _seed_plan(user_id, recipe=recipe)

    stored = db.get_plan(user_id, plan_id)
    assert stored["recipe"] == {
        "version": 1, "days_of_week": [0, 2, 4], "hours_per_week": 6.0,
        "hit_days_per_week": 1, "hard_days": [2], "model": "polarized",
    }
    assert stored["active"] is True
    assert db.list_plans(user_id)[0]["recipe"] == stored["recipe"]


def test_unparseable_recipe_degrades_to_legacy(user_id):
    plan_id = _seed_plan(user_id)
    _sql("UPDATE plans SET recipe = 'not json' WHERE id = ?", plan_id)

    assert db.get_plan(user_id, plan_id)["recipe"] is None
    assert reflow.reflow_plan(user_id, plan_id, now=NOW)["reason"] == "legacy"


def test_recipe_from_a_future_version_is_refused(user_id):
    plan_id = _seed_plan(user_id)
    recipe = dict(reflow.build_recipe([0, 2, 4], 6.0, 1), version=99)
    _set_recipe(user_id, plan_id, recipe)
    before = _rows(user_id, plan_id)

    assert reflow.reflow_plan(user_id, plan_id, now=NOW) == {
        "status": "not_reflowable", "reason": "unsupported_version"
    }
    assert _rows(user_id, plan_id) == before


# ------------------------------------------------------- schema migration
def test_v19_migrates_to_v20_in_place(tmp_path):
    """A live v19 database gains recipe/active/origin, keeping all rows."""
    path = str(tmp_path / "v19.db")
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL,
            created TEXT NOT NULL);
        CREATE TABLE plans (id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL, name TEXT NOT NULL,
            start_date TEXT NOT NULL, weeks INTEGER NOT NULL,
            created TEXT NOT NULL, model TEXT);
        CREATE TABLE plan_workouts (id INTEGER PRIMARY KEY AUTOINCREMENT,
            plan_id INTEGER NOT NULL, user_id INTEGER NOT NULL, date TEXT NOT NULL,
            name TEXT NOT NULL, type TEXT NOT NULL, duration_s INTEGER NOT NULL,
            tss REAL NOT NULL, zwo_or_segments TEXT NOT NULL,
            completed_activity_id INTEGER, completed_date TEXT,
            adapted TEXT, adapted_at TEXT, rpe INTEGER, variant TEXT,
            compliance REAL, effective_ftp REAL,
            feedback_applied INTEGER NOT NULL DEFAULT 0,
            feedback_batch_id INTEGER);
        INSERT INTO users (username, password_hash, created)
            VALUES ('keeper', 'x', '2026-01-01');
        INSERT INTO plans (user_id, name, start_date, weeks, created, model)
            VALUES (1, 'Keep', '2026-07-06', 4, '2026-01-01', 'polarized');
        INSERT INTO plan_workouts
            (plan_id, user_id, date, name, type, duration_s, tss, zwo_or_segments)
            VALUES (1, 1, '2026-07-07', 'W', 'endurance', 3600, 60.0, '<x/>');
        PRAGMA user_version = 19;
        """
    )
    conn.commit()
    conn.close()

    db.init_db(path=path)

    conn = sqlite3.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    conn.close()

    stored = db.get_plan(1, 1, path=path)
    # Legacy rows are deliberately NOT backfilled: a guessed recipe would
    # rewrite a plan the user never asked to change.
    assert stored["recipe"] is None and stored["active"] is False
    assert db.get_active_plan(1, path=path) is None
    workouts = db.plan_workouts_for_plan(1, 1, path=path)
    assert [w["name"] for w in workouts] == ["W"]
    assert workouts[0]["origin"] is None
