"""Guards on the macOS packaging pipeline.

These mirror the Windows packaging guards in test_windows_secrets.py: none of
this can be exercised by CI here, so the invariants that would otherwise be
noticed only by shipping a broken artifact are asserted from the checkout.
"""
from pathlib import Path
import re

SPEC = Path("packaging/wattracker.spec")
WORKFLOW = Path(".github/workflows/macos-release.yml")


def test_pyinstaller_is_pinned_exactly_in_one_place():
    pyproject = Path("pyproject.toml").read_text()
    pins = re.findall(r'"pyinstaller==([^"]+)"', pyproject)
    assert pins == ["6.16.0"]


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
    # An agent app: no Cocoa event loop, so a Dock icon would only ever be
    # reported as not responding.
    assert '"LSUIElement": True' in bundle
    # CoreBluetooth terminates a process that touches it without a purpose
    # string, which would kill the app from the ride page.
    assert '"NSBluetoothAlwaysUsageDescription"' in bundle
    # The bundle version is read from pyproject, never hard-coded.
    assert '"CFBundleShortVersionString": _project_version()' in bundle
    assert "0.1.0" not in bundle


def test_release_workflow_is_tag_only_and_hard_disabled():
    workflow = WORKFLOW.read_text()
    assert 'tags:\n      - "v*"' in workflow
    assert "workflow_dispatch" not in workflow
    assert "inputs.ref" not in workflow
    job = workflow.split("  build-test-sign:\n", 1)[1]
    # The disabling `if` must be part of the job header, before any step can run.
    assert "    if: ${{ false }}\n" in job.split("\n    steps:", 1)[0]


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
