"""Confirmation-first OOTO adjustment persistence and routes."""
import datetime as dt

import pytest

pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from wattracker import db  # noqa: E402
from wattracker.prescribe import reflow  # noqa: E402
from wattracker.server import create_app  # noqa: E402


@pytest.fixture()
def client():
    app = create_app()
    with TestClient(app) as c:
        yield c


def _register(client):
    client.post("/register", data={"username": "rider", "password": "password123"})


def _plan(uid):
    plan_id = db.create_plan(uid, "Plan", "2026-08-03", 4)
    rows = []
    for date, kind, tss in (
        ("2026-08-10", "threshold", 60.0),
        ("2026-08-12", "vo2max", 60.0),
        # A believable Zone 2 TSS for 60 min: rebalance only steps a session up
        # when the higher-dose build genuinely IS a higher dose.
        ("2026-08-20", "endurance", 40.0),
    ):
        rows.append(db.add_plan_workout(
            plan_id, uid, date, kind.title(), kind, 3600, tss, "<x/>",
            origin="generated",
        ))
    return plan_id, rows


def test_ooto_add_creates_pending_proposal_without_plan_mutation(client, monkeypatch):
    import wattracker.server as servermod

    _register(client)
    uid = db.get_user_by_username("rider")["id"]
    plan_id, row_ids = _plan(uid)
    monkeypatch.setattr(servermod, "utc_today", lambda: dt.date(2026, 8, 1))

    response = client.post(
        "/ooto/add",
        data={"start_date": "2026-08-10", "end_date": "2026-08-12"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "adjustment_id=" in response.headers["location"]

    pending = db.list_pending_ooto_adjustments(uid)
    assert len(pending) == 1
    assert pending[0]["plan_id"] == plan_id
    assert pending[0]["proposal"]["recommended_option"] == "reschedule"
    assert [r["adjustment_state"] for r in db.plan_workouts_for_plan(uid, plan_id)] == [
        None, None, None,
    ]
    assert row_ids == [r["id"] for r in db.plan_workouts_for_plan(uid, plan_id)]

    calendar = client.get(response.headers["location"])
    assert "Review your OOTO adjustment" in calendar.text
    assert "2026-08-10" in calendar.text and "2026-08-20" in calendar.text


def test_confirm_reschedule_records_source_and_replacement_provenance(
    client, monkeypatch,
):
    import wattracker.server as servermod

    _register(client)
    uid = db.get_user_by_username("rider")["id"]
    plan_id, row_ids = _plan(uid)
    monkeypatch.setattr(servermod, "utc_today", lambda: dt.date(2026, 8, 1))
    client.post(
        "/ooto/add",
        data={"start_date": "2026-08-10", "end_date": "2026-08-12"},
    )
    adjustment = db.list_pending_ooto_adjustments(uid)[0]

    response = client.post(
        f"/ooto-adjustment/{adjustment['id']}/confirm",
        data={"option": "reschedule"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert db.get_ooto_adjustment(uid, adjustment["id"])["status"] == "applied"

    rows = db.plan_workouts_for_plan(uid, plan_id)
    source = next(r for r in rows if r["id"] == row_ids[0])
    displaced = next(r for r in rows if r["id"] == row_ids[2])
    replacement = next(r for r in rows if r["adjustment_state"] == "rescheduled")
    assert source["adjustment_state"] == "ooto_canceled"
    assert displaced["adjustment_state"] == "displaced"
    assert replacement["date"] == "2026-08-20"
    assert replacement["adjustment_source_id"] == source["id"]
    assert replacement["origin"] == "adjusted"


def test_confirm_rebalance_raises_the_dose_and_moves_nothing(client, monkeypatch):
    """Rebalance is now a genuinely different answer from reschedule.

    UPDATED DELIBERATELY: this test used to assert the old behaviour, where
    rebalance overwrote the target row with the key workout - the same training
    outcome as reschedule, presented to the rider as a second choice, and with
    the target's original prescription destroyed unrecoverably. Rebalance now
    relocates nothing, destroys nothing, and only raises the dose of surviving
    easy days at their existing durations.
    """
    import wattracker.server as servermod

    _register(client)
    uid = db.get_user_by_username("rider")["id"]
    plan_id, row_ids = _plan(uid)
    monkeypatch.setattr(servermod, "utc_today", lambda: dt.date(2026, 8, 1))
    client.post(
        "/ooto/add",
        data={"start_date": "2026-08-10", "end_date": "2026-08-12"},
    )
    adjustment = db.list_pending_ooto_adjustments(uid)[0]

    before = {r["id"]: dict(r) for r in db.plan_workouts_for_plan(uid, plan_id)}
    response = client.post(
        f"/ooto-adjustment/{adjustment['id']}/confirm",
        data={"option": "rebalance"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert db.get_ooto_adjustment(uid, adjustment["id"])["status"] == "applied"
    rows = db.plan_workouts_for_plan(uid, plan_id)
    # Nothing inserted, nothing deleted, nothing relocated.
    assert [r["id"] for r in rows] == row_ids
    assert [r["date"] for r in rows] == [before[i]["date"] for i in row_ids]

    source = next(r for r in rows if r["id"] == row_ids[0])
    target = next(r for r in rows if r["id"] == row_ids[2])
    assert source["adjustment_state"] is None
    assert source["type"] == "threshold"
    assert target["adjustment_state"] == "rebalanced"
    assert target["adjustment_source_id"] == source["id"]
    assert target["type"] == "tempo"
    # Weekly minutes cannot move: only the dose changed.
    assert target["duration_s"] == before[row_ids[2]]["duration_s"]
    assert sum(r["duration_s"] for r in rows) == sum(
        r["duration_s"] for r in before.values()
    )
    assert target["tss"] > before[row_ids[2]]["tss"]


def test_confirm_rejects_stale_proposal_without_mutation(client, monkeypatch):
    import wattracker.server as servermod

    _register(client)
    uid = db.get_user_by_username("rider")["id"]
    plan_id, row_ids = _plan(uid)
    monkeypatch.setattr(servermod, "utc_today", lambda: dt.date(2026, 8, 1))
    client.post(
        "/ooto/add",
        data={"start_date": "2026-08-10", "end_date": "2026-08-12"},
    )
    adjustment = db.list_pending_ooto_adjustments(uid)[0]

    conn = db.connect()
    try:
        conn.execute("UPDATE plan_workouts SET name = ? WHERE id = ?", ("Changed", row_ids[0]))
        conn.commit()
    finally:
        conn.close()

    response = client.post(
        f"/ooto-adjustment/{adjustment['id']}/confirm",
        data={"option": "reschedule"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert db.get_ooto_adjustment(uid, adjustment["id"])["status"] == "stale"
    assert len(db.plan_workouts_for_plan(uid, plan_id)) == 3
    assert all(r["adjustment_state"] is None for r in db.plan_workouts_for_plan(uid, plan_id))


def test_dismiss_keeps_ooto_skip_without_schedule_mutation(client, monkeypatch):
    import wattracker.server as servermod

    _register(client)
    uid = db.get_user_by_username("rider")["id"]
    plan_id, _ = _plan(uid)
    monkeypatch.setattr(servermod, "utc_today", lambda: dt.date(2026, 8, 1))
    client.post(
        "/ooto/add",
        data={"start_date": "2026-08-10", "end_date": "2026-08-12"},
    )
    adjustment = db.list_pending_ooto_adjustments(uid)[0]
    response = client.post(
        f"/ooto-adjustment/{adjustment['id']}/dismiss", follow_redirects=False,
    )
    assert response.status_code == 303
    assert db.get_ooto_adjustment(uid, adjustment["id"])["status"] == "dismissed"
    assert len(db.plan_workouts_for_plan(uid, plan_id)) == 3


# ------------------------------------------------------ reverting a trip

def _confirmed(client, monkeypatch, option):
    """Register, plan, add OOTO, confirm ``option``. Returns (uid, plan_id, ids)."""
    import wattracker.server as servermod

    _register(client)
    uid = db.get_user_by_username("rider")["id"]
    plan_id, row_ids = _plan(uid)
    monkeypatch.setattr(servermod, "utc_today", lambda: dt.date(2026, 8, 1))
    client.post("/ooto/add",
                data={"start_date": "2026-08-10", "end_date": "2026-08-12"})
    adjustment = db.list_pending_ooto_adjustments(uid)[0]
    client.post(f"/ooto-adjustment/{adjustment['id']}/confirm",
                data={"option": option})
    return uid, plan_id, row_ids, adjustment["id"]


def _shape(rows):
    return sorted(
        (r["date"], r["name"], r["type"], r["duration_s"], round(r["tss"], 4),
         r["variant"], r["origin"], r["adjustment_state"])
        for r in rows
    )


def test_deleting_the_ooto_range_reverts_a_confirmed_reschedule(
    client, monkeypatch,
):
    """A cancelled trip must leave no trace on the plan.

    Without this the cancelled key workout stayed cancelled and unexported
    forever, the displaced easy session stayed destroyed, and every row the
    adjustment touched was permanently invisible to reflow and adaptation.
    """
    _register(client)
    uid = db.get_user_by_username("rider")["id"]
    plan_id, row_ids = _plan(uid)
    import wattracker.server as servermod
    monkeypatch.setattr(servermod, "utc_today", lambda: dt.date(2026, 8, 1))
    before = _shape(db.plan_workouts_for_plan(uid, plan_id, include_zwo=True))

    client.post("/ooto/add",
                data={"start_date": "2026-08-10", "end_date": "2026-08-12"})
    adjustment_id = db.list_pending_ooto_adjustments(uid)[0]["id"]
    client.post(f"/ooto-adjustment/{adjustment_id}/confirm",
                data={"option": "reschedule"})
    assert len(db.plan_workouts_for_plan(uid, plan_id)) == 4  # inserted a row

    ooto_id = db.list_ooto_ranges(uid)[0]["id"]
    assert db.delete_ooto_range(uid, ooto_id) is True

    after = db.plan_workouts_for_plan(uid, plan_id, include_zwo=True)
    assert _shape(after) == before
    assert [r["id"] for r in after] == row_ids
    assert db.get_ooto_adjustment(uid, adjustment_id)["status"] == "reverted"
    # ...and the rows are ordinary generator-owned rows again.
    assert all(r["adjustment_id"] is None for r in after)
    assert db.live_ooto_adjustment_ids(uid) == set()
    adaptable = db.adaptable_plan_workouts(uid, "2026-08-01", "2026-08-31")
    assert {r["id"] for r in adaptable} == set(row_ids)
    live = db.live_ooto_adjustment_ids(uid)
    assert all(reflow._eligible(r, "2026-08-01", live_adjustments=live)
               for r in after)


def test_deleting_the_ooto_range_reverts_a_confirmed_rebalance(
    client, monkeypatch,
):
    """A dose change has to be as undoable as a move."""
    _register(client)
    uid = db.get_user_by_username("rider")["id"]
    plan_id, row_ids = _plan(uid)
    import wattracker.server as servermod
    monkeypatch.setattr(servermod, "utc_today", lambda: dt.date(2026, 8, 1))
    before = _shape(db.plan_workouts_for_plan(uid, plan_id, include_zwo=True))

    client.post("/ooto/add",
                data={"start_date": "2026-08-10", "end_date": "2026-08-12"})
    adjustment_id = db.list_pending_ooto_adjustments(uid)[0]["id"]
    client.post(f"/ooto-adjustment/{adjustment_id}/confirm",
                data={"option": "rebalance"})
    boosted = next(r for r in db.plan_workouts_for_plan(uid, plan_id)
                   if r["adjustment_state"] == "rebalanced")
    assert boosted["type"] == "tempo"

    db.delete_ooto_range(uid, db.list_ooto_ranges(uid)[0]["id"])

    after = db.plan_workouts_for_plan(uid, plan_id, include_zwo=True)
    assert _shape(after) == before
    assert [r["id"] for r in after] == row_ids
    assert db.get_ooto_adjustment(uid, adjustment_id)["status"] == "reverted"


def test_a_double_submit_does_not_re_apply_the_adjustment(client, monkeypatch):
    uid, plan_id, row_ids, adjustment_id = _confirmed(
        client, monkeypatch, "reschedule",
    )
    after_first = db.plan_workouts_for_plan(uid, plan_id, include_zwo=True)

    result = db.apply_ooto_adjustment(uid, adjustment_id, "reschedule",
                                      now=dt.date(2026, 8, 1))
    assert result["status"] == "already_resolved"
    assert result["resolution"] == "applied"
    assert _shape(db.plan_workouts_for_plan(uid, plan_id, include_zwo=True)) \
        == _shape(after_first)


def test_an_elapsed_adjustment_stops_locking_its_rows(client, monkeypatch):
    """The exclusion lasts while the adjustment is live, not for the plan's life."""
    uid, plan_id, row_ids, adjustment_id = _confirmed(
        client, monkeypatch, "reschedule",
    )
    assert db.live_ooto_adjustment_ids(uid) == {adjustment_id}
    # Still upcoming: retirement must refuse to touch it.
    assert db.retire_elapsed_ooto_adjustments(uid, "2026-08-15") == 0
    assert db.live_ooto_adjustment_ids(uid) == {adjustment_id}

    assert db.retire_elapsed_ooto_adjustments(uid, "2026-09-01") == 1
    assert db.get_ooto_adjustment(uid, adjustment_id)["status"] == "retired"
    assert db.live_ooto_adjustment_ids(uid) == set()
    rows = db.plan_workouts_for_plan(uid, plan_id, include_zwo=True)
    live = db.live_ooto_adjustment_ids(uid)
    # The generator's own rows - cancelled and displaced alike - are its again.
    # (The inserted replacement stays origin='adjusted' and so stays off
    # limits; that is the origin rule, not the adjustment exclusion.)
    original = [r for r in rows if r["id"] in row_ids]
    assert len(original) == 3
    assert all(reflow._eligible(r, "2026-08-01", live_adjustments=live)
               for r in original)
    assert db.adaptable_plan_workouts(uid, "2026-08-01", "2026-08-31")


# ------------------------------------- reverting over a row the rider RODE
#
# Revert stays unconditional (no fingerprint, no staleness gate: see #97) with
# exactly one carve-out - a row carrying a completion is never rewritten and
# never deleted. What the rider actually did outranks the prescription the
# adjustment displaced, and the adjustment markers still come off so the row
# stops being locked against reflow and adaptation.

def _completed(uid, workout_id, activity_id=4242, date="2026-08-20"):
    assert db.mark_plan_workout_completed(
        uid, workout_id, activity_id, date, compliance=0.97, effective_ftp=250.0,
    ) is True


def _markers(row):
    return (row["adjustment_id"], row["adjustment_state"],
            row["adjustment_source_id"])


def test_revert_leaves_a_completed_rebalanced_row_exactly_as_ridden(
    client, monkeypatch,
):
    """The #97 case: a re-dosed session the rider rode before the trip died.

    Restoring the Zone 2 prescription under a tempo ride would attach the ride
    to a session nobody did, and completion matching gates on work/recovery
    contrast, so the mismatch can mint a wrong effective_ftp.
    """
    uid, plan_id, row_ids, adjustment_id = _confirmed(
        client, monkeypatch, "rebalance",
    )
    boosted = next(r for r in db.plan_workouts_for_plan(uid, plan_id)
                   if r["adjustment_state"] == "rebalanced")
    ridden = {k: boosted[k] for k in ("date", "name", "type", "duration_s",
                                      "tss", "variant")}
    _completed(uid, boosted["id"])

    db.delete_ooto_range(uid, db.list_ooto_ranges(uid)[0]["id"])

    after = db.plan_workouts_for_plan(uid, plan_id)
    assert [r["id"] for r in after] == row_ids  # nothing deleted
    row = next(r for r in after if r["id"] == boosted["id"])
    assert {k: row[k] for k in ridden} == ridden
    assert row["type"] == "tempo"
    assert row["completed_activity_id"] == 4242
    # ...and it is an ordinary row again, not one an adjustment owns.
    assert _markers(row) == (None, None, None)
    assert db.get_ooto_adjustment(uid, adjustment_id)["status"] == "reverted"
    assert db.live_ooto_adjustment_ids(uid) == set()


def test_revert_keeps_a_completed_rescheduled_row_instead_of_deleting_it(
    client, monkeypatch,
):
    """Deleting it would leave the rider's ride with no plan row at all."""
    uid, plan_id, row_ids, adjustment_id = _confirmed(
        client, monkeypatch, "reschedule",
    )
    replacement = next(r for r in db.plan_workouts_for_plan(uid, plan_id)
                       if r["adjustment_state"] == "rescheduled")
    ridden = {k: replacement[k] for k in ("date", "name", "type", "duration_s",
                                          "tss", "variant")}
    _completed(uid, replacement["id"])

    db.delete_ooto_range(uid, db.list_ooto_ranges(uid)[0]["id"])

    after = db.plan_workouts_for_plan(uid, plan_id)
    assert sorted(r["id"] for r in after) == sorted(row_ids + [replacement["id"]])
    row = next(r for r in after if r["id"] == replacement["id"])
    assert {k: row[k] for k in ridden} == ridden
    assert row["completed_activity_id"] == 4242
    assert _markers(row) == (None, None, None)
    # The rows it was moved off and onto revert normally around it.
    assert all(_markers(r) == (None, None, None) for r in after)
    assert db.get_ooto_adjustment(uid, adjustment_id)["status"] == "reverted"


def test_revert_keeps_a_completed_ooto_canceled_row_as_ridden(
    client, monkeypatch,
):
    """A cancelled session the rider rode anyway is history, not a hole.

    Cancellation only ever tombstoned the row, so the revert has nothing to
    write back; this pins that a completion cannot turn a marker-clear into a
    content rewrite or a delete.
    """
    uid, plan_id, row_ids, adjustment_id = _confirmed(
        client, monkeypatch, "reschedule",
    )
    canceled = next(r for r in db.plan_workouts_for_plan(uid, plan_id)
                    if r["adjustment_state"] == "ooto_canceled")
    ridden = {k: canceled[k] for k in ("date", "name", "type", "duration_s",
                                       "tss", "variant", "origin")}
    _completed(uid, canceled["id"], date=canceled["date"])

    db.delete_ooto_range(uid, db.list_ooto_ranges(uid)[0]["id"])

    after = db.plan_workouts_for_plan(uid, plan_id)
    assert [r["id"] for r in after] == row_ids  # replacement gone, this stayed
    row = next(r for r in after if r["id"] == canceled["id"])
    assert {k: row[k] for k in ridden} == ridden
    assert row["completed_activity_id"] == 4242
    assert _markers(row) == (None, None, None)
    assert db.get_ooto_adjustment(uid, adjustment_id)["status"] == "reverted"


def test_revert_keeps_a_completed_displaced_row_as_ridden(client, monkeypatch):
    """The easy day the replacement landed on top of, ridden regardless."""
    uid, plan_id, row_ids, adjustment_id = _confirmed(
        client, monkeypatch, "reschedule",
    )
    displaced = next(r for r in db.plan_workouts_for_plan(uid, plan_id)
                     if r["adjustment_state"] == "displaced")
    ridden = {k: displaced[k] for k in ("date", "name", "type", "duration_s",
                                        "tss", "variant", "origin")}
    _completed(uid, displaced["id"], date=displaced["date"])

    db.delete_ooto_range(uid, db.list_ooto_ranges(uid)[0]["id"])

    after = db.plan_workouts_for_plan(uid, plan_id)
    assert [r["id"] for r in after] == row_ids
    row = next(r for r in after if r["id"] == displaced["id"])
    assert {k: row[k] for k in ridden} == ridden
    assert row["completed_activity_id"] == 4242
    assert _markers(row) == (None, None, None)
    assert db.get_ooto_adjustment(uid, adjustment_id)["status"] == "reverted"


def test_a_completed_rows_zwo_is_not_pruned_by_the_revert(client, monkeypatch):
    """The orphan list drives a file prune, so it must track what survives.

    A completed rebalanced row keeps its date and its re-dosed name through the
    revert, so its .zwo still describes a session the plan holds; listing it as
    an orphan would delete a live workout's file.
    """
    uid, plan_id, row_ids, adjustment_id = _confirmed(
        client, monkeypatch, "rebalance",
    )
    boosted = next(r for r in db.plan_workouts_for_plan(uid, plan_id)
                   if r["adjustment_state"] == "rebalanced")
    ooto_id = db.list_ooto_ranges(uid)[0]["id"]
    assert db.ooto_range_revert_orphans(uid, ooto_id) == [
        {"date": boosted["date"], "name": boosted["name"]},
    ]

    _completed(uid, boosted["id"])
    assert db.ooto_range_revert_orphans(uid, ooto_id) == []
