"""The release-sensitive paths stay owned by the repository owner.

`.github/CODEOWNERS` is one half of the merge-time gate that replaced the
manual approval on the `ios-code-signing` environment; the other half is a
branch ruleset requiring code-owner review on `main`. If a path drops out of
CODEOWNERS - a typo, a reordering, a broader ownerless pattern added below it
- GitHub silently stops asking for owner review and the gate is open.

This test is a tripwire for that kind of accident, not the control. The
control is the ruleset plus the file as it stands on `main`. The matcher below
implements only the subset of CODEOWNERS syntax the file uses (a leading `/`
anchor, a trailing `/` for a directory, and `*` within one path segment) and
rejects anything else, so a new kind of pattern forces this test to be
extended rather than misread.
"""

from fnmatch import fnmatchcase
from pathlib import Path
import re

import pytest


ROOT = Path(__file__).parents[1]
CODEOWNERS = ROOT / ".github" / "CODEOWNERS"
OWNER = "@poopaskoopa"

OWNED = [
    ".github/CODEOWNERS",
    ".github/workflows/ios-release.yml",
    ".github/workflows/cloud.yml",
    ".github/workflows/new-workflow.yml",
    ".github/certs/AppleWWDRCAG3.cer",
    ".github/pull_request_template.md",
    "CODEOWNERS",
    "docs/CODEOWNERS",
    "ios/WatTracker/WatTracker.xcodeproj/project.pbxproj",
    "ios/WatTracker/Config/Release.xcconfig",
    "ios/README.md",
    "scripts/ios_release_secrets.py",
    "scripts/ios_distribution_cert.py",
    "scripts/deploy_cloud.py",
    "scripts/hooks/pre-push",
    "Dockerfile",
    "Dockerfile.cloud",
    "AGENTS.md",
]

# Ordinary contributions must not be routed through owner review.
UNOWNED = [
    "android/app/build.gradle.kts",
    "android/AGENTS.md",
    "wattracker/server.py",
    "tests/test_codeowners.py",
    "tests/vectors/pairing_code_v1.json",
    "docs/mobile-parity.md",
    "README.md",
    "docker-compose.yml",
]


def _rules():
    rules = []
    for lineno, raw in enumerate(CODEOWNERS.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        pattern, *owners = line.split()
        assert re.fullmatch(r"/[A-Za-z0-9._*/-]+", pattern), (
            f"line {lineno}: {pattern!r} uses syntax this test does not model"
        )
        assert "**" not in pattern and "*" not in pattern.rstrip("/").rsplit("/", 1)[0], (
            f"line {lineno}: {pattern!r} - only a final-segment `*` is modelled"
        )
        for owner in owners:
            assert re.fullmatch(r"@[A-Za-z0-9-]+(/[A-Za-z0-9._-]+)?", owner), (
                f"line {lineno}: {owner!r} is not a user or team handle"
            )
        rules.append((pattern, owners))
    return rules


def _matches(pattern, path):
    segments = pattern.strip("/").split("/")
    parts = path.split("/")
    if pattern.endswith("/"):
        # A directory pattern owns everything beneath it.
        return len(parts) > len(segments) and all(
            fnmatchcase(p, s) for p, s in zip(parts, segments)
        )
    return len(parts) == len(segments) and all(
        fnmatchcase(p, s) for p, s in zip(parts, segments)
    )


def _owners(path):
    owners = []
    for pattern, rule_owners in _rules():
        if _matches(pattern, path):
            owners = rule_owners  # last match wins
    return owners


def test_codeowners_lives_where_github_looks_first():
    assert CODEOWNERS.is_file()
    assert not (ROOT / "CODEOWNERS").exists()
    assert not (ROOT / "docs" / "CODEOWNERS").exists()


@pytest.mark.parametrize("path", OWNED)
def test_release_sensitive_path_is_owned(path):
    assert _owners(path) == [OWNER]


@pytest.mark.parametrize("path", UNOWNED)
def test_ordinary_path_needs_no_owner_review(path):
    assert _owners(path) == []


def test_every_workflow_is_owned():
    workflows = sorted((ROOT / ".github" / "workflows").glob("*.yml"))
    assert workflows
    for path in workflows:
        assert _owners(path.relative_to(ROOT).as_posix()) == [OWNER], path
