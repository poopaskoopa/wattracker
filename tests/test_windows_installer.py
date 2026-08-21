from pathlib import Path
import re


ROOT = Path(__file__).parents[1]
ISS = (ROOT / "packaging" / "wattracker.iss").read_text(encoding="utf-8")
LAUNCHER = (ROOT / "scripts" / "wattracker.ps1").read_text(encoding="utf-8")
SMOKE = (ROOT / "packaging" / "smoke_installer.ps1").read_text(encoding="utf-8")
WORKFLOW = (ROOT / ".github" / "workflows" / "windows.yml").read_text(encoding="utf-8")

# The one job-level gate the self-hosted jobs are allowed to carry. Duplicated
# in tests/test_workflow_security.py rather than shared: tests/ is not a
# package, and a security invariant is worth asserting from both directions.
FORK_GATE = (
    "github.event_name == 'push' "
    "|| github.event.pull_request.head.repo.full_name == github.repository"
)


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


def test_version_comes_from_pyproject_compile_define():
    # Regex rather than tomllib keeps collection independent of the test
    # runner's standard-library modules. packaging/wattracker.spec reads the
    # version the same way.
    match = re.search(
        r'(?m)^version\s*=\s*"([^"]+)"',
        (ROOT / "pyproject.toml").read_text(encoding="utf-8"),
    )
    assert match, "could not read version from pyproject.toml"
    version = match.group(1)
    assert "#ifndef AppVersion" in ISS
    assert "AppVersion={#AppVersion}" in ISS
    assert "OutputBaseFilename=wattracker-{#AppVersion}-windows-x64-unsigned-setup" in ISS
    assert "tomllib" in WORKFLOW
    assert "/DAppVersion=$version" in WORKFLOW
    assert version not in ISS


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


def test_workflow_runs_the_installer_job_on_the_self_hosted_runner():
    """The installer job runs; the suite job does not. Both halves matter.

    `package-unsigned` reverting to a gate would take the only execution of
    the setup compiler with it - the state this repository was in until the
    self-hosted runner existed. An ungated `test` job would put a duplicate of
    the macOS runner's suite on the single physical Windows box.
    """
    test_job, package_job = WORKFLOW.split("  package-unsigned:", 1)
    assert "if: ${{ false }}" in test_job
    # Matched at job indentation, not anywhere in the job. A *step* inside
    # package-unsigned may legitimately be gated - the upload is, while the
    # storage question is open - and that must not read as the job reverting to
    # a gate, which is the thing this asserts against.
    #
    # Exactly one job-level gate is allowed, and only this one: the fork
    # exclusion. It is not the failure mode above - it still runs for every
    # push to main and every pull request raised from a branch in this
    # repository, so installer coverage is intact. Anything else at this
    # indentation is the reversion this test exists to catch.
    job_gates = re.findall(r"(?m)^    if: (.+)$", package_job)
    assert job_gates == [FORK_GATE]
    # The `Windows` label is load-bearing: a bare [self-hosted] also matches the
    # macOS runner that the Cloud workflow uses.
    assert "runs-on: [self-hosted, Windows, X64]" in package_job
    # Cancelling mid-job skips smoke_installer.ps1's `finally`, which is what
    # uninstalls the product - leaving a half-installed application on a runner
    # that persists between jobs. Serializing is the correct trade.
    #
    # Matched as a YAML key, not as a substring: the workflow comment explaining
    # this decision necessarily contains the word.
    assert re.search(r"(?m)^\s*concurrency:", WORKFLOW)
    assert not re.search(r"(?m)^\s*cancel-in-progress\s*:", WORKFLOW)


def test_installer_job_uses_the_runners_machine_wide_python():
    """No setup-python on this job, and the interpreter is asserted instead.

    actions/setup-python is not a tool-cache unpack on Windows: the setup
    script in actions/python-versions runs the official installer with
    InstallAllUsers=1 and clears keys under HKLM, so it needs administrator.
    The runner's service account deliberately is not one, and handing it admin
    would remove the account isolation the installer smoke test depends on -
    so reintroducing the action would either fail the job or undo that.

    The cost is that the interpreter becomes a property of the machine rather
    than of this file. Asserting the version is what buys it back: a drifted
    runner fails on a line that names what it found.
    """
    _, package_job = WORKFLOW.split("  package-unsigned:", 1)
    # Matched as a `uses:` line, not as a substring: the comment in the workflow
    # explaining why the action is absent necessarily names it.
    assert not re.search(r"(?m)^\s*-?\s*uses:\s*actions/setup-python", package_job)
    assert "sys.version_info[:2] == (3, 12)" in package_job


def test_workflow_builds_smokes_and_uploads_the_wheel_and_setup_artifacts():
    assert "innosetup-6.7.3.exe" in WORKFLOW
    assert "9c73c3bae7ed48d44112a0f48e66742c00090bdb5bef71d9d3c056c66e97b732" in WORKFLOW
    assert "Get-FileHash -LiteralPath $innoInstaller -Algorithm SHA256" in WORKFLOW
    assert '$publisher -cne "Pyrsys B.V."' in WORKFLOW
    assert "packaging\\smoke_frozen.ps1" in WORKFLOW
    assert "packaging\\wattracker.iss" in WORKFLOW
    assert "packaging\\smoke_installer.ps1" in WORKFLOW
    assert "dist/*.whl" in WORKFLOW
    assert "dist/*-unsigned-setup.exe" in WORKFLOW


def test_a_full_storage_quota_cannot_red_check_a_green_build():
    """The build's own signal must not ride on GitHub's storage accounting.

    Run 32477527515 - this job's first on main - compiled the installer and
    passed every smoke, then failed on `Artifact storage quota has been hit`.
    The account's 500 MB is committed elsewhere (this repo holds no artifacts)
    and usage recalculates only every 6-12 hours, so the wall is neither ours
    to clear nor short-lived. The upload is therefore best-effort, and the
    question it used to answer - did the build actually produce an installer? -
    is answered against the filesystem instead.
    """
    # Anchored to the start of a line, not a bare substring: `# continue-on-
    # error: true` still contains the substring, so an `in WORKFLOW` check
    # passes against a commented-out setting and pins nothing. Every assertion
    # here was mutation-checked by commenting the real line out.
    assert re.search(r"(?m)^\s+continue-on-error: true$", WORKFLOW)
    assert re.search(r"(?m)^\s+if-no-files-found: warn$", WORKFLOW)
    # The replacement signal. Without these the upload's continue-on-error
    # would mean a build that produced nothing still reported success.
    assert re.search(r'(?m)^\s+if \(\$whl\.Count -eq 0\) \{ throw ', WORKFLOW)
    assert re.search(r'(?m)^\s+if \(\$exe\.Count -eq 0\) \{ throw ', WORKFLOW)
    # The connector ships in the same upload and needs the same signal: it is
    # the one artifact here that no other file duplicates, so a freeze that
    # silently produced nothing would otherwise be invisible.
    assert re.search(r'(?m)^\s+if \(\$connector\.Count -eq 0\) \{ throw ', WORKFLOW)


def test_heavy_steps_yield_the_box_to_a_hardware_session():
    """The runner shares a machine with the trainer and Zwift.

    Nothing in the job touches Bluetooth, so the trainer link is never
    contended - but the PyInstaller freeze and the Inno Setup compress each
    saturate every core for minutes, and a build can start in the middle of a
    session. Dropping the shell lets Zwift preempt it; children inherit.

    BelowNormal rather than Idle is the load-bearing half: at Idle the runner's
    heartbeat can starve under sustained load and the job is reported lost.
    """
    _, package_job = WORKFLOW.split("  package-unsigned:", 1)
    drops = re.findall(r"PriorityClass = '(\w+)'", package_job)
    assert drops, "no step lowers its priority"
    assert set(drops) == {"BelowNormal"}
    # The four steps that do real work: dependency install, wheel build, the
    # app's freeze plus installer compile, and the connector's freeze. Counted
    # rather than merely checked for presence, so that a new heavy step added
    # without the drop fails here instead of quietly competing with a ride.
    assert len(drops) == 4


def test_push_is_filtered_to_main_so_a_commit_runs_once():
    """One physical runner means a duplicate run is queued, not parallel.

    A bare `push:` beside `pull_request:` fires twice for every commit on a
    branch with an open PR, and the second waits for the first: runs
    32386196713 and 32386202547 were one commit, 10m45s of wall for 5m30s of
    work. Filtering push to main gives a PR run while the work is in review and
    a push run when it merges - which is also what the upload keys off.
    """
    on = WORKFLOW.split("permissions:", 1)[0]
    assert re.search(r"(?m)^  push:$", on)
    assert re.search(r"(?m)^    branches: \[main\]$", on)
    assert re.search(r"(?m)^  pull_request:$", on)


def test_workflow_keeps_ci_artifacts_inside_the_storage_quota():
    """The two things that stop this job re-hitting the artifact quota.

    A run's payload was 106.8 MB - a 44.4 MB setup exe, a 61.8 MB portable zip
    and the wheel - and the upload took the 90-day default retention. On a free
    account's 500 MB of shared storage that is about four runs before
    `upload-artifact` fails with "Artifact storage quota has been hit", which is
    how 45 stale artifacts reached 2693 MB and blocked the job outright.

    The zip is the half worth pinning. It duplicated the installer's payload -
    the same onedir tree, wrapped differently - so dropping it cost no coverage,
    and the portable form still ships from windows-release.yml, which builds and
    signs its own on a `v*` tag. Reintroducing it here would put 61.8 MB per run
    back and quietly restart the countdown, so assert its absence rather than
    trusting a reviewer to notice a re-added Compress-Archive.
    """
    assert re.search(r"(?m)^\s*retention-days:\s*5\s*$", WORKFLOW)
    assert "Compress-Archive" not in WORKFLOW
    assert "wattracker-windows-x64-unsigned.zip" not in WORKFLOW

    # Upload on a merge to main, not on every PR commit - the difference
    # between ~8.75 uploads a week and ~93. The condition sits on the step, so
    # every PR commit still runs the whole build; only the artifact is skipped.
    body = WORKFLOW.splitlines()
    at = next(
        n for n, line in enumerate(body) if "uses: actions/upload-artifact" in line
    )
    gate = body[at - 1].strip()
    assert "github.event_name == 'push'" in gate
    assert "github.ref == 'refs/heads/main'" in gate


def test_frozen_restore_dispatch_contract_is_unchanged():
    entry = (ROOT / "packaging" / "wattracker_entry.py").read_text(encoding="utf-8")
    assert 'sys.argv[1] == "restore"' in entry
    assert "main(sys.argv[2:])" in entry
