"""Tests for the multi-week plan generator, persistence, and batch export."""
import datetime as dt
import os
import re
import sqlite3
import xml.etree.ElementTree as ET
from collections import defaultdict

import pytest

from wattracker import auth, db
from wattracker.prescribe import plan, zwo

MONDAY = dt.date(2026, 7, 6)  # a Monday


def _by_week(p):
    per = defaultdict(list)
    for w in p["workouts"]:
        d = dt.date.fromisoformat(w["date"])
        wk = (d - dt.date.fromisoformat(p["start_date"])).days // 7
        per[wk].append(w)
    return per


# --------------------------------------------------------- generation
def test_only_selected_days_get_workouts():
    p = plan.generate_plan("P", MONDAY, 3, [1, 3], 6.0, 1)  # Tue, Thu
    used = {dt.date.fromisoformat(w["date"]).weekday() for w in p["workouts"]}
    assert used == {1, 3}
    assert len(p["workouts"]) == 3 * 2  # weeks * selected days


def test_correct_workout_count():
    p = plan.generate_plan("P", MONDAY, 5, [0, 2, 4], 6.0, 1)
    assert len(p["workouts"]) == 5 * 3


def test_hit_days_per_week_respected():
    p = plan.generate_plan("P", MONDAY, 3, [0, 1, 2, 3], 8.0, 2)
    for wk, workouts in _by_week(p).items():
        hit = sum(1 for w in workouts if w["type"] in ("vo2max", "threshold"))
        assert hit == 2


def test_polarized_split_mostly_easy():
    p = plan.generate_plan("P", MONDAY, 4, [0, 2, 4, 5], 8.0, 2)
    frac = p["polarized_hard_fraction"]
    # Targeting ~80/20: hard time is a minority slice.
    assert 0.08 <= frac <= 0.30
    assert (1 - frac) >= 0.70


def test_flat_volume_and_recovery_week():
    p = plan.generate_plan("P", MONDAY, 4, [0, 2, 4, 5], 8.0, 2)
    vol = [wk["total_s"] for wk in p["weekly"]]
    # Volume is flat across non-recovery weeks (no week-over-week increase)...
    assert vol[0] == vol[1] == vol[2]
    # ...then a recovery week (4th) with clearly less volume.
    assert vol[3] < vol[2]
    assert p["weekly"][3]["recovery"] is True
    assert p["weekly"][3]["total_s"] <= 0.75 * vol[2]


def test_weekly_volume_matches_requested_and_never_exceeds():
    hours = 8.0
    target_s = hours * 3600
    p = plan.generate_plan("P", MONDAY, 6, [0, 2, 4, 5], hours, 2)
    for wk in p["weekly"]:
        if wk["recovery"]:
            assert wk["total_s"] < target_s
            continue
        # Non-recovery weeks sum to ~requested hours (per-workout whole-minute
        # quantization can leave a week slightly short) and NEVER exceed them.
        assert abs(wk["total_s"] - target_s) <= 300
        assert wk["total_s"] <= target_s


# ------------------------------------------- the weekly-volume invariant
# "A week never exceeds the hours the rider asked for" is a hard promise, and
# it has to hold for EVERY configuration, not the handful the examples above
# happen to use. Two things once broke it: session floors that could not fit
# the budget at all (now refused by validate_plan_inputs), and per-session
# whole-minute rounding that pushed a week a minute or two over (now
# reconciled inside generate_plan). This sweep is what keeps both fixed.

_SWEEP_HOURS = [3.0, 3.5, 4.0, 5.0, 6.0, 8.0, 10.0, 12.0, 15.0, 20.0]
_SWEEP_RACES = [
    {"id": 1, "date": "2026-08-16", "priority": "A", "duration_min": 180},
    {"id": 2, "date": "2026-07-18", "priority": "B"},
    {"id": 3, "date": "2026-07-27", "priority": "B", "duration_min": 60},
]


def _sweep_configs():
    for hours in _SWEEP_HOURS:
        for n_days in range(2, 8):
            for hit in range(1, 5):
                for model in plan.MODELS:
                    yield hours, list(range(n_days)), hit, model


@pytest.mark.parametrize("with_races", [False, True])
def test_weekly_volume_never_exceeds_requested_hours_across_the_grid(with_races):
    races = _SWEEP_RACES if with_races else None
    checked = 0
    for hours, days, hit, model in _sweep_configs():
        if plan.validate_plan_inputs(8, days, hours, hit, None, model):
            continue  # a configuration the generator refuses to build
        p = plan.generate_plan("P", MONDAY, 8, days, hours, hit, model=model,
                               races=races)
        cap_s = hours * 3600
        for wk in p["weekly"]:
            assert wk["total_s"] <= cap_s, (
                f"{hours}h, {len(days)} days, {hit} hard, {model}, "
                f"week {wk['week']}: {wk['total_s'] / 60:.1f} min "
                f"> {cap_s / 60:.0f} min"
            )
        checked += 1
    assert checked > 200  # the grid really was exercised


def test_infeasible_configurations_are_refused_with_both_knobs_named():
    # 3 h across 7 days with 2 hard days: 2*50 + 5*20 = 200 min > 180 min.
    err = plan.validate_plan_inputs(4, [0, 1, 2, 3, 4, 5, 6], 3.0, 2)
    assert err
    assert "200 min" in err
    assert "fewer days" in err and "fewer hard days" in err and "hours" in err
    with pytest.raises(ValueError):
        plan.generate_plan("P", MONDAY, 4, [0, 1, 2, 3, 4, 5, 6], 3.0, 2)


def test_a_configuration_that_exactly_fits_is_allowed():
    # 2 hard (50) + 5 easy (20) = 200 min, and 200 min/week is 3.334 h.
    days = [0, 1, 2, 3, 4, 5, 6]
    assert plan.validate_plan_inputs(4, days, 200 / 60, 2) is None
    p = plan.generate_plan("P", MONDAY, 4, days, 200 / 60, 2)
    for wk in p["weekly"]:
        assert wk["total_s"] <= 200 * 60


def test_rounding_excess_is_trimmed_from_the_longest_easy_day():
    """5 days at 97.5 min each would round to 98 and overshoot by 2 minutes."""
    p = plan.generate_plan("P", MONDAY, 1, [0, 1, 2, 3, 4], 8.0, 1,
                           model="sweet_spot")
    assert p["weekly"][0]["total_s"] <= 8 * 3600
    minutes = sorted(w["duration_s"] // 60 for w in p["workouts"])
    # The hard day keeps its full 90 minutes; the trim lands on easy days.
    assert max(minutes) == 98
    assert sum(minutes) == 480


def test_both_hit_types_used():
    p = plan.generate_plan("P", MONDAY, 4, [0, 2, 4, 5], 8.0, 2)
    types = {w["type"] for w in p["workouts"]}
    assert "vo2max" in types and "threshold" in types


def _hard_weekdays(workouts):
    return {
        dt.date.fromisoformat(w["date"]).weekday()
        for w in workouts
        if w["type"] in ("vo2max", "threshold")
    }


def test_hard_days_pin_hit_days():
    p = plan.generate_plan("P", MONDAY, 3, [0, 2, 4, 5], 8.0, 2, hard_days=[2, 5])
    assert p["hard_days"] == [2, 5]
    for _wk, workouts in _by_week(p).items():
        assert _hard_weekdays(workouts) == {2, 5}


def test_hard_days_partial_marks_fill_to_cap():
    # One marked day + cap of 2: the marked day is always hard, and the second
    # HIT slot is auto-filled, keeping exactly `hit_days_per_week` hard days.
    p = plan.generate_plan("P", MONDAY, 2, [0, 2, 4, 5], 8.0, 2, hard_days=[0])
    for _wk, workouts in _by_week(p).items():
        hard = _hard_weekdays(workouts)
        assert 0 in hard
        assert len(hard) == 2


def test_hard_days_validation_errors():
    with pytest.raises(ValueError):
        # More days marked hard than the HIT cap.
        plan.generate_plan("P", MONDAY, 2, [0, 2, 4], 6.0, 1, hard_days=[0, 2])
    with pytest.raises(ValueError):
        # Hard day not among the selected ride days.
        plan.generate_plan("P", MONDAY, 2, [0, 2], 6.0, 1, hard_days=[3])


def test_hard_days_none_keeps_auto_assignment():
    auto = plan.generate_plan("P", MONDAY, 2, [0, 2, 4, 5], 8.0, 2)
    explicit_none = plan.generate_plan(
        "P", MONDAY, 2, [0, 2, 4, 5], 8.0, 2, hard_days=None
    )
    assert [w["type"] for w in auto["workouts"]] == [
        w["type"] for w in explicit_none["workouts"]
    ]


def test_validation_errors():
    with pytest.raises(ValueError):
        plan.generate_plan("P", MONDAY, 0, [0], 6.0, 0)  # weeks < 1
    with pytest.raises(ValueError):
        plan.generate_plan("P", MONDAY, 2, [0, 1], 6.0, 3)  # HIT > selected days
    with pytest.raises(ValueError):
        plan.generate_plan("P", MONDAY, 2, [], 6.0, 0)  # no days
    with pytest.raises(ValueError):
        plan.generate_plan("P", MONDAY, 2, [0], 0.0, 0)  # hours <= 0


def test_sessions_are_valid_and_dated():
    p = plan.generate_plan("P", MONDAY, 1, [0, 2, 4], 6.0, 1)
    for w in p["workouts"]:
        assert w["duration_s"] > 0
        assert w["tss"] > 0
        # session renders to valid .zwo
        ET.fromstring(zwo.zwo_string(w["session"]))


# ------------------------------------------------------- training models
def test_model_caps_at_boundary_ok():
    # polarized: 4 ride days -> cap 2; sweet_spot: 5 days -> cap 4; pyramidal 3.
    assert plan.validate_plan_inputs(4, [0, 1, 2, 3], 8.0, 2, model="polarized") is None
    assert plan.validate_plan_inputs(
        4, [0, 1, 2, 3, 4], 8.0, 4, model="sweet_spot") is None
    assert plan.validate_plan_inputs(
        4, [0, 1, 2, 3, 4], 8.0, 3, model="pyramidal") is None


def test_model_cap_plus_one_rejected_names_model():
    msg = plan.validate_plan_inputs(4, [0, 1, 2, 3], 8.0, 3, model="polarized")
    assert msg and "polarized" in msg and "2" in msg
    msg = plan.validate_plan_inputs(4, [0, 1, 2, 3, 4], 8.0, 5, model="sweet_spot")
    assert msg and "sweet_spot" in msg and "4" in msg
    msg = plan.validate_plan_inputs(4, [0, 1, 2, 3, 4], 8.0, 4, model="pyramidal")
    assert msg and "pyramidal" in msg and "3" in msg


def test_unknown_model_rejected():
    msg = plan.validate_plan_inputs(4, [0, 2, 4], 8.0, 1, model="nonsense")
    assert msg and "nonsense" in msg
    with pytest.raises(ValueError):
        plan.generate_plan("P", MONDAY, 2, [0, 2, 4], 8.0, 1, model="nonsense")


def test_polarized_cannot_be_all_hard():
    # 5 ride days, every day hard -> exceeds polarized cap of 2.
    msg = plan.validate_plan_inputs(
        4, [0, 1, 2, 3, 4], 8.0, 5, model="polarized")
    assert msg and "polarized" in msg
    with pytest.raises(ValueError):
        plan.generate_plan("P", MONDAY, 2, [0, 1, 2, 3, 4], 8.0, 5,
                           model="polarized")


def test_sweet_spot_model_hard_types():
    # Enough hard days that a sweet_spot hard slot must appear (seq is
    # sweet_spot, sweet_spot, threshold). vo2max is never a hard slot here, and
    # the model never builds a vo2max session at all.
    p = plan.generate_plan("P", MONDAY, 4, [0, 2, 4, 5], 10.0, 2,
                           model="sweet_spot")
    assert p["model"] == "sweet_spot"
    types = {w["type"] for w in p["workouts"]}
    assert "sweet_spot" in types
    assert "vo2max" not in types


def test_pyramidal_model_hard_types():
    # Hard slots are threshold-weighted with some vo2max (seq threshold,
    # threshold, vo2max). Both appear across a multi-week plan; sweet_spot is
    # never a pyramidal hard slot (only the occasional easy-day tempo).
    p = plan.generate_plan("P", MONDAY, 6, [0, 2, 4, 5], 10.0, 2,
                           model="pyramidal")
    assert p["model"] == "pyramidal"
    hard_weekdays = _hard_weekdays(p["workouts"])
    hard_types = {
        w["type"] for w in p["workouts"]
        if dt.date.fromisoformat(w["date"]).weekday() in hard_weekdays
        and w["type"] in ("vo2max", "threshold")
    }
    assert "threshold" in hard_types
    assert hard_types.issubset({"threshold", "vo2max"})


def test_polarized_default_unchanged():
    # Default model must match an explicit polarized call (backward compat).
    a = plan.generate_plan("P", MONDAY, 3, [0, 2, 4, 5], 8.0, 2)
    b = plan.generate_plan("P", MONDAY, 3, [0, 2, 4, 5], 8.0, 2,
                           model="polarized")
    assert [w["type"] for w in a["workouts"]] == [w["type"] for w in b["workouts"]]
    assert a["model"] == "polarized"


# --------------------------------------------------------- persistence
def test_persistence_and_isolation():
    db.init_db()
    a = db.create_user("alice", auth.hash_password("password123"))
    b = db.create_user("bob", auth.hash_password("password123"))

    pid = db.create_plan(a, "Base", "2026-07-06", 3)
    db.add_plan_workout(pid, a, "2026-07-07", "VO2max", "vo2max", 3000, 55.0, "<x/>")
    db.add_plan_workout(pid, a, "2026-07-09", "Endurance", "endurance", 3600, 42.0, "<y/>")

    assert len(db.list_plans(a)) == 1
    assert db.list_plans(b) == []
    assert db.get_plan(a, pid)["weeks"] == 3
    assert db.get_plan(b, pid) is None  # isolation

    pw = db.plan_workouts_for_plan(a, pid)
    assert len(pw) == 2
    assert db.plan_workouts_for_plan(b, pid) == []

    got = db.get_plan_workout(a, pw[0]["id"])
    assert got["zwo_or_segments"] in ("<x/>", "<y/>")
    assert db.get_plan_workout(b, pw[0]["id"]) is None


def test_calendar_month_query():
    db.init_db()
    a = db.create_user("alice", auth.hash_password("password123"))
    pid = db.create_plan(a, "Base", "2026-07-06", 2)
    db.add_plan_workout(pid, a, "2026-07-07", "VO2max", "vo2max", 3000, 55.0, "<x/>")
    db.add_plan_workout(pid, a, "2026-08-04", "Endurance", "endurance", 3600, 42.0, "<y/>")

    july = db.plan_workouts_for_month(a, 2026, 7)
    assert len(july) == 1 and july[0]["date"] == "2026-07-07"
    aug = db.plan_workouts_for_month(a, 2026, 8)
    assert len(aug) == 1 and aug[0]["date"] == "2026-08-04"
    # Empty month is safe.
    assert db.plan_workouts_for_month(a, 2026, 9) == []


# ------------------------------------------------------- schema migration
def test_v10_migrates_to_v11_in_place(tmp_path):
    """A live v10 database gains rpe + model columns, keeping all rows."""
    path = str(tmp_path / "v10.db")
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL,
            created TEXT NOT NULL);
        CREATE TABLE plans (id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL, name TEXT NOT NULL,
            start_date TEXT NOT NULL, weeks INTEGER NOT NULL, created TEXT NOT NULL);
        CREATE TABLE plan_workouts (id INTEGER PRIMARY KEY AUTOINCREMENT,
            plan_id INTEGER NOT NULL, user_id INTEGER NOT NULL, date TEXT NOT NULL,
            name TEXT NOT NULL, type TEXT NOT NULL, duration_s INTEGER NOT NULL,
            tss REAL NOT NULL, zwo_or_segments TEXT NOT NULL,
            completed_activity_id INTEGER, completed_date TEXT,
            adapted TEXT, adapted_at TEXT);
        INSERT INTO users (username, password_hash, created)
            VALUES ('keeper', 'x', '2026-01-01');
        INSERT INTO plans (user_id, name, start_date, weeks, created)
            VALUES (1, 'Keep', '2026-07-06', 4, '2026-01-01');
        INSERT INTO plan_workouts
            (plan_id, user_id, date, name, type, duration_s, tss, zwo_or_segments)
            VALUES (1, 1, '2026-07-07', 'W', 'endurance', 3600, 60.0, '<x/>');
        PRAGMA user_version = 10;
        """
    )
    conn.commit()
    conn.close()

    db.init_db(path=path)

    conn = sqlite3.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    pw = conn.execute(
        "SELECT name, rpe FROM plan_workouts"
    ).fetchone()
    assert pw == ("W", None)  # row kept, new nullable column present
    pl = conn.execute("SELECT name, model FROM plans").fetchone()
    assert pl == ("Keep", None)
    assert conn.execute("SELECT username FROM users").fetchone()[0] == "keeper"
    conn.close()


def test_create_plan_persists_model():
    db.init_db()
    a = db.create_user("mira", auth.hash_password("password123"))
    pid = db.create_plan(a, "SS", "2026-07-06", 3, model="sweet_spot")
    assert db.get_plan(a, pid)["model"] == "sweet_spot"
    assert db.list_plans(a)[0]["model"] == "sweet_spot"


# ------------------------------------------------------------- export
def test_batch_export_writes_dated_zwo(tmp_path):
    out = tmp_path / "zwo"
    p = plan.generate_plan("P", MONDAY, 1, [0, 2, 4], 6.0, 1)
    workouts = [
        {"date": w["date"], "name": w["name"], "zwo": zwo.zwo_string(w["session"])}
        for w in p["workouts"]
    ]
    result = zwo.write_plan_to_zwift(workouts, "me", workouts_override=str(out))

    assert result["count"] == 3
    assert result["directory"] == str(out)
    files = sorted(os.listdir(out))
    assert len(files) == 3
    for fname in files:
        assert fname.endswith(".zwo")
        assert re.match(r"^\d{4}-\d{2}-\d{2} ", fname)  # date-led
        ET.fromstring(open(os.path.join(out, fname), encoding="utf-8").read())
