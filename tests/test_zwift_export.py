"""Tests: Zwift player-folder detection, export-target resolution, auto-export."""
import os
import re

import pytest

pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from wattracker import db, paths  # noqa: E402
from wattracker.server import create_app  # noqa: E402


def _zwift_root() -> str:
    return os.environ["WATTRACKER_ZWIFT_WORKOUTS_ROOT"]


def _mk_player_folder(zwift_id: str) -> str:
    p = os.path.join(_zwift_root(), zwift_id)
    os.makedirs(p, exist_ok=True)
    return p


@pytest.fixture()
def client():
    app = create_app()
    with TestClient(app) as c:
        yield c


def _register(client, username="rider"):
    client.post("/register", data={"username": username, "password": "password123"})


PLAN_FORM = {
    "name": "Base Plan",
    "weeks": "2",
    "hours_per_week": "6",
    "hit_days_per_week": "1",
    "start_date": "2026-08-03",
    "days": ["0", "2", "4"],
}


# ------------------------------------------------------------- detection
def test_candidate_zwift_ids_numeric_only_most_recent_first(tmp_path):
    root = tmp_path / "W"
    root.mkdir()
    for name in ("7654321", "1234567", "Downloaded", "-42", "notes.txt"):
        (root / name).mkdir() if name != "notes.txt" else (root / name).write_text("x")
    os.utime(root / "1234567", (2_000_000_000, 2_000_000_000))  # most recent
    cands = paths.candidate_zwift_ids(root=str(root))
    assert [c["zwift_id"] for c in cands] == ["1234567", "7654321"]


def test_resolve_export_dir_branches(tmp_path, home_dir, monkeypatch):
    # missing: empty root (conftest default)
    assert paths.resolve_export_dir(None, None) == (None, "missing")
    # a confined override always wins
    assert paths.resolve_export_dir("123", str(home_dir / "o")) == (
        str(home_dir / "o"), "override")
    # ...but an override outside the trusted roots is refused, not obeyed.
    assert paths.resolve_export_dir("123", str(tmp_path / "outside")) == (
        None, "blocked")
    # single candidate -> detected
    only = _mk_player_folder("1234567")
    d, reason = paths.resolve_export_dir(None, None)
    assert (d, reason) == (only, "detected")
    # matching zwift_id folder -> zwift_id (even with several candidates)
    other = _mk_player_folder("7654321")
    d, reason = paths.resolve_export_dir("7654321", None)
    assert (d, reason) == (other, "zwift_id")
    # several candidates, no id -> user must choose
    assert paths.resolve_export_dir(None, None) == (None, "choose")
    assert paths.resolve_export_dir("999", None) == (None, "choose")  # id folder absent


# ------------------------------------------------------------ auto-export
def test_plan_creation_auto_exports_to_single_player_folder(client):
    _register(client)
    folder = _mk_player_folder("1234567")
    r = client.post("/generate/plan", data=PLAN_FORM)
    assert r.status_code == 200
    assert "Auto-exported" in r.text
    files = sorted(os.listdir(folder))
    assert len(files) == 2 * 3  # weeks * days
    assert all(f.endswith(".zwo") for f in files)
    assert all(f[:4] == "2026" for f in files)  # date-named


def test_plan_creation_with_multiple_folders_asks_user_to_choose(client):
    _register(client)
    a = _mk_player_folder("1234567")
    b = _mk_player_folder("7654321")
    r = client.post("/generate/plan", data=PLAN_FORM)
    assert r.status_code == 200
    assert "Pick yours" in r.text and "/settings" in r.text
    assert os.listdir(a) == [] and os.listdir(b) == []  # never guesses


def test_plan_creation_uses_saved_zwift_id(client):
    _register(client)
    _mk_player_folder("1234567")
    mine = _mk_player_folder("7654321")
    uid = db.get_user_by_username("rider")["id"]
    db.save_user_settings(uid, {"zwift_id": "7654321"})
    r = client.post("/generate/plan", data=PLAN_FORM)
    assert "Auto-exported" in r.text
    assert len(os.listdir(mine)) == 6


def test_plan_creation_without_zwift_folders_hints_settings(client):
    _register(client)
    r = client.post("/generate/plan", data=PLAN_FORM)
    assert r.status_code == 200
    assert "No Zwift Workouts folder" in r.text


# ------------------------------------------------------- settings picker
def test_settings_page_lists_detected_player_folders(client):
    _register(client)
    _mk_player_folder("1234567")
    _mk_player_folder("7654321")
    text = client.get("/settings").text
    assert 'name="zwift_id_choice"' in text
    assert "1234567" in text and "7654321" in text


def test_settings_radio_choice_persists_zwift_id(client):
    _register(client)
    _mk_player_folder("1234567")
    r = client.post("/settings", data={"zwift_id_choice": "1234567"})
    assert r.status_code == 200
    uid = db.get_user_by_username("rider")["id"]
    assert db.get_user_settings(uid)["zwift_id"] == "1234567"


def test_settings_free_text_still_works_without_choice(client):
    _register(client)
    client.post("/settings", data={"zwift_id": "424242"})
    uid = db.get_user_by_username("rider")["id"]
    assert db.get_user_settings(uid)["zwift_id"] == "424242"


def _checked_player_radios(html: str) -> "list[str]":
    """The zwift_id values whose picker radio renders as checked."""
    out = []
    for tag in re.findall(r"<input[^>]*name=\"zwift_id_choice\"[^>]*>", html, re.S):
        if "checked" in tag:
            out.append(re.search(r"value=\"([^\"]*)\"", tag).group(1))
    return out


def test_single_detected_folder_is_preselected_in_the_form(client):
    """With nothing saved, the one detected folder must show as CHECKED.

    resolve_export_dir() already exports to it ("detected"), so an unchecked
    radio next to an empty text field told the user the opposite of what the
    exporter was doing - and the id was never written back, so the day a
    second folder appeared the export turned into "choose" with nothing saved.
    """
    _register(client)
    _mk_player_folder("1234567")
    assert _checked_player_radios(client.get("/settings").text) == ["1234567"]


def test_several_detected_folders_preselect_nothing(client):
    """Pre-selecting only works because there is nothing to guess between."""
    _register(client)
    _mk_player_folder("1234567")
    _mk_player_folder("7654321")
    assert _checked_player_radios(client.get("/settings").text) == []


def test_saved_zwift_id_still_wins_over_the_detected_one(client):
    _register(client)
    _mk_player_folder("1234567")
    _mk_player_folder("7654321")
    uid = db.get_user_by_username("rider")["id"]
    db.save_user_settings(uid, {"zwift_id": "7654321"})
    assert _checked_player_radios(client.get("/settings").text) == ["7654321"]


# ------------------------------------------- explicit export buttons (#44)
#
# The three explicit export routes used to pass `zwift_id or "me"` into a
# resolver that fell back to a literal "me" folder, so with no zwift_id set
# they reported success while writing where Zwift never looks. They now share
# the auto-export sweep's resolver, which means they can also FAIL - and a
# failure has to reach the page instead of escaping as a 500.

def _seed_plan(client):
    """A one-workout plan, created before any player folder exists.

    Creating it first keeps the auto-export sweep out of the way, so what the
    assertions see is what the explicit button did.
    """
    _register(client)
    client.post("/generate/plan", data=dict(PLAN_FORM, weeks="1", days=["0"]))
    uid = db.get_user_by_username("rider")["id"]
    plan_id = db.list_plans(uid)[0]["id"]
    return uid, plan_id


def test_plan_export_button_without_any_folder_says_so(client):
    uid, plan_id = _seed_plan(client)
    r = client.post(f"/plan/{plan_id}/export")
    assert r.status_code == 200
    assert "No Zwift Workouts folder" in r.text and "/settings" in r.text
    assert "Exported" not in r.text
    assert os.listdir(_zwift_root()) == []


def test_plan_export_button_with_two_folders_asks_user_to_choose(client):
    uid, plan_id = _seed_plan(client)
    a = _mk_player_folder("1234567")
    b = _mk_player_folder("7654321")
    r = client.post(f"/plan/{plan_id}/export")
    assert r.status_code == 200
    assert "Pick yours" in r.text and "/settings" in r.text
    assert os.listdir(a) == [] and os.listdir(b) == []


def test_plan_export_button_reports_a_refused_folder_with_the_reason(client, tmp_path):
    """`blocked` is the one case where a folder WAS configured: say which."""
    uid, plan_id = _seed_plan(client)
    outside = tmp_path / "outside"
    outside.mkdir()
    # Written straight to the row: /settings would have refused it, but a
    # restored backup or an older release would not have.
    db.save_user_settings(uid, {})
    conn = db.connect()
    try:
        conn.execute(
            "UPDATE user_settings SET workouts_dir = ? WHERE user_id = ?",
            (str(outside), uid),
        )
        conn.commit()
    finally:
        conn.close()
    assert db.get_user_settings(uid)["workouts_dir"] == str(outside)
    r = client.post(f"/plan/{plan_id}/export")
    assert r.status_code == 200
    assert "will not write to that folder" in r.text and "/settings" in r.text
    assert os.listdir(outside) == []


def test_workout_export_button_without_any_folder_says_so(client):
    uid, plan_id = _seed_plan(client)
    w = db.plan_workouts_for_plan(uid, plan_id, include_zwo=True)[0]
    r = client.post(f"/plan/workout/{w['id']}/export")
    assert r.status_code == 200
    assert "No Zwift Workouts folder" in r.text
    assert os.listdir(_zwift_root()) == []


def test_generate_export_with_two_folders_asks_user_to_choose(client):
    _register(client)
    a = _mk_player_folder("1234567")
    b = _mk_player_folder("7654321")
    assert client.post("/generate", data={"duration_min": "60"}).status_code == 200
    r = client.post("/generate/export", data={"scheduled_date": "2026-08-05"})
    assert r.status_code == 200
    assert "Pick yours" in r.text and "/settings" in r.text
    assert "Exported to Zwift" not in r.text
    assert os.listdir(a) == [] and os.listdir(b) == []
    uid = db.get_user_by_username("rider")["id"]
    # Nothing was written, so nothing may be recorded as exported either.
    assert not db.standalone_workouts_on_date(uid, "2026-08-05")


def test_export_buttons_ignore_a_leftover_me_folder(client):
    """The regression from issue #44, through HTTP.

    A user bitten by the old fallback already has a stale ...\\Workouts\\me\\.
    While the routes passed the literal "me", the resolver found that folder
    and exported into it again - the bug reproducing for exactly the people who
    already had it.
    """
    uid, plan_id = _seed_plan(client)
    stale = _mk_player_folder("me")
    real = _mk_player_folder("1234567")
    assert db.get_user_settings(uid)["zwift_id"] in (None, "")

    r = client.post(f"/plan/{plan_id}/export")
    assert r.status_code == 200
    assert "Exported" in r.text
    assert os.listdir(stale) == []
    assert len(os.listdir(real)) == 1


# ------------------------------- relocated Zwift folder, end to end (#44)
#
# A rider who moved ~/Documents/Zwift/Workouts/<id> to another drive with
# `mklink /J` (Windows) or a symlink (macOS/Linux). The player folder entry is
# still inside the trusted Workouts root; its contents are not. Detection finds
# it, so every export route reaches the writer with it - and while the writer
# applied a stricter rule than the resolver, two of those routes turned into
# 500s with no handler in sight.

posix_only = pytest.mark.skipif(
    os.name != "posix", reason="symlink/junction semantics exercised on POSIX"
)


def _relocate_player_folder(tmp_path, zwift_id="1234567"):
    """<Workouts root>/<id>  ->  <another drive>/ZwiftWorkouts."""
    other_drive = tmp_path / "D_drive" / "ZwiftWorkouts"
    other_drive.mkdir(parents=True)
    os.symlink(other_drive, os.path.join(_zwift_root(), zwift_id),
               target_is_directory=True)
    return other_drive


@posix_only
def test_plan_creation_exports_into_a_relocated_player_folder(client, tmp_path):
    """POST /generate/plan: 200 with a real export, not a 500 after commit.

    The plan rows are written before the auto-export runs, so an unhandled
    refusal here left the user with a plan they could not see.
    """
    _register(client)
    other_drive = _relocate_player_folder(tmp_path)
    r = client.post("/generate/plan", data=PLAN_FORM)
    assert r.status_code == 200
    assert "Auto-exported" in r.text
    assert len(os.listdir(other_drive)) == 2 * 3


@posix_only
def test_export_all_route_exports_into_a_relocated_player_folder(client, tmp_path):
    """POST /plan/export-all: 303 back to the calendar, files on the drive."""
    _register(client)
    other_drive = _relocate_player_folder(tmp_path)
    client.post("/generate/plan", data=PLAN_FORM)
    r = client.post("/plan/export-all", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/calendar?exported=ok"
    assert len(os.listdir(other_drive)) == 2 * 3


@posix_only
def test_explicit_export_button_uses_a_relocated_player_folder(client, tmp_path):
    _register(client)
    other_drive = _relocate_player_folder(tmp_path)
    client.post("/generate/plan", data=dict(PLAN_FORM, weeks="1", days=["0"]))
    uid = db.get_user_by_username("rider")["id"]
    plan_id = db.list_plans(uid)[0]["id"]
    r = client.post(f"/plan/{plan_id}/export")
    assert r.status_code == 200
    assert "Exported" in r.text
    assert len(os.listdir(other_drive)) == 1


# --------------------------- no export route may 500 on a refusal (#44)
#
# ExportTargetUnavailable is a RuntimeError on purpose - "not writable" is not
# an I/O error, and no `except OSError` should swallow it. That makes every
# call site that does NOT handle it a 500. The refusal is injected here rather
# than provoked through a resolver/writer disagreement precisely because those
# two now agree: the handlers exist to keep a future disagreement (or a folder
# that changes underneath us between resolve and write) off the error page.

@pytest.fixture()
def refusing_writer(monkeypatch):
    """Make zwo.write_plan_to_zwift refuse the way paths.workouts_dir does."""
    from wattracker.prescribe import zwo as zwomod

    def _refuse(*a, **kw):
        raise paths.ExportTargetUnavailable("blocked", "injected refusal")

    monkeypatch.setattr(zwomod, "write_plan_to_zwift", _refuse)


def test_export_all_route_reports_a_refusal_instead_of_500(
    client, tmp_path, refusing_writer
):
    _register(client)
    _mk_player_folder("1234567")
    client.post("/generate/plan", data=dict(PLAN_FORM, weeks="1", days=["0"]))
    r = client.post("/plan/export-all", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/calendar?exported=blocked"


def test_sync_plan_exports_returns_a_status_instead_of_raising(client, tmp_path,
                                                               refusing_writer):
    from wattracker import exporter

    _register(client)
    _mk_player_folder("1234567")
    client.post("/generate/plan", data=dict(PLAN_FORM, weeks="1", days=["0"]))
    uid = db.get_user_by_username("rider")["id"]
    result = exporter.sync_plan_exports(uid)
    assert result["status"] == "blocked"
    assert result["exported"] == 0


def test_plan_creation_reports_a_refusal_instead_of_500(client, refusing_writer):
    """And the plan itself still exists - the rows are committed before this."""
    _register(client)
    _mk_player_folder("1234567")
    r = client.post("/generate/plan", data=PLAN_FORM)
    assert r.status_code == 200
    assert "Auto-exported" not in r.text
    assert "will not write to that folder" in r.text  # the 'blocked' wording
    uid = db.get_user_by_username("rider")["id"]
    assert len(db.list_plans(uid)) == 1


def test_plan_creation_reports_an_oserror_instead_of_rendering_nothing(
    client, monkeypatch
):
    """The dead-reason bug: `auto_export_reason = "error: ..."` matched no
    branch, so a failed auto-export rendered an empty gap on the plan card and
    the user was told nothing at all."""
    from wattracker.prescribe import zwo as zwomod

    def _boom(*a, **kw):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(zwomod, "write_plan_to_zwift", _boom)
    _register(client)
    _mk_player_folder("1234567")
    r = client.post("/generate/plan", data=PLAN_FORM)
    assert r.status_code == 200
    assert "Auto-exported" not in r.text
    assert "Not exported" in r.text
    assert "Permission denied" in r.text


def test_reexport_workout_is_best_effort_when_the_writer_refuses(
    client, refusing_writer
):
    """adapt.reexport_workout documents itself as best effort; a refusal must
    not escape it (reflow calls it while rewriting a plan)."""
    from wattracker.prescribe import adapt

    _register(client)
    _mk_player_folder("1234567")
    client.post("/generate/plan", data=dict(PLAN_FORM, weeks="1", days=["0"]))
    uid = db.get_user_by_username("rider")["id"]
    w = db.plan_workouts_for_plan(uid, plan_id=db.list_plans(uid)[0]["id"],
                                  include_zwo=True)[0]
    adapt.reexport_workout(uid, w["date"], w["name"], w["name"] + " v2",
                           zwo_str=w["zwo_or_segments"])


@posix_only
def test_a_resolved_directory_must_not_be_laundered_through_the_override(
    client, tmp_path
):
    """workouts_override is the SUBMITTED-path input, and only that.

    Callers used to resolve a target and hand it straight back as
    workouts_override. That re-labels a folder the app DISCOVERED under a
    trusted root as one the user typed, so the writer judged it by the stricter
    submitted-path rule and refused a legitimate relocated folder - the second,
    subtler half of the same disagreement. This pins the rule (the resolved
    path IS refused as an override) and, with it, why every caller now passes
    the stored setting instead.
    """
    from wattracker.prescribe import zwo as zwomod

    other_drive = _relocate_player_folder(tmp_path)
    resolved, reason = paths.resolve_export_dir(None, None)
    assert reason == "detected"

    workout = [{"date": "2026-08-05", "name": "Test", "zwo": "<workout_file/>"}]
    with pytest.raises(paths.ExportTargetUnavailable) as excinfo:
        zwomod.write_plan_to_zwift(workout, None, workouts_override=resolved)
    assert excinfo.value.reason == "blocked"
    assert os.listdir(other_drive) == []

    # Letting the writer resolve for itself is the supported way, and lands in
    # the same folder the caller's own resolve() named.
    result = zwomod.write_plan_to_zwift(workout, None, workouts_override=None)
    assert result["directory"] == resolved
    assert len(os.listdir(other_drive)) == 1


# ---------------------------------------- no export path manufactures an id
#
# The three ExportManifest call sites - the plan-export sweep, the plan-delete
# prune, and the adapt/reflow re-export - each defaulted the rider's stored
# zwift_id to the literal "me". That is not a harmless placeholder: it is a
# valid bare folder name, so safe_zwift_id() passes it, resolve_export_dir()
# takes its zwift_id branch, and <Workouts>/me comes back as a real directory
# whenever it exists - which is every install upgraded from the version that
# created it. The export then reports status 'ok' with a directory, so the UI
# says success while Zwift never reads the folder. That is issue #44, and it
# is the DEFAULT local path.


def test_the_plan_export_sweep_never_falls_back_to_a_me_folder(client):
    from wattracker import exporter

    uid, _plan_id = _seed_plan(client)
    stale = _mk_player_folder("me")
    real = _mk_player_folder("1234567")
    assert db.get_user_settings(uid)["zwift_id"] in (None, "")

    manifest = exporter.plan_export_manifest(uid)
    assert manifest.zwift_id != "me"

    result = exporter.sync_plan_exports(uid)
    assert result["status"] == "ok"
    assert result["directory"] == real
    assert os.listdir(stale) == []
    assert len(os.listdir(real)) == 1


def test_the_plan_delete_prune_never_falls_back_to_a_me_folder(client):
    from wattracker import exporter

    uid, plan_id = _seed_plan(client)
    stale = _mk_player_folder("me")
    real = _mk_player_folder("1234567")
    exporter.sync_plan_exports(uid)
    assert len(os.listdir(real)) == 1

    result = exporter.remove_plan_exports(uid, plan_id)
    assert result["directory"] == real
    assert os.listdir(real) == []
    assert os.listdir(stale) == []


def test_the_adapt_reexport_never_falls_back_to_a_me_folder(client):
    from wattracker.prescribe import adapt

    uid, plan_id = _seed_plan(client)
    stale = _mk_player_folder("me")
    real = _mk_player_folder("1234567")
    workout = db.plan_workouts_for_plan(uid, plan_id)[0]

    adapt.reexport_workout(
        uid, workout["date"], workout["name"], "Renamed", "<workout_file/>"
    )
    assert os.listdir(stale) == []
    assert [n for n in os.listdir(real)] == [
        f"{workout['date']} Renamed.zwo"
    ]
