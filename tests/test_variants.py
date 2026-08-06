"""Tests for per-day workout variants: purpose-preserving variety.

Covers determinism, byte-for-byte legacy (variant=None/classic/unknown)
equivalence, IF/TSS comparability to classic at 60min, plan-generation
rotation, the graph API rebuilding the stored variant, v13 migration data
preservation + legacy NULL-variant rebuild, and the adapt path.
"""
import math
import sqlite3

import pytest

from wattracker import db
from wattracker.prescribe import zwo
from wattracker.prescribe.planner import VARIANTS, build_workout


def _if(session):
    tot = session.total_duration()
    acc = sum(s.duration * s.avg_fraction() ** 2 for s in session.segments)
    return math.sqrt(acc / tot) if tot else 0.0


# ---------------------------------------------------------------- determinism
def test_build_workout_deterministic():
    for kind, variants in VARIANTS.items():
        for v in variants:
            a = zwo.zwo_string(build_workout(kind, 60, v))
            b = zwo.zwo_string(build_workout(kind, 60, v))
            assert a == b, (kind, v)


# ------------------------------------------- legacy: None == classic == unknown
@pytest.mark.parametrize("kind", list(VARIANTS))
@pytest.mark.parametrize("minutes", [30, 60, 120])
def test_variant_none_matches_classic_byte_for_byte(kind, minutes):
    def render(variant):
        try:
            return zwo.zwo_string(build_workout(kind, minutes, variant))
        except ValueError as e:
            return ("RAISE", str(e))
    # None, "classic" and an unknown variant must all resolve to the classic
    # builder's exact output (or all raise identically for durations classic
    # never supported, e.g. vo2max at 30min).
    assert render(None) == render("classic") == render("nonexistent")


def test_classic_golden_segments_unchanged():
    """Pin classic segment lists so a regression in a builder is caught."""
    # 5 x 4min, with recoveries only BETWEEN the reps: the repeated block holds
    # four of them and the fifth is its own steadystate, so the session no
    # longer ends on a recovery that runs straight into the cooldown. The
    # 240s the dropped recovery used to occupy come back as Zone 2 base.
    vo2 = build_workout("vo2max", 60, "classic")
    assert [(s.kind, s.duration) for s in vo2.segments] == [
        ("warmup", 600), ("steadystate", 240), ("intervals", 1920),
        ("steadystate", 240), ("cooldown", 600)
    ]
    iv = vo2.segments[2]
    assert (iv.repeat, iv.on_duration, iv.off_duration, iv.on_power, iv.off_power) \
        == (4, 240, 240, 1.12, 0.50)
    assert (vo2.segments[3].power, vo2.segments[3].duration) == (1.12, 240)

    z2 = build_workout("endurance", 90, "classic")
    assert [(s.kind, s.duration) for s in z2.segments] == [
        ("warmup", 600), ("steadystate", 4500), ("cooldown", 300)
    ]
    assert z2.segments[1].power == 0.70


# --------------------------------------------------- IF / TSS comparability
@pytest.mark.parametrize("kind", list(VARIANTS))
def test_variants_comparable_to_classic_at_60min(kind):
    classic = build_workout(kind, 60, "classic")
    c_tss, c_if = classic.estimated_tss, _if(classic)
    for v in VARIANTS[kind]:
        s = build_workout(kind, 60, v)
        assert s.total_duration() == 3600
        assert abs(_if(s) - c_if) <= 0.03, (kind, v, _if(s), c_if)
        assert abs(s.estimated_tss - c_tss) <= 0.10 * c_tss, \
            (kind, v, s.estimated_tss, c_tss)


def test_each_variant_has_distinct_name():
    for kind, variants in VARIANTS.items():
        names = {build_workout(kind, 60, v).name for v in variants}
        assert len(names) == len(variants), kind


def test_variants_fit_across_plan_durations():
    ranges = {"vo2max": (50, 91), "threshold": (50, 91), "sweet_spot": (35, 91),
              "endurance": (20, 181), "recovery": (20, 121),
              # Just Ride kinds: offered from the 30min minimum upwards.
              "tempo": (30, 181), "sprint": (30, 181)}
    for kind, variants in VARIANTS.items():
        lo, hi = ranges[kind]
        for v in variants:
            for d in range(lo, hi, 5):
                s = build_workout(kind, d, v)
                assert s.total_duration() == d * 60, (kind, v, d)


# ------------------------------------------------------------ rotation in plan
def test_plan_generation_rotates_variants():
    from wattracker.prescribe import plan as planmod
    import datetime as dt

    gen = planmod.generate_plan(
        "Rot", dt.date(2026, 8, 3), weeks=4, days_of_week=[0, 2, 4, 5],
        hours_per_week=8, hit_days_per_week=2, model="polarized",
    )
    by_kind = {}
    for w in gen["workouts"]:
        by_kind.setdefault(w["type"], []).append(w["variant"])
    # Every workout carries a variant.
    assert all(w.get("variant") for w in gen["workouts"])
    # A kind with 3+ occurrences uses >=2 distinct variants, and consecutive
    # same-kind occurrences differ (deterministic index rotation).
    for kind, seq in by_kind.items():
        if len(VARIANTS[kind]) == 1:
            continue
        if len(seq) >= 3:
            assert len(set(seq)) >= 2, (kind, seq)
        for a, b in zip(seq, seq[1:]):
            assert a != b, (kind, seq)


# --------------------------------------------- graph API rebuilds stored variant
def test_graph_api_returns_stored_variant_segments(user_id):
    # Store a workout with a non-classic variant and confirm the graph API
    # rebuilds THAT variant, not classic.
    plan_id = db.create_plan(user_id, "P", "2026-08-03", 1)
    session = build_workout("vo2max", 60, "short_short")
    wid = db.add_plan_workout(
        plan_id, user_id, "2026-08-03", session.name, "vo2max",
        session.total_duration(), session.estimated_tss,
        zwo.zwo_string(session), variant="short_short",
    )
    stored = db.get_plan_workout(user_id, wid)
    assert stored["variant"] == "short_short"

    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    from wattracker.server import create_app
    from wattracker import auth

    app = create_app()
    with TestClient(app) as c:
        # Authenticate as the fixture user via a fresh session cookie.
        # The fixture user has a known password.
        # (auth uses username/password login.)
        r = c.post("/login", data={"username": "tester", "password": "password123"})
        detail = c.get(f"/api/plan/workout/{wid}")
        assert detail.status_code == 200
        names = [seg for seg in detail.json()["segments"]]
        # short_short = 4 interval sets + rests; classic vo2max is 1 interval.
        n_intervals = sum(1 for s in names if s["kind"] == "intervals")
        assert n_intervals >= 2, detail.json()


# --------------------------------------------------- v13 migration + legacy rows
def test_v13_migration_preserves_data_and_legacy_null_rebuilds(tmp_path):
    p = str(tmp_path / "legacy.db")
    # Fresh v13 db, then simulate a legacy row by nulling variant + set v12.
    db.init_db(p)
    uid = db.create_user("leg", "hash", path=p)
    plan_id = db.create_plan(uid, "P", "2026-08-03", 1, path=p)
    classic = build_workout("threshold", 60, "classic")
    wid = db.add_plan_workout(
        plan_id, uid, "2026-08-03", classic.name, "threshold",
        classic.total_duration(), classic.estimated_tss,
        zwo.zwo_string(classic), variant=None, path=p,
    )
    conn = sqlite3.connect(p)
    conn.execute("UPDATE plan_workouts SET variant = NULL WHERE id = ?", (wid,))
    conn.execute("PRAGMA user_version = 12")
    conn.commit()
    n_before = conn.execute("SELECT COUNT(*) FROM plan_workouts").fetchone()[0]
    conn.close()

    # Re-run init: migrate 12 -> 13.
    db.init_db(p)
    conn = sqlite3.connect(p)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    assert conn.execute("SELECT COUNT(*) FROM plan_workouts").fetchone()[0] == n_before
    conn.close()

    w = db.get_plan_workout(uid, wid, path=p)
    assert w["variant"] is None
    # A NULL-variant legacy row rebuilds identically to classic.
    rebuilt = build_workout(w["type"], w["duration_s"] / 60, w["variant"])
    assert zwo.zwo_string(rebuilt) == zwo.zwo_string(classic)


# ------------------------------------------------------------------- adapt path
def test_adapt_stores_classic_variant_on_stimulus_swap(user_id):
    import datetime as dt
    from wattracker.prescribe import adapt
    from wattracker.analysis.state import TrainingState

    now = dt.datetime(2026, 7, 10, 9, 0)
    date = (now.date() + dt.timedelta(days=2)).isoformat()
    plan_id = db.create_plan(user_id, "P", date, 1)
    session = build_workout("vo2max", 60, "short_short")
    wid = db.add_plan_workout(
        plan_id, user_id, date, session.name, "vo2max",
        session.total_duration(), session.estimated_tss,
        zwo.zwo_string(session), variant="short_short",
    )
    state = TrainingState(ftp=250.0, tsb=-10.0, plateau=True, overreach=False)
    adapt.apply_adaptations(user_id, state, now)

    w = db.get_plan_workout(user_id, wid)
    assert w["type"] == "threshold"          # vo2max -> threshold swap
    assert w["variant"] == "classic"         # reset to new kind's classic
    # Stored .zwo matches the classic threshold rebuild.
    rebuilt = build_workout("threshold", w["duration_s"] / 60, "classic")
    assert w["name"] == rebuilt.name
