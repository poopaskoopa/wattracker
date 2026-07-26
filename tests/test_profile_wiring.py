"""Where profile-awareness meets the rest of the app.

Three failure modes live here, none of which are visible from either feature
alone:

* the stored ``.zwo`` and every path that REBUILDS the session must agree - if
  only some call sites pass the rider profile, Zwift and wattracker run
  different workouts;
* adapt and reflow must not fight over the same rows every night; and
* a "no target" sprint must not leak a wattage into ERG or the UI.
"""
from __future__ import annotations

import datetime as dt
import xml.etree.ElementTree as ET

import pytest

pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from wattracker import db
from wattracker.analysis.state import TrainingState
from wattracker.ble.runner import FREERIDE_ERG_FRACTION, RideController, flatten_session
from wattracker.metrics import profile_cache
from wattracker.metrics.rider import RiderMetrics
from wattracker.prescribe import adapt, plan as planmod, reflow, zwo
from wattracker.prescribe.planner import (
    SPRINT_LOAD_RATIO_DEFAULT,
    SPRINT_RATIO_MAX,
    SPRINT_RATIO_MIN,
    build_workout,
    sprint_load_ratio,
)
from wattracker.server import create_app


@pytest.fixture()
def client():
    app = create_app()
    with TestClient(app) as c:
        yield c

MONDAY = dt.date(2026, 7, 6)
NOW = dt.datetime(2026, 7, 15, 9, 0)
STRONG = RiderMetrics(ftp=250.0, sprint_ratio=4.35, vo2_ratio=1.30)


# ------------------------------------------------ defect 3: sprint load clamp
@pytest.mark.parametrize("ratio", [0, -1, -1e9, float("nan"), float("inf")])
def test_sprint_load_rejects_impossible_values(ratio):
    """Junk falls back to the population constant, never propagates."""
    assert sprint_load_ratio(RiderMetrics(sprint_ratio=ratio)) == \
        SPRINT_LOAD_RATIO_DEFAULT


@pytest.mark.parametrize("ratio", [15, 1e9, 6.5])
def test_sprint_load_clamps_a_spiked_peak(ratio):
    """A power-meter spike is the classic corrupt 5s peak.

    The figure is SQUARED to make TSS, so an unclamped 15x FTP turned a 60-min
    sprint session into 931 TSS.
    """
    assert sprint_load_ratio(RiderMetrics(sprint_ratio=ratio)) == SPRINT_RATIO_MAX
    session = build_workout("sprint", 60,
                            profile=RiderMetrics(sprint_ratio=ratio))
    reference = build_workout("sprint", 60,
                              profile=RiderMetrics(sprint_ratio=SPRINT_RATIO_MAX))
    assert session.estimated_tss == reference.estimated_tss
    assert session.estimated_tss < 200


def test_sprint_load_clamps_an_implausibly_low_ratio():
    assert sprint_load_ratio(RiderMetrics(sprint_ratio=0.4)) == SPRINT_RATIO_MIN


def test_sprint_load_leaves_real_ratios_alone():
    for ratio in (2.5, 3.35, 4.35, 5.95):
        assert SPRINT_RATIO_MIN <= sprint_load_ratio(
            RiderMetrics(sprint_ratio=ratio)) <= SPRINT_RATIO_MAX
        assert sprint_load_ratio(RiderMetrics(sprint_ratio=ratio)) == \
            pytest.approx(ratio, abs=0.025)  # only quantization moves it


# --------------------------------- defect 4: no target reaches trainer or UI
def test_erg_holds_a_fixed_resistance_through_a_sprint():
    """The ERG number must not scale with the rider's measured sprint power.

    Using the load-accounting figure here would hand a stronger rider a HARDER
    block on the one segment that is supposed to have no target at all.
    """
    weak = build_workout("sprint", 45, profile=RiderMetrics(sprint_ratio=2.5))
    strong = build_workout("sprint", 45, profile=STRONG)
    for session in (weak, strong):
        blocks, _ = flatten_session(session)
        free = [b for b in blocks if b[2] == "free"]
        assert free, "sprint efforts must flatten to free blocks"
        assert all(b[3] == FREERIDE_ERG_FRACTION for b in free)

    ctl = RideController(strong, ftp=250.0, autosave=False)
    start = next(b[0] for b in flatten_session(strong)[0] if b[2] == "free")
    # 0.55 x 250 = 138 W of resistance, not 4.35 x 250 = 1088 W.
    assert ctl.target_watts(start + 1) == 138


def test_ride_preview_quotes_no_wattage_for_a_sprint_effort(client):
    uid = _register(client, "sprinter")
    db.save_user_settings(uid, {"ftp": 250})
    data = client.get("/ride/workout/preview?type=sprint&minutes=60").json()

    free_blocks = [b for b in data["profile"] if b.get("free")]
    assert free_blocks, "the sprint efforts must be flagged as untargeted"
    # Regression: this block used to be plotted at 750 W (3.00 x FTP), on the
    # same response whose segment row reads "Max effort - no target".
    assert all(b["watts_start"] == b["watts_end"] == 138 for b in free_blocks)
    assert max(b["watts_start"] for b in data["profile"]) <= 250

    labelled = [s for s in data["segments"] if s["label"] == "Max effort - no target"]
    assert labelled
    assert all(s["watts_low"] is None and s["watts_high"] is None
               for s in labelled)


def test_ride_preview_still_reports_targets_for_targeted_sessions(client):
    uid = _register(client, "vo2rider")
    db.save_user_settings(uid, {"ftp": 250})
    data = client.get("/ride/workout/preview?type=vo2max&minutes=60").json()
    assert not any(b.get("free") for b in data["profile"])
    assert max(b["watts_start"] for b in data["profile"]) > 250


# --------------------------- defect 1: every rebuild path sees the same rider
def _register(client, username="rider"):
    client.post("/register", data={"username": username,
                                   "password": "password123"})
    return db.get_user_by_username(username)["id"]


def _seed_plan(user_id, profile=None, weeks=4):
    recipe = reflow.build_recipe([0, 2, 4], 6.0, 1)
    generated = planmod.generate_plan(
        "Base", MONDAY, weeks, recipe["days_of_week"], recipe["hours_per_week"],
        recipe["hit_days_per_week"], model=recipe["model"], profile=profile,
    )
    plan_id = db.create_plan(user_id, "Base", generated["start_date"],
                             generated["weeks"], model=generated["model"],
                             recipe=recipe)
    for w in generated["workouts"]:
        db.add_plan_workout(
            plan_id, user_id, w["date"], w["name"], w["type"], w["duration_s"],
            w["tss"], zwo.zwo_string(w["session"]), variant=w.get("variant"),
            origin=reflow.GENERATED,
        )
    db.set_active_plan(user_id, plan_id)
    return plan_id


def _use_profile(monkeypatch, profile):
    """Pin the profile every consumer reads (they all go through the cache)."""
    monkeypatch.setattr(profile_cache, "for_user",
                        lambda *a, **k: profile)


def test_plan_detail_rebuild_matches_the_stored_zwo(client, monkeypatch):
    """Regression: the endpoint rebuilt with population constants while the
    stored .zwo had been generated from the rider's measured 5-min power - a
    ~30 W gap at FTP 250 on the same workout."""
    uid = _register(client, "detail")
    _use_profile(monkeypatch, STRONG)
    plan_id = _seed_plan(uid, profile=STRONG)

    for row in db.plan_workouts_for_plan(uid, plan_id, include_zwo=True):
        data = client.get(f"/api/plan/workout/{row['id']}").json()
        rebuilt = build_workout(row["type"], row["duration_s"] / 60,
                                row["variant"], profile=STRONG)
        assert data["description"] == rebuilt.description, row["date"]
        stored = ET.fromstring(row["zwo_or_segments"])
        assert data["description"] in stored.find("description").text


def test_ride_session_matches_the_stored_zwo(client, monkeypatch):
    """The in-app ERG ride and the exported .zwo must be one workout."""
    uid = _register(client, "erg")
    db.save_user_settings(uid, {"ftp": 250})
    _use_profile(monkeypatch, STRONG)
    plan_id = _seed_plan(uid, profile=STRONG)
    row = next(r for r in db.plan_workouts_for_plan(uid, plan_id, include_zwo=True)
               if r["type"] == "vo2max")

    with client.websocket_connect(f"/ride/ws?workout_id={row['id']}") as ws:
        payload = ws.receive_json()["workout"]

    rebuilt = build_workout(row["type"], row["duration_s"] / 60, row["variant"],
                            profile=STRONG)
    blocks, _ = flatten_session(rebuilt)
    assert len(payload["profile"]) == len(blocks)
    peak = max(b["watts_start"] for b in payload["profile"])
    stored_peak = max(
        float(el.attrib.get("OnPower", 0)) for el in
        ET.fromstring(row["zwo_or_segments"]).find("workout")
    )
    assert peak == pytest.approx(round(stored_peak * 250), abs=1)


def test_generated_plan_is_born_profile_aware(client, monkeypatch):
    """Otherwise the first nightly reflow rewrites the plan the user just made."""
    uid = _register(client, "creator")
    _use_profile(monkeypatch, STRONG)
    client.post("/generate/plan", data={
        "name": "P", "weeks": "3", "hours_per_week": "6", "days": ["0", "2", "4"],
        "hit_days_per_week": "1", "model": "polarized",
        "start_date": MONDAY.isoformat(),
    })
    plan = db.get_active_plan(uid)
    assert plan is not None
    result = reflow.reflow_plan(uid, plan["id"], now=dt.datetime(2026, 7, 7, 9, 0))
    assert (result["updated"], result["inserted"], result["deleted"]) == (0, 0, 0)


# --------------------------------------------------- the profile cache itself
def test_profile_cache_serves_repeats_without_recomputing(user_id, monkeypatch):
    calls = []
    profile_cache.invalidate()

    def counted(uid, state=None, now=None):
        calls.append(uid)
        return RiderMetrics(vo2_ratio=1.20)

    monkeypatch.setattr(profile_cache.rider, "for_user", counted)
    for _ in range(5):
        assert profile_cache.for_user(user_id).vo2_ratio == 1.20
    assert len(calls) == 1


def test_profile_cache_notices_a_new_activity(user_id, monkeypatch):
    calls = []
    profile_cache.invalidate()
    monkeypatch.setattr(profile_cache.rider, "for_user",
                        lambda uid, state=None, now=None: calls.append(uid)
                        or RiderMetrics())
    profile_cache.for_user(user_id)
    db.insert_activity(user_id, {
        "dedup_hash": "h1", "filename": "r.fit",
        "start_time": "2026-07-14T08:00:00", "duration_s": 3600,
        "distance_m": 0.0, "avg_power": 200.0, "avg_hr": 140.0, "np": 200.0,
        "if_": 0.8, "tss": 60.0, "streams": {"power": [200] * 60},
    })
    profile_cache.for_user(user_id)
    assert len(calls) == 2


def test_profile_cache_notices_a_settings_change(user_id, monkeypatch):
    calls = []
    profile_cache.invalidate()
    monkeypatch.setattr(profile_cache.rider, "for_user",
                        lambda uid, state=None, now=None: calls.append(uid)
                        or RiderMetrics())
    profile_cache.for_user(user_id)
    db.save_user_settings(user_id, {"weight_kg": 71})
    profile_cache.for_user(user_id)
    assert len(calls) == 2


def test_profile_cache_bypasses_for_explicit_arguments(user_id, monkeypatch):
    calls = []
    profile_cache.invalidate()
    monkeypatch.setattr(profile_cache.rider, "for_user",
                        lambda uid, state=None, now=None: calls.append((state, now))
                        or RiderMetrics())
    profile_cache.for_user(user_id)
    profile_cache.for_user(user_id, state=TrainingState(ftp=250.0))
    profile_cache.for_user(user_id, now=NOW)
    assert len(calls) == 3  # neither served from nor written to the cache
    profile_cache.for_user(user_id)
    assert len(calls) == 3


# -------------------------- defect 2: the nightly adapt <-> reflow ping-pong
def _nights(user_id, n, state, start=NOW):
    """Simulate n nightly sweeps of adapt-then-reflow. Returns per-night counts."""
    out = []
    for i in range(n):
        now = start + dt.timedelta(days=0, hours=i)  # same day, later "nights"
        summary = adapt.apply_adaptations(user_id, state, now)
        result = reflow.reflow_plan(user_id, _active(user_id), now=now)
        out.append((summary["adjusted"],
                    result["updated"] + result["inserted"] + result["deleted"]))
    return out


def _active(user_id):
    return db.get_active_plan(user_id)["id"]


def test_adapt_skips_race_windows_and_the_nightly_loop_settles(user_id):
    """Regression: adapt eased 5 rows every night and reflow reverted all 5.

    Net content was stable, so nothing in the data showed it - but it was 10
    DB writes and 10 Zwift file rewrites every night, forever, while the
    dashboard claimed workouts had been eased that never reached the export.
    """
    _seed_plan(user_id)
    # A race 10 days out: everything from here to it is inside the taper.
    race = (NOW.date() + dt.timedelta(days=10)).isoformat()
    db.add_race_date(user_id, race, priority="A", name="Nationals",
                     duration_min=120)
    reflow.reflow_plan(user_id, _active(user_id), now=NOW)  # apply the taper

    nights = _nights(user_id, 5, TrainingState(ftp=250.0, tsb=-30.0,
                                               overreach=True))

    assert all(adjusted == 0 for adjusted, _ in nights), nights
    assert all(writes == 0 for _, writes in nights), nights


def test_adapt_still_eases_days_outside_any_race_window(user_id):
    """The skip must be surgical: a race must not switch adaptation off."""
    _seed_plan(user_id)
    # Race far enough out that its taper cannot reach the adaptation window.
    db.add_race_date(user_id, (NOW.date() + dt.timedelta(days=60)).isoformat(),
                     priority="A", name="Late", duration_min=120)

    summary = adapt.apply_adaptations(
        user_id, TrainingState(ftp=250.0, tsb=-30.0, overreach=True), NOW)

    assert summary["adjusted"] > 0
    assert summary["skipped_raced"] == 0


def test_adapt_reports_what_it_skipped_for_a_race(user_id):
    _seed_plan(user_id)
    db.add_race_date(user_id, (NOW.date() + dt.timedelta(days=10)).isoformat(),
                     priority="A", name="Nationals", duration_min=120)
    summary = adapt.apply_adaptations(
        user_id, TrainingState(ftp=250.0, tsb=-30.0, overreach=True), NOW)
    assert summary["adjusted"] == 0
    assert summary["skipped_raced"] > 0


def test_adaptation_outside_a_race_window_still_survives_reflow(user_id):
    """The pre-existing guarantee, re-checked with the skip in place."""
    _seed_plan(user_id)
    summary = adapt.apply_adaptations(
        user_id, TrainingState(ftp=250.0, tsb=-30.0, overreach=True), NOW)
    assert summary["adjusted"] > 0
    adapted_before = db.upcoming_adapted_counts(user_id, NOW.date().isoformat())

    reflow.reflow_plan(user_id, _active(user_id), now=NOW)

    assert db.upcoming_adapted_counts(user_id, NOW.date().isoformat()) == \
        adapted_before
