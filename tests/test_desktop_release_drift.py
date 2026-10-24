from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "check_desktop_release_drift.py"
spec = importlib.util.spec_from_file_location("desktop_release_drift", SCRIPT)
drift = importlib.util.module_from_spec(spec)
spec.loader.exec_module(drift)


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def _commit(root: Path, message: str) -> str:
    _git(root, "add", ".")
    _git(root, "commit", "-m", message)
    return _git(root, "rev-parse", "HEAD")


def _repo(tmp_path: Path) -> tuple[Path, str, str]:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "test")
    _git(root, "config", "user.email", "test@example.invalid")
    (root / "wattracker").mkdir()
    (root / "wattracker" / "app.py").write_text("BASE = 1\n", encoding="utf-8")
    version_sha = _commit(root, "version")
    _git(root, "tag", "v0.1.0")
    (root / "README.md").write_text("docs\n", encoding="utf-8")
    _commit(root, "docs")
    _git(root, "tag", "preview-0.1.0")
    (root / "README.md").write_text("desktop preview\n", encoding="utf-8")
    _commit(root, "preview setup")
    preview_sha = _git(root, "rev-parse", "HEAD")
    _git(root, "tag", "preview-latest")
    return root, version_sha, preview_sha


def _releases(monkeypatch, records):
    monkeypatch.setattr(drift, "run_gh", lambda args: json.dumps(records))


def test_drift_fails_for_app_commits_after_the_nearest_published_release(tmp_path, monkeypatch):
    root, version_sha, preview_sha = _repo(tmp_path)
    (root / "wattracker" / "app.py").write_text("BASE = 2\n", encoding="utf-8")
    _commit(root, "desktop change")
    _releases(monkeypatch, [
        {"tagName": "v0.1.0", "publishedAt": "2026-01-01T00:00:00Z", "isDraft": False},
        {"tagName": "preview-0.1.0", "publishedAt": "2026-01-02T00:00:00Z", "isDraft": False},
        {"tagName": "preview-latest", "publishedAt": "2026-01-03T00:00:00Z", "isDraft": False},
        {"tagName": "ios-v0.1.5", "publishedAt": "2026-01-03T00:00:00Z", "isDraft": False},
    ])

    with pytest.raises(drift.DriftError, match=r"1 commit\(s\).*preview-latest"):
        drift.check(root)

    assert _git(root, "rev-parse", "refs/tags/v0.1.0^{commit}") == version_sha
    assert _git(root, "rev-parse", "refs/tags/preview-latest^{commit}") == preview_sha


def test_docs_only_commits_after_release_do_not_cause_drift(tmp_path, monkeypatch):
    root, _, _ = _repo(tmp_path)
    (root / "docs").mkdir()
    (root / "docs" / "desktop.md").write_text("docs only\n", encoding="utf-8")
    _commit(root, "docs only")
    _releases(monkeypatch, [
        {"tagName": "preview-latest", "publishedAt": "2026-01-02T00:00:00Z", "isDraft": False},
        {"tagName": "v0.1.0", "publishedAt": "2026-01-01T00:00:00Z", "isDraft": False},
    ])

    result = drift.check(root)

    assert result["release_tag"] == "preview-latest"
    assert result["desktop_commits"] == 0


def test_latest_published_release_wins_when_two_tags_point_at_same_commit(tmp_path, monkeypatch):
    root, _, _ = _repo(tmp_path)
    _releases(monkeypatch, [
        {"tagName": "v0.1.0", "publishedAt": "2026-01-01T00:00:00Z", "isDraft": False},
        {"tagName": "preview-latest", "publishedAt": "2026-01-02T00:00:00Z", "isDraft": False},
    ])

    assert drift.latest_published_release(root)[0] == "preview-latest"


def test_latest_published_release_uses_publication_time_not_commit_distance(tmp_path, monkeypatch):
    root, version_sha, _ = _repo(tmp_path)
    _git(root, "tag", "v0.2.0", version_sha)
    _releases(monkeypatch, [
        {"tagName": "preview-latest", "publishedAt": "2026-01-03T00:00:00Z", "isDraft": False},
        {"tagName": "v0.2.0", "publishedAt": "2026-01-04T00:00:00Z", "isDraft": False},
    ])

    assert drift.latest_published_release(root)[0] == "v0.2.0"


def test_release_with_tag_missing_from_local_checkout_is_skipped(tmp_path, monkeypatch):
    root, _, _ = _repo(tmp_path)
    _git(root, "tag", "-d", "preview-latest")
    _releases(monkeypatch, [
        {"tagName": "preview-latest", "publishedAt": "2026-01-03T00:00:00Z", "isDraft": False},
        {"tagName": "preview-0.1.0", "publishedAt": "2026-01-02T00:00:00Z", "isDraft": False},
    ])

    assert drift.latest_published_release(root) == (
        "preview-0.1.0",
        _git(root, "rev-parse", "refs/tags/preview-0.1.0^{commit}"),
    )


def test_numeric_preview_release_is_a_valid_pre_rolling_baseline(tmp_path, monkeypatch):
    root, _, _ = _repo(tmp_path)
    _releases(monkeypatch, [
        {"tagName": "preview-0.1.0", "publishedAt": "2026-01-02T00:00:00Z", "isDraft": False},
    ])

    assert drift.latest_published_release(root)[0] == "preview-0.1.0"


def test_no_published_desktop_release_is_a_clear_failure(tmp_path, monkeypatch):
    root, _, _ = _repo(tmp_path)
    _releases(monkeypatch, [
        {"tagName": "ios-v0.1.5", "publishedAt": "2026-01-03T00:00:00Z", "isDraft": False},
    ])

    with pytest.raises(drift.DriftError, match="no published desktop release"):
        drift.check(root)
