import ntpath
import os

import pytest

from wattracker import paths


def _windows(monkeypatch):
    monkeypatch.setattr(paths.sys, "platform", "win32")


def test_windows_candidates_order_redirects_and_dedupe(monkeypatch):
    _windows(monkeypatch)
    monkeypatch.setattr(paths, "_windows_documents_known_folder", lambda: r"C:\Users\Rider\OneDrive\Documents")
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\Rider\AppData\Local")
    monkeypatch.setenv("OneDriveConsumer", r"C:\Users\Rider\OneDrive")
    monkeypatch.setenv("OneDrive", r"c:\users\rider\onedrive")
    monkeypatch.setenv("OneDriveCommercial", r"D:\Team OneDrive")
    monkeypatch.setenv("USERPROFILE", r"C:\Users\Rider")
    monkeypatch.setattr(paths, "_home", lambda: r"C:\Users\Rider")
    candidates = paths.candidate_activities_dirs()
    assert candidates[0] == os.path.normpath(r"C:\Users\Rider\AppData\Local/Zwift/Activities")
    keys = [ntpath.normcase(ntpath.normpath(path)) for path in candidates]
    assert len(keys) == len(set(keys))
    assert any("Team OneDrive" in path for path in candidates)


def test_windows_missing_env_still_has_documents_candidate(monkeypatch):
    _windows(monkeypatch)
    monkeypatch.setattr(paths, "_windows_documents_known_folder", lambda: None)
    for key in ("LOCALAPPDATA", "OneDriveConsumer", "OneDrive", "OneDriveCommercial", "USERPROFILE"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(paths, "_home", lambda: r"C:\Users\Üser")
    assert paths.candidate_activities_dirs() == [os.path.normpath(r"C:\Users\Üser/Documents/Zwift/Activities")]


def test_unicode_unc_known_folder_is_preserved(monkeypatch):
    _windows(monkeypatch)
    monkeypatch.setattr(paths, "_windows_documents_known_folder", lambda: r"\\server\riders\Zoë Documents")
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    assert paths.candidate_activities_dirs()[0].startswith(r"\\server")
    assert "Zoë Documents" in paths.candidate_activities_dirs()[0]


def test_overrides_and_first_existing_selection(monkeypatch):
    candidates = ["/missing/one", "/present/two"]
    monkeypatch.setattr(paths, "candidate_activities_dirs", lambda: candidates)
    monkeypatch.setattr(paths.os.path, "isdir", lambda value: value == candidates[1])
    assert paths.activities_dir() == candidates[1]
    assert paths.activities_dir("/manual") == "/manual"
    monkeypatch.setenv("WATTRACKER_ACTIVITIES_DIR", "/environment")
    assert paths.activities_dir() == "/environment"


def test_workouts_uses_first_existing_documents_root(monkeypatch):
    roots = ["/first/Workouts", "/second/Workouts"]
    monkeypatch.delenv("WATTRACKER_ZWIFT_WORKOUTS_ROOT", raising=False)
    monkeypatch.setattr(paths, "candidate_workouts_roots", lambda: roots)
    monkeypatch.setattr(paths.os.path, "isdir", lambda value: value == roots[1])
    assert paths.zwift_workouts_root() == roots[1]


def test_workouts_dir_uses_the_player_folder_under_an_existing_root(
    monkeypatch, home_dir
):
    """The id names a folder that EXISTS under one of the Workouts roots.

    workouts_dir() no longer joins the id onto whichever root happens to exist
    and hands that back regardless; it returns the folder Zwift actually reads,
    picked by the same resolver the automatic sweep uses.
    """
    first = home_dir / "OneDrive" / "Zwift" / "Workouts"
    second = home_dir / "Documents" / "Zwift" / "Workouts"
    first.mkdir(parents=True)
    (second / "123").mkdir(parents=True)
    monkeypatch.delenv("WATTRACKER_ZWIFT_WORKOUTS_ROOT", raising=False)
    monkeypatch.setattr(
        paths, "candidate_workouts_roots", lambda: [str(first), str(second)]
    )
    assert paths.workouts_dir("123") == str(second / "123")
    assert paths.resolve_export_dir("123") == (str(second / "123"), "zwift_id")


def test_workouts_dir_refuses_when_the_player_folder_does_not_exist(
    monkeypatch, home_dir
):
    """No fallback: the old code returned <root>/<id> (or <root>/me) for an id
    with no folder, so the .zwo landed somewhere Zwift never reads and the UI
    still said 'exported'."""
    root = home_dir / "Documents" / "Zwift" / "Workouts"
    root.mkdir(parents=True)
    monkeypatch.delenv("WATTRACKER_ZWIFT_WORKOUTS_ROOT", raising=False)
    monkeypatch.setattr(paths, "candidate_workouts_roots", lambda: [str(root)])

    with pytest.raises(paths.ExportTargetUnavailable) as excinfo:
        paths.workouts_dir("123")
    assert excinfo.value.reason == "missing"
    assert not excinfo.value.refused  # nothing was determined, nothing refused
    assert os.listdir(root) == []


def test_player_folder_discovery_checks_all_workout_roots(monkeypatch, tmp_path):
    first = tmp_path / "OneDrive" / "Zwift" / "Workouts"
    second = tmp_path / "Documents" / "Zwift" / "Workouts"
    (first / "111").mkdir(parents=True)
    (second / "222").mkdir(parents=True)
    monkeypatch.delenv("WATTRACKER_ZWIFT_WORKOUTS_ROOT", raising=False)
    monkeypatch.setattr(paths, "candidate_workouts_roots", lambda: [str(first), str(second)])
    assert {item["zwift_id"] for item in paths.candidate_zwift_ids()} == {"111", "222"}
    assert paths.resolve_export_dir("222") == (str(second / "222"), "zwift_id")


def test_workouts_env_override_wins_consistently(monkeypatch, tmp_path):
    exact = tmp_path / "Zwift" / "Workouts" / "98765"
    exact.mkdir(parents=True)
    monkeypatch.setenv("WATTRACKER_WORKOUTS_DIR", str(exact))
    assert paths.workouts_dir("123") == str(exact)
    assert paths.resolve_export_dir("123") == (str(exact), "override")
    assert paths.candidate_zwift_ids() == [{
        "zwift_id": "98765",
        "path": str(exact),
        "mtime": os.path.getmtime(exact),
    }]


def test_function_workouts_override_wins_over_environment(
    monkeypatch, tmp_path, home_dir
):
    # The per-user override still beats the environment - but only a confined
    # one, i.e. a folder inside the sandboxed HOME that the Settings form would
    # also accept.
    per_user = home_dir / "per-user"
    per_user.mkdir()
    monkeypatch.setenv("WATTRACKER_WORKOUTS_DIR", str(tmp_path / "environment"))
    assert paths.workouts_dir("123", str(per_user)) == str(per_user)
    assert paths.resolve_export_dir("123", str(per_user)) == (str(per_user), "override")


def test_workouts_override_outside_trusted_roots_is_refused(monkeypatch, tmp_path):
    """A stored workouts_dir pointing outside the trusted roots is not honoured.

    The Settings form validates on write, but a row can predate that check,
    come from a restored backup or be hand-edited - and this value reaches
    os.makedirs() plus open(..., "w"), so it is an arbitrary directory create
    plus arbitrary .zwo write. Both entry points now say the same thing:
    resolve_export_dir() reports (None, 'blocked') and workouts_dir() raises
    rather than substituting a default. The old fallback returned
    <ZwiftWorkouts>/123 here, i.e. it silently exported somewhere else.
    """
    escape = "/private/tmp/wt_unit_escape/deep"
    env_root = tmp_path / "ZwiftWorkouts"  # conftest's trusted root
    monkeypatch.delenv("WATTRACKER_WORKOUTS_DIR", raising=False)

    assert paths.resolve_export_dir("123", escape) == (None, "blocked")

    with pytest.raises(paths.ExportTargetUnavailable) as excinfo:
        paths.workouts_dir("123", override=escape)
    assert excinfo.value.reason == "blocked"
    assert excinfo.value.refused  # a target WAS named, and was rejected
    assert not os.path.exists("/private/tmp/wt_unit_escape")
    assert os.listdir(env_root) == []  # and no substitute folder was created


def test_workouts_override_symlink_escape_is_refused(tmp_path, home_dir):
    """Containment is checked on the realpath, so a symlink out is refused.

    The link itself sits INSIDE the sandboxed HOME - a lexical containment
    check would accept it. Only resolving the target catches this.
    """
    outside = tmp_path / "outside-target"
    outside.mkdir()
    link = home_dir / "looks-legit"
    link.symlink_to(outside, target_is_directory=True)
    # Guard against the test passing for the wrong reason: the link path really
    # is under the trusted home, and a sibling real directory there is accepted.
    sibling = home_dir / "real"
    sibling.mkdir()
    assert paths.resolve_export_dir("123", str(sibling)) == (str(sibling), "override")
    assert paths.workouts_dir("123", override=str(sibling)) == str(sibling)

    assert paths.resolve_export_dir("123", str(link)) == (None, "blocked")
    with pytest.raises(paths.ExportTargetUnavailable) as excinfo:
        paths.workouts_dir("123", override=str(link))
    assert excinfo.value.refused
    assert os.listdir(outside) == []  # nothing created through the link


# --------------------------------------------- the refusal contract (issue #44)
#
# workouts_dir() used to end with os.path.join(zwift_workouts_root(), id or
# "me"). With no zwift_id set - the default state - every explicit export
# button wrote into <Documents>\Zwift\Workouts\me\, a folder Zwift never reads,
# and the UI reported that path as a success. The fallback also absorbed both
# confinement failures (escaping override, unusable id), so a rejected input
# still produced a path. It is gone: the caller gets a directory that passed
# confine_storage_dir(), or an ExportTargetUnavailable it has to handle.

def _zwift_root():
    return os.environ["WATTRACKER_ZWIFT_WORKOUTS_ROOT"]


def test_workouts_dir_refuses_instead_of_inventing_a_me_folder():
    """The reported bug, at the unit that caused it."""
    with pytest.raises(paths.ExportTargetUnavailable) as excinfo:
        paths.workouts_dir(None)
    assert excinfo.value.reason == "missing"
    assert not excinfo.value.refused
    assert os.listdir(_zwift_root()) == []
    assert not os.path.exists(os.path.join(_zwift_root(), "me"))


def test_a_leftover_me_folder_is_not_treated_as_a_player_folder():
    """Users hit by the bug already have a stale 'me' folder on disk.

    With no id passed, detection only considers numeric player folders, so the
    leftover is ignored and the export goes where Zwift actually reads. This is
    the behaviour the export routes get once they stop passing the literal
    ``settings.get("zwift_id") or "me"`` - which, while it is still there,
    resolves that leftover folder as if it were a player id and reproduces the
    original bug for exactly the users who already suffered it.
    """
    root = _zwift_root()
    os.mkdir(os.path.join(root, "me"))
    os.mkdir(os.path.join(root, "1234567"))

    assert paths.workouts_dir(None) == os.path.realpath(
        os.path.join(root, "1234567")
    )


def test_workouts_dir_refuses_to_guess_between_player_folders():
    """Two riders on one machine: guessing exports into the wrong folder."""
    root = _zwift_root()
    os.mkdir(os.path.join(root, "111"))
    os.mkdir(os.path.join(root, "222"))

    with pytest.raises(paths.ExportTargetUnavailable) as excinfo:
        paths.workouts_dir(None)
    assert excinfo.value.reason == "choose"
    assert not excinfo.value.refused
    assert paths.resolve_export_dir(None, None) == (None, "choose")


def test_unusable_zwift_id_is_never_joined_onto_a_root():
    """A poisoned id degrades to 'no id at all' - it cannot produce a path.

    Detection then supplies the single real player folder, which is what an
    unset id would have used too. The old code returned <root>/me here.
    """
    root = _zwift_root()
    os.mkdir(os.path.join(root, "1234567"))

    resolved = paths.workouts_dir("../../../../tmp/pwned")
    assert resolved == os.path.realpath(os.path.join(root, "1234567"))


def test_unusable_zwift_id_with_nothing_to_detect_is_refused():
    with pytest.raises(paths.ExportTargetUnavailable) as excinfo:
        paths.workouts_dir("../../../../tmp/pwned")
    assert excinfo.value.reason == "missing"
    assert os.listdir(_zwift_root()) == []


@pytest.mark.parametrize("zwift_id, override", [
    ("../../pwned", None),
    ("..", None),
    ("/etc", None),
    ("a/b", None),
    (r"C:\Windows\Temp", None),
    ("with:colon", None),
    ("123", "/etc"),
    ("123", "/private/tmp/wt_unit_escape/deep"),
    ("123", "../../../../private/tmp/wt_unit_escape"),
    ("123", r"\\attacker-nas\share\Workouts"),
    ("123", "~root/Workouts"),
    ("../../pwned", "/etc/pwned"),
])
def test_no_input_produces_a_path_outside_the_trusted_roots(zwift_id, override):
    """The property the removed fallback used to carry, asserted directly.

    Whatever the id/override, workouts_dir() either refuses or returns a
    directory inside the trusted roots - it never returns an unchecked path,
    and it creates nothing on the way. With an empty Workouts root (conftest)
    there is nothing legitimate to detect, so every one of these must refuse.
    """
    root = os.path.realpath(_zwift_root())
    with pytest.raises(paths.ExportTargetUnavailable):
        paths.workouts_dir(zwift_id, override=override)
    assert os.listdir(root) == []
    assert not os.path.exists("/private/tmp/wt_unit_escape")
    assert not os.path.exists("/etc/pwned")


def test_trusted_roots_include_redirects_and_process_overrides(monkeypatch):
    _windows(monkeypatch)
    monkeypatch.setattr(
        paths,
        "_windows_documents_known_folder",
        lambda: r"D:\Redirected Documents",
    )
    monkeypatch.setenv("USERPROFILE", r"C:\Users\Rider")
    monkeypatch.setenv("WATTRACKER_ACTIVITIES_DIR", r"E:\Cycling\Activities")
    monkeypatch.setenv("WATTRACKER_WORKOUTS_DIR", r"\\nas\rider\Workouts\123")
    roots = paths.trusted_storage_roots()
    assert os.path.normpath(r"D:\Redirected Documents") in roots
    assert os.path.normpath(r"E:\Cycling\Activities") in roots
    assert os.path.normpath(r"\\nas\rider\Workouts\123") in roots


def test_real_windows_known_folder_smoke():
    if not paths.sys.platform.startswith("win"):
        return
    candidates = paths.candidate_documents_dirs()
    assert candidates
    assert all(isinstance(path, str) and path for path in candidates)


# ------------------------------------ one path decision, not two (issue #44)
#
# resolve_export_dir() picks the target and workouts_dir() re-checks it. If the
# two apply DIFFERENT rules, the resolver hands callers a directory the writer
# then refuses - and since ExportTargetUnavailable is deliberately not an
# OSError, every caller that pre-resolved a target and passed it as
# workouts_override gets an unhandled exception instead of an export. The
# relocated-player-folder setup below is the case where they disagreed.

posix_only = pytest.mark.skipif(
    os.name != "posix", reason="symlink/junction semantics exercised on POSIX"
)


def _assert_resolver_and_writer_agree(zwift_id=None, override=None):
    """resolve_export_dir() is truthy IFF workouts_dir() returns, same value.

    Returns the agreed directory, or None when both refused. This is the
    invariant the regression broke: anything that reads (directory, reason)
    from the resolver and then hands ``directory`` to a writer is only correct
    while this holds.
    """
    directory, reason = paths.resolve_export_dir(zwift_id, override)
    try:
        written = paths.workouts_dir(zwift_id, override)
    except paths.ExportTargetUnavailable as exc:
        assert directory is None, (
            f"resolver returned {directory!r} ({reason}) but the writer "
            f"refused it as {exc.reason!r}"
        )
        assert exc.reason == reason
        return None
    assert directory == written, (
        f"resolver said {directory!r} ({reason}), writer said {written!r}"
    )
    return written


@posix_only
def test_relocated_player_folder_is_exported_to_not_blocked(tmp_path):
    """The rider who moved their Zwift Workouts folder to another drive.

    ``mklink /J`` on Windows or a plain symlink on macOS/Linux: the player
    folder ENTRY still lives in the trusted Workouts root, its contents live
    on another volume. That is a supported, common Zwift setup, so the export
    has to follow the link - being told the folder you configured through the
    OS is "blocked" is not an acceptable answer.

    It is also the case where the resolver and the writer disagreed: detection
    returned the link path, and the writer's realpath containment check saw the
    other drive and refused it.
    """
    root = _zwift_root()
    other_drive = tmp_path / "D_drive" / "ZwiftWorkouts"  # outside every root
    other_drive.mkdir(parents=True)
    os.symlink(other_drive, os.path.join(root, "1234567"), target_is_directory=True)

    expected = os.path.join(os.path.realpath(root), "1234567")
    assert paths.resolve_export_dir(None, None) == (expected, "detected")
    assert _assert_resolver_and_writer_agree() == expected
    assert paths.ensure_workouts_dir(None) == expected

    # And it is a real export: the file lands on the other drive.
    with open(os.path.join(expected, "probe.zwo"), "w") as fh:
        fh.write("<workout_file/>")
    assert os.listdir(other_drive) == ["probe.zwo"]


@posix_only
def test_relocated_player_folder_picked_by_saved_zwift_id_also_works(tmp_path):
    """Same link, reached through the saved zwift_id instead of detection."""
    root = _zwift_root()
    other_drive = tmp_path / "D_drive" / "ZwiftWorkouts"
    other_drive.mkdir(parents=True)
    os.symlink(other_drive, os.path.join(root, "1234567"), target_is_directory=True)
    os.mkdir(os.path.join(root, "7654321"))  # a second folder: detection abstains

    expected = os.path.join(os.path.realpath(root), "1234567")
    assert paths.resolve_export_dir("1234567", None) == (expected, "zwift_id")
    assert _assert_resolver_and_writer_agree("1234567") == expected


@posix_only
def test_the_same_link_is_still_refused_when_it_is_TYPED_as_a_folder(tmp_path):
    """The trust boundary, stated as a test.

    A link DISCOVERED inside a trusted Workouts root is the user's own
    filesystem layout - whoever created it already had write access to that
    folder. The identical path SUBMITTED as a workouts_dir (Settings form,
    stored row, restored backup) is untrusted input, and there the target is
    resolved and refused. Detection getting more lenient must not make the
    override field more lenient.
    """
    root = _zwift_root()
    other_drive = tmp_path / "D_drive" / "ZwiftWorkouts"
    other_drive.mkdir(parents=True)
    link = os.path.join(root, "1234567")
    os.symlink(other_drive, link, target_is_directory=True)

    assert paths.resolve_export_dir(None, override=link) == (None, "blocked")
    assert _assert_resolver_and_writer_agree(override=link) is None
    assert os.listdir(other_drive) == []


@posix_only
def test_a_link_out_of_the_workouts_root_is_refused_however_it_is_reached(tmp_path):
    """Leniency is for the leaf ENTRY, not for the root it is enumerated from.

    Here the trusted-looking name is a link that leaves the Workouts root, and
    the folder it points at is not a player folder discovered under a trusted
    root at all - it is reached by a path that escapes. Both the resolver and
    the writer must refuse the escaping value.
    """
    outside = tmp_path / "outside"
    (outside / "1234567").mkdir(parents=True)
    assert paths.resolve_export_dir("1234567", str(outside)) == (None, "blocked")
    assert _assert_resolver_and_writer_agree("1234567", str(outside)) is None
    assert os.listdir(outside / "1234567") == []


@posix_only
def test_leniency_applies_only_directly_under_a_workouts_root(tmp_path, home_dir):
    """The junction exception covers ONE shape and does not spread.

    A link that is not a player folder sitting directly in a Zwift Workouts
    root gets the strict rule, so the exception cannot be inherited by some
    later caller that hands confine_detected_dir() an arbitrary path under
    $HOME. Both links here point at the same folder outside the roots; only
    the one in the Workouts root is followed.
    """
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    in_root = os.path.join(_zwift_root(), "1234567")
    os.symlink(outside, in_root, target_is_directory=True)
    in_home = str(home_dir / "1234567")
    os.symlink(outside, in_home, target_is_directory=True)

    assert paths.confine_detected_dir(in_root)[0] == os.path.join(
        os.path.realpath(_zwift_root()), "1234567"
    )
    clean, err = paths.confine_detected_dir(in_home)
    assert clean is None and "must be inside" in err

    # One level deeper inside the Workouts root is not a player folder either.
    nested = os.path.join(_zwift_root(), "sub")
    os.mkdir(nested)
    os.symlink(outside, os.path.join(nested, "1234567"), target_is_directory=True)
    assert paths.confine_detected_dir(os.path.join(nested, "1234567"))[0] is None
    assert os.listdir(outside) == []


def test_resolver_and_writer_agree_across_every_reason(tmp_path, home_dir):
    """The biconditional, over the whole reason vocabulary."""
    root = _zwift_root()
    # missing
    assert _assert_resolver_and_writer_agree() is None
    # blocked (escaping override)
    assert _assert_resolver_and_writer_agree(override=str(tmp_path / "nope")) is None
    # override (confined)
    good = home_dir / "my-workouts"
    good.mkdir()
    assert _assert_resolver_and_writer_agree(override=str(good)) == str(good)
    # detected
    os.mkdir(os.path.join(root, "1234567"))
    assert _assert_resolver_and_writer_agree() == os.path.join(
        os.path.realpath(root), "1234567"
    )
    # zwift_id + choose
    os.mkdir(os.path.join(root, "7654321"))
    assert _assert_resolver_and_writer_agree("7654321") == os.path.join(
        os.path.realpath(root), "7654321"
    )
    assert _assert_resolver_and_writer_agree() is None  # choose
    # unusable id degrades to detection, which now has two candidates
    assert _assert_resolver_and_writer_agree("../../pwned") is None
