"""Just Ride: tempo/sprint builders, published type metadata, preview route."""
import re

import pytest

from wattracker.ble.runner import flatten_session
from wattracker.prescribe.planner import (
    JUST_RIDE_DURATIONS,
    MAX_COOLDOWN_S,
    WORKOUT_BUILDERS,
    WORKOUT_TYPE_INFO,
    WORKOUT_TYPE_KEYS,
    absorb_long_cooldown,
    build_workout,
    workout_type_info,
)

pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from wattracker import db  # noqa: E402
from wattracker.server import create_app  # noqa: E402


@pytest.fixture()
def client():
    app = create_app()
    with TestClient(app) as c:
        yield c


def _register(client, username="rider"):
    client.post("/register", data={"username": username, "password": "password123"})
    return db.get_user_by_username(username)["id"]


# ------------------------------------------------------------------ builders
@pytest.mark.parametrize("minutes", [30, 60, 120])
def test_tempo_builder(minutes):
    s = build_workout("tempo", minutes)
    assert s.workout_type == "tempo"
    assert s.name == "Tempo Intervals"
    assert s.total_duration() == minutes * 60
    interval = next(seg for seg in s.segments if seg.kind == "intervals")
    assert 0.76 <= interval.on_power <= 0.90
    assert interval.repeat >= 2
    assert s.estimated_tss > 0


@pytest.mark.parametrize("minutes", [30, 60, 120])
def test_sprint_builder(minutes):
    """Sprints are maximal free efforts: no power target, only a cue."""
    s = build_workout("sprint", minutes)
    assert s.workout_type == "sprint"
    assert s.name == "Sprint / Neuromuscular"
    assert s.total_duration() == minutes * 60
    efforts = [seg for seg in s.segments if seg.kind == "freeride"]
    assert len(efforts) >= 3
    for effort in efforts:
        assert effort.duration == 12
        assert effort.power is None and effort.on_power is None
        assert "all out" in effort.text
        # Load accounting only - never a target sent to the trainer.
        assert effort.load_fraction > 1.50  # Coggan L7 neuromuscular
    assert s.estimated_tss > 0


def test_build_workout_accepts_new_kinds():
    for kind in ("tempo", "sprint"):
        assert build_workout(kind, 45).workout_type == kind


def test_plan_generator_kinds_unchanged():
    assert set(WORKOUT_BUILDERS) == {
        "vo2max", "threshold", "sweet_spot", "endurance", "recovery"
    }


# ------------------------------------------------------------------ metadata
def test_workout_type_info_order_and_zones():
    keys = [info["key"] for info in WORKOUT_TYPE_INFO]
    assert keys == [
        "endurance", "tempo", "sweet_spot", "threshold", "vo2max",
        "sprint", "recovery",
    ]
    assert workout_type_info("tempo")["zone"] == "Zone 3"
    assert workout_type_info("tempo")["low"] == 0.76
    assert workout_type_info("tempo")["high"] == 0.90
    assert workout_type_info("sprint")["high"] is None
    assert workout_type_info("nope") is None


def test_just_ride_durations():
    assert JUST_RIDE_DURATIONS[0] == 30
    assert JUST_RIDE_DURATIONS[-1] == 240
    assert all(b - a == 15 for a, b in zip(JUST_RIDE_DURATIONS, JUST_RIDE_DURATIONS[1:]))


# ------------------------------------------------------------------- preview
def test_preview_returns_watts_for_known_ftp(client):
    uid = _register(client)
    db.save_user_settings(uid, {"ftp": 200})
    data = client.get("/ride/workout/preview?type=tempo&minutes=60").json()

    assert data["name"] == "Tempo Intervals"
    assert data["workout_type"] == "tempo"
    assert data["duration_s"] == 3600
    assert data["estimated_tss"] > 0
    assert data["profile"]
    info = data["type_info"]
    assert info["zone"] == "Zone 3"
    assert info["low_watts"] == round(0.76 * data["ftp"])
    assert info["high_watts"] == round(0.90 * data["ftp"])
    interval = next(s for s in data["segments"] if s["watts_on"] is not None)
    assert interval["watts_on"] == round(0.80 * data["ftp"])
    assert sum(s["duration_s"] for s in data["segments"]) == 3600


def test_preview_rejects_bad_type_and_duration(client):
    _register(client)
    bad_type = client.get("/ride/workout/preview?type=bogus&minutes=60")
    assert bad_type.status_code == 400
    assert "bogus" in bad_type.json()["error"]

    too_short = client.get("/ride/workout/preview?type=tempo&minutes=10")
    assert too_short.status_code == 400

    not_a_number = client.get("/ride/workout/preview?type=tempo&minutes=abc")
    assert not_a_number.status_code == 400


def test_preview_selects_variant_and_returns_duration_variant_profiles(client):
    uid = _register(client, "rider_variant_preview")
    db.save_user_settings(uid, {"ftp": 200})
    response = client.get(
        "/ride/workout/preview?type=tempo&minutes=60&variant=progression"
    )
    assert response.status_code == 200
    data = response.json()
    assert data["variant"] == "progression"
    assert data["variant_options"] == ["classic", "progression"]
    profiles = data["variant_profiles"]
    assert set(profiles) == {"classic", "progression"}
    assert set(profiles["classic"]) == {"60"}
    assert profiles["classic"]["60"]["duration_s"] == 3600
    assert profiles["progression"]["60"]["profile"] != profiles["classic"]["60"]["profile"]


def test_preview_rejects_unknown_variant(client):
    _register(client, "rider_bad_variant")
    response = client.get(
        "/ride/workout/preview?type=tempo&minutes=60&variant=surprise"
    )
    assert response.status_code == 400
    assert "variant" in response.json()["error"]


def test_preview_requires_auth(client):
    assert client.get(
        "/ride/workout/preview?type=tempo&minutes=60", follow_redirects=False
    ).status_code == 303


def test_ride_page_offers_just_ride(client):
    _register(client)
    text = client.get("/ride").text
    assert 'id="rideTypeSelect"' in text
    assert 'id="rideDurationSelect"' in text
    assert 'value="sprint"' in text
    assert "1 h 15 min" in text
    assert "innerHTML" not in text

# ------------------------------------------------- every type, key durations
@pytest.mark.parametrize("kind", WORKOUT_TYPE_KEYS)
@pytest.mark.parametrize("minutes", [30, 60, 120, 240])
def test_every_type_builds_at_key_durations(kind, minutes):
    # 30/240 are the offered bounds, 60/120 the mid-range. The full
    # JUST_RIDE_DURATIONS ladder adds no builder branch not covered here.
    s = build_workout(kind, minutes)
    assert s.workout_type == kind
    assert s.total_duration() == minutes * 60
    assert s.estimated_tss > 0


@pytest.mark.parametrize("kind", WORKOUT_TYPE_KEYS)
@pytest.mark.parametrize("minutes", [30, 240])
def test_preview_every_type_at_extremes(client, kind, minutes):
    uid = _register(client, f"rider_{kind}_{minutes}")
    db.save_user_settings(uid, {"ftp": 200})
    r = client.get(f"/ride/workout/preview?type={kind}&minutes={minutes}")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["workout_type"] == kind
    assert data["duration_s"] == minutes * 60
    assert sum(s["duration_s"] for s in data["segments"]) == minutes * 60
    info = workout_type_info(kind)
    if kind == "sprint":
        # Prescribed as a maximal effort with no power target, so its work
        # blocks deliberately carry no watts to compare against the band.
        assert any(s["watts_high"] is None for s in data["segments"])
        return
    peak = max(
        w for w in (
            [s["watts_on"] for s in data["segments"] if s["watts_on"] is not None]
            + [s["watts_high"] for s in data["segments"] if s["watts_high"] is not None]
        )
    )
    assert peak >= round(info["low"] * data["ftp"])


def test_vo2max_builds_at_thirty_minutes():
    s = build_workout("vo2max", 30)
    assert s.total_duration() == 1800
    interval = next(seg for seg in s.segments if seg.kind == "intervals")
    assert interval.on_power == 1.12
    # The final rep is emitted separately (no recovery after it), so the
    # repeated block carries reps-1 and the rider still gets >= 3 efforts.
    final = next(seg for seg in s.segments
                 if seg.kind == "steadystate" and seg.power == 1.12)
    assert interval.repeat + 1 >= 3
    assert final.duration == interval.on_duration


def test_vo2max_short_durations_all_build():
    for minutes in range(30, 46):
        assert build_workout("vo2max", minutes).total_duration() == minutes * 60


# ------------------------------------------------------- degenerate long rides
@pytest.mark.parametrize("kind", WORKOUT_TYPE_KEYS)
def test_long_just_rides_are_not_mostly_cooldown(client, kind):
    """Every offered type, ridden long via Just Ride, must stay a real workout.
    The 240-min extreme is the harshest case; the cooldown cap is
    duration-agnostic (see MAX_COOLDOWN_S), so 90/120 add no new branch."""
    minutes = 240
    uid = _register(client, f"rider_cd_{kind}")
    db.save_user_settings(uid, {"ftp": 200})
    data = client.get(
        f"/ride/workout/preview?type={kind}&minutes={minutes}"
    ).json()
    segs = data["segments"]
    assert sum(s["duration_s"] for s in segs) == minutes * 60
    cooldown = sum(s["duration_s"] for s in segs if s["label"] == "Cooldown")
    assert cooldown <= MAX_COOLDOWN_S, f"{kind} @{minutes}: {cooldown}s cooldown"
    # ...and the cooldown is never the biggest thing in the ride.
    assert cooldown < max(s["duration_s"] for s in segs)


@pytest.mark.parametrize("kind", WORKOUT_TYPE_KEYS)
def test_long_just_rides_via_the_session_builder_end_short(client, kind):
    """The ad-hoc branch of _ride_session (websocket path) gets the same fix."""
    _register(client, f"rider_ws_cd_{kind}")
    with client.websocket_connect(f"/ride/ws?type={kind}&minutes=240") as ws:
        msg = ws.receive_json()
    assert msg["status"] == "workout"
    profile = msg["workout"]["profile"]
    assert msg["workout"]["duration_s"] == 240 * 60
    tail = profile[-1]
    assert tail["end"] - tail["start"] <= MAX_COOLDOWN_S


@pytest.mark.parametrize("kind", WORKOUT_TYPE_KEYS)
def test_description_discloses_the_inserted_zone2_base(client, kind):
    """Whenever a Zone 2 base block is inserted, the description says so -
    uniformly across every type and call site (preview + builder)."""
    minutes = 240  # the longest offers are where a base block is ever inserted
    uid = _register(client, f"rider_disc_{kind}")
    db.save_user_settings(uid, {"ftp": 200})

    # The builder now caps the cooldown at the source and discloses the base in
    # its description; a second absorb pass has nothing left to move.
    reference = build_workout(kind, minutes)
    assert absorb_long_cooldown(reference) == 0

    data = client.get(
        f"/ride/workout/preview?type={kind}&minutes={minutes}"
    ).json()
    if "Zone 2 base" in reference.description:
        assert "Zone 2 base" in data["description"], (
            f"{kind} @{minutes}: {data['description']!r}"
        )


@pytest.mark.parametrize("kind", WORKOUT_TYPE_KEYS)
def test_absorb_long_cooldown_second_call_does_not_double_suffix(kind):
    s = build_workout(kind, 240)
    moved = absorb_long_cooldown(s)
    description_after_first = s.description
    moved_again = absorb_long_cooldown(s)
    assert moved_again == 0
    assert s.description == description_after_first
    if moved:
        assert s.description.count("Zone 2 base") == 1


@pytest.mark.parametrize("kind", ["tempo", "sweet_spot", "threshold", "vo2max",
                                  "sprint"])
def test_reclaimed_cooldown_becomes_a_zone2_base_before_the_work(kind):
    s = build_workout(kind, 240)
    # tempo/sprint already absorb inside the builder; the rest do it here.
    absorb_long_cooldown(s)
    # Idempotent either way: a second pass has nothing left to move.
    assert absorb_long_cooldown(s) == 0
    assert s.total_duration() == 240 * 60
    base = [seg for seg in s.segments if seg.kind == "steadystate"]
    assert base and base[0].power == 0.68
    kinds = [seg.kind for seg in s.segments]
    # Sprints carry their work as untargeted freeride blocks, not intervals.
    work_kind = "freeride" if kind == "sprint" else "intervals"
    assert kinds.index("steadystate") < kinds.index(work_kind)
    assert kinds[0] == "warmup"


def test_absorb_long_cooldown_is_a_no_op_when_already_short():
    s = build_workout("endurance", 60)
    before = s.to_dict()
    assert absorb_long_cooldown(s) == 0
    assert s.to_dict() == before


def test_absorb_long_cooldown_preserves_duration_and_recomputes_tss():
    # The builder now caps the cooldown at the source (in _finish) and re-runs
    # compute_tss, so a fresh long build is already absorbed.
    s = build_workout("sweet_spot", 240)
    assert s.total_duration() == 14400
    tail = s.segments[-1]
    assert tail.kind == "cooldown" and tail.duration == 600
    # The reclaimed time became a Zone 2 base right after the warmup.
    base = s.segments[1]
    assert base.kind == "steadystate" and base.power == 0.68
    # 3 x 12min work with only 2 recoveries: 3*720 + 2*300 = 2760.
    assert base.duration == 14400 - 600 - 2760 - 600
    # A second explicit pass has nothing left to move, and TSS stays consistent.
    assert absorb_long_cooldown(s) == 0
    assert s.total_duration() == 14400
    assert s.estimated_tss == pytest.approx(s.compute_tss())


def test_plan_workouts_also_get_the_cooldown_fix(client):
    """The fix now lives in _finish, so the workout_id (plan) branch is capped
    at the source too - a long plan sweet_spot is no longer a ~3h cooldown."""
    uid = _register(client, "rider_planpath")
    plan_id = db.create_plan(uid, "Base", "2026-06-01", 4)
    wid = db.add_plan_workout(
        plan_id, uid, "2026-06-02", "Long Sweet Spot", "sweet_spot",
        240 * 60, 200.0, "<>"
    )
    with client.websocket_connect(f"/ride/ws?workout_id={wid}") as ws:
        msg = ws.receive_json()
    expected, _ = flatten_session(build_workout("sweet_spot", 240))
    assert msg["workout"]["duration_s"] == 240 * 60
    profile = msg["workout"]["profile"]
    assert len(profile) == len(expected)
    tail = profile[-1]
    # Capped at 10min, not the old 14400 - 600 - 3060 = 10740s absorbing cooldown.
    assert tail["end"] - tail["start"] == MAX_COOLDOWN_S
    # The reclaimed time is ridden as a Zone 2 base right after the warmup.
    session = build_workout("sweet_spot", 240)
    assert "Zone 2 base" in session.description
    base = session.segments[1]
    assert base.kind == "steadystate" and base.power == 0.68


def test_long_rides_scale_the_work_up():
    assert next(
        seg for seg in build_workout("tempo", 240).segments if seg.kind == "intervals"
    ).repeat == 5
    # Sprint reps are one freeride block each (no interval target to scale).
    assert len(
        [seg for seg in build_workout("sprint", 240).segments
         if seg.kind == "freeride"]
    ) == 12


# ------------------------------------- metadata matches what the builder makes
def _peak_work_fraction(session):
    """Highest prescribed work power (ignores warmup/cooldown ramps)."""
    out = []
    for seg in session.segments:
        if seg.kind in ("warmup", "cooldown"):
            continue
        if seg.kind == "intervals":
            out.append(max(seg.on_power or 0.0, seg.off_power or 0.0))
        elif seg.kind == "freeride":
            # A maximal effort with no target: unbounded by construction, so it
            # satisfies any published floor and has no ceiling to check.
            out.append(float("inf"))
        elif seg.power is not None:
            out.append(seg.power)
    return max(out)


@pytest.mark.parametrize("info", WORKOUT_TYPE_INFO, ids=lambda i: i["key"])
@pytest.mark.parametrize("minutes", [30, 240])
def test_declared_band_matches_builder(info, minutes):
    work = _peak_work_fraction(build_workout(info["key"], minutes))
    assert work >= info["low"], f"{info['key']} @{minutes}: {work} < low"
    if info["high"] is not None:
        assert work <= info["high"], f"{info['key']} @{minutes}: {work} > high"


# Matches "5-6 x 4min", "2 x 20min", "6-8 x 12s", "3 sets of 10 x 30s".
_REP_COUNT = re.compile(r"\d+\s*(?:-\s*\d+\s*)?x\s*\d|\bsets? of\b", re.I)


@pytest.mark.parametrize("info", WORKOUT_TYPE_INFO, ids=lambda i: i["key"])
def test_type_metadata_states_no_rep_counts(info):
    """Rep counts scale with duration, so the static blurbs must not claim any.

    The per-duration truth is Session.description, which the preview shows.
    """
    for field in ("label", "zone", "focus", "structure"):
        text = info[field]
        assert not _REP_COUNT.search(text), f"{info['key']}.{field}: {text!r}"


def test_preview_exposes_the_per_duration_description(client):
    uid = _register(client, "rider_desc")
    db.save_user_settings(uid, {"ftp": 200})
    short = client.get("/ride/workout/preview?type=vo2max&minutes=45").json()
    long_ = client.get("/ride/workout/preview?type=vo2max&minutes=240").json()
    assert short["description"] and long_["description"]
    assert short["description"] != long_["description"]
    # The duration-agnostic blurb is still served alongside it.
    assert short["type_info"]["structure"] == long_["type_info"]["structure"]


def test_ride_page_renders_the_this_ride_description(client):
    _register(client, "rider_thisride")
    text = client.get("/ride").text
    assert "data.description" in text
    assert "This ride: " in text
    assert "innerHTML" not in text


def test_recovery_metadata_describes_the_recovery_builder():
    info = workout_type_info("recovery")
    assert info["low"] == 0.45 and info["high"] == 0.65 and info["work"] == 0.65
    assert info["zone"] == "Zone 1-2"
    assert "56%" not in info["structure"]


# -------------------------------------------------------------- validation
def test_preview_rejects_non_finite_durations(client):
    _register(client, "rider_nf")
    for minutes in ("1e999", "-1e999", "nan", "inf", "-inf"):
        r = client.get(f"/ride/workout/preview?type=tempo&minutes={minutes}")
        assert r.status_code == 400
        assert "error" in r.json()


def test_preview_rejects_durations_outside_the_offered_set(client):
    _register(client, "rider_off")
    for minutes in (7, 20, 37, 241, 300, 480):
        r = client.get(f"/ride/workout/preview?type=tempo&minutes={minutes}")
        assert r.status_code == 400


# ------------------------------------------------------------------ websocket
def test_ws_reports_error_for_invalid_explicit_just_ride(client):
    _register(client, "rider_ws")
    with client.websocket_connect("/ride/ws?type=vo2max&minutes=37") as ws:
        msg = ws.receive_json()
    assert msg["status"] == "error"
    assert "duration" in msg["error"]


def test_ws_reports_error_for_unknown_explicit_type(client):
    _register(client, "rider_ws2")
    with client.websocket_connect("/ride/ws?type=bogus&minutes=60") as ws:
        msg = ws.receive_json()
    assert msg["status"] == "error"
    assert "bogus" in msg["error"]


def test_ws_explicit_vo2max_thirty_is_honoured(client):
    _register(client, "rider_ws3")
    with client.websocket_connect("/ride/ws?type=vo2max&minutes=30") as ws:
        msg = ws.receive_json()
    assert msg["status"] == "workout"
    assert msg["workout"]["duration_s"] == 1800
    assert msg["workout"]["name"] == "VO2max Intervals"


def test_ws_honours_selected_variant(client):
    _register(client, "rider_ws_variant")
    with client.websocket_connect(
        "/ride/ws?type=tempo&minutes=60&variant=progression"
    ) as ws:
        msg = ws.receive_json()
    assert msg["status"] == "workout"
    assert msg["workout"]["name"] == "Tempo Progression"


def test_ws_rejects_unknown_variant(client):
    _register(client, "rider_ws_bad_variant")
    with client.websocket_connect(
        "/ride/ws?type=tempo&minutes=60&variant=surprise"
    ) as ws:
        msg = ws.receive_json()
    assert msg["status"] == "error"
    assert "variant" in msg["error"]


def test_ws_without_a_type_still_defaults(client):
    _register(client, "rider_ws4")
    with client.websocket_connect("/ride/ws") as ws:
        msg = ws.receive_json()
    assert msg["status"] == "workout"
    assert msg["workout"]["duration_s"] == 45 * 60


def test_ws_plan_workout_path_is_unaffected(client):
    uid = _register(client, "rider_ws5")
    plan_id = db.create_plan(uid, "Base", "2026-06-01", 4)
    wid = db.add_plan_workout(
        plan_id, uid, "2026-06-02", "Tuesday VO2", "vo2max", 3600, 80.0, "<>"
    )
    with client.websocket_connect(f"/ride/ws?workout_id={wid}") as ws:
        msg = ws.receive_json()
    assert msg["status"] == "workout"
    assert msg["workout"]["name"] == "Tuesday VO2"
    assert msg["workout"]["duration_s"] == 3600
