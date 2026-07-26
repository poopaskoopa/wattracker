"""Profile-aware workout targets.

Two properties matter here and they pull in opposite directions:

* a rider with MEASURED capacity gets a prescription built on it, and
* a rider without one gets exactly the prescription this app has always
  produced - ``profile=None`` is not "a profile of defaults", it is the old
  code path, down to the .zwo string.

The second property is what makes the first safe to ship, so most of this file
is about pinning it down.
"""
from __future__ import annotations

import datetime as dt
import xml.etree.ElementTree as ET

import pytest

from wattracker import db
from wattracker.metrics.rider import RiderMetrics
from wattracker.prescribe import plan as planmod
from wattracker.prescribe import reflow, zwo
from wattracker.timeutil import utc_today
from wattracker.prescribe.planner import (
    SPRINT_LOAD_RATIO_DEFAULT,
    VO2_RATIO_DEFAULT,
    VO2_RATIO_MAX,
    VO2_RATIO_MIN,
    VARIANTS,
    Segment,
    Session,
    build_workout,
    vo2_target,
)

MONDAY = dt.date(2026, 7, 6)
NOW = dt.datetime(2026, 7, 15, 9, 0)  # a Wednesday, inside week 2

# The real rider this feature was built for: a 5s peak of 4.35x FTP is nowhere
# near the 3.00x stand-in, and 5min power of 1.20x FTP is a measured VO2 ceiling.
MEASURED = RiderMetrics(ftp=250.0, sprint_ratio=4.35, vo2_ratio=1.20)
UNMEASURED = RiderMetrics()  # every field None - a brand-new rider

DURATIONS = (30, 45, 60, 90, 120, 240)
POWER_ATTRS = ("Power", "PowerLow", "PowerHigh", "OnPower", "OffPower")


def _all_kind_variants():
    for kind in sorted(VARIANTS):
        for variant in VARIANTS[kind]:
            yield kind, variant


def _fingerprint(session) -> tuple:
    return (session.name, session.workout_type, session.description,
            session.total_duration(), session.estimated_tss)


# ------------------------------------------------- profile=None == status quo
@pytest.mark.parametrize("kind,variant", list(_all_kind_variants()))
def test_no_profile_matches_an_all_none_profile(kind, variant):
    """A rider who has measured nothing is indistinguishable from no rider.

    This is the property that lets reflow pass a profile unconditionally: a
    brand-new user with no rides cannot have their plan quietly changed.
    """
    for minutes in DURATIONS:
        plain = build_workout(kind, minutes, variant)
        empty = build_workout(kind, minutes, variant, profile=UNMEASURED)
        assert _fingerprint(plain) == _fingerprint(empty), (kind, variant, minutes)
        assert zwo.zwo_string(plain) == zwo.zwo_string(empty)


def test_plan_without_a_profile_is_byte_identical():
    """A generated plan is unchanged by the existence of the profile argument."""
    kwargs = dict(name="P", start_date=MONDAY, weeks=6, days_of_week=[0, 2, 4, 5],
                  hours_per_week=8.0, hit_days_per_week=2)
    baseline = planmod.generate_plan(**kwargs)
    for profile in (None, UNMEASURED):
        other = planmod.generate_plan(**kwargs, profile=profile)
        assert other["weekly"] == baseline["weekly"]
        assert len(other["workouts"]) == len(baseline["workouts"])
        for a, b in zip(baseline["workouts"], other["workouts"]):
            assert (a["date"], a["name"], a["type"], a["variant"],
                    a["duration_s"], a["tss"], a["hard_s"]) == (
                b["date"], b["name"], b["type"], b["variant"],
                b["duration_s"], b["tss"], b["hard_s"])
            assert zwo.zwo_string(a["session"]) == zwo.zwo_string(b["session"])


def test_unmeasured_targets_are_the_documented_constants():
    """Pins the population prescription itself, not just self-consistency.

    Without this, the test above would still pass if every unmeasured target
    silently moved together.
    """
    vo2 = next(s for s in build_workout("vo2max", 60).segments
               if s.kind == "intervals")
    assert vo2.on_power == VO2_RATIO_DEFAULT == 1.12
    assert "110-115% FTP" in build_workout("vo2max", 60).description
    work = {
        "threshold": 0.93, "sweet_spot": 0.90, "tempo": 0.80,
    }
    for kind, expected in work.items():
        seg = next(s for s in build_workout(kind, 60).segments
                   if s.kind == "intervals")
        assert seg.on_power == expected, kind
    assert build_workout("endurance", 60).segments[1].power == 0.70
    assert build_workout("recovery", 60).segments[1].power == 0.65


@pytest.mark.parametrize("kind", ["threshold", "sweet_spot", "tempo",
                                  "endurance", "recovery"])
def test_ftp_anchored_kinds_ignore_the_profile(kind):
    """FTP already IS a measured quantity, so these must not move."""
    for minutes in DURATIONS:
        assert zwo.zwo_string(build_workout(kind, minutes)) == zwo.zwo_string(
            build_workout(kind, minutes, profile=MEASURED)
        )


# ------------------------------------------------------------------- sprints
def _zwo_power_values(xml: str) -> list:
    root = ET.fromstring(xml)
    return [float(el.attrib[a]) for el in root.find("workout")
            for a in POWER_ATTRS if a in el.attrib]


@pytest.mark.parametrize("profile", [None, MEASURED])
def test_sprint_prescribes_no_power_target(profile):
    """The whole point: a sprint is the rider driving the trainer, not ERG
    clamping the rider to a number."""
    session = build_workout("sprint", 60, profile=profile)
    xml = zwo.zwo_string(session)
    root = ET.fromstring(xml)
    frees = root.find("workout").findall("FreeRide")
    assert frees, "sprint efforts must be FreeRide blocks"
    assert all(f.attrib["Duration"] == "12" for f in frees)
    assert not root.find("workout").findall("IntervalsT")
    # Nothing above the recovery/warmup band survives anywhere in the file:
    # no sprint wattage is prescribed, whatever the rider can produce.
    assert max(_zwo_power_values(xml)) <= 0.85
    assert "4.35" not in xml and "435" not in xml
    assert session.estimated_tss > 0


def test_sprint_load_accounting_uses_measured_sprint_power():
    plain = build_workout("sprint", 60)
    measured = build_workout("sprint", 60, profile=MEASURED)
    assert measured.total_duration() == plain.total_duration()
    efforts = [s for s in measured.segments if s.kind == "freeride"]
    assert efforts and all(s.load_fraction == 4.35 for s in efforts)
    # 4.35x FTP does far more work in 12s than the 3.00x stand-in credits.
    assert measured.estimated_tss > plain.estimated_tss
    assert [s.load_fraction for s in plain.segments if s.kind == "freeride"] == \
        [SPRINT_LOAD_RATIO_DEFAULT] * len(efforts)


def test_freeride_load_fraction_feeds_tss():
    """A freeride block is not zero watts for load purposes - nor a target."""
    free = Segment(kind="freeride", duration=60, load_fraction=2.0)
    assert free.avg_fraction() == 2.0
    assert Segment(kind="freeride", duration=60).avg_fraction() == 0.0
    session = Session(name="n", description="d", workout_type="sprint",
                      segments=[free])
    # 60s at 2.0x FTP: 60 * 2^2 / 3600 * 100, stored to 1dp like every session.
    assert session.compute_tss() == pytest.approx(6.7)


def test_sprint_zwo_round_trips_through_xml():
    xml = zwo.zwo_string(build_workout("sprint", 90, profile=MEASURED))
    root = ET.fromstring(xml)
    total = 0
    for el in root.find("workout"):
        if el.tag == "IntervalsT":
            total += int(el.attrib["Repeat"]) * (
                int(el.attrib["OnDuration"]) + int(el.attrib["OffDuration"]))
        else:
            total += int(el.attrib["Duration"])
    assert total == 90 * 60
    free = root.find("workout").find("FreeRide")
    assert set(free.attrib) == {"Duration"}  # no power attribute at all
    assert "all out" in free.find("textevent").attrib["message"]


# -------------------------------------------------------------------- VO2max
def test_vo2_target_falls_back_when_unmeasured():
    assert vo2_target(None) is None
    assert vo2_target(UNMEASURED) is None
    assert vo2_target(RiderMetrics(vo2_ratio=0)) is None
    session = build_workout("vo2max", 60, profile=UNMEASURED)
    seg = next(s for s in session.segments if s.kind == "intervals")
    assert seg.on_power == VO2_RATIO_DEFAULT


def test_vo2_target_derives_from_measured_five_minute_power():
    # 5min power is a MAXIMAL effort; 4-6 repeats of 4min sit ~92% of it.
    assert vo2_target(RiderMetrics(vo2_ratio=1.20)) == pytest.approx(1.104)
    session = build_workout("vo2max", 60, profile=MEASURED)
    seg = next(s for s in session.segments if s.kind == "intervals")
    assert seg.on_power == pytest.approx(1.104)
    assert "110% FTP" in session.description
    assert "110% FTP" in seg.text


@pytest.mark.parametrize("ratio,expected", [
    (5.0, VO2_RATIO_MAX),    # corrupt MMP point: clamped, not obeyed
    (2.0, VO2_RATIO_MAX),
    (0.5, VO2_RATIO_MIN),    # FTP set far too high: still a real session
    (1.10, VO2_RATIO_MIN),
])
def test_vo2_target_is_clamped_at_both_ends(ratio, expected):
    assert vo2_target(RiderMetrics(vo2_ratio=ratio)) == expected
    seg = next(s for s in build_workout(
        "vo2max", 60, profile=RiderMetrics(vo2_ratio=ratio)).segments
        if s.kind == "intervals")
    assert seg.on_power == expected


def test_derived_vo2_session_still_fits_and_scores():
    for minutes in DURATIONS:
        s = build_workout("vo2max", minutes, profile=MEASURED)
        assert s.total_duration() == minutes * 60
        assert s.estimated_tss > 0


# -------------------------------------------------------------------- reflow
def _seed_plan(user_id, hours=6.0, start=MONDAY, weeks=4):
    recipe = reflow.build_recipe([0, 2, 4], hours, 1)
    generated = planmod.generate_plan(
        "Base", start, weeks, recipe["days_of_week"], recipe["hours_per_week"],
        recipe["hit_days_per_week"], model=recipe["model"],
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
    return plan_id


def _use_profile(monkeypatch, profile):
    monkeypatch.setattr(reflow.rider, "for_user", lambda *a, **k: profile)


def _rows(user_id, plan_id):
    return db.plan_workouts_for_plan(user_id, plan_id, include_zwo=True)


def _sql(statement, *params):
    conn = db.connect()
    try:
        conn.execute(statement, params)
        conn.commit()
    finally:
        conn.close()


def test_reflow_rewrites_workouts_the_profile_changed(user_id, monkeypatch):
    _use_profile(monkeypatch, UNMEASURED)
    plan_id = _seed_plan(user_id)
    today = NOW.date().isoformat()
    before = {r["id"]: r for r in _rows(user_id, plan_id)}

    _use_profile(monkeypatch, MEASURED)
    result = reflow.reflow_plan(user_id, plan_id, now=NOW)

    assert result["status"] == "ok"
    changed = [r for r in _rows(user_id, plan_id)
               if r["zwo_or_segments"] != before[r["id"]]["zwo_or_segments"]]
    assert changed, "the measured VO2 target should have rewritten sessions"
    assert all(r["date"] > today for r in changed)
    assert all(r["type"] == "vo2max" for r in changed)
    assert result["updated"] == len(changed)


def test_reflow_with_a_profile_is_idempotent(user_id, monkeypatch):
    """The property that makes an unattended DAILY reflow safe: once converged,
    re-running changes nothing and renames no .zwo file."""
    _use_profile(monkeypatch, MEASURED)
    plan_id = _seed_plan(user_id)
    first = reflow.reflow_plan(user_id, plan_id, now=NOW)
    snapshot = _rows(user_id, plan_id)

    second = reflow.reflow_plan(user_id, plan_id, now=NOW)

    assert (second["updated"], second["inserted"], second["deleted"]) == (0, 0, 0)
    assert second["skipped_locked"] == first["skipped_locked"]
    assert _rows(user_id, plan_id) == snapshot


def test_reflow_with_a_profile_still_preserves_adaptations(user_id, monkeypatch):
    """A daily profile-driven reflow must not undo adapt.py's work."""
    _use_profile(monkeypatch, UNMEASURED)
    plan_id = _seed_plan(user_id)
    today = NOW.date().isoformat()
    target = next(r for r in _rows(user_id, plan_id)
                  if r["date"] > today and r["type"] == "vo2max")
    _sql("UPDATE plan_workouts SET adapted = 'recovery', "
         "adapted_at = '2026-07-15T09:00:00' WHERE id = ?", target["id"])

    _use_profile(monkeypatch, MEASURED)
    result = reflow.reflow_plan(user_id, plan_id, now=NOW)

    kept = db.get_plan_workout(user_id, target["id"])
    assert kept["adapted"] == "recovery"
    assert kept["tss"] == target["tss"]
    assert result["skipped_locked"] >= 1


def test_reflow_reads_the_profile_fresh_every_time(user_id, monkeypatch):
    """The profile is an input, never stored in the recipe - so a rider whose
    measured capacity moves gets a new prescription without re-creating the
    plan."""
    _use_profile(monkeypatch, UNMEASURED)
    plan_id = _seed_plan(user_id)
    reflow.reflow_plan(user_id, plan_id, now=NOW)
    baseline = _rows(user_id, plan_id)

    _use_profile(monkeypatch, RiderMetrics(vo2_ratio=1.30))
    reflow.reflow_plan(user_id, plan_id, now=NOW)
    grown = _rows(user_id, plan_id)

    assert grown != baseline
    stored_recipe = db.get_plan(user_id, plan_id)["recipe"]
    assert "profile" not in stored_recipe and "vo2_ratio" not in str(stored_recipe)


# ------------------------------------------------------ daily maintenance
def _neutralize_sweep(monkeypatch):
    from wattracker import server as servermod

    monkeypatch.setattr(servermod.importer, "run_auto_scan",
                        lambda: {"users": 0, "imported": 0, "completed": 0})
    monkeypatch.setattr(servermod.races, "refresh_race_results",
                        lambda *a, **k: None)
    return servermod


def test_daily_sweep_reflows_the_active_plan(user_id, monkeypatch):
    """The sweep has no `now` override, so the plan has to straddle the real
    date for its future rows to be rewritable."""
    servermod = _neutralize_sweep(monkeypatch)
    _use_profile(monkeypatch, UNMEASURED)
    today = utc_today()
    plan_id = _seed_plan(user_id, start=today - dt.timedelta(days=today.weekday()),
                         weeks=6)
    before = {r["id"]: r for r in _rows(user_id, plan_id)}
    _use_profile(monkeypatch, MEASURED)

    totals = servermod.run_daily_maintenance()

    assert totals["reflowed"] > 0
    changed = [r for r in _rows(user_id, plan_id)
               if r["zwo_or_segments"] != before[r["id"]]["zwo_or_segments"]]
    assert changed


def test_daily_sweep_leaves_users_without_an_active_plan_alone(user_id,
                                                               monkeypatch):
    servermod = _neutralize_sweep(monkeypatch)
    called = []
    monkeypatch.setattr(servermod.reflow, "reflow_plan",
                        lambda *a, **k: called.append(a) or {})

    totals = servermod.run_daily_maintenance()

    assert called == []
    assert totals["reflowed"] == 0


def test_daily_sweep_survives_a_failing_reflow(user_id, monkeypatch):
    """One user's broken plan must not abort the whole nightly sweep."""
    servermod = _neutralize_sweep(monkeypatch)
    _seed_plan(user_id)

    def boom(*a, **k):
        raise RuntimeError("nope")

    monkeypatch.setattr(servermod.reflow, "reflow_plan", boom)
    totals = servermod.run_daily_maintenance()  # must not raise
    assert totals["reflowed"] == 0
