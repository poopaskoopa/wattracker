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


def test_windows_ci_uses_one_fixed_python_version():
    workflow = Path(".github/workflows/windows.yml").read_text()
    test_job = workflow.split("  test:\n", 1)[1].split(
        "\n  package-unsigned:", 1
    )[0]
    assert 'python-version: "3.12"' in test_job
    assert "matrix:" not in test_job
    assert test_job.count("python-version:") == 1


def test_windows_signed_release_job_is_hard_disabled():
    """The *signed* release stays gated; the unsigned installer job does not.

    `package-unsigned` deliberately carries no gate any more: it runs on the
    self-hosted Windows runner, which is what finally executes the setup
    compiler this repository had never once run. The release job is a different
    case - it needs hosted minutes and the code-signing secrets - so it keeps
    its gate, and this asserts the two do not get conflated again.

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
