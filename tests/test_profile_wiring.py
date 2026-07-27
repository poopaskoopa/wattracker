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
import pathlib
import re
import threading
import xml.etree.ElementTree as ET

import pytest

pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from wattracker import db
from wattracker.analysis.state import TrainingState
from wattracker.ble.runner import FREERIDE_ERG_FRACTION, RideController, flatten_session
from wattracker.metrics import profile_store
from wattracker.metrics.rider import RiderMetrics
from wattracker import server as server_mod
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


# ------------------------- one segment shape, shared by every consumer
def test_both_endpoints_describe_a_sprint_identically(client):
    """Regression: the calendar modal showed "0% - 0 W" twelve times.

    api_plan_workout_detail had its own segment formatter, so it never learned
    the freeride case the preview already handled; the template then filled the
    resulting nulls with zeros - telling the rider a maximal sprint has a
    zero-watt target, on the same session ride.html labels "no target".
    """
    uid = _register(client, "both")
    db.save_user_settings(uid, {"ftp": 250})
    plan_id = db.create_plan(uid, "P", MONDAY.isoformat(), 1)
    session = build_workout("sprint", 60)
    wid = db.add_plan_workout(plan_id, uid, MONDAY.isoformat(), session.name,
                              "sprint", session.total_duration(),
                              session.estimated_tss, zwo.zwo_string(session))

    detail = client.get(f"/api/plan/workout/{wid}").json()["segments"]
    preview = client.get("/ride/workout/preview?type=sprint&minutes=60"
                         ).json()["segments"]

    assert detail == preview, "one session, two shapes"
    free = [s for s in detail if s["free"]]
    assert free, "the sprint efforts must be marked free"
    for row in free:
        # Nothing a renderer could turn into "0 W" or "0%".
        for key in ("watts", "watts_low", "watts_high", "watts_on", "watts_off",
                    "power", "on_power", "off_power"):
            assert row[key] is None, (key, row)
        assert row["label"] == "Max effort - no target"


def test_no_segment_reports_a_zero_watt_target(client):
    """A zero-watt target is never a real prescription, in any workout kind."""
    uid = _register(client, "zerowatt")
    db.save_user_settings(uid, {"ftp": 250})
    for kind in ("sprint", "vo2max", "endurance", "recovery"):
        data = client.get(f"/ride/workout/preview?type={kind}&minutes=60").json()
        for row in data["segments"]:
            for key in ("watts", "watts_low", "watts_high", "watts_on",
                        "watts_off"):
                assert row[key] is None or row[key] > 0, (kind, key, row)


def test_detail_and_preview_agree_for_every_kind(client):
    """The shared formatter is the point: they cannot drift apart again."""
    from wattracker.prescribe.present import segment_rows

    uid = _register(client, "everykind")
    db.save_user_settings(uid, {"ftp": 250})
    plan_id = db.create_plan(uid, "P", MONDAY.isoformat(), 1)
    for i, kind in enumerate(("sprint", "vo2max", "threshold", "endurance")):
        date = (MONDAY + dt.timedelta(days=i)).isoformat()
        session = build_workout(kind, 60)
        wid = db.add_plan_workout(plan_id, uid, date, session.name, kind,
                                  session.total_duration(),
                                  session.estimated_tss,
                                  zwo.zwo_string(session))
        detail = client.get(f"/api/plan/workout/{wid}").json()["segments"]
        assert detail == segment_rows(build_workout(kind, 60), 250.0)


def test_workout_chart_breaks_the_line_across_a_free_block():
    """The shared SVG renderer must not trace a target through a free block."""
    js = (pathlib.Path("wattracker/web/static/workout_graph.js")).read_text()
    # The line is built per RUN of targeted blocks, so a free block ends one
    # subpath and the next starts with a fresh move.
    assert "if (b.free) { current = null; return; }" in js
    assert "runs.push(current)" in js


def test_workout_chart_fills_each_targeted_segment_separately():
    js = pathlib.Path("wattracker/web/static/workout_graph.js").read_text()
    fill_block = js.split("// One closed fill per prescribed segment", 1)[1].split(
        "// The target line is BROKEN", 1
    )[0]

    assert "profile.forEach(function (b)" in fill_block
    assert "if (b.free) return;" in fill_block
    assert "' L ' + x(b.start) + ' ' + y(b.watts_start)" in fill_block
    assert "' L ' + x(b.end) + ' ' + y(b.watts_end)" in fill_block
    assert "' L ' + x(b.end) + ' ' + y(0) + ' Z'" in fill_block
    assert 'class="pf-area' in fill_block


def test_workout_chart_zone_boundaries_and_average_target():
    js = pathlib.Path("wattracker/web/static/workout_graph.js").read_text()
    classifier = js.split("function zoneClass", 1)[1].split(
        "// Target-power LINE graph", 1
    )[0]

    assert "((wattsStart + wattsEnd) / 2) / ftp" in classifier
    assert re.findall(
        r'if \(ratio ([<]=? [0-9.]+)\) return " pf-zone-([1-6])";',
        classifier,
    ) == [
        ("< 0.56", "1"),
        ("<= 0.75", "2"),
        ("<= 0.90", "3"),
        ("<= 1.05", "4"),
        ("<= 1.20", "5"),
        ("<= 1.50", "6"),
    ]
    assert 'return " pf-zone-7";' in classifier


def test_workout_chart_missing_ftp_keeps_neutral_fill():
    js = pathlib.Path("wattracker/web/static/workout_graph.js").read_text()

    assert 'if (!Number.isFinite(ftp) || ftp <= 0) return "";' in js
    assert 'svg += \'<path d="\' + area + \'" class="pf-area\'' in js
    assert "hasFtp ? ftpW : NaN" in js


def test_workout_chart_profile_css_contract():
    css = pathlib.Path("wattracker/web/static/style.css").read_text()
    profile_css = css.split(".profile-wrap {", 1)[1].split("/* Ride */", 1)[0]

    assert "max-width: 760px" not in profile_css
    assert ".pf-area { fill: var(--accent);" in profile_css
    fills = re.findall(
        r"\.pf-zone-([1-7]) \{ fill-opacity: ([.0-9]+); \}",
        profile_css,
    )
    assert [zone for zone, _ in fills] == [str(i) for i in range(1, 8)]
    assert [float(opacity) for _, opacity in fills] == sorted(
        float(opacity) for _, opacity in fills
    )
    for selector in (".pf-grid", ".pf-line", ".pf-ftp"):
        rule = profile_css.split(selector + " {", 1)[1].split("}", 1)[0]
        assert "vector-effect: non-scaling-stroke" in rule
    assert "font-size: var(--fs-xs)" in profile_css
    assert "font-size: 10px" not in profile_css
    assert "dominant-baseline: middle" in profile_css
    assert ".pf-xlab { text-anchor: middle; }" in profile_css
    assert ".pf-ylab, .profile-svg .pf-ftplab { text-anchor: end; }" in profile_css


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


def test_sprint_type_info_advertises_no_work_wattage(client):
    """The picker meta line read ">150% FTP - 375 W" for a targetless session."""
    from wattracker.prescribe.planner import workout_type_info

    assert workout_type_info("sprint")["work"] is None
    uid = _register(client, "picker")
    db.save_user_settings(uid, {"ftp": 250})
    info = client.get("/ride/workout/preview?type=sprint&minutes=60").json()["type_info"]
    assert info["work"] is None and info["work_watts"] is None
    # Every other type still publishes one.
    other = client.get("/ride/workout/preview?type=vo2max&minutes=60").json()
    assert other["type_info"]["work_watts"] == 280


def test_live_ride_reports_no_target_during_a_sprint_effort():
    """The in-ride readout must not announce a target on a free block.

    RideController still holds ERG resistance there - the trainer needs a
    number - but the state flags it so the UI can say MAX instead of quoting
    the resistance as a goal (including to screen readers).
    """
    session = build_workout("sprint", 45, profile=STRONG)
    ctl = RideController(session, ftp=250.0, autosave=False)
    blocks, _ = flatten_session(session)
    free_start = next(b[0] for b in blocks if b[2] == "free")
    steady_start = next(b[0] for b in blocks if b[2] == "const")

    assert ctl.target_is_free(free_start + 1) is True
    assert ctl.target_is_free(steady_start + 1) is False

    ctl.elapsed = free_start + 1
    assert ctl.state()["target_free"] is True
    ctl.elapsed = steady_start + 1
    assert ctl.state()["target_free"] is False


def test_live_ride_state_flags_nothing_free_in_a_targeted_workout():
    ctl = RideController(build_workout("vo2max", 60), ftp=250.0, autosave=False)
    for t in (0, 100, 1500, 3000):
        ctl.elapsed = t
        assert ctl.state()["target_free"] is False


def test_ride_preview_still_reports_targets_for_targeted_sessions(client):
    uid = _register(client, "vo2rider")
    db.save_user_settings(uid, {"ftp": 250})
    data = client.get("/ride/workout/preview?type=vo2max&minutes=60").json()
    assert not any(b.get("free") for b in data["profile"])
    assert max(b["watts_start"] for b in data["profile"]) > 250


# ------------------------------------- the target readout, and dead UI hooks
def test_target_card_can_hide_its_unit(client):
    """Regression: the readout showed "MAX W" - "MAX" plus a literal " W".

    The unit was a bare text sibling of #rTarget, and the class the script
    toggled to hide it did not exist in any stylesheet.
    """
    _register(client, "readout")
    html = client.get("/ride").text
    # The unit must be an element the script can hide, not loose text.
    assert re.search(r'<span id="rTarget">[^<]*</span>\s*<span class="unit" '
                     r'id="rTargetUnit">\s*W\s*</span>', html)
    # The unit must not be loose text right after the number.
    assert not re.search(r'id="rTarget">[^<]*</span>\s*W', html)
    assert 'document.getElementById("rTargetUnit").hidden' in html


def test_no_javascript_toggles_a_class_that_has_no_style():
    """Generalises the dead `no-target` hook that let "MAX W" ship.

    A class toggled in JS with no CSS rule behind it is a silent no-op; the
    code reads as if it handles the case and nothing happens.
    """
    css = pathlib.Path("wattracker/web/static/style.css").read_text()
    roots = [pathlib.Path("wattracker/web/templates"),
             pathlib.Path("wattracker/web/static")]
    missing = []
    for root in roots:
        for path in list(root.glob("*.html")) + list(root.glob("*.js")):
            for name in re.findall(
                r"classList\.(?:add|toggle|remove)\(\s*[\"']([A-Za-z0-9_-]+)[\"']",
                path.read_text(),
            ):
                if not re.search(r"\.%s\b" % re.escape(name), css):
                    missing.append(f"{path.name}: .{name}")
    assert not missing, f"classes toggled in JS with no CSS rule: {missing}"


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


def _store_profile(user_id, profile):
    """Write the snapshot every consumer will read (the real path)."""
    db.save_rider_profile(
        user_id,
        {f: getattr(profile, f, None) for f in db.RIDER_PROFILE_FIELDS},
    )


def _measures_as(monkeypatch, profile):
    """Make the expensive COMPUTATION yield ``profile``.

    For tests that exercise a WRITER (the sweep, an import): they refresh the
    snapshot themselves, so a pre-stored row would just be overwritten.
    """
    monkeypatch.setattr(profile_store.rider, "for_user",
                        lambda uid, state=None, now=None: profile)


def test_plan_detail_rebuild_matches_the_stored_zwo(client, monkeypatch):
    """Regression: the endpoint rebuilt with population constants while the
    stored .zwo had been generated from the rider's measured 5-min power - a
    ~30 W gap at FTP 250 on the same workout."""
    uid = _register(client, "detail")
    _store_profile(uid, STRONG)
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
    _store_profile(uid, STRONG)
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
    _store_profile(uid, STRONG)
    client.post("/generate/plan", data={
        "name": "P", "weeks": "3", "hours_per_week": "6", "days": ["0", "2", "4"],
        "hit_days_per_week": "1", "model": "polarized",
        "start_date": MONDAY.isoformat(),
    })
    plan = db.get_active_plan(uid)
    assert plan is not None
    result = reflow.reflow_plan(uid, plan["id"], now=dt.datetime(2026, 7, 7, 9, 0))
    assert (result["updated"], result["inserted"], result["deleted"]) == (0, 0, 0)


# ------------------------------------------- the stored profile (not a cache)
# The profile depends on WALL-CLOCK time, not only on the activity set: FTP
# decays across a layoff and HRmax detection has a rolling lookback. An
# in-process cache keyed on the activity set therefore served a 30-day-stale
# rider forever - the reason this is a stored snapshot refreshed by writers.
def test_detraining_changes_the_prescription_without_a_new_activity(user_id,
                                                                    monkeypatch):
    """Regression: 30 days off moved FTP 269.7 -> 238.0 with no new rides.

    The old cache's key never moved, so it kept prescribing 1.16 x 269.7 while
    the rider's FTP had fallen to 238 - a 33 W error that never expired.
    """
    day0 = RiderMetrics(ftp=269.7, vo2_ratio=1.261)
    day30 = RiderMetrics(ftp=238.0, vo2_ratio=1.429)

    _measures_as(monkeypatch, day0)
    profile_store.refresh(user_id)
    fresh = profile_store.for_user(user_id)
    assert fresh.ftp == pytest.approx(269.7)
    before = build_workout("vo2max", 60, profile=fresh)

    # A month passes. No ride is imported; only the wall clock and the decayed
    # FTP estimate move - exactly the case the cache could not see.
    _measures_as(monkeypatch, day30)
    profile_store.refresh(user_id)
    after_profile = profile_store.for_user(user_id)

    assert after_profile.ftp == pytest.approx(238.0)
    assert after_profile.vo2_ratio == pytest.approx(1.429)
    after = build_workout("vo2max", 60, profile=after_profile)
    assert zwo.zwo_string(after) != zwo.zwo_string(before)


def test_an_ftp_history_write_reaches_the_next_prescription(user_id):
    """FTP comes from ftp_history, which no activity-set key can see."""
    from wattracker.ingest import importer

    db.insert_activity(user_id, {
        "dedup_hash": "h1", "filename": "r.fit",
        "start_time": "2026-07-14T08:00:00", "duration_s": 3600,
        "distance_m": 0.0, "avg_power": 250.0, "avg_hr": 150.0, "np": 250.0,
        "if_": 1.0, "tss": 100.0, "streams": {"power": [250] * 3600},
    })
    db.add_ftp_entry(user_id, "2026-07-14", 300.0, "manual")
    profile_store.refresh(user_id)
    assert profile_store.for_user(user_id).ftp == pytest.approx(300.0)

    # Same activity set, new FTP row: the snapshot must move with it.
    db.add_ftp_entry(user_id, "2026-07-15", 240.0, "manual")
    profile_store.refresh(user_id)
    assert profile_store.for_user(user_id).ftp == pytest.approx(240.0)
    assert importer.current_ftp(user_id) == pytest.approx(240.0)


def test_reads_never_compute_however_many_there_are(user_id, monkeypatch):
    """No compute-on-read path means no cold miss and no thundering herd.

    The old cache let N concurrent callers each trigger a full computation;
    here a read is one indexed row read and the expensive function is simply
    not reachable from it.
    """
    computed = []
    monkeypatch.setattr(profile_store.rider, "for_user",
                        lambda uid, state=None, now=None: computed.append(uid)
                        or RiderMetrics(vo2_ratio=1.20))
    _store_profile(user_id, RiderMetrics(vo2_ratio=1.20))

    results = []
    threads = [threading.Thread(target=lambda: results.append(
        profile_store.for_user(user_id))) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == 16
    assert all(r.vo2_ratio == pytest.approx(1.20) for r in results)
    assert computed == []


def test_v21_migration_adds_the_profile_table_and_keeps_data(tmp_path):
    """The snapshot arrives by migration, not by wiping the user's database."""
    path = str(tmp_path / "migration.db")
    db.init_db(path)
    uid = db.create_user("kept", "hash", path)
    import sqlite3

    conn = sqlite3.connect(path)
    conn.execute("DROP TABLE rider_profile")
    conn.execute("PRAGMA user_version=21")
    conn.commit()
    conn.close()

    db.init_db(path)

    conn = sqlite3.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    assert conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='rider_profile'"
    ).fetchone()
    assert conn.execute("SELECT username FROM users WHERE id=?",
                        (uid,)).fetchone()[0] == "kept"
    # Not backfilled: an absent row prescribes the population constants, which
    # is exactly what the app did before profiles existed.
    assert conn.execute("SELECT COUNT(*) FROM rider_profile").fetchone()[0] == 0
    conn.close()


def test_stored_profile_round_trips_every_field(user_id):
    full = RiderMetrics(
        ftp=250.0, weight_kg=72.5, hr_max=189.0, hr_max_source="measured",
        n_hr_activities=12, cp=245.0, wprime=21000.0, wprime_j_per_kg=290.0,
        cp_w_per_kg=3.4, peak_5s=1088.0, peak_60s=520.0, peak_300s=300.0,
        sprint_ratio=4.35, vo2_ratio=1.20,
    )
    _store_profile(user_id, full)
    assert profile_store.for_user(user_id) == full


def test_a_missing_snapshot_reads_as_unmeasured(user_id):
    """"Not computed yet" prescribes the population constants, not nonsense."""
    assert db.get_rider_profile(user_id) is None
    profile = profile_store.for_user(user_id)
    assert profile.vo2_ratio is None and profile.sprint_ratio is None
    assert zwo.zwo_string(build_workout("vo2max", 60, profile=profile)) == \
        zwo.zwo_string(build_workout("vo2max", 60))


def test_snapshot_records_when_it_was_computed(user_id, monkeypatch):
    """Staleness has to be inspectable, which is half the point of storing it."""
    assert profile_store.computed_at(user_id) is None
    _measures_as(monkeypatch, RiderMetrics(vo2_ratio=1.20))
    profile_store.refresh(user_id)
    stamp = profile_store.computed_at(user_id)
    assert stamp and stamp.startswith("20")


def test_refresh_keeps_the_previous_snapshot_when_computation_fails(user_id,
                                                                    monkeypatch):
    """Stale but coherent beats empty: the next sweep tries again."""
    _store_profile(user_id, RiderMetrics(vo2_ratio=1.20))

    def boom(*a, **k):
        raise RuntimeError("stream decompression failed")

    monkeypatch.setattr(profile_store.rider, "for_user", boom)
    profile_store.refresh(user_id)  # must not raise
    assert profile_store.for_user(user_id).vo2_ratio == pytest.approx(1.20)


def test_import_refreshes_the_snapshot(user_id, monkeypatch):
    """A new ride can set a new 5s or 5min peak; the plan must see it."""
    from wattracker.ingest import importer

    _measures_as(monkeypatch, RiderMetrics(vo2_ratio=1.25, sprint_ratio=4.35))
    monkeypatch.setattr(importer, "evaluate_ftp", lambda *a, **k: False)
    monkeypatch.setattr(importer, "match_plan_completions", lambda *a, **k: 0)
    monkeypatch.setattr(importer, "ingest_file", lambda *a, **k: 1)
    monkeypatch.setattr(importer.db, "record_scanned_file", lambda *a, **k: None)
    assert db.get_rider_profile(user_id) is None

    import os
    import tempfile
    folder = tempfile.mkdtemp()
    open(os.path.join(folder, "ride.fit"), "wb").write(b"x")
    importer.scan_activities(user_id, directory=folder)

    stored = profile_store.for_user(user_id)
    assert stored.vo2_ratio == pytest.approx(1.25)
    assert stored.sprint_ratio == pytest.approx(4.35)


# ------------------------------ every path that changes the rider refreshes it
def test_an_in_app_ride_refreshes_the_snapshot(user_id, monkeypatch):
    """An in-app BLE ride is not a file import, so scan_activities never sees
    it - a rider who only rides in the app had a snapshot that went stale until
    the next sweep, or forever with the sweep disabled."""
    _measures_as(monkeypatch, RiderMetrics(vo2_ratio=1.22, sprint_ratio=4.10))
    ctl = RideController(build_workout("endurance", 45), ftp=250.0,
                         user_id=user_id)
    ctl._samples = {"power": [200] * 120, "cadence": [90] * 120,
                    "heartrate": [140] * 120}
    ctl.started_at = dt.datetime(2026, 7, 20, 8, 0)
    ctl.elapsed = 120
    assert db.get_rider_profile(user_id) is None

    ctl._save()

    stored = profile_store.for_user(user_id)
    assert stored.vo2_ratio == pytest.approx(1.22)
    assert stored.sprint_ratio == pytest.approx(4.10)


def test_linking_duplicates_refreshes_the_snapshot(client, monkeypatch):
    """Duplicates are excluded from the MMP curve, so linking them moves CP,
    W' and every peak the profile is built from."""
    uid = _register(client, "dupes")
    refreshed = []
    monkeypatch.setattr(server_mod.importer, "backfill_duplicate_links",
                        lambda u: 3)
    monkeypatch.setattr(server_mod.profile_store, "refresh",
                        lambda u, state=None: refreshed.append(u))

    client.post("/activities/link-duplicates", follow_redirects=False)

    assert refreshed == [uid]


def test_linking_nothing_does_not_refresh(client, monkeypatch):
    uid = _register(client, "nodupes")
    refreshed = []
    monkeypatch.setattr(server_mod.importer, "backfill_duplicate_links",
                        lambda u: 0)
    monkeypatch.setattr(server_mod.profile_store, "refresh",
                        lambda u, state=None: refreshed.append(u))
    client.post("/activities/link-duplicates", follow_redirects=False)
    assert refreshed == []


def test_sweep_syncs_races_before_recomputing_the_profile(user_id, monkeypatch):
    """Zwift writes the rider's weight during a race sync, and weight feeds
    wprime_j_per_kg / cp_w_per_kg - so a profile computed first was a day stale
    every time the weight moved."""
    order = []

    monkeypatch.setattr(server_mod.importer, "run_auto_scan",
                        lambda: {"users": 0, "imported": 0, "completed": 0})

    def fake_race_refresh(uid, respect_backoff=True):
        order.append("races")
        db.save_user_settings(uid, {"weight_kg": 71.5})

    def fake_profile_refresh(uid, state=None):
        order.append("profile")
        # The weight the race sync just wrote must be visible here.
        order.append(db.get_user_settings(uid).get("weight_kg"))

    monkeypatch.setattr(server_mod.races, "refresh_race_results",
                        fake_race_refresh)
    monkeypatch.setattr(server_mod.profile_store, "refresh", fake_profile_refresh)

    server_mod.run_daily_maintenance()

    assert order == ["races", "profile", 71.5]


# ------------------------------------------ the profile state is visible
def test_profile_page_says_when_targets_are_not_personalised(client):
    """The silence around an uncomputed profile is what let a stale-profile bug
    live: with no sweep, targets are population constants and nothing said so."""
    _register(client, "generic")
    html = client.get("/profile").text
    assert "no rider profile computed yet" in html
    assert "112% FTP" in html  # the population default it is actually using


def test_profile_page_shows_the_personalised_targets_and_when(client):
    uid = _register(client, "personal")
    _store_profile(uid, RiderMetrics(ftp=250.0, vo2_ratio=1.20,
                                     sprint_ratio=4.35))
    html = client.get("/profile").text
    assert "Personalised from your own data" in html
    assert "110% FTP" in html          # the derived VO2 target
    assert "4.35x FTP" in html         # the measured sprint peak
    assert profile_store.computed_at(uid) in html


def test_target_status_is_honest_about_a_half_measured_rider():
    from wattracker.prescribe.present import target_status

    status = target_status(RiderMetrics(vo2_ratio=1.20), "2026-07-26T06:00:00")
    assert status["personalised"] is True
    assert status["vo2_target"] == pytest.approx(1.10)
    # Sprint is unmeasured, so it is the population figure, not the rider's.
    assert status["sprint_ratio"] is None
    assert status["sprint_load"] == status["sprint_default"]


def test_refresh_returns_what_is_actually_stored(user_id, monkeypatch):
    """The return value and the stored row must never diverge."""
    _measures_as(monkeypatch, RiderMetrics(vo2_ratio=1.25))
    assert profile_store.refresh(user_id).vo2_ratio == pytest.approx(1.25)

    def boom(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(profile_store.db, "save_rider_profile", boom)
    _measures_as(monkeypatch, RiderMetrics(vo2_ratio=1.40))
    assert profile_store.refresh(user_id) is None       # write failed
    # ... and the stored row is untouched, so readers stay coherent.
    assert profile_store.for_user(user_id).vo2_ratio == pytest.approx(1.25)


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


def test_the_banner_explains_a_race_skip(user_id):
    """Silently doing nothing reads as a broken adaptation - say why."""
    _seed_plan(user_id)
    db.add_race_date(user_id, (NOW.date() + dt.timedelta(days=10)).isoformat(),
                     priority="A", name="Nationals", duration_min=120)
    state = TrainingState(ftp=250.0, tsb=-30.0, overreach=True)
    summary = adapt.apply_adaptations(user_id, state, NOW)
    banner = adapt.banner_for(state, summary)
    assert banner["race_skipped"] == summary["skipped_raced"] > 0
    assert "race taper" in banner["race_note"]


def test_the_post_race_summary_has_the_same_shape(user_id):
    """Two summary shapes is a trap for every consumer."""
    _seed_plan(user_id)
    db.add_race_date(user_id, (NOW.date() - dt.timedelta(days=2)).isoformat(),
                     priority="B", name="Yesterday", duration_min=90)
    summary = adapt.apply_adaptations(
        user_id, TrainingState(ftp=250.0, tsb=-30.0, overreach=True), NOW)
    assert summary["status"] == adapt.POST_RACE
    assert set(summary) >= {"status", "adjusted", "skipped_raced", "upcoming",
                            "window_days"}
    assert adapt.banner_for(TrainingState(ftp=250.0), summary)["race_skipped"] == 0


def test_race_window_is_scoped_to_one_plan(user_id):
    """A second, overlapping plan's ride days are days THIS plan never had.

    Feeding them to race_effects moved post-race recovery onto dates the
    generator does not own, so adapt and the generator disagreed about the
    window.
    """
    plan_id = _seed_plan(user_id)
    race_day = NOW.date() + dt.timedelta(days=3)
    db.add_race_date(user_id, race_day.isoformat(), priority="A", name="R",
                     duration_min=300)
    mine = adapt.race_window(user_id, plan_id, NOW)

    # A second plan riding EVERY day, overlapping the first.
    other = db.create_plan(user_id, "Other", MONDAY.isoformat(), 4)
    for offset in range(0, 28):
        day = (MONDAY + dt.timedelta(days=offset)).isoformat()
        db.add_plan_workout(other, user_id, day, "X", "endurance", 3600, 50.0,
                            "<x/>", origin=reflow.GENERATED)

    assert adapt.race_window(user_id, plan_id, NOW) == mine


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
