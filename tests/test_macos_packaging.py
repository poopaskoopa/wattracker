"""Guards on the macOS packaging pipeline.

These mirror the Windows packaging guards in test_windows_secrets.py: none of
this can be exercised by CI here, so the invariants that would otherwise be
noticed only by shipping a broken artifact are asserted from the checkout.
"""
from pathlib import Path
import re
import shutil
import subprocess
import sys
import textwrap

import pytest

SPEC = Path("packaging/wattracker.spec")
WORKFLOW = Path(".github/workflows/macos-release.yml")
MACOS_BUILD = Path("packaging/build-macos.sh")


def test_pyinstaller_is_pinned_exactly_in_one_place():
    pyproject = Path("pyproject.toml").read_text()
    pins = re.findall(r'"pyinstaller==([^"]+)"', pyproject)
    assert pins == ["6.16.0"]


def test_macos_artifact_architecture_comes_from_build_interpreter():
    script = MACOS_BUILD.read_text()
    validation = script.index("sys.version_info >= (3, 12)")
    architecture = script.index('arch="$("$python_bin" -c')
    assert architecture > validation
    assert "import platform; print(platform.machine())" in script
    assert 'arch="$(uname -m)"' not in script


def test_spec_shares_analysis_between_platforms():
    spec = SPEC.read_text()
    # One Analysis, one datas block: the templates/static trees and the uvicorn
    # and keyring hidden imports must not be duplicated per platform, because
    # duplicates drift and a missing data tree is an invisible packaging bug.
    assert spec.count("a = Analysis(") == 1
    assert spec.count('collect_submodules("uvicorn")') == 1
    assert spec.count('collect_submodules("keyring.backends")') == 1
    assert spec.count('"wattracker" / "web" / "templates"') == 1
    assert spec.count('"wattracker" / "web" / "static"') == 1


def test_bleak_backends_are_collected_per_platform():
    spec = SPEC.read_text()
    # WinRT exists only on Windows; collecting it elsewhere pulls nothing and
    # masks the fact that macOS needs CoreBluetooth instead.
    assert '"bleak.backends.winrt" if IS_WINDOWS else "bleak.backends.corebluetooth"' in spec


def test_bundle_is_macos_only_and_declares_its_plist():
    spec = SPEC.read_text()
    bundle = spec.split("if IS_MACOS:", 1)[1]
    assert "BUNDLE(" in bundle
    assert 'bundle_identifier="com.wattracker.wattracker"' in bundle
    plist_builder = spec.split("def _macos_info_plist", 1)[1].split("hidden =", 1)[0]
    # An agent app: no Cocoa event loop, so a Dock icon would only ever be
    # reported as not responding.
    assert '"LSUIElement": True' in plist_builder
    # CoreBluetooth terminates a process that touches it without a purpose
    # string, which would kill the app from the ride page.
    assert '"NSBluetoothAlwaysUsageDescription"' in plist_builder
    # Apple's plist versions stay numeric while sharing the Git-derived source.
    assert '"CFBundleShortVersionString": version_helper.macos_short_version(project_version)' in plist_builder
    assert '"CFBundleVersion": version_helper.macos_build_version(project_version, root=root)' in plist_builder
    assert "info_plist=_macos_info_plist(project_version, version_helper)" in bundle
    assert 'version=version_helper.macos_short_version(project_version)' in bundle
    assert "0.1.0" not in bundle


def test_release_workflow_is_tag_only_and_environment_gated():
    workflow = WORKFLOW.read_text()
    # A signed build may only ever be produced from a version tag. No manual
    # dispatch and no caller-supplied ref, because either would let an
    # arbitrary commit be signed with the Developer ID and notarised.
    assert 'tags:\n      - "v*"' in workflow
    assert "workflow_dispatch" not in workflow
    assert "inputs.ref" not in workflow
    header = workflow.split("  build-test-sign:\n", 1)[1].split("\n    steps:", 1)[0]
    # This job used to be hard-disabled with `if: ${{ false }}` because Actions
    # was blocked at the account level and macOS minutes billed at 10x on a
    # private repo. The repository is public and neither holds, so the job runs
    # (see the comment in the workflow). What still has to hold is that the
    # signing secrets stay behind an environment: `macos-code-signing` is what
    # applies GitHub's protection rules to them, and it is the only remaining
    # gate now that the `if` is gone.
    assert "    environment: macos-code-signing\n" in header
    assert "    runs-on: macos-latest\n" in header


def _release_version_check(workflow: str) -> str:
    step = workflow.split("      - name: Verify release tag matches desktop version\n", 1)[1]
    step = step.split("\n      - ", 1)[0]
    return textwrap.dedent(step.split("        run: |\n", 1)[1])


def test_signed_release_tag_check_precedes_signing_on_both_platforms():
    macos = WORKFLOW.read_text()
    windows = Path(".github/workflows/windows-release.yml").read_text()
    for workflow, signing_step in (
        (macos, "Import Developer ID certificate into a temporary keychain"),
        (windows, "Sign and verify every frozen binary"),
    ):
        check = workflow.index("- name: Verify release tag matches desktop version")
        assert workflow.index("- name: Install pinned build tools and project") < check
        assert check < workflow.index(f"- name: {signing_step}")
        check_step = workflow.split(
            "      - name: Verify release tag matches desktop version\n", 1
        )[1].split("\n      - ", 1)[0]
        assert "shell: bash" in check_step
        assert '[[ ! "$GITHUB_REF_NAME" =~ ^v[0-9]+\\.[0-9]+\\.[0-9]+$ ]]' in check_step
        assert 'git cat-file -t "refs/tags/$GITHUB_REF_NAME"' in check_step
        assert "Signed stable tags must be annotated" in check_step
        assert 'actual="$(python packaging/_version.py)"' in check_step
        assert '"$actual" != "$expected"' in check_step


def test_signed_release_tag_check_accepts_only_matching_plain_release_tags(tmp_path):
    pytest.importorskip("setuptools_scm")
    root = tmp_path / "repo"
    (root / "packaging").mkdir(parents=True)
    shutil.copy2("pyproject.toml", root / "pyproject.toml")
    shutil.copy2("packaging/_version.py", root / "packaging/_version.py")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=root, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.invalid"], cwd=root, check=True
    )
    (root / "README.md").write_text("fixture\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=root, check=True)
    subprocess.run(
        ["git", "tag", "-a", "v0.4.0", "-m", "stable release"], cwd=root, check=True
    )
    subprocess.run(
        ["git", "commit", "--allow-empty", "-qm", "next release"], cwd=root, check=True
    )
    subprocess.run(
        ["git", "tag", "-a", "v0.4.1", "-m", "stable release"], cwd=root, check=True
    )
    subprocess.run(["git", "checkout", "--quiet", "v0.4.0"], cwd=root, check=True)
    subprocess.run(["git", "tag", "v0.4.2"], cwd=root, check=True)

    minimal_bin = tmp_path / "bin"
    minimal_bin.mkdir()
    for executable in ("git",):
        source = shutil.which(executable)
        assert source is not None
        (minimal_bin / executable).symlink_to(source)
    python = minimal_bin / "python"
    python.write_text(
        f'#!/bin/sh\nexec "{sys.executable}" "$@"\n', encoding="utf-8"
    )
    python.chmod(0o755)
    def run(tag: str) -> subprocess.CompletedProcess[str]:
        env = {
            "PATH": str(minimal_bin),
            "HOME": str(tmp_path),
            "GITHUB_REF_NAME": tag,
        }
        return subprocess.run(
            ["/bin/bash", "-e", "-c", check],
            cwd=root,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    for path in (
        WORKFLOW,
        Path(".github/workflows/windows-release.yml"),
    ):
        check = _release_version_check(path.read_text())
        assert run("v0.4.0").returncode == 0
        mismatch = run("v0.4.1")
        assert mismatch.returncode != 0
        assert "resolves to 0.4.0, expected 0.4.1" in mismatch.stderr
        lightweight = run("v0.4.2")
        assert lightweight.returncode != 0
        assert "Signed stable tags must be annotated" in lightweight.stderr
        for invalid in ("v0.4.0-rc1", "v1.0"):
            result = run(invalid)
            assert result.returncode != 0
            assert "plain vX.Y.Z" in result.stderr


def test_release_workflow_scopes_signing_secrets_to_their_steps():
    workflow = WORKFLOW.read_text()
    job_env = workflow.split("    env:\n", 1)[1].split("\n\n    steps:", 1)[0]
    for secret in (
        "WATTRACKER_MACOS_SIGNING_P12_B64",
        "WATTRACKER_MACOS_SIGNING_P12_PASSWORD",
        "WATTRACKER_MACOS_NOTARY_PASSWORD",
    ):
        assert secret not in job_env
    import_step = workflow.split(
        "      - name: Import Developer ID certificate into a temporary keychain", 1
    )[1].split("\n      - name:", 1)[0]
    assert "secrets.WATTRACKER_MACOS_SIGNING_P12_B64" in import_step
    assert "secrets.WATTRACKER_MACOS_SIGNING_P12_PASSWORD" in import_step
    # The temporary keychain must be removed even when a later step fails.
    assert "security delete-keychain" in workflow
    assert "if: always()" in workflow


def test_signing_script_fails_closed_without_an_identity_env_var():
    script = Path("packaging/sign-macos.sh").read_text()
    assert "set -euo pipefail" in script
    # Ad-hoc must never be silently substituted for a requested Developer ID
    # signature, and the ad-hoc path must say out loud that it is not
    # distributable.
    assert 'if [ -n "$identity" ]; then' in script
    assert "do not pass Gatekeeper" in script
    assert "--options runtime --timestamp" in script


def test_developer_id_dmg_branch_signs_the_image_without_contents_traversal():
    script = Path("packaging/sign-macos.sh").read_text()
    dmg_branch = script.split('if [[ "$target" == *.dmg ]]; then', 1)[1].split(
        "        return\n", 1
    )[0]
    assert 'codesign --force --sign "$identity" --timestamp "$target"' in dmg_branch
    assert 'codesign --verify --verbose=2 "$target"' in dmg_branch
    assert "Contents" not in dmg_branch
    assert "find " not in dmg_branch


def test_no_notary_secret_is_ever_passed_on_argv():
    """argv is world-readable; secrets must reach notarytool another way.

    This is the same bar docs/windows-security.md holds sign-windows.ps1 to
    when it refuses to put the PFX password on a child process command line.
    """
    def code_only(text):
        # The comments explain *why* these forms are refused, so they name them.
        return "\n".join(
            line for line in text.splitlines() if not line.lstrip().startswith("#")
        )

    script = code_only(Path("packaging/sign-macos.sh").read_text())
    workflow = code_only(WORKFLOW.read_text())
    for forbidden in ("--password", "--apple-id", "NOTARY_PASSWORD"):
        assert forbidden not in script
        assert forbidden not in workflow
    # The two permitted forms both keep the secret out of argv: the keychain,
    # or a .p8 file whose path is not itself a secret.
    assert "--keychain-profile" in script
    assert "--key-id" in script


def test_frozen_smoke_test_cannot_touch_real_user_data():
    smoke = Path("packaging/smoke_frozen_macos.py").read_text()
    # HOME is the load-bearing override: config.app_data_dir() and paths.py both
    # fall back to expanduser("~").
    assert "HOME=str(home)" in smoke
    assert 'WATTRACKER_DATA_DIR=str(data)' in smoke
    assert 'WATTRACKER_DB=str(data / "wattracker.db")' in smoke
    # Inherited WATTRACKER_* variables are stripped, never merged.
    assert 'if not k.startswith("WATTRACKER_")' in smoke
    # A kernel-assigned port, so a wattracker already running on the default
    # port is never disturbed - WATTRACKER_PORT is never left to its default.
    assert "free_loopback_port()" in smoke
    assert "WATTRACKER_PORT=str(port)" in smoke
    assert "the smoke test modified the real wattracker database" in smoke
