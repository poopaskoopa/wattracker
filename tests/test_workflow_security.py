"""Fork pull requests must never reach a self-hosted runner.

This repository is public, and two of its jobs run on physical machines that
are not torn down between jobs. Both execute code from the branch under test
before any human reads it: the macOS job runs the branch's build backend via
`uv pip install -e` and its conftest.py via pytest, and the Windows job also
runs its PyInstaller spec, its Inno Setup script, and an installer built from
the branch. Without a gate, opening a pull request is remote code execution on
someone's desk.

GitHub's first-time-contributor approval prompt does not cover this: approving
a contributor once exempts every later pull request from that account. The gate
in the workflow is the control, so it is asserted here.
"""

from pathlib import Path
import re


WORKFLOW_DIR = Path(__file__).parents[1] / ".github" / "workflows"

# Kept byte-identical to the copy in tests/test_windows_installer.py.
FORK_GATE = (
    "github.event_name == 'push' "
    "|| github.event.pull_request.head.repo.full_name == github.repository"
)


def _jobs(text):
    """Split a workflow into (name, body) pairs at two-space job indentation.

    Scoped to the `jobs:` section first. Keys under `on:` sit at the same
    indentation as job names - `  pull_request:` is indistinguishable from a
    job called `pull_request` by indentation alone.
    """
    section = re.search(r"(?ms)^jobs:\n(.*)\Z", text)
    if not section:
        return
    body_text = section.group(1)
    starts = [m for m in re.finditer(r"(?m)^  ([a-zA-Z0-9_-]+):$", body_text)]
    for i, m in enumerate(starts):
        end = starts[i + 1].start() if i + 1 < len(starts) else len(body_text)
        yield m.group(1), body_text[m.start():end]


def test_every_self_hosted_job_excludes_fork_pull_requests():
    checked = []
    for path in sorted(WORKFLOW_DIR.glob("*.yml")):
        text = path.read_text(encoding="utf-8")
        # Only workflows a fork can trigger are at risk. A tag- or
        # dispatch-triggered workflow is not reachable from a pull request.
        if not re.search(r"(?m)^  pull_request:", text):
            continue
        for name, body in _jobs(text):
            # Matched on the `runs-on:` line, not anywhere in the job: several
            # of these jobs carry comments explaining why they do or do not use
            # the self-hosted runner, and a comment is not a dispatch target.
            if not re.search(r"(?m)^\s*runs-on:.*self-hosted", body):
                continue
            gates = re.findall(r"(?m)^    if: (.+)$", body)
            assert FORK_GATE in gates or "${{ false }}" in " ".join(gates), (
                f"{path.name}:{name} runs on a self-hosted runner and is "
                f"reachable from a fork pull request. Add the fork gate."
            )
            checked.append(f"{path.name}:{name}")

    # A refactor that renames the jobs or restructures the workflows must not
    # quietly reduce this to asserting nothing at all.
    assert sorted(checked) == ["cloud.yml:tests", "windows.yml:package-unsigned"]


def test_no_workflow_uses_pull_request_target():
    """`pull_request_target` runs the base branch's workflow with write-scoped
    secrets while checking out attacker-controlled code - the classic path to
    exfiltrating repository secrets from a fork. It has never been used here.
    """
    for path in sorted(WORKFLOW_DIR.glob("*.yml")):
        text = path.read_text(encoding="utf-8")
        assert "pull_request_target" not in text, path.name


def test_no_workflow_interpolates_event_data_into_a_run_block():
    """`${{ github.event.* }}` inside `run:` is shell injection: a pull request
    title or branch name containing backticks executes on the runner. Values
    that need to reach a script must go through `env:` instead.
    """
    pattern = re.compile(r"\$\{\{\s*github\.(event\.|head_ref)")
    for path in sorted(WORKFLOW_DIR.glob("*.yml")):
        for name, body in _jobs(path.read_text(encoding="utf-8")):
            for block in re.findall(r"(?ms)^\s*run: \|?\s*\n(.*?)(?=^\s*- |^\s{0,4}\w|\Z)", body):
                assert not pattern.search(block), f"{path.name}:{name}"
