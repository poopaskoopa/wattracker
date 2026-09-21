import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "deploy_cloud.py"
SHA = "a" * 40
PUBLISHED_SHA = "b" * 40
OLD_REF = "ghcr.io/poopaskoopa/wattracker-cloud@sha256:" + "1" * 64
NEW_REF = "ghcr.io/poopaskoopa/wattracker-cloud@sha256:" + "2" * 64


def _load_script():
    spec = importlib.util.spec_from_file_location("deploy_cloud_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def deploy_cloud():
    return _load_script()


def _initial_drift():
    return {
        "image_ref": OLD_REF,
        "run_id": 10,
        "published_sha": PUBLISHED_SHA,
        "main_sha": SHA,
        "main_ahead": 1,
        "main_behind": 0,
    }


def _param_file(tmp_path):
    path = tmp_path / "main.local.bicepparam"
    path.write_bytes(
        b"using './main.bicep'\n"
        b"param location = 'secret-location'\n"
        b"param readImage = '" + OLD_REF.encode() + b"'\n"
        b"param syncImage = '" + OLD_REF.encode() + b"'\n"
        b"param operatorToken = readEnvironmentVariable('WATTRACKER_OPERATOR_TOKEN')\n"
    )
    return path


def _patch_image_deploy(monkeypatch, module):
    monkeypatch.setattr(module, "_require_clean_main", lambda *_: SHA)
    monkeypatch.setattr(module, "_changed_paths", lambda *_: ["wattracker/cloud/api.py"])
    monkeypatch.setattr(module, "_resolve_published_image", lambda *_: (NEW_REF, 42))
    monkeypatch.setattr(module, "_running_commit", lambda: SHA)
    monkeypatch.setattr(module, "_check_drift", lambda parameter, deployment_commit=None: (
        {**_initial_drift(), "main_ahead": 0, "main_behind": 0}
        if deployment_commit
        else _initial_drift()
    ))


def test_current_image_exits_without_deploy(monkeypatch, capsys, deploy_cloud, tmp_path):
    module = deploy_cloud
    monkeypatch.setattr(module, "_require_clean_main", lambda *_: SHA)
    monkeypatch.setattr(module, "_check_drift", lambda *_: {
        "main_sha": SHA,
        "main_ahead": 0,
        "main_behind": 0,
    })
    monkeypatch.setattr(module, "_run_azure_deployment", lambda *_: pytest.fail("deployed"))

    assert module.deploy(tmp_path / "params", "rg", "deployment") == 0
    assert "no deployment needed" in capsys.readouterr().out


def test_docs_only_drift_exits_without_deploy(monkeypatch, capsys, deploy_cloud, tmp_path):
    module = deploy_cloud
    monkeypatch.setattr(module, "_require_clean_main", lambda *_: SHA)
    monkeypatch.setattr(module, "_check_drift", lambda *_: _initial_drift())
    monkeypatch.setattr(module, "_changed_paths", lambda *_: [
        "docs/cloud-sync.md", "tests/test_cloud_deployment.py", "AGENTS.md",
    ])
    monkeypatch.setattr(module, "_run_azure_deployment", lambda *_: pytest.fail("deployed"))

    assert module.deploy(tmp_path / "params", "rg", "deployment") == 0
    output = capsys.readouterr().out
    assert "no image changes" in output
    assert "no deployment" in output
    assert "image changes: no" in output
    assert "docs/cloud-sync.md" in output


def test_image_drift_updates_both_pins_and_deploys(monkeypatch, deploy_cloud, tmp_path):
    module = deploy_cloud
    path = _param_file(tmp_path)
    _patch_image_deploy(monkeypatch, module)
    deployments = []
    monkeypatch.setattr(module, "_run_azure_deployment", lambda *args: deployments.append(args))

    assert module.deploy(path, "rg", "deployment") == 0
    updated = path.read_text()
    assert updated.count(NEW_REF) == 2
    assert "param location = 'secret-location'" in updated
    assert "operatorToken = readEnvironmentVariable('WATTRACKER_OPERATOR_TOKEN')" in updated
    assert deployments == [(path, "rg", "deployment")]


def test_no_successful_publish_run_stops_before_parameter_write(
    monkeypatch, deploy_cloud, tmp_path
):
    module = deploy_cloud
    path = _param_file(tmp_path)
    original = path.read_bytes()
    monkeypatch.setattr(module, "_require_clean_main", lambda *_: SHA)
    monkeypatch.setattr(module, "_check_drift", lambda *_: _initial_drift())
    monkeypatch.setattr(module, "_changed_paths", lambda *_: ["Dockerfile.cloud"])
    monkeypatch.setattr(module.drift_checker, "_run_records", lambda: [])

    with pytest.raises(module.DeployError, match="no successful cloud publishing run"):
        module.deploy(path, "rg", "deployment")
    assert path.read_bytes() == original


def test_resolves_digest_from_successful_run_for_current_main(monkeypatch, deploy_cloud):
    module = deploy_cloud
    monkeypatch.setattr(module.drift_checker, "_run_records", lambda: [{
        "databaseId": 42,
        "headSha": SHA,
    }])
    monkeypatch.setattr(
        module,
        "_checker_run_gh",
        lambda args: "Published " + NEW_REF if args[:3] == ["run", "view", "42"] else "",
    )

    assert module._resolve_published_image(SHA) == (NEW_REF, 42)


def test_validate_failure_restores_parameter_file_and_stops_before_create(
    monkeypatch, deploy_cloud, tmp_path
):
    module = deploy_cloud
    path = _param_file(tmp_path)
    original = path.read_bytes()
    _patch_image_deploy(monkeypatch, module)
    calls = []

    def fail_validate(command, *, label, cwd=module.REPOSITORY_ROOT):
        calls.append((command, label))
        raise module.DeployError("Azure deployment validation failed; inspect its diagnostics and retry")

    monkeypatch.setattr(module, "_run_process", fail_validate)
    with pytest.raises(module.DeployError, match="validation failed"):
        module.deploy(path, "rg", "deployment")
    assert path.read_bytes() == original
    assert len(calls) == 1
    assert calls[0][1] == "Azure deployment validation"


def test_atomic_restore_after_admin_version_failure(monkeypatch, deploy_cloud, tmp_path):
    module = deploy_cloud
    path = _param_file(tmp_path)
    original = path.read_bytes()
    _patch_image_deploy(monkeypatch, module)
    monkeypatch.setattr(module, "_run_azure_deployment", lambda *_: None)
    monkeypatch.setattr(
        module,
        "_running_commit",
        lambda: (_ for _ in ()).throw(module.DeployError("cloud admin version command failed")),
    )

    with pytest.raises(module.DeployError, match="admin version"):
        module.deploy(path, "rg", "deployment")
    assert path.read_bytes() == original


def test_non_main_checkout_is_refused(monkeypatch, deploy_cloud):
    module = deploy_cloud
    monkeypatch.setattr(module, "_git_output", lambda args: "agent2/347-cloud-deploy\n")
    with pytest.raises(module.DeployError, match="not main"):
        module._require_clean_main(module.REPOSITORY_ROOT / "infra/azure/main.local.bicepparam")


def test_explicit_untracked_parameter_file_is_the_only_dirty_entry_allowed(
    monkeypatch, deploy_cloud
):
    module = deploy_cloud
    parameter_file = module.REPOSITORY_ROOT / "infra/azure/main.local.bicepparam"

    def fake_git(args):
        if args == ["branch", "--show-current"]:
            return "main\n"
        if args == ["status", "--porcelain=v1", "-z", "--untracked-files=all"]:
            return "?? infra/azure/main.local.bicepparam\0"
        if args == ["rev-parse", "HEAD"]:
            return SHA + "\n"
        raise AssertionError(args)

    monkeypatch.setattr(module, "_git_output", fake_git)
    assert module._require_clean_main(parameter_file) == SHA


def test_other_untracked_entry_still_blocks_deploy(monkeypatch, deploy_cloud):
    module = deploy_cloud
    parameter_file = module.REPOSITORY_ROOT / "infra/azure/main.local.bicepparam"
    monkeypatch.setattr(module, "_git_output", lambda args: {
        ("branch", "--show-current"): "main\n",
        ("status", "--porcelain=v1", "-z", "--untracked-files=all"): "?? notes.txt\0",
    }[tuple(args)])
    with pytest.raises(module.DeployError, match="working tree is dirty"):
        module._require_clean_main(parameter_file)


def test_alternate_untracked_parameter_path_is_rejected(monkeypatch, deploy_cloud):
    module = deploy_cloud
    alternate = module.REPOSITORY_ROOT / "infra/azure/other.bicepparam"
    monkeypatch.setattr(module, "_git_output", lambda args: {
        ("branch", "--show-current"): "main\n",
    }[tuple(args)])
    with pytest.raises(module.DeployError, match="main.local.bicepparam"):
        module._require_clean_main(alternate)


def test_credentials_parameter_contents_and_response_bodies_are_not_leaked(
    monkeypatch, capsys, deploy_cloud
):
    module = deploy_cloud
    secret = "operator-token-that-must-not-appear"
    parameter_contents = "param cloudServerSecret = 'parameter-value-that-must-not-appear'"

    class Result:
        returncode = 0
        stdout = json.dumps({"commit": secret})
        stderr = parameter_contents

    monkeypatch.setattr(module.subprocess, "run", lambda *args, **kwargs: Result())
    with pytest.raises(module.DeployError):
        module._running_commit()
    captured = capsys.readouterr()
    assert secret not in captured.out + captured.err
    assert parameter_contents not in captured.out + captured.err


def test_gh_resolution_uses_arm64_homebrew_path(monkeypatch, deploy_cloud):
    module = deploy_cloud
    calls = []

    class Result:
        returncode = 0
        stdout = "Mach-O 64-bit executable arm64"

    def fake_run(command, **kwargs):
        calls.append(command)
        if command[0] == "file":
            return Result()
        result = Result()
        result.stdout = "{}"
        return result

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    monkeypatch.setattr(module, "_run_process", lambda command, **kwargs: calls.append(command) or "{}")
    monkeypatch.setattr(module, "GH_PATH", Path("/opt/homebrew/bin/gh"))
    module._run_gh(["run", "list"])
    assert calls[-1][0] == "/opt/homebrew/bin/gh"
    assert "/usr/local/bin/gh" not in calls[-1]


def test_dry_run_preserves_parameter_file_and_never_runs_azure(
    monkeypatch, capsys, deploy_cloud, tmp_path
):
    module = deploy_cloud
    checkout = tmp_path / "checkout"
    parameter_file = checkout / "infra/azure/main.local.bicepparam"
    parameter_file.parent.mkdir(parents=True)
    parameter_file.write_bytes(b"readImage=old\nsyncImage=old\n")
    before = parameter_file.read_bytes()
    before_mtime = parameter_file.stat().st_mtime_ns
    monkeypatch.setattr(module, "REPOSITORY_ROOT", checkout)
    monkeypatch.setattr(module, "_require_clean_main", lambda *_args, **_kwargs: "c" * 40)
    monkeypatch.setattr(module, "_check_drift", lambda *_args, **_kwargs: _initial_drift())
    monkeypatch.setattr(module, "_changed_paths", lambda *_args: ["Dockerfile.cloud"])
    monkeypatch.setattr(module, "_resolve_published_image", lambda *_args: (NEW_REF, 42))
    commands = []
    monkeypatch.setattr(
        module,
        "_run_process",
        lambda command, **_kwargs: commands.append(list(command)) or "",
    )

    assert module.deploy(parameter_file, "resource-group", "deployment", dry_run=True) == 0

    assert parameter_file.read_bytes() == before
    assert parameter_file.stat().st_mtime_ns == before_mtime
    assert not any(command and command[0] == "az" for command in commands)
    output = capsys.readouterr().out
    assert "dry-run finding: local checkout commit differs" in output
    assert NEW_REF in output
    assert "real run would execute after pinning both image parameters" in output
    assert "scripts/deploy_cloud.py without --dry-run" in output
    validate_command, create_command = module._format_azure_commands(
        parameter_file, "resource-group", "deployment"
    )
    assert validate_command in output
    assert create_command in output


def test_dry_run_allows_dirty_topic_checkout_and_parameter_copy(
    monkeypatch, deploy_cloud, tmp_path
):
    module = deploy_cloud
    checkout = tmp_path / "checkout"
    parameter_file = checkout / "infra/azure/main.dry-run.bicepparam"
    parameter_file.parent.mkdir(parents=True)
    parameter_file.write_bytes(b"copy\n")
    monkeypatch.setattr(module, "REPOSITORY_ROOT", checkout)

    def fake_git(args):
        assert args == ["rev-parse", "HEAD"]
        return SHA + "\n"

    monkeypatch.setattr(module, "_git_output", fake_git)
    monkeypatch.setattr(module, "_check_drift", lambda *_args, **_kwargs: {
        **_initial_drift(),
        "main_ahead": 0,
        "main_behind": 0,
    })
    monkeypatch.setattr(module, "_run_azure_deployment", lambda *_args: pytest.fail("deployed"))

    assert module.deploy(parameter_file, "resource-group", "deployment", dry_run=True) == 0


def test_dry_run_still_rejects_parameter_outside_checkout(monkeypatch, deploy_cloud, tmp_path):
    module = deploy_cloud
    checkout = tmp_path / "checkout"
    monkeypatch.setattr(module, "REPOSITORY_ROOT", checkout)
    outside = tmp_path / "outside.bicepparam"
    with pytest.raises(module.DeployError, match="alongside the template"):
        module._require_clean_main(outside, dry_run=True)


def test_real_azure_path_still_validates_then_creates(monkeypatch, deploy_cloud, tmp_path):
    module = deploy_cloud
    commands = []
    monkeypatch.setattr(
        module,
        "_run_process",
        lambda command, **_kwargs: commands.append(list(command)) or "",
    )

    module._run_azure_deployment(tmp_path / "params.bicepparam", "resource-group", "deployment")

    assert [command[:4] for command in commands] == [
        ["az", "deployment", "group", "validate"],
        ["az", "deployment", "group", "create"],
    ]
