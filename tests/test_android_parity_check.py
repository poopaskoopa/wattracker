"""The Android-parity pull request check, and the workflow filters it relies on.

See docs/mobile-parity.md. The check itself is scripts/check_android_parity.py;
these tests pin which paths oblige an `Android:` line, which lines satisfy it,
and that the workflows carrying it stay wired the way the doc says.
"""

import importlib.util
import re
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[1]
WORKFLOW_DIR = ROOT / ".github" / "workflows"
SPEC = importlib.util.spec_from_file_location(
    "check_android_parity", ROOT / "scripts" / "check_android_parity.py"
)
parity = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(parity)


@pytest.mark.parametrize("path", [
    "ios/WatTracker/WatTracker/Screens/CalendarScreen.swift",
    "ios/README.md",
    "wattracker/cloud/api.py",
    "tests/vectors/calendar_grid_v1.json",
])
def test_shared_paths_require_the_line(path):
    assert parity.touches_shared_surface(["README.md", path]) == [path]


@pytest.mark.parametrize("path", [
    "android/app/build.gradle.kts",
    "wattracker/server.py",
    "wattracker/cloudy.py",
    "tests/test_cloud_api.py",
    "docs/mobile-parity.md",
    "scripts/ios_release_secrets.py",
    ".github/workflows/ios-release.yml",
])
def test_other_paths_do_not(path):
    assert parity.touches_shared_surface([path]) == []


@pytest.mark.parametrize("body", [
    "Android: n/a - server-only refactor",
    "Android: n/a — process/docs only; notes already on #195–#199",
    "android: posted on #198",
    "ANDROID: #196",
    "Android:#419",
    "Summary\n\n- Android: note on #197\n\nMore text",
    "* Android: n/a, Swift test-only change",
    "Intro\r\nAndroid: posted on #198\r\n",
    "Android: n/a ok",
    "```\ncode\n```\nAndroid: posted on #198",
    "<!-- note -->\nAndroid: n/a - docs only",
])
def test_bodies_that_pass(body):
    assert parity.has_android_line(body)


@pytest.mark.parametrize("body", [
    None,
    "",
    "Android:",
    "Android: n/a",
    "Android: n/a   ",
    "Android: TODO",
    "Android: see the epic",
    "The Android: n/a line goes here",
    "Android note posted on #198",
    "<!-- Android: n/a <reason>  or  Android: posted on #N -->\nAndroid: ",
    "`Android: posted on #198`",
    "Android: n/a.",
    "Android: n/abc",
    "Android: n/a <reason>",
    "Android: n/a <reason>  ",
    "Android: n/a -",
    "Android: posted on #<N>",
    "Android: posted on #N",
    "Text\n```\nAndroid: posted on #198\n```\nAndroid:",
    "Text\n```\nAndroid: posted on #198",
    "Text\n<!-- unclosed\nAndroid: posted on #198",
])
def test_bodies_that_fail(body):
    assert not parity.has_android_line(body)


def _git(cwd, *args):
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        cwd=cwd, check=True, capture_output=True,
    )


def test_moving_a_file_out_of_a_shared_tree_still_counts(tmp_path, monkeypatch):
    _git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "ios").mkdir()
    (tmp_path / "ios" / "Foo.swift").write_text("let shared = 1\n" * 20)
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "base")
    (tmp_path / "other").mkdir()
    _git(tmp_path, "mv", "ios/Foo.swift", "other/Foo.swift")
    _git(tmp_path, "commit", "-q", "-m", "move")
    monkeypatch.chdir(tmp_path)
    changed = parity.changed_files("HEAD~1", "HEAD")
    assert sorted(changed) == ["ios/Foo.swift", "other/Foo.swift"]
    assert parity.check(changed, "") is not None


def test_check_passes_untouched_prs_without_a_body():
    assert parity.check(["wattracker/server.py"], "") is None


def test_check_failure_names_files_format_and_doc():
    paths = ["ios/a.swift"] + [f"tests/vectors/v{i}.json" for i in range(12)]
    message = parity.check(paths, "no line here")
    assert message is not None
    assert "ios/a.swift" in message
    assert "... and 3 more" in message
    assert "Android: n/a <reason>" in message
    assert "Android: posted on #<N>" in message
    assert "docs/mobile-parity.md" in message


def test_main_exit_codes_and_annotation(capsys):
    shared = ["wattracker/cloud/api.py"]
    assert parity.main(env={"PR_BODY": "Android: #198"}, paths=shared) == 0
    assert parity.main(env={}, paths=["android/x.kt"]) == 0
    assert parity.main(env={}, paths=shared) == 1
    assert "::error " in capsys.readouterr().out


def test_parity_workflow_passes_body_through_env_only():
    text = (WORKFLOW_DIR / "mobile-parity.yml").read_text(encoding="utf-8")
    assert re.search(r"(?m)^  pull_request:\n    types: \[opened, edited, synchronize, reopened\]$", text)
    assert re.search(r"(?m)^  contents: read$", text)
    assert "pull_request_target" not in text
    assert "runs-on: ubuntu-latest" in text
    assert "self-hosted" not in text
    assert "fetch-depth: 0" in text
    assert "PR_BODY: ${{ github.event.pull_request.body }}" in text
    assert "run: python3 scripts/check_android_parity.py" in text
    # Only first-party actions: no third-party code sees the PR.
    for action in re.findall(r"uses: (\S+)", text):
        assert action.startswith("actions/"), action
    # Rapid description edits cancel the run they supersede.
    assert "group: mobile-parity-${{ github.event.pull_request.number }}" in text
    assert "cancel-in-progress: true" in text
    # The body is referenced exactly once, and that is the env entry above.
    assert text.count("pull_request.body") == 1


def test_android_workflow_runs_when_a_shared_vector_changes():
    text = (WORKFLOW_DIR / "android.yml").read_text(encoding="utf-8")
    on = text.split("\npermissions:", 1)[0]
    for event in ("push", "pull_request"):
        block = re.search(rf"(?ms)^  {event}:\n(.*?)(?=^  \S|\Z)", on).group(1)
        paths = re.findall(r"^      - '([^']+)'$", block, re.MULTILINE)
        assert {"android/**", "tests/vectors/**", ".github/workflows/android.yml"} <= set(paths), event


def test_kotlin_vector_loader_still_reads_from_tests_vectors():
    # The android.yml filter above is only correct while this is where the
    # Kotlin tests find the vectors.
    loader = next((ROOT / "android").rglob("TestVectors.kt")).read_text(encoding="utf-8")
    assert '"tests"' in loader and '"vectors"' in loader


def test_unfilled_pr_template_does_not_pass_but_a_filled_one_does():
    template = (ROOT / ".github" / "pull_request_template.md").read_text(encoding="utf-8")
    assert not parity.has_android_line(template)
    filled = template.replace("\nAndroid:\n", "\nAndroid: posted on #198\n")
    assert filled != template
    assert parity.has_android_line(filled)
