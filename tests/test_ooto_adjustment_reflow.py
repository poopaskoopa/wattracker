"""OOTO adjustments against a REAL generated plan, and against nightly reflow.

The other OOTO tests build plans by hand with a uniform ``duration_s=3600``,
which is exactly why the duration-equality gate looked harmless: in a real
``generate_plan`` every hard session is 3000 s and every easy session is
7800 s or 4020 s, so equality matched almost nothing and silently dropped most
affected key workouts. Everything here is built from generator output.
"""
import datetime as dt
import json

import pytest

from wattracker import db
from wattracker.prescribe import ooto_adjust, reflow, zwo
from wattracker.prescribe.plan import generate_plan

pytest.importorskip("httpx")

DAYS = [0, 2, 4, 6]
HOURS_PER_WEEK = 6.0
START = dt.date(2026, 8, 3)
WEEKS = 4
TODAY = "2026-08-01"


@pytest.fixture()
def rider():
    db.init_db()
    uid = db.create_user("rider", "hash")
    generated = generate_plan(
        "Real", START, WEEKS, days_of_week=DAYS,
        hours_per_week=HOURS_PER_WEEK, hit_days_per_week=2,
    )
    plan_id = db.create_plan(
        uid, "Real", START.isoformat(), WEEKS,
        recipe=reflow.build_recipe(DAYS, HOURS_PER_WEEK, 2),
    )
    for w in generated["workouts"]:
        db.add_plan_workout(
            plan_id, uid, w["date"], w["name"], w["type"], w["duration_s"],
            w["tss"], zwo.zwo_string(w["session"]), variant=w.get("variant"),
            origin="generated",
        )
    return uid, plan_id


def _rows(uid, plan_id):
    return db.plan_workouts_for_plan(uid, plan_id, include_zwo=True)


def _weekly_minutes(rows):
    weeks = {}
    for r in rows:
        if r["adjustment_state"] in {"ooto_canceled", "displaced"}:
            continue  # not ridden and not exported
        day = dt.date.fromisoformat(r["date"])
        monday = (day - dt.timedelta(days=day.weekday())).isoformat()
        weeks[monday] = weeks.get(monday, 0) + r["duration_s"] / 60.0
    return weeks


def _evaluate(uid, plan_id, start="2026-08-09", end="2026-08-16"):
    return ooto_adjust.evaluate_ooto(
        db.get_plan(uid, plan_id), _rows(uid, plan_id), start, end, TODAY,
    )


def _option(proposal, kind):
    return next(o for o in proposal["options"] if o["kind"] == kind)


def test_a_real_plan_never_has_a_key_workout_uniformly_sized(rider):
    """Guards the premise of this whole file."""
    uid, plan_id = rider
    durations = {r["duration_s"] for r in _rows(uid, plan_id)}
    assert durations == {3000, 7800, 4020}


def test_every_affected_key_workout_is_moved_or_named(rider):
    uid, plan_id = rider
    proposal = _evaluate(uid, plan_id)
    keys = [a for a in proposal["affected"] if a["key"]]
    assert len(keys) == 4  # threshold, sweet_spot, vo2max, threshold

    reschedule = _option(proposal, "reschedule")
    assert reschedule["affected_keys"] == 4
    moved = {a["source_date"] for a in reschedule["actions"]}
    named = {item["date"] for item in reschedule["unresolved"]}
    # The whole point: nothing is discarded in silence.
    assert moved | named == {k["date"] for k in keys}
    assert not moved & named
    assert reschedule["resolved_keys"] == len(moved)

    # A 7800 s sweet spot has no easy day long enough to take it without
    # growing that week, and the last threshold's only remaining slot sits
    # next to an existing hard day.
    assert sorted(named) == ["2026-08-10", "2026-08-16"]
    assert sorted(a["target_date"] for a in reschedule["actions"]) == [
        "2026-08-17", "2026-08-28",
    ]
    # Each moved session keeps its own 3000 s prescription; the 7800 s and
    # 4020 s easy sessions it lands on are lost.
    assert reschedule["volume_delta_s"] == (3000 - 7800) + (3000 - 4020)


def test_the_surfaced_delta_reaches_the_rider(rider):
    uid, plan_id = rider
    from wattracker import server as servermod

    proposal = _evaluate(uid, plan_id)
    view = servermod._ooto_adjustment_view({"proposal": proposal})
    summary = _option(view["proposal"], "reschedule")["summary"]
    assert "4 key workouts affected" in summary
    assert "2 moved" in summary
    assert "97 min less" in summary  # 5820 s // 60
    assert "sweet_spot on 2026-08-10" in summary
    assert "threshold on 2026-08-16" in summary

    rebalance = _option(view["proposal"], "rebalance")["summary"]
    assert "Nothing moves and no session is lost." in rebalance
    assert "weekly minutes are unchanged" in rebalance


def test_neither_option_can_grow_a_week_past_its_budget(rider):
    uid, plan_id = rider
    budget = HOURS_PER_WEEK * 60
    before = _weekly_minutes(_rows(uid, plan_id))
    assert max(before.values()) <= budget

    for option_kind in ("reschedule", "rebalance"):
        ooto_id = db.add_ooto_range(uid, "2026-08-09", "2026-08-16")
        proposal = _evaluate(uid, plan_id)
        adjustment_id = db.create_ooto_adjustment(
            uid, plan_id, ooto_id, "2026-08-09", "2026-08-16", proposal,
        )
        applied = db.apply_ooto_adjustment(
            uid, adjustment_id, option_kind, now=dt.date(2026, 8, 1),
        )
        assert applied["status"] == "applied"
        after = _weekly_minutes(_rows(uid, plan_id))
        assert max(after.values()) <= budget
        for monday, minutes in after.items():
            assert minutes <= before.get(monday, 0) + 1e-9
        if option_kind == "rebalance":
            # Rebalance changes dose only: every week comes out identical.
            assert after == before
        db.delete_ooto_range(uid, ooto_id)
        assert _weekly_minutes(_rows(uid, plan_id)) == before


def test_reflow_reports_no_conflict_on_a_rescheduled_date(rider):
    """The displaced tombstone must not read as "something else wrote here".

    ``_by_date`` treats a date holding two rows as a conflict and skips it
    forever. A reschedule deliberately leaves the displaced original next to
    its replacement, so before this fix the app's own feature permanently fired
    reflow's "outside writer" signal on that date.
    """
    uid, plan_id = rider
    now = dt.datetime(2026, 8, 1, 12, 0, 0)
    first = reflow.reflow_plan(uid, plan_id, now=now)
    assert first["status"] == "ok"
    assert first["conflicts"] == 0

    ooto_id = db.add_ooto_range(uid, "2026-08-09", "2026-08-16")
    proposal = _evaluate(uid, plan_id)
    adjustment_id = db.create_ooto_adjustment(
        uid, plan_id, ooto_id, "2026-08-09", "2026-08-16", proposal,
    )
    assert db.apply_ooto_adjustment(
        uid, adjustment_id, "reschedule", now=dt.date(2026, 8, 1),
    )["status"] == "applied"
    doubled = {"2026-08-17", "2026-08-28"}
    dates = [r["date"] for r in _rows(uid, plan_id)]
    assert all(dates.count(d) == 2 for d in doubled)

    after = reflow.reflow_plan(uid, plan_id, now=now)
    assert after["status"] == "ok"
    assert after["conflicts"] == 0
    # A second run is still a no-op: reflow must not oscillate on those dates.
    again = reflow.reflow_plan(uid, plan_id, now=now)
    assert again["conflicts"] == 0
    assert (again["updated"], again["inserted"], again["deleted"]) == (0, 0, 0)
    # And it did not back-fill a third row onto the adjusted dates.
    dates = [r["date"] for r in _rows(uid, plan_id)]
    assert all(dates.count(d) == 2 for d in doubled)

    replacements = [r for r in _rows(uid, plan_id)
                    if r["adjustment_state"] == "rescheduled"]
    assert {r["type"] for r in replacements} == {"threshold", "vo2max"}


def test_reflow_leaves_a_live_adjustment_alone(rider):
    """The other half of the liveness rule: LIVE means off limits.

    The revert/retire tests prove the exclusion ends. Nothing proved it applies
    while the adjustment is live, so deleting the guard from ``_eligible``
    outright left the whole suite green.
    """
    uid, plan_id = rider
    now = dt.datetime(2026, 8, 1, 12, 0, 0)
    assert reflow.reflow_plan(uid, plan_id, now=now)["status"] == "ok"

    ooto_id = db.add_ooto_range(uid, "2026-08-09", "2026-08-16")
    proposal = _evaluate(uid, plan_id)
    adjustment_id = db.create_ooto_adjustment(
        uid, plan_id, ooto_id, "2026-08-09", "2026-08-16", proposal,
    )
    assert db.apply_ooto_adjustment(
        uid, adjustment_id, "reschedule", now=dt.date(2026, 8, 1),
    )["status"] == "applied"
    assert db.live_ooto_adjustment_ids(uid) == {adjustment_id}

    owned = {r["id"]: dict(r) for r in _rows(uid, plan_id)
             if r["adjustment_state"] is not None}
    assert len(owned) == 6  # 2 canceled, 2 displaced, 2 rescheduled

    # Make the recipe genuinely disagree with every stored row, so reflow WANTS
    # to rewrite them and the only thing stopping it is the live adjustment.
    conn = db.connect()
    try:
        conn.execute(
            "UPDATE plans SET recipe = ? WHERE id = ?",
            (json.dumps(reflow.build_recipe(DAYS, 8.0, 2)), plan_id),
        )
        conn.commit()
    finally:
        conn.close()

    result = reflow.reflow_plan(uid, plan_id, now=now)
    assert result["status"] == "ok"
    assert result["updated"] > 0  # it really did rewrite the unowned rows

    after = {r["id"]: dict(r) for r in _rows(uid, plan_id)}
    for row_id, before in owned.items():
        assert after[row_id] == before, "a live adjustment's row was rewritten"
    # Counted as locked, not silently stepped over. (Not one per owned row:
    # `_apply_one` works per DATE, and a date whose fresh content happens to
    # match the stored row returns before the eligibility check.)
    assert result["skipped_locked"] > 0
    # And nothing was inserted onto or deleted from an adjusted date.
    assert (result["inserted"], result["deleted"]) == (0, 0)
    assert len(_rows(uid, plan_id)) == len(after)
