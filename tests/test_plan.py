"""Tests for the multi-week plan generator, persistence, and batch export."""
import datetime as dt
import os
import re
import xml.etree.ElementTree as ET
from collections import defaultdict

import pytest

from tranalyzer import auth, db
from tranalyzer.prescribe import plan, zwo

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


def test_ramp_and_recovery_week():
    p = plan.generate_plan("P", MONDAY, 4, [0, 2, 4, 5], 8.0, 2)
    vol = [wk["total_s"] for wk in p["weekly"]]
    # Gentle upward ramp on weeks 1-3...
    assert vol[0] < vol[1] < vol[2]
    # ...then a recovery week (4th) with clearly less volume.
    assert vol[3] < vol[2]
    assert p["weekly"][3]["recovery"] is True
    assert p["weekly"][3]["total_s"] <= 0.75 * vol[2]


def test_both_hit_types_used():
    p = plan.generate_plan("P", MONDAY, 4, [0, 2, 4, 5], 8.0, 2)
    types = {w["type"] for w in p["workouts"]}
    assert "vo2max" in types and "threshold" in types


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
