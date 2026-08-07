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
from wattracker.prescribe.planner import (
    VARIANTS, WORKOUT_TYPE_INFO, build_workout)


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


# ----------------------------------------- time in zone across every duration
# TSS and IF at 60min hid a real defect: `_tempo_progression` hard-coded
# 4 x 8min, so EVERY session of 60min or more got the same 32 minutes of Zone
# 3 while classic tempo scaled to 75. The Zone 2 base absorbed the difference,
# so TSS stayed comparable and only the dose - the thing that makes a tempo
# session a tempo session - diverged. Assert on time in zone, at the durations
# where the fitting loops actually change shape.
_TIZ_DURATIONS = (60, 75, 90, 120, 180, 240)

# Variants whose dose diverges from classic by more than the band. Six entries
# lived here from PR #63 until issue #66 fixed them (short_short, long_intervals
# and descending on vo2max; over_unders on threshold; long_blocks and
# with_surges on sweet_spot). Every variant now reads classic's dose for the
# same ride and fits its own shape to it, so the list is empty and must stay
# that way: a new entry means a variant is prescribing the wrong training load.
_UNREVIEWED_DOSE: dict = {}

_BANDS = {info["key"]: (info["low"], info["high"]) for info in WORKOUT_TYPE_INFO}


def _time_in_zone(session, kind, tol=1e-9):
    """Seconds prescribed inside this workout type's own published band.

    The band comes from WORKOUT_TYPE_INFO rather than a literal so the check
    follows whatever each type publishes to the picker. A `high` of None (the
    open-ended sprint level) means no ceiling.
    """
    low, high = _BANDS[kind]
    high = float("inf") if high is None else high

    def inside(power):
        return power is not None and low - tol <= power <= high + tol

    total = 0
    for s in session.segments:
        if s.kind == "intervals" and s.repeat:
            if inside(s.on_power):
                total += s.repeat * (s.on_duration or 0)
            if inside(s.off_power):
                total += s.repeat * (s.off_duration or 0)
        elif s.kind == "steadystate" and inside(s.power):
            total += s.duration
        elif s.kind == "freeride" and inside(s.load_fraction):
            # A sprint prescribes no target; its load fraction is the honest
            # stand-in for accounting (see `_sprint`).
            total += s.duration
    return total


def _tiz_params():
    for kind, variants in VARIANTS.items():
        for v in variants:
            if v == "classic":
                continue
            for minutes in _TIZ_DURATIONS:
                marks = []
                if minutes in _UNREVIEWED_DOSE.get((kind, v), ()):
                    marks = [pytest.mark.xfail(
                        strict=True,
                        reason="dose diverges from classic; flagged in review, "
                               "scope decision pending")]
                yield pytest.param(kind, v, minutes, marks=marks,
                                   id=f"{kind}-{v}-{minutes}")


def test_no_variant_is_exempt_from_the_dose_check():
    """`_UNREVIEWED_DOSE` must stay empty, and this is what enforces it.

    Entries there xfail a variant out of the dose check. Six lived there from
    PR #63 until issue #66 fixed them, and while they did, a rider asking for
    VO2max work could receive 58% of the intended stimulus and the suite stayed
    green. An exemption is therefore a statement that a variant is knowingly
    prescribing the wrong training load - never a way to quiet a failure.

    Adding an entry must break this test, so the exemption is a deliberate,
    reviewed act rather than a silent one. If you are here because you added
    one: open an issue like #66, put its number in the reason, and change this
    test in the same commit so the exemption is visible in review.
    """
    assert _UNREVIEWED_DOSE == {}, (
        "variants exempted from the dose check: "
        f"{sorted(_UNREVIEWED_DOSE)}. See this test's docstring."
    )
    # An empty dict is not enough on its own. Review found the guard could be
    # defeated without touching it at all: adding a name to the `continue` in
    # _tiz_params, or trimming _TIZ_DURATIONS, drops cases silently and the
    # file still goes green - a one-line change that reads like test tidying.
    # So pin the generated case set against the full cross-product.
    expected = {
        (kind, variant, minutes)
        for kind, variants in VARIANTS.items()
        for variant in variants
        if variant != "classic"
        for minutes in _TIZ_DURATIONS
    }
    actual = {tuple(param.values) for param in _tiz_params()}
    assert actual == expected, (
        "the dose check does not cover every variant at every duration; "
        f"missing {sorted(expected - actual)}, unexpected {sorted(actual - expected)}"
    )


@pytest.mark.parametrize("kind,variant,minutes", list(_tiz_params()))
def test_variant_time_in_zone_tracks_classic(kind, variant, minutes):
    classic = build_workout(kind, minutes, "classic")
    session = build_workout(kind, minutes, variant)
    c_tiz = _time_in_zone(classic, kind)
    v_tiz = _time_in_zone(session, kind)
    assert c_tiz > 0, (kind, minutes)
    assert abs(v_tiz - c_tiz) <= 0.10 * c_tiz, \
        (kind, variant, minutes, v_tiz / 60, c_tiz / 60)


def _work_powers(session, kind):
    """Prescribed powers of the in-band work efforts, in session order.

    A builder may emit its final effort as a plain `steadystate` rather than an
    `intervals` segment (see `_interval_block`): the last recovery is dropped so
    it cannot run into the cooldown. Both shapes are the same work effort, so
    the ramp has to be read across both.
    """
    low, high = _BANDS[kind]
    powers = []
    for seg in session.segments:
        p = seg.on_power if seg.kind == "intervals" else (
            seg.power if seg.kind == "steadystate" else None)
        if p is not None and low <= p <= high:
            powers.append(p)
    return powers


def test_tempo_progression_dose_grows_with_the_ride():
    """The dose must scale, not saturate - and the ramp must survive scaling."""
    doses = [_time_in_zone(build_workout("tempo", m, "progression"), "tempo")
             for m in (60, 90, 120)]
    assert doses[0] < doses[1] < doses[2], doses
    for minutes in (60, 90, 120):
        s = build_workout("tempo", minutes, "progression")
        powers = _work_powers(s, "tempo")
        assert len(powers) >= 2 and powers == sorted(powers), (minutes, powers)
        assert powers[0] < powers[-1], (minutes, powers)
        # Still Zone 3 at both ends of the ramp.
        low, high = _BANDS["tempo"]
        assert low <= min(powers) and max(powers) <= high, (minutes, powers)


def test_tempo_progression_does_not_end_on_a_recovery():
    """The top block runs into the cooldown, not into a 2-4min recovery.

    `_interval_block` exists to stop an interval builder ending "easy spin"
    immediately followed by "cool down easy"; `_tempo_progression` was the one
    builder added in #63 that did not use it.
    """
    for minutes in (60, 90, 120, 180):
        s = build_workout("tempo", minutes, "progression")
        work = [seg for seg in s.segments
                if seg.kind in ("intervals", "steadystate")]
        last = work[-1]
        assert last.kind == "steadystate", (minutes, last.kind)
        low, high = _BANDS["tempo"]
        assert low <= last.power <= high, (minutes, last.power)


@pytest.mark.parametrize("kind,variant", [
    ("vo2max", "short_short"), ("vo2max", "long_intervals"),
    ("vo2max", "descending"), ("threshold", "over_unders"),
    ("sweet_spot", "long_blocks"), ("sweet_spot", "with_surges"),
])
def test_fixed_variant_dose_tracks_the_ride_not_a_literal(kind, variant):
    """The six #66 variants must follow the ride, not saturate or over-dose.

    `test_variant_time_in_zone_tracks_classic` pins each duration against
    classic; this pins the direction. Five of these carried a hard-coded shape
    that handed a 4-hour ride exactly the dose of a 1-hour one; the sixth
    (sweet_spot/long_blocks) took a flat 40% of ride time and ran away past
    classic on long rides.
    """
    at60, at90, at240 = (
        _time_in_zone(build_workout(kind, m, variant), kind)
        for m in (60, 90, 240))
    # A longer ride never buys less time in zone.
    assert at60 <= at90 <= at240, (kind, variant, at60, at90, at240)
    # And a long ride lands on classic's dose, not on the 60min shape's.
    ceiling = _time_in_zone(build_workout(kind, 240, "classic"), kind)
    assert 0.90 * ceiling <= at240 <= 1.10 * ceiling, \
        (kind, variant, at240, ceiling)


def test_variant_work_efforts_stay_inside_the_published_band():
    """No variant may lift a work effort out of the band its type publishes.

    The dose fixes reshape six builders; the shapes must not buy their time in
    zone by prescribing something the picker never advertised.
    """
    for kind, variants in VARIANTS.items():
        if kind in ("sprint", "endurance", "recovery"):
            continue  # base-heavy kinds: most of the ride is deliberately easy
        low, high = _BANDS[kind]
        for v in variants:
            for minutes in _TIZ_DURATIONS:
                s = build_workout(kind, minutes, v)
                for seg in s.segments:
                    for p in (seg.on_power, seg.off_power, seg.power):
                        # Work efforts are anything at or above the band floor;
                        # recoveries and the Zone 2 base sit below it.
                        if p is not None and p >= low:
                            assert p <= high, (kind, v, minutes, p)


def test_sweet_spot_surge_power_is_pinned():
    """The surge is a sweet-spot surge, not a threshold rep.

    #63 re-specced this 1.10 -> 0.94 under an unchanged variant key with no
    test asserting the value, so the session silently changed character for
    every rider holding a stored `with_surges` row. 0.94 is the top of the
    published sweet-spot band; anything above it makes this a different type.
    """
    low, high = _BANDS["sweet_spot"]
    for minutes in (60, 90, 180):
        s = build_workout("sweet_spot", minutes, "with_surges")
        blocks = [seg for seg in s.segments if seg.kind == "intervals"]
        assert blocks, minutes
        for seg in blocks:
            assert seg.on_power == 0.89, (minutes, seg.on_power)
            assert seg.off_power == 0.94 == high, (minutes, seg.off_power)
            assert seg.off_duration == 10 and seg.on_duration == 170
        assert low <= 0.89


def test_endurance_tempo_finish_power_is_pinned():
    """The finish is upper Zone 2, not tempo.

    #63 re-specced this 0.80 -> 0.74 under an unchanged variant key with no
    test asserting the value. 0.74 is inside the published endurance band; the
    old 0.80 was Zone 3, i.e. the workout's own name was wrong.
    """
    low, high = _BANDS["endurance"]
    for minutes in (60, 120, 240):
        s = build_workout("endurance", minutes, "tempo_finish")
        finish = [seg for seg in s.segments if seg.kind == "steadystate"][-1]
        assert finish.power == 0.74, (minutes, finish.power)
        assert low <= finish.power <= high


def test_vo2max_short_short_sets_do_not_end_on_a_recovery():
    """Each 30/30 set ends on the effort, not on the 30s easy.

    Without `_interval_block` the last set finished with a 30s recovery that ran
    straight into the cooldown, and every earlier set finished with one running
    into the 3min set rest - two easy blocks back to back either way.
    """
    for minutes in (60, 90, 180):
        s = build_workout("vo2max", minutes, "short_short")
        work = [seg for seg in s.segments
                if seg.kind == "intervals"
                or (seg.kind == "steadystate" and (seg.power or 0) >= 1.06)]
        assert work, minutes
        # An `intervals` set is always closed by its own steadystate effort.
        for a, b in zip(work, work[1:]):
            if a.kind == "intervals":
                assert b.kind == "steadystate", (minutes, a.kind, b.kind)
        assert work[-1].kind == "steadystate", minutes


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
