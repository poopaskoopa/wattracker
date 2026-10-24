"""Platform-neutral unit tests for the Windows DPAPI wrapper."""
import hashlib
import re
from pathlib import Path

import pytest

from wattracker import windows_secrets


class FakeDPAPI:
    """Authenticated stand-in whose output is bound to supplied entropy."""

    def protect(self, plaintext: bytes, entropy: bytes) -> bytes:
        return hashlib.sha256(entropy).digest() + plaintext[::-1]

    def unprotect(self, ciphertext: bytes, entropy: bytes) -> bytes:
        expected = hashlib.sha256(entropy).digest()
        if not ciphertext.startswith(expected):
            raise windows_secrets.DPAPIError("wrong entropy")
        return ciphertext[len(expected):][::-1]


def test_module_imports_without_loading_dpapi_on_non_windows():
    assert windows_secrets.entropy_for("svc", 7) == b"svc\x00user:7"


def test_protect_unprotect_roundtrip_with_injected_backend():
    api = FakeDPAPI()
    marker = windows_secrets.protect_password(
        "correct horse", "wattracker-Zwift", 42, backend=api
    )
    assert marker.startswith("dpapi1$")
    assert "correct horse" not in marker
    assert windows_secrets.unprotect_password(
        marker, "wattracker-Zwift", 42, backend=api
    ) == "correct horse"


def test_entropy_isolates_users_and_services():
    api = FakeDPAPI()
    marker = windows_secrets.protect_password("pw", "service-a", 1, backend=api)
    with pytest.raises(windows_secrets.DPAPIError):
        windows_secrets.unprotect_password(marker, "service-a", 2, backend=api)
    with pytest.raises(windows_secrets.DPAPIError):
        windows_secrets.unprotect_password(marker, "service-b", 1, backend=api)


@pytest.mark.parametrize("marker", ["", "enc1$abc", "dpapi1$", "dpapi1$%%%"])
def test_corrupt_marker_is_rejected(marker):
    with pytest.raises(windows_secrets.DPAPIError):
        windows_secrets.unprotect_password(marker, "svc", 1, backend=FakeDPAPI())


def test_backend_failures_are_wrapped_without_plaintext():
    class Broken:
        def protect(self, plaintext, entropy):
            raise OSError("native failure")

    with pytest.raises(windows_secrets.DPAPIError) as exc:
        windows_secrets.protect_password(
            "do-not-leak-this", "svc", 1, backend=Broken()
        )
    assert "do-not-leak-this" not in str(exc.value)


def test_unprotect_rejects_non_utf8_plaintext():
    class NonUtf8:
        def unprotect(self, ciphertext, entropy):
            return b"\xff\xfe"

    marker = "dpapi1$YQ=="
    with pytest.raises(windows_secrets.DPAPIError):
        windows_secrets.unprotect_password(
            marker, "svc", 1, backend=NonUtf8()
        )


def test_release_workflow_scopes_signing_secrets_to_sign_step():
    workflow = Path(".github/workflows/windows-release.yml").read_text()
    job_env = workflow.split("    env:\n", 1)[1].split("\n\n    steps:", 1)[0]
    assert "WATTRACKER_SIGNING_PFX_B64" not in job_env
    assert "WATTRACKER_SIGNING_PFX_PASSWORD" not in job_env

    sign_step = workflow.split(
        "      - name: Sign and verify every frozen binary", 1
    )[1].split("\n      - name:", 1)[0]
    assert "secrets.WATTRACKER_SIGNING_PFX_B64" in sign_step
    assert "secrets.WATTRACKER_SIGNING_PFX_PASSWORD" in sign_step
    # The release build must install the BLE extra (so the shipped binary can
    # talk to hardware) and the [package] extra, which is where the PyInstaller
    # pin lives - inlining a version here would let Windows and macOS drift.
    # The extras are checked individually rather than as one exact string: this
    # job now builds the connector as well as the app, so the set grows, and a
    # literal match would fail for the wrong reason every time it does.
    extras = re.search(r'pip install [^\n]*"\.\[([^\]]+)\]"', workflow)
    assert extras, "release build must install the project with extras"
    installed = set(extras.group(1).split(","))
    assert {"dev", "ble", "package"} <= installed, installed
    # The connector's own halves: the websockets client it dials with, and the
    # WebView binding its tray window uses. Both are collected best-effort by
    # the spec, so a build without them installed silently loses a feature.
    assert {"connector", "webview"} <= installed, installed
    assert "pyinstaller==" not in workflow


def test_signed_release_only_builds_the_triggering_release_tag():
    workflow = Path(".github/workflows/windows-release.yml").read_text()
    assert "workflow_dispatch" not in workflow
    assert "inputs.ref" not in workflow
    assert 'tags:\n      - "v*"' in workflow


def test_signed_release_tag_version_is_checked_before_signing():
    for path, signing_step in (
        (".github/workflows/macos-release.yml", "Import Developer ID certificate"),
        (".github/workflows/windows-release.yml", "Sign and verify every frozen binary"),
    ):
        workflow = Path(path).read_text()
        check = workflow.index("- name: Verify release tag matches desktop version")
        assert check < workflow.index(f"- name: {signing_step}")
        block = workflow.split(
            "      - name: Verify release tag matches desktop version\n", 1
        )[1].split("\n      - ", 1)[0]
        assert "^v[0-9]+\\.[0-9]+\\.[0-9]+$" in block
        assert 'actual="$(python packaging/_version.py)"' in block


def test_preview_workflow_guards_uploads_and_avoids_release_tag_triggers():
    workflow = Path(".github/workflows/windows.yml").read_text()
    trigger = workflow.split("jobs:", 1)[0]
    assert "branches: [main]" in trigger
    assert "pull_request:" in trigger
    assert "tags:" not in trigger

    handoff = workflow.split("      - name: Hand off the DMG to the publisher\n", 1)[1]
    handoff = handoff.split("\n  publish-preview:", 1)[0]
    assert "if-no-files-found: error" in handoff

    replace = workflow.split("      - name: Replace rolling preview release\n", 1)[1]
    replace = replace.split("\n      - ", 1)[0]
    assert "if: steps.freshness.outputs.publish == 'true'" in replace


def test_first_preview_publish_creates_tag_before_release():
    workflow = Path(".github/workflows/windows.yml").read_text()
    publisher = workflow.split("  publish-preview:\n", 1)[1]
    create_tag = publisher.index('gh api --method POST "repos/$GITHUB_REPOSITORY/git/refs"')
    first_release = publisher.index("gh release create preview-latest", create_tag)
    assert create_tag < first_release
    before_release = publisher[create_tag:first_release]
    assert "ref=refs/tags/preview-latest" in before_release
    assert 'sha="$GITHUB_SHA"' in before_release
    assert "--verify-tag" in publisher[first_release:]
    first_create = publisher[first_release:].split("\n            fi", 1)[0]
    assert "--draft" not in first_create


def test_version_derived_workflow_jobs_fetch_full_tag_history():
    for path, job_names in (
        (
            ".github/workflows/windows.yml",
            ("windows-real", "package-unsigned", "macos-preview", "publish-preview"),
        ),
        (".github/workflows/macos-release.yml", ("build-test-sign",)),
        (".github/workflows/windows-release.yml", ("build-test-sign",)),
    ):
        workflow = Path(path).read_text()
        for index, job_name in enumerate(job_names):
            marker = f"  {job_name}:"
            section = workflow.split(marker, 1)[1]
            if index + 1 < len(job_names):
                section = section.split(f"\n  {job_names[index + 1]}:", 1)[0]
            assert "fetch-depth: 0" in section, f"{path} job {job_name} must fetch tags/history"


def test_windows_ci_uses_one_fixed_python_version():
    workflow = Path(".github/workflows/windows.yml").read_text()
    test_job = workflow.split("  test:\n", 1)[1].split(
        "\n  windows-real:", 1
    )[0]
    real_job = workflow.split("  windows-real:\n", 1)[1].split(
        "\n  package-unsigned:", 1
    )[0]
    package_job = workflow.split("  package-unsigned:\n", 1)[1].split(
        "\n  macos-preview:", 1
    )[0]
    for job in (test_job, real_job, package_job):
        assert 'python-version: "3.12"' in job
        assert "matrix:" not in job
        assert job.count("python-version:") == 1


def test_windows_signed_release_job_is_hard_disabled():
    """The *signed* release stays gated; the unsigned installer job does not.

    `package-unsigned` deliberately carries no gate any more: it runs on the
    hosted Windows runner, which is what finally executes the setup compiler
    this repository had never once run. The release job is a different case -
    it needs hosted minutes and the code-signing secrets - so it keeps its gate,
    and this asserts the two do not get conflated again.

    `tests/test_windows_installer.py` pins the shape of the job that now runs.
    """
    workflow = Path(".github/workflows/windows.yml").read_text()
    package_job = workflow.split("  package-unsigned:\n", 1)[1]
    assert not package_job.startswith("    if: ${{ false }}\n")

    release_workflow = Path(
        ".github/workflows/windows-release.yml"
    ).read_text()
    release_job = release_workflow.split("  build-test-sign:\n", 1)[1]
    assert release_job.startswith("    if: ${{ false }}\n")


def test_signing_script_requires_rfc3161_timestamp_verification():
    script = Path("packaging/sign-windows.ps1").read_text()
    assert "/tr $TimestampUrl /td SHA256" in script
    assert "verify /pa /all /v /tw" in script
    assert 'PSObject.Properties[\n            "TimeStamperCertificate"' in script
    assert "RFC3161 timestamp verification failed" in script
