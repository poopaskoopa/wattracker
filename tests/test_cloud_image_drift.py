import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[1]
SPEC = importlib.util.spec_from_file_location(
    "check_cloud_image_drift", ROOT / "scripts" / "check_cloud_image_drift.py"
)
drift = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(drift)

DIGEST = "a" * 64
PUBLISHED = "1" * 40
MAIN = "2" * 40


def params(tmp_path, read, sync=None):
    path = tmp_path / "main.local.bicepparam"
    path.write_text(f"param readImage = '{read}'\nparam syncImage = '{sync or read}'\n")
    return path


def fake_gh(monkeypatch, main_sha=MAIN, ahead=3, behind=0):
    calls = []

    def run(args):
        calls.append(args)
        if args[:3] == ["run", "list", "--workflow"]:
            return json.dumps([{"databaseId": 42, "headSha": PUBLISHED}])
        if args[:3] == ["run", "view", "42"]:
            return f"Published ghcr.io/poopaskoopa/wattracker-cloud@sha256:{DIGEST}\n"
        if args[:2] == ["api", "repos/poopaskoopa/wattracker/commits/main"]:
            return json.dumps({"sha": main_sha})
        if args[:2] == ["api", f"repos/poopaskoopa/wattracker/compare/{PUBLISHED}...main"]:
            return json.dumps({"ahead_by": ahead, "behind_by": behind})
        raise AssertionError(args)

    monkeypatch.setattr(drift, "run_gh", run)
    return calls


def test_rejects_malformed_and_mismatched_parameters(tmp_path):
    with pytest.raises(drift.DriftError, match="same image"):
        drift.read_image_reference(params(tmp_path, "not-an-image", "other"))
    with pytest.raises(drift.DriftError, match="full sha256"):
        drift.read_image_reference(params(tmp_path, f"{drift.IMAGE}:latest"))


def test_fails_closed_for_unknown_digest_or_failing_gh(tmp_path, monkeypatch):
    path = params(tmp_path, f"{drift.IMAGE}@sha256:{DIGEST}")
    monkeypatch.setattr(drift, "run_gh", lambda args: "[]" if args[0:2] == ["run", "list"] else "")
    with pytest.raises(drift.DriftError, match="not found"):
        drift.check(path)

    def fail(_args):
        raise drift.DriftError("gh failed")

    monkeypatch.setattr(drift, "run_gh", fail)
    with pytest.raises(drift.DriftError, match="gh failed"):
        drift.check(path)


def test_maps_digest_and_reports_ahead_behind(tmp_path, monkeypatch, capsys):
    calls = fake_gh(monkeypatch, ahead=6, behind=1)
    path = params(tmp_path, f"{drift.IMAGE}@sha256:{DIGEST}")
    assert drift.main([str(path), "--deployment-commit", PUBLISHED]) == 0
    output = capsys.readouterr().out
    assert PUBLISHED in output
    assert MAIN in output
    assert "publishing run: 42" in output
    assert "main ahead: 6; main behind: 1" in output
    assert any("--workflow" in call for call in calls)


def test_rejects_deployment_commit_mismatch(tmp_path, monkeypatch):
    fake_gh(monkeypatch)
    path = params(tmp_path, f"{drift.IMAGE}@sha256:{DIGEST}")
    with pytest.raises(drift.DriftError, match="different commit"):
        drift.check(path, "f" * 40)


def test_missing_gh_is_a_user_facing_error(monkeypatch):
    def missing(*_args, **_kwargs):
        raise FileNotFoundError

    monkeypatch.setattr(drift.subprocess, "run", missing)
    with pytest.raises(drift.DriftError, match="install and authenticate"):
        drift.run_gh(["run", "list"])


def test_digest_match_cannot_have_extra_hex_suffix(tmp_path, monkeypatch):
    path = params(tmp_path, f"{drift.IMAGE}@sha256:{DIGEST}")

    def run(args):
        if args[:3] == ["run", "list", "--workflow"]:
            return json.dumps([{"databaseId": 42, "headSha": PUBLISHED}])
        if args[:3] == ["run", "view", "42"]:
            return f"Published {drift.IMAGE}@sha256:{DIGEST}deadbeef\n"
        raise AssertionError(args)

    monkeypatch.setattr(drift, "run_gh", run)
    with pytest.raises(drift.DriftError, match="not found"):
        drift.resolve_published_run(DIGEST)
