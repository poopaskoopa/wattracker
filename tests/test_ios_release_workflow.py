"""The automatic iOS TestFlight release: triggers, gates, permissions, versions.

Static checks parse .github/workflows/ios-release.yml; behavioural checks run
scripts/ios_release.py against throwaway git repositories and fake GitHub
clients. Nothing here reaches the network, Keychain, or an Apple credential.
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

from scripts import ios_release as rel


WORKFLOW = Path(".github/workflows/ios-release.yml")
PBXPROJ = Path("ios/WatTracker/WatTracker.xcodeproj/project.pbxproj")
PROJECT_DIR = Path("ios/WatTracker")
REPO = "poopaskoopa/wattracker"


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _on(workflow: dict) -> dict:
    # PyYAML (YAML 1.1) reads the bare key `on` as boolean True.
    return workflow.get("on", workflow.get(True))


def _step(job: dict, name: str) -> dict:
    return next(step for step in job["steps"] if step.get("name") == name)


# ------------------------------------------------------------------ triggers


def test_main_push_trigger_carries_exactly_the_app_path_filter():
    on = _on(_workflow())
    assert on["push"]["branches"] == ["main"]
    assert on["push"]["tags"] == ["ios-v*"]
    assert on["push"]["paths"] == rel.workflow_path_filters()
    assert on["push"]["paths"] == [
        "ios/WatTracker/WatTracker/**",
        "ios/WatTracker/WatTracker.xcodeproj/**",
        "ios/WatTracker/Config/**",
        "ios/WatTracker/ExportOptions.plist",
    ]
    assert "paths-ignore" not in on["push"]


def test_no_trigger_can_run_this_workflow_for_untrusted_code():
    on = _on(_workflow())
    assert set(on) == {"push", "workflow_dispatch"}
    for forbidden in ("pull_request", "pull_request_target", "workflow_run", "issue_comment"):
        assert forbidden not in on
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "must not gain one" in text


def _github_glob_matches(pattern: str, path: str) -> bool:
    regex = ""
    i = 0
    while i < len(pattern):
        if pattern.startswith("**", i):
            regex += ".*"
            i += 2
        elif pattern[i] == "*":
            regex += "[^/]*"
            i += 1
        else:
            regex += re.escape(pattern[i])
            i += 1
    return re.fullmatch(regex, path) is not None


APP_CHANGES = [
    "ios/WatTracker/WatTracker/WatTrackerApp.swift",
    "ios/WatTracker/WatTracker/Screens/CalendarScreen.swift",
    "ios/WatTracker/WatTracker/Assets.xcassets/AppIcon.appiconset/Contents.json",
    "ios/WatTracker/WatTracker/Info.plist",
    "ios/WatTracker/WatTracker.xcodeproj/project.pbxproj",
    "ios/WatTracker/WatTracker.xcodeproj/xcshareddata/xcschemes/wattracker.xcscheme",
    "ios/WatTracker/Config/Release.xcconfig",
    "ios/WatTracker/ExportOptions.plist",
]
NON_APP_CHANGES = [
    "ios/WatTracker/WatTrackerTests/CloudSessionTests.swift",
    "ios/WatTracker/WatTrackerTests/BehaviourVectorTests.swift",
    "tests/vectors/calendar_grid_v1.json",
    "ios/README.md",
    "ios/WatTracker/AppIcon.source.html",
    "docs/ios-testflight.md",
    "AGENTS.md",
    ".github/workflows/ios-release.yml",
    "scripts/ios_release.py",
    "wattracker/server.py",
    "android/app/build.gradle.kts",
    "ios/WatTracker/WatTrackerX/File.swift",
]


@pytest.mark.parametrize("path", APP_CHANGES)
def test_app_changes_trigger_a_release(path):
    filters = _on(_workflow())["push"]["paths"]
    assert any(_github_glob_matches(p, path) for p in filters)
    assert rel.touches_app(path)


@pytest.mark.parametrize("path", NON_APP_CHANGES)
def test_test_only_and_doc_only_changes_do_not(path):
    filters = _on(_workflow())["push"]["paths"]
    assert not any(_github_glob_matches(p, path) for p in filters)
    assert not rel.touches_app(path)


# --- the path list must cover everything the archive actually builds


def _pbx_objects() -> dict[str, str]:
    text = PBXPROJ.read_text(encoding="utf-8")
    objects: dict[str, str] = {}
    for match in re.finditer(
        r"^\t\t([0-9A-F]{24}) (?:/\*.*?\*/ )?= \{(.*?)\};$", text, re.MULTILINE
    ):
        if "\n" not in match.group(0):
            objects[match.group(1)] = match.group(2)
    for match in re.finditer(
        r"^\t\t([0-9A-F]{24}) (?:/\*[^\n]*?\*/ )?= \{\n(.*?)^\t\t\};", text, re.MULTILINE | re.DOTALL
    ):
        objects.setdefault(match.group(1), match.group(2))
    return objects


def _field(body: str, name: str) -> str | None:
    match = re.search(rf"(?:^|[\s;{{]){re.escape(name)} = (\"[^\"]*\"|[^;]+);", body)
    if match is None:
        return None
    return re.sub(r"/\*.*?\*/", "", match.group(1)).strip().strip('"')


def _ids(body: str, name: str) -> list[str]:
    match = re.search(rf"{re.escape(name)} = \((.*?)\);", body, re.DOTALL)
    return [] if match is None else re.findall(r"[0-9A-F]{24}", match.group(1))


def _target_inputs(product_type: str) -> list[Path]:
    objects = _pbx_objects()
    parents = {
        child: oid
        for oid, body in objects.items()
        if _field(body, "isa") in {"PBXGroup", "PBXVariantGroup"}
        for child in _ids(body, "children")
    }

    def resolve(oid: str) -> Path:
        body = objects[oid]
        path = _field(body, "path") or ""
        tree = _field(body, "sourceTree")
        if tree == "SOURCE_ROOT":
            return Path(os.path.normpath(PROJECT_DIR / path))
        if tree == "<group>":
            base = resolve(parents[oid]) if oid in parents else PROJECT_DIR
            return Path(os.path.normpath(base / path))
        raise AssertionError(f"unhandled sourceTree {tree!r} for {oid}")

    target = next(
        body for body in objects.values()
        if _field(body, "isa") == "PBXNativeTarget" and _field(body, "productType") == product_type
    )
    inputs: list[Path] = []
    for phase in _ids(target, "buildPhases"):
        for build_file in _ids(objects[phase], "files"):
            inputs.append(resolve(_field(objects[build_file], "fileRef")))
    configs = _ids(objects[_field(target, "buildConfigurationList")], "buildConfigurations")
    project = next(body for body in objects.values() if _field(body, "isa") == "PBXProject")
    configs += _ids(objects[_field(project, "buildConfigurationList")], "buildConfigurations")
    for config in configs:
        body = objects[config]
        plist = _field(body, "INFOPLIST_FILE")
        if plist:
            inputs.append(PROJECT_DIR / plist)
        base = re.search(r"baseConfigurationReference = ([0-9A-F]{24})", body)
        if base:
            inputs.append(resolve(base.group(1)))
    return inputs


def test_every_input_of_the_app_target_is_under_the_release_path_filter():
    inputs = _target_inputs("com.apple.product-type.application")
    names = {p.as_posix() for p in inputs}
    # The parser must have found real inputs, or this test proves nothing.
    assert "ios/WatTracker/WatTracker/WatTrackerApp.swift" in names
    assert "ios/WatTracker/WatTracker/Assets.xcassets" in names
    assert "ios/WatTracker/WatTracker/Info.plist" in names
    assert "ios/WatTracker/Config/Release.xcconfig" in names
    assert len(inputs) > 30
    for path in inputs:
        assert path.exists(), path
        assert rel.touches_app(path.as_posix()), f"{path} is built into the app but not in APP_PATHS"
    for xcconfig in PROJECT_DIR.joinpath("Config").glob("*.xcconfig"):
        for include in re.findall(r'^#include\??\s+"([^"]+)"', xcconfig.read_text(), re.MULTILINE):
            assert rel.touches_app((xcconfig.parent / include).as_posix())


def test_the_test_target_has_inputs_outside_the_filter_so_the_guard_discriminates():
    inputs = _target_inputs("com.apple.product-type.bundle.unit-test")
    outside = [p for p in inputs if not rel.touches_app(p.as_posix())]
    assert any(p.as_posix().startswith("tests/vectors/") for p in outside)
    assert any("WatTrackerTests" in p.as_posix() for p in outside)


# ------------------------------------------------------------------ upload gate


def _evaluate(condition: str, context: dict[str, str]) -> bool:
    """A tiny evaluator for the subset of GitHub expressions the gates use."""
    expr = condition.strip()
    if expr.startswith("${{") and expr.endswith("}}"):
        expr = expr[3:-2]
    expr = re.sub(r"startsWith\(([^,]+),\s*([^)]+)\)", r"_starts(\1, \2)", expr)
    expr = expr.replace("!cancelled()", "True")
    expr = expr.replace("&&", " and ").replace("||", " or ")

    def lookup(match: re.Match) -> str:
        return repr(context.get(match.group(0), ""))

    expr = re.sub(r"\b(?:github|steps|needs)\.[A-Za-z0-9_.-]+", lookup, expr)
    assert re.fullmatch(r"[\s()'\"A-Za-z0-9_/.*!=,-]*", expr), expr
    return bool(eval(expr, {"__builtins__": {}}, {"_starts": str.startswith, "True": True}))  # noqa: S307


def _upload_if() -> str:
    job = _workflow()["jobs"]["archive-export-upload"]
    return _step(job, "Validate and upload to TestFlight")["if"]


@pytest.mark.parametrize(
    ("event", "ref", "uploads"),
    [
        ("push", "refs/heads/main", True),
        ("push", "refs/tags/ios-v0.1.10", True),
        ("push", "refs/heads/feature", False),
        ("push", "refs/tags/v1.2.3", False),
        ("workflow_dispatch", "refs/heads/main", False),
        ("workflow_dispatch", "refs/tags/ios-v0.1.10", False),
    ],
)
def test_upload_is_gated_to_main_push_or_release_tag(event, ref, uploads):
    context = {"github.event_name": event, "github.ref": ref}
    assert _evaluate(_upload_if(), context) is uploads


def test_upload_gate_names_the_refs_itself_and_only_one_step_uploads():
    condition = _upload_if()
    assert condition == (
        "github.event_name == 'push' && (github.ref == 'refs/heads/main' || "
        "startsWith(github.ref, 'refs/tags/ios-v'))"
    )
    steps = _workflow()["jobs"]["archive-export-upload"]["steps"]
    uploaders = [s["name"] for s in steps if "--upload-app" in s.get("run", "")]
    assert uploaders == ["Validate and upload to TestFlight"]
    upload = _step(_workflow()["jobs"]["archive-export-upload"], "Validate and upload to TestFlight")
    assert upload["id"] == "upload"
    assert upload["run"].index("--upload-app") < upload["run"].index('echo "uploaded=true"')


def test_the_signing_job_makes_no_github_api_call():
    job = _workflow()["jobs"]["archive-export-upload"]
    text = yaml.safe_dump(job)
    assert "github.token" not in text
    assert "GITHUB_TOKEN" not in text
    assert "GH_TOKEN" not in text
    assert "gh api" not in text
    assert "scripts/ios_release.py" not in text


# ------------------------------------------------------------------ permissions


def test_permissions_are_least_privilege_and_scoped_per_job():
    workflow = _workflow()
    assert workflow["permissions"] == {}
    jobs = workflow["jobs"]
    assert set(jobs) == {"plan", "archive-export-upload", "notify"}
    assert jobs["plan"]["permissions"] == {"contents": "read"}
    assert jobs["archive-export-upload"]["permissions"] == {"contents": "read"}
    assert jobs["notify"]["permissions"] == {
        "contents": "read",
        "issues": "write",
        "pull-requests": "read",
    }


def test_signing_environment_and_secrets_live_only_in_the_archive_job():
    jobs = _workflow()["jobs"]
    for name, job in jobs.items():
        text = yaml.safe_dump(job)
        if name == "archive-export-upload":
            assert job["environment"] == "ios-code-signing"
            assert "secrets." in text
        else:
            assert "environment" not in job, name
            assert "secrets." not in text, name


def test_the_workflow_never_writes_contents_or_tags():
    # ios-v* tag creation being admin-only is half of the signing boundary
    # (see the workflow header): a workflow-created tag can be dispatched on
    # and would reach the signing environment. Nothing here may create one.
    workflow = _workflow()
    for name, job in workflow["jobs"].items():
        assert job["permissions"].get("contents") == "read", name
        assert "actions" not in job["permissions"], name
    text = WORKFLOW.read_text(encoding="utf-8")
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    for forbidden in ("git/refs", "git tag", "git push", "contents: write", "actions: write"):
        assert forbidden not in code, forbidden


def test_header_states_the_signing_boundary_and_the_bypass_trap():
    header = WORKFLOW.read_text(encoding="utf-8").split("\non:\n", 1)[0]
    assert "(a) a merge to `main`, gated by the code-owner `main` ruleset (#478)" in header
    assert "repository admins create, move or delete" in header
    assert "a bypass actor of ANY kind on that ruleset" in header
    assert "never creates, moves or deletes a tag" in header


def test_plan_and_notify_check_out_full_history():
    # The version count, the manual-tag checks (refs/remotes/origin/main) and
    # the notify range all need every commit and tag.
    jobs = _workflow()["jobs"]
    for name in ("plan", "notify"):
        checkout = next(s for s in jobs[name]["steps"] if "actions/checkout@" in s.get("uses", ""))
        assert checkout["with"]["fetch-depth"] == 0, name
        assert checkout["with"]["persist-credentials"] is False, name


def test_notify_job_cannot_fail_the_release_and_never_holds_signing():
    notify = _workflow()["jobs"]["notify"]
    assert notify["continue-on-error"] is True
    assert notify["runs-on"] == "ubuntu-latest"
    for step in notify["steps"]:
        if "uses" in step:
            assert step["with"]["persist-credentials"] is False


def test_every_job_is_pinned_to_this_repository():
    for name, job in _workflow()["jobs"].items():
        assert "github.repository == 'poopaskoopa/wattracker'" in job["if"], name


@pytest.mark.parametrize(
    ("job", "event", "ref", "uploaded", "runs"),
    [
        ("notify", "push", "refs/heads/main", "true", True),
        ("notify", "push", "refs/tags/ios-v0.2.0", "true", True),
        ("notify", "push", "refs/heads/main", "", False),
        ("notify", "workflow_dispatch", "refs/heads/main", "true", False),
    ],
)
def test_follow_up_jobs_run_only_after_a_successful_upload(job, event, ref, uploaded, runs):
    condition = _workflow()["jobs"][job]["if"]
    assert condition.startswith("!cancelled()")
    context = {
        "github.repository": REPO,
        "github.event_name": event,
        "github.ref": ref,
        "needs.archive-export-upload.outputs.uploaded": uploaded,
    }
    assert _evaluate(condition, context) is runs


def test_every_needs_and_steps_reference_resolves():
    # actionlint is not available locally; this is the part of it that a typo
    # here would defeat silently (an unknown output evaluates to '').
    jobs = _workflow()["jobs"]
    for name, job in jobs.items():
        needs = job.get("needs", [])
        needs = [needs] if isinstance(needs, str) else needs
        ids = {step["id"] for step in job.get("steps", []) if "id" in step}
        text = yaml.safe_dump(job)
        for target, output in re.findall(r"needs\.([A-Za-z0-9_-]+)\.outputs\.([A-Za-z0-9_]+)", text):
            assert target in needs, (name, target)
            assert output in jobs[target]["outputs"], (name, target, output)
        for step_id in re.findall(r"steps\.([A-Za-z0-9_-]+)\.outputs", text):
            assert step_id in ids, (name, step_id)
        for value in job.get("outputs", {}).values():
            assert re.fullmatch(r"\$\{\{ steps\.[a-z_-]+\.outputs\.[a-z_]+ \}\}", value)
    plan_outputs = set(rel.Plan(True, "main", "0.1.10", "ios-v0.1.9", "").outputs())
    assert plan_outputs == set(jobs["plan"]["outputs"])


# ------------------------------------------------------------------ concurrency


def test_concurrency_never_cancels_in_progress_and_separates_tags_from_main():
    workflow = _workflow()
    concurrency = workflow["concurrency"]
    assert concurrency["cancel-in-progress"] is False
    assert concurrency["group"] == "ios-release-${{ github.event_name }}-${{ github.ref_type }}"
    for job in workflow["jobs"].values():
        assert "concurrency" not in job
    main = concurrency["group"].replace("${{ github.event_name }}", "push").replace(
        "${{ github.ref_type }}", "branch"
    )
    tag = concurrency["group"].replace("${{ github.event_name }}", "push").replace(
        "${{ github.ref_type }}", "tag"
    )
    dispatch = concurrency["group"].replace("${{ github.event_name }}", "workflow_dispatch").replace(
        "${{ github.ref_type }}", "branch"
    )
    assert len({main, tag, dispatch}) == 3


# ------------------------------------------------------------------ derive step


def _run_step(script: str, env: dict[str, str], tmp_path: Path, path_prefix: Path | None = None):
    environment = {
        "PATH": os.environ["PATH"] if path_prefix is None else f"{path_prefix}:{os.environ['PATH']}",
        "GITHUB_ENV": str(tmp_path / "env"),
        "GITHUB_OUTPUT": str(tmp_path / "output"),
        "RUNNER_TEMP": str(tmp_path),
        **env,
    }
    return subprocess.run(
        ["/bin/bash", "-c", script], env=environment, capture_output=True, text=True, check=False
    )


def _derive_script() -> str:
    return _step(_workflow()["jobs"]["archive-export-upload"], "Derive the version and build number")["run"]


@pytest.mark.parametrize(
    ("event", "mode", "version", "ok"),
    [
        ("push", "main", "0.1.10", True),
        ("push", "tag", "1.2", True),
        ("workflow_dispatch", "dispatch", "0.0.0", True),
        ("workflow_dispatch", "dispatch", "0.1.10", False),
        ("workflow_dispatch", "main", "0.0.0", False),
        ("push", "main", "", False),
        ("push", "main", "0.1.10; touch /tmp/x", False),
        ("push", "main", "1.2.3.4", False),
    ],
)
def test_derive_step_revalidates_the_planned_version(tmp_path, event, mode, version, ok):
    result = _run_step(
        _derive_script(),
        {
            "PLAN_VERSION": version,
            "PLAN_MODE": mode,
            "RUN_NUMBER": "41",
            "RUN_ATTEMPT": "2",
            "GITHUB_EVENT_NAME": event,
        },
        tmp_path,
    )
    assert (result.returncode == 0) is ok, result.stderr
    if ok:
        assert f"MARKETING_VERSION={version}\nBUILD_NUMBER=41.2\n" == (tmp_path / "env").read_text()
        assert (tmp_path / "output").read_text() == "build_number=41.2\n"


# ------------------------------------------------------------------ version rule


@pytest.mark.parametrize(
    ("tag", "parsed"),
    [
        ("ios-v0.1.9", (0, 1, 9)),
        ("ios-v0.2", (0, 2, 0)),
        ("ios-v1", (1, 0, 0)),
        ("ios-v0.1.9-rc1", None),
        ("ios-vfoo", None),
        ("v0.1.9", None),
        ("ios-v0.1.9.1", None),
        ("ios-v1234567890.0.0", None),
    ],
)
def test_parse_tag(tag, parsed):
    assert rel.parse_tag(tag) == parsed


def test_latest_tag_is_numeric_not_lexicographic():
    tags = ["ios-v0.1.9", "ios-v0.1.10", "ios-v0.1.2", "ios-vbogus", "v9.9.9"]
    assert rel.latest_tag(tags) == "ios-v0.1.10"
    assert rel.latest_tag(["nope"]) is None


@pytest.mark.parametrize(
    ("base", "count", "expected"),
    [
        ("ios-v0.1.9", 1, "0.1.10"),
        ("ios-v0.1.9", 0, "0.1.10"),
        ("ios-v0.1.9", 3, "0.1.12"),
        ("ios-v0.2", 1, "0.2.1"),
        ("ios-v1", 2, "1.0.2"),
    ],
)
def test_next_version(base, count, expected):
    assert rel.next_version(base, count) == expected


class GitRepo:
    def __init__(self, path: Path):
        self.path = path
        path.mkdir(parents=True)
        self("init", "-q", "-b", "main")

    def __call__(self, *args: str) -> str:
        return subprocess.run(
            [
                "git", "-C", str(self.path),
                "-c", "user.name=test", "-c", "user.email=test@example.invalid",
                "-c", "commit.gpgsign=false", "-c", "tag.gpgsign=false",
                "-c", "core.hooksPath=/dev/null",
                *args,
            ],
            check=True, capture_output=True, text=True,
        ).stdout

    def runner(self, args):
        return self(*args)

    def commit(self, path: str, message: str) -> str:
        target = self.path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as handle:
            handle.write(message + "\n")
        self("add", path)
        self("commit", "-q", "-m", message)
        return self("rev-parse", "HEAD").strip()

    def merge(self, branch_path: str, name: str) -> str:
        self("checkout", "-q", "-b", name)
        self.commit(branch_path, name)
        self("checkout", "-q", "main")
        self("merge", "-q", "--no-ff", "-m", f"Merge {name}", name)
        return self("rev-parse", "HEAD").strip()


APP_FILE = "ios/WatTracker/WatTracker/Screens/CalendarScreen.swift"
TEST_FILE = "ios/WatTracker/WatTrackerTests/CloudSessionTests.swift"


@pytest.fixture
def repo(tmp_path):
    git = GitRepo(tmp_path / "repo")
    git.commit(APP_FILE, "initial")
    git("tag", "ios-v0.1.9")
    return git


def test_plan_main_push_counts_first_parent_app_merges_since_the_latest_tag(repo):
    repo.merge(TEST_FILE, "tests-only")
    repo.merge("docs/ios-testflight.md", "docs-only")
    head = repo.merge(APP_FILE, "app-fix")
    result = rel.plan("push", "refs/heads/main", repo.runner)
    assert result.release is True
    assert result.mode == "main"
    assert result.version == "0.1.10"
    assert result.base_tag == "ios-v0.1.9"
    assert rel.app_commits(repo.runner, "ios-v0.1.9", head) == [head]


def test_plan_version_is_the_same_whether_or_not_the_previous_tag_was_written(repo):
    first = repo.merge(APP_FILE, "fix-one")
    assert rel.plan("push", "refs/heads/main", repo.runner).version == "0.1.10"
    repo.merge(APP_FILE, "fix-two")
    untagged = rel.plan("push", "refs/heads/main", repo.runner)
    repo("tag", "ios-v0.1.10", first)
    tagged = rel.plan("push", "refs/heads/main", repo.runner)
    assert untagged.version == tagged.version == "0.1.11"
    assert untagged.base_tag == "ios-v0.1.9"
    assert tagged.base_tag == "ios-v0.1.10"


def test_plan_skips_a_main_push_whose_commit_already_carries_a_release_tag(repo):
    head = repo.merge(APP_FILE, "fix")
    repo("tag", "-a", "-m", "release", "ios-v0.1.10", head)
    result = rel.plan("push", "refs/heads/main", repo.runner)
    assert result.release is False
    assert result.outputs()["release"] == "false"


def test_plan_follows_a_manual_minor_bump(repo):
    repo.merge(APP_FILE, "feature")
    repo("tag", "ios-v0.2.0")
    repo.merge(APP_FILE, "fix")
    assert rel.plan("push", "refs/heads/main", repo.runner).version == "0.2.1"


def test_plan_tag_push_ships_the_tag_version_and_reports_the_previous_tag(repo):
    repo.merge(APP_FILE, "fix")
    repo("update-ref", rel.MAIN_REF, "HEAD")
    repo("tag", "ios-v0.3")
    result = rel.plan("push", "refs/tags/ios-v0.3", repo.runner)
    assert (result.release, result.mode, result.version, result.base_tag) == (
        True, "tag", "0.3", "ios-v0.1.9",
    )


@pytest.mark.parametrize("ref", ["refs/tags/ios-v0.1.9-beta", "refs/tags/ios-v1.2.3.4", "refs/heads/feature"])
def test_plan_refuses_malformed_tags_and_other_branches(repo, ref):
    with pytest.raises(rel.PlanError):
        rel.plan("push", ref, repo.runner)


def test_plan_refuses_a_main_push_with_no_tag_to_count_from(tmp_path):
    git = GitRepo(tmp_path / "bare")
    git.commit(APP_FILE, "initial")
    with pytest.raises(rel.PlanError, match="No ios-v"):
        rel.plan("push", "refs/heads/main", git.runner)


def _ship_three_untagged_merges(repo):
    """ios-v0.1.9, then three app merges the automatic release shipped as
    0.1.10, 0.1.11 and 0.1.12 without any tag being written."""
    commits = [repo.merge(APP_FILE, f"fix-{n}") for n in range(3)]
    assert rel.plan("push", "refs/heads/main", repo.runner).version == "0.1.12"
    repo("update-ref", rel.MAIN_REF, commits[-1])
    return commits


@pytest.mark.parametrize("tag", ["ios-v0.1.10", "ios-v0.1.12", "ios-v0.1", "ios-v0.0.99"])
def test_plan_refuses_a_manual_tag_at_or_below_what_already_shipped(repo, tag):
    _ship_three_untagged_merges(repo)
    repo("tag", tag)
    with pytest.raises(rel.PlanError, match=r"not above 0\.1\.12"):
        rel.plan("push", f"refs/tags/{tag}", repo.runner)


@pytest.mark.parametrize("tag", ["ios-v0.1.13", "ios-v0.2", "ios-v1.0.0"])
def test_plan_accepts_a_manual_tag_above_what_already_shipped(repo, tag):
    _ship_three_untagged_merges(repo)
    repo("tag", tag)
    result = rel.plan("push", f"refs/tags/{tag}", repo.runner)
    assert (result.release, result.mode, result.version) == (True, "tag", tag[len("ios-v"):])
    assert result.base_tag == "ios-v0.1.9"


@pytest.mark.parametrize("tag", ["ios-v0.1.11", "ios-v0.4.0"])
def test_plan_refuses_a_tag_main_has_already_shipped_past(repo, tag):
    first, *_ = _ship_three_untagged_merges(repo)
    # Two app merges landed on main after `first`, and shipped as 0.1.11 and
    # 0.1.12. Even a version above everything is refused: shipping `first`
    # now would put an older build on top of a newer one.
    repo("tag", tag, first)
    with pytest.raises(rel.PlanError, match=r"behind main: 2 app change"):
        rel.plan("push", f"refs/tags/{tag}", repo.runner, head=first)


def test_rerunning_an_old_tag_run_after_main_shipped_from_it_is_refused(repo):
    tagged = repo.merge(APP_FILE, "feature")
    repo("tag", "ios-v0.4.0", tagged)
    repo("update-ref", rel.MAIN_REF, tagged)
    assert rel.plan("push", "refs/tags/ios-v0.4.0", repo.runner, head=tagged).version == "0.4.0"
    repo.merge(APP_FILE, "fix")  # main auto-ships 0.4.1 from the tag
    assert rel.plan("push", "refs/heads/main", repo.runner).version == "0.4.1"
    repo("update-ref", rel.MAIN_REF, "HEAD")
    with pytest.raises(rel.PlanError, match="behind main"):
        rel.plan("push", "refs/tags/ios-v0.4.0", repo.runner, head=tagged)


def test_plan_allows_a_tag_followed_on_main_only_by_non_app_changes(repo):
    tagged = repo.merge(APP_FILE, "feature")
    repo.merge("docs/ios-testflight.md", "docs-only")
    repo.merge(TEST_FILE, "tests-only")
    repo("update-ref", rel.MAIN_REF, "HEAD")
    repo("tag", "ios-v0.1.11", tagged)
    assert rel.plan("push", "refs/tags/ios-v0.1.11", repo.runner, head=tagged).version == "0.1.11"


def test_plan_refuses_a_tag_below_a_newer_admin_tag_on_main(repo):
    tagged = repo.merge(APP_FILE, "feature")
    repo.merge("docs/ios-testflight.md", "docs-only")
    repo("tag", "ios-v0.2.0")
    repo("update-ref", rel.MAIN_REF, "HEAD")
    repo("tag", "ios-v0.1.20", tagged)
    with pytest.raises(rel.PlanError, match=r"not above 0\.2\.0"):
        rel.plan("push", "refs/tags/ios-v0.1.20", repo.runner, head=tagged)


def test_plan_refuses_a_tag_on_a_commit_that_is_not_on_main(repo):
    main_tip = repo("rev-parse", "HEAD").strip()
    repo("checkout", "-q", "-b", "hotfix")
    hotfix = repo.commit(APP_FILE, "hotfix")
    repo("tag", "ios-v0.2.0", hotfix)
    repo("update-ref", rel.MAIN_REF, main_tip)
    with pytest.raises(rel.PlanError, match="not on main"):
        rel.plan("push", "refs/tags/ios-v0.2.0", repo.runner, head=hotfix)


def test_plan_allows_reshipping_an_unchanged_commit_with_the_next_patch(repo):
    repo.merge("docs/ios-testflight.md", "docs-only")
    repo("update-ref", rel.MAIN_REF, "HEAD")
    repo("tag", "ios-v0.1.10")
    assert rel.plan("push", "refs/tags/ios-v0.1.10", repo.runner).version == "0.1.10"


def test_plan_fails_closed_when_main_is_not_in_the_checkout(repo):
    repo.merge(APP_FILE, "fix")
    repo("tag", "ios-v9.0.0")
    with pytest.raises(rel.PlanError, match="is not in this checkout"):
        rel.plan("push", "refs/tags/ios-v9.0.0", repo.runner)


def test_versions_keep_rising_after_an_accepted_manual_tag(repo):
    _ship_three_untagged_merges(repo)
    repo("tag", "ios-v0.1.13")
    repo.merge(APP_FILE, "after-tag")
    assert rel.plan("push", "refs/heads/main", repo.runner).version == "0.1.14"


def test_plan_dispatch_is_the_diagnostic_version(repo):
    result = rel.plan("workflow_dispatch", "refs/heads/main", repo.runner)
    assert (result.release, result.mode, result.version) == (True, "dispatch", "0.0.0")


def test_plan_cli_writes_github_outputs(repo, tmp_path, monkeypatch):
    repo.merge(APP_FILE, "fix")
    output = tmp_path / "out"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.chdir(repo.path)
    assert rel.main(["plan", "--event", "push", "--ref", "refs/heads/main"]) == 0
    assert output.read_text() == "release=true\nmode=main\nversion=0.1.10\nbase_tag=ios-v0.1.9\n"


class FakeClient:
    def __init__(self, rest=None, graphql=None):
        self._rest = rest or {}
        self._graphql = graphql or {}
        self.posts: list[tuple[str, dict]] = []

    def rest(self, method, path, body=None):
        if method == "POST":
            if path in self._rest and isinstance(self._rest[path], Exception):
                raise self._rest[path]
            self.posts.append((path, body))
            return {}
        value = self._rest.get(path, [])
        if isinstance(value, Exception):
            raise value
        return value

    def graphql(self, query, variables):
        return self._graphql[variables["number"]]


# ------------------------------------------------------------------ notify


C1, C2 = "a" * 40, "b" * 40


def _closing(*numbers, repo=REPO):
    return {"repository": {"pullRequest": {"closingIssuesReferences": {"nodes": [
        {"number": n, "repository": {"nameWithOwner": repo}} for n in numbers
    ]}}}}


def _notify_client(**overrides):
    rest = {
        f"/repos/{REPO}/commits/{C1}/pulls": [
            {"number": 487, "merged_at": "2026-11-01T00:00:00Z", "merge_commit_sha": C1},
            {"number": 400, "merged_at": "2026-10-01T00:00:00Z", "merge_commit_sha": "c" * 40},
        ],
        f"/repos/{REPO}/commits/{C2}/pulls": [
            {"number": 490, "merged_at": "2026-11-02T00:00:00Z", "merge_commit_sha": C2},
        ],
        f"/repos/{REPO}/issues/479/comments?per_page=100&page=1": [],
        f"/repos/{REPO}/issues/486/comments?per_page=100&page=1": [],
    }
    rest.update(overrides)
    cross_repo = _closing(479, 486)
    cross_repo["repository"]["pullRequest"]["closingIssuesReferences"]["nodes"].append(
        {"number": 7, "repository": {"nameWithOwner": "someone/else"}}
    )
    graphql = {487: _closing(479), 490: cross_repo, 400: _closing(1)}
    return FakeClient(rest=rest, graphql=graphql)


def _git_listing(*commits):
    def runner(args):
        assert args[:2] == ["rev-list", "--first-parent"]
        assert list(args[-len(rel.APP_PATHS):]) == list(rel.APP_PATHS)
        return "".join(f"{c}\n" for c in commits)
    return runner


def test_notify_comments_once_per_closed_issue_with_version_and_build():
    client = _notify_client()
    code = rel.notify(client, _git_listing(C2, C1), REPO, "ios-v0.1.9", C2, "0.1.10", "57.1")
    assert code == 0
    bodies = dict(client.posts)
    assert set(bodies) == {f"/repos/{REPO}/issues/479/comments", f"/repos/{REPO}/issues/486/comments"}
    body_479 = bodies[f"/repos/{REPO}/issues/479/comments"]["body"]
    assert body_479.startswith("Shipped in TestFlight 0.1.10 (build 57.1) via #487, #490.")
    assert rel.marker(487) in body_479 and rel.marker(490) in body_479
    assert "#400" not in body_479  # not the merge commit's own PR


def test_notify_skips_what_the_workflow_already_said_but_not_what_a_human_pasted():
    previous = rel.comment_body("0.1.10", "57.1", [487])
    client = _notify_client(**{
        f"/repos/{REPO}/issues/479/comments?per_page=100&page=1": [
            {"user": {"login": "github-actions[bot]"}, "body": previous},
        ],
        f"/repos/{REPO}/issues/486/comments?per_page=100&page=1": [
            {"user": {"login": "drive-by"}, "body": rel.comment_body("9", "9", [490])},
        ],
    })
    assert rel.notify(client, _git_listing(C2, C1), REPO, "ios-v0.1.9", C2, "0.1.11", "58.1") == 0
    bodies = {path: body["body"] for path, body in client.posts}
    assert bodies[f"/repos/{REPO}/issues/479/comments"].startswith(
        "Shipped in TestFlight 0.1.11 (build 58.1) via #490."
    )
    assert f"/repos/{REPO}/issues/486/comments" in bodies


class StatefulIssues(FakeClient):
    """One PR per merge commit, each closing its own issue; comments persist."""

    def __init__(self):
        super().__init__()
        self.comments: dict[int, list[dict]] = {}

    def rest(self, method, path, body=None):
        match = re.fullmatch(rf"/repos/{REPO}/commits/([0-9a-f]{{40}})/pulls", path)
        if match:
            sha = match.group(1)
            return [{"number": int(sha, 16) % 10_000, "merged_at": "x", "merge_commit_sha": sha}]
        match = re.fullmatch(rf"/repos/{REPO}/issues/(\d+)/comments(\?per_page=100&page=(\d+))?", path)
        if match and method == "GET":
            return self.comments.get(int(match.group(1)), []) if match.group(3) == "1" else []
        if match and method == "POST":
            self.comments.setdefault(int(match.group(1)), []).append(
                {"user": {"login": "github-actions[bot]"}, "body": body["body"]}
            )
            return {}
        raise AssertionError(path)

    def graphql(self, query, variables):
        return _closing(variables["number"] + 50_000)


def test_overlapping_ranges_comment_exactly_once_per_shipped_pr_even_past_30():
    # No tag is ever written, so every release's range starts at the same
    # admin tag and grows. 40 releases: each ships one new merge, and some
    # ship two (a queued run GitHub superseded).
    client = StatefulIssues()
    shipped: list[str] = []
    for release in range(40):
        shipped.insert(0, f"{release + 1:040x}")
        if release % 7 == 3:
            shipped.insert(0, f"{release + 1001:040x}")
        version = f"0.1.{10 + release}"
        assert rel.notify(client, _git_listing(*shipped), REPO, "ios-v0.1.9", shipped[0],
                          version, f"{release}.1") == 0
        for issue, comments in client.comments.items():
            assert len(comments) == 1, (release, issue)
    assert len(client.comments) == len(shipped)
    first_release = {
        int(sha, 16) % 10_000 + 50_000: f"0.1.{10 + (int(sha, 16) % 1000) - 1}"
        for sha in shipped
    }
    for issue, comments in client.comments.items():
        assert comments[0]["body"].startswith(f"Shipped in TestFlight {first_release[issue]} ")


def test_notify_looks_at_exactly_the_newest_30_app_commits():
    commits = [f"{n:040x}" for n in range(45, 0, -1)]  # newest first
    client = StatefulIssues()
    looked_up: list[str] = []
    original = client.rest

    def recording(method, path, body=None):
        match = re.fullmatch(rf"/repos/{REPO}/commits/([0-9a-f]{{40}})/pulls", path)
        if match:
            looked_up.append(match.group(1))
        return original(method, path, body)

    client.rest = recording
    assert rel.notify(client, _git_listing(*commits), REPO, "ios-v0.1.9", commits[0], "0.1.54", "9.1") == 0
    assert looked_up == commits[:30]
    assert len(client.comments) == 30


def test_notify_keeps_going_after_a_failure_and_reports_it():
    client = _notify_client(**{f"/repos/{REPO}/issues/479/comments": RuntimeError("403")})
    code = rel.notify(client, _git_listing(C2, C1), REPO, "ios-v0.1.9", C2, "0.1.10", "57.1")
    assert code == 1
    assert [path for path, _ in client.posts] == [f"/repos/{REPO}/issues/486/comments"]


@pytest.mark.parametrize(
    ("repo", "base", "head", "version", "build"),
    [
        ("bad repo", "ios-v0.1.9", C1, "0.1.10", "1.1"),
        (REPO, "ios-v0.1.9", "HEAD", "0.1.10", "1.1"),
        (REPO, "ios-v0.1.9", C1, "0.1.10\n@everyone", "1.1"),
        (REPO, "ios-v0.1.9", C1, "0.1.10", "1.1 <img>"),
        (REPO, "--all", C1, "0.1.10", "1.1"),
    ],
)
def test_notify_rejects_unexpected_inputs(repo, base, head, version, build):
    with pytest.raises(ValueError):
        rel.notify(FakeClient(), _git_listing(), repo, base, head, version, build)


def test_github_client_refuses_plain_http_and_an_empty_token():
    with pytest.raises(ValueError):
        rel.GitHub("token", "http://api.github.com", "https://api.github.com/graphql")
    with pytest.raises(ValueError):
        rel.GitHub("", "https://api.github.com", "https://api.github.com/graphql")


# ------------------------------------------------------------------ docs


def test_docs_describe_the_automatic_release_and_the_owner_settings():
    docs = Path("docs/ios-testflight.md").read_text(encoding="utf-8")
    assert "merge to `main`" in docs
    assert "## Settings the automatic release needs" in docs
    start = docs.index("## Settings the automatic release needs")
    settings = docs[start:docs.index("\n## ", start + 1)]
    first = settings.index("\n1. **Land #478")
    second = settings.index("\n2. **Remove the required reviewer")
    assert first < second
    assert "\n3. " not in settings
    assert "Do not add a bypass actor of any kind" in " ".join(settings.split())
    assert "15368" not in docs
    assert "**Only re-run the newest release run.**" in docs
    agents = Path("AGENTS.md").read_text(encoding="utf-8")
    finishing = agents[agents.index("### Finishing an item"):agents.index("## Scope")]
    assert "TestFlight automatically on merge" in finishing
