from pathlib import Path
import re


ROOT = Path(__file__).parents[1]
ISS = (ROOT / "packaging" / "wattracker.iss").read_text(encoding="utf-8")
LAUNCHER = (ROOT / "scripts" / "wattracker.ps1").read_text(encoding="utf-8")
SMOKE = (ROOT / "packaging" / "smoke_installer.ps1").read_text(encoding="utf-8")
WORKFLOW = (ROOT / ".github" / "workflows" / "windows.yml").read_text(encoding="utf-8")

def test_installer_is_stable_per_user_and_ships_the_full_payload():
    assert re.search(r"(?m)^AppId=\{\{[0-9A-F-]{36}\}$", ISS)
    assert "PrivilegesRequired=lowest" in ISS
    assert r"DefaultDirName={localappdata}\Programs\wattracker" in ISS
    assert r'Source: "..\dist\wattracker\*"' in ISS
    assert "recursesubdirs" in ISS
    assert "createallsubdirs" in ISS
    assert r'Source: "..\scripts\wattracker.ps1"' in ISS
    assert "firewall" not in ISS.lower()
    assert "run at startup" not in ISS.lower()


def test_version_comes_from_git_derived_compile_define():
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "#ifndef AppVersion" in ISS
    assert "AppVersion={#AppVersion}" in ISS
    assert "OutputBaseFilename=wattracker-{#AppVersion}-windows-x64-unsigned-setup" in ISS
    assert 'dynamic = ["version"]' in pyproject
    assert "setuptools-scm" in pyproject
    assert "python packaging\\_version.py" in WORKFLOW
    assert "/DAppVersion=$version" in WORKFLOW
    assert "pyproject.toml" not in ISS


def test_installer_lifecycle_uses_only_identity_safe_launcher():
    assert "PrepareToInstall" in ISS
    assert "InitializeUninstall" in ISS
    assert "[UninstallRun]" not in ISS
    assert "Result := False" in ISS
    assert "Uninstall did not remove any application files." in ISS
    assert ISS.count(r"{app}\scripts\wattracker.ps1") >= 2
    combined = "\n".join((ISS, SMOKE, WORKFLOW))
    assert not re.search(r"\btaskkill\b", combined, re.IGNORECASE)
    assert not re.search(r"Get-NetTCPConnection|Win32_Process.*Terminate", combined, re.IGNORECASE)
    assert "Stop-Process -Name" not in combined
    assert "Stop-Process -Id" not in LAUNCHER
    assert ".process.Kill()" in LAUNCHER
    assert "Confirm-Managed" in LAUNCHER
    assert r"--wattracker-managed=[0-9a-f]{32}" in LAUNCHER


def test_uninstall_preserves_profile_data_and_removes_installed_metadata():
    assert r"{userprofile}\.wattracker" not in ISS.lower()
    assert "Remove-Item" not in ISS
    assert "keep-after-uninstall.txt" in SMOKE
    assert "uninstall removed user data" in SMOKE
    assert "tampered state did not block uninstall" in SMOKE
    assert "blocked uninstall removed application files" in SMOKE
    assert "Start Menu shortcut survived uninstall" in SMOKE
    assert "uninstall metadata survived uninstall" in SMOKE


def test_browser_open_is_opt_in_and_happens_after_health():
    assert "[switch]$OpenBrowser" in LAUNCHER
    assert LAUNCHER.index("if (Test-Health)") < LAUNCHER.index("Open-ReadyBrowser", LAUNCHER.index("if (Test-Health)"))
    assert '$env:WATTRACKER_OPEN_BROWSER = "0"' in LAUNCHER
    assert "-Action start -OpenBrowser" in ISS


def test_launcher_prefers_override_then_frozen_then_source_virtualenv():
    override = LAUNCHER.index("if ($env:WATTRACKER_EXECUTABLE)")
    frozen = LAUNCHER.index('Join-Path $Root "wattracker.exe"', override)
    virtualenv = LAUNCHER.index('Join-Path $Root ".venv\\Scripts\\python.exe"', frozen)
    assert override < frozen < virtualenv
    assert "Test-Path -LiteralPath" in LAUNCHER[override:virtualenv]
    assert "$env:WATTRACKER_EXECUTABLE = $Executable" not in SMOKE


def test_workflow_runs_the_installer_job_on_a_hosted_runner():
    """The installer job runs; the suite job does not. Both halves matter.

    `package-unsigned` reverting to a gate would take the only execution of
    the setup compiler with it. Enabling the `test` job would duplicate the
    platform-neutral suite that already runs in the Cloud workflow.
    """
    test_job, jobs_after_package = WORKFLOW.split("  package-unsigned:", 1)
    package_job = jobs_after_package.split("  macos-preview:", 1)[0]
    assert "if: ${{ false }}" in test_job
    # Main-only conditions belong to individual handoff steps and later jobs;
    # the Windows packaging job itself stays enabled on PRs.
    job_gates = re.findall(r"(?m)^    if: (.+)$", package_job)
    assert job_gates == []
    assert "runs-on: windows-latest" in package_job


def test_installer_job_uses_setup_python_312():
    _, package_job = WORKFLOW.split("  package-unsigned:", 1)
    assert re.search(
        r'(?m)^\s*- uses: actions/setup-python@v5\n\s+with:\n\s+python-version: "3\.12"$',
        package_job,
    )
    assert "Assert the runner's machine-wide Python" not in package_job
    assert r".venv\Scripts\python" not in package_job


def test_lifecycle_uses_the_installed_console_launcher():
    _, package_job = WORKFLOW.split("  package-unsigned:", 1)
    assert "$env:WATTRACKER_EXECUTABLE = (Get-Command wattracker).Source" in package_job


def test_workflow_builds_and_smokes_the_wheel_then_hands_off_release_payloads():
    assert "innosetup-6.7.3.exe" in WORKFLOW
    assert "9c73c3bae7ed48d44112a0f48e66742c00090bdb5bef71d9d3c056c66e97b732" in WORKFLOW
    assert "Get-FileHash -LiteralPath $innoInstaller -Algorithm SHA256" in WORKFLOW
    assert '$publisher -cne "Pyrsys B.V."' in WORKFLOW
    assert "packaging\\smoke_frozen.ps1" in WORKFLOW
    assert "packaging\\wattracker.iss" in WORKFLOW
    assert "packaging\\smoke_installer.ps1" in WORKFLOW
    assert "python -m build --wheel" in WORKFLOW
    assert "dist/*-unsigned-setup.exe" in WORKFLOW
    assert "dist/WattrackerConnector.exe" in WORKFLOW
    assert "desktop-preview-windows" in WORKFLOW


def test_release_handoff_fails_visibly_when_artifact_upload_fails():
    """A preview must not report success without payloads for the publisher."""
    assert not re.search(r"(?m)^\s+continue-on-error: true$", WORKFLOW)
    assert re.search(r"(?m)^\s+if-no-files-found: error$", WORKFLOW)
    assert re.search(r"(?m)^\s+retention-days: 1$", WORKFLOW)
    assert re.search(r'(?m)^\s+if \(\$exe\.Count -eq 0\) \{ throw ', WORKFLOW)
    assert re.search(r'(?m)^\s+if \(\$connector\.Count -eq 0\) \{ throw ', WORKFLOW)


def test_push_is_filtered_to_main_so_a_commit_runs_once():
    """Review each PR commit and upload from the post-merge main run."""
    on = WORKFLOW.split("permissions:", 1)[0]
    assert re.search(r"(?m)^  push:$", on)
    assert re.search(r"(?m)^    branches: \[main\]$", on)
    assert re.search(r"(?m)^  pull_request:$", on)


def test_preview_handoff_is_small_and_expires_after_one_day():
    assert "Compress-Archive" not in WORKFLOW
    assert "wattracker-windows-x64-unsigned.zip" not in WORKFLOW
    windows_handoff = WORKFLOW.split("name: desktop-preview-windows", 1)[1].split(
        "  macos-preview:", 1
    )[0]
    assert "dist/*-unsigned-setup.exe" in windows_handoff
    assert "dist/WattrackerConnector.exe" in windows_handoff
    assert "dist/*.whl" not in windows_handoff
    assert "if-no-files-found: error" in windows_handoff
    assert "retention-days: 1" in windows_handoff


def test_frozen_restore_dispatch_contract_is_unchanged():
    entry = (ROOT / "packaging" / "wattracker_entry.py").read_text(encoding="utf-8")
    assert 'sys.argv[1] == "restore"' in entry
    assert "main(sys.argv[2:])" in entry
