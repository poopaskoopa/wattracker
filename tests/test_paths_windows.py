import ntpath
import os

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
    assert paths.workouts_dir("123") == os.path.join(roots[1], "123")


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


def test_function_workouts_override_wins_over_environment(monkeypatch):
    monkeypatch.setenv("WATTRACKER_WORKOUTS_DIR", "/environment")
    assert paths.workouts_dir("123", "/per-user") == "/per-user"
    assert paths.resolve_export_dir("123", "/per-user") == ("/per-user", "override")


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
