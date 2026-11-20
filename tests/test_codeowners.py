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

Within that subset it follows GitHub's gitignore-style semantics: a pattern
without a trailing `/` matches a file *or a directory* at that path, and a
matched directory covers everything beneath it. So `/ios` owns (or, with no
owner, disowns) the whole `ios/` tree, exactly like `/ios/`; the trailing `/`
only stops the pattern from matching a plain file named `ios`.
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


def _rules(text=None):
    if text is None:
        text = CODEOWNERS.read_text(encoding="utf-8")
    rules = []
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        pattern, *owners = line.split()
        assert re.fullmatch(r"/[A-Za-z0-9._*/-]+", pattern), (
            f"line {lineno}: {pattern!r} uses syntax this test does not model"
        )
        assert "//" not in pattern and pattern.strip("/"), (
            f"line {lineno}: {pattern!r} has an empty path segment"
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
    """Whether an anchored CODEOWNERS pattern covers the file at `path`.

    gitignore semantics: the pattern's segments must match a leading run of the
    path's segments. If they match the whole path, the pattern names the file
    itself; if they match a proper prefix, the pattern names a directory the
    file sits in, which covers it. A trailing `/` restricts the pattern to
    directories, so it never names the file itself.
    """
    segments = pattern.strip("/").split("/")
    parts = path.split("/")
    if pattern.endswith("/"):
        if len(parts) <= len(segments):
            return False
    elif len(parts) < len(segments):
        return False
    return all(fnmatchcase(p, s) for p, s in zip(parts, segments))


def _owners(path, rules=None):
    owners = []
    for pattern, rule_owners in _rules() if rules is None else rules:
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


@pytest.mark.parametrize(
    "pattern, path, expected",
    [
        # No trailing slash: the path itself, and everything beneath it when
        # it is a directory.
        ("/ios", "ios", True),
        ("/ios", "ios/README.md", True),
        ("/ios", "ios/WatTracker/Config/Release.xcconfig", True),
        ("/.github", ".github/CODEOWNERS", True),
        ("/.github", ".github/workflows/ios-release.yml", True),
        ("/ios", "iosapp/README.md", False),
        ("/ios", "ios.md", False),
        ("/ios", "android/ios/README.md", False),
        ("/ios", "README.md", False),
        # Trailing slash: descendants only, never a file of that name.
        ("/ios/", "ios/README.md", True),
        ("/ios/", "ios/WatTracker/Config/Release.xcconfig", True),
        ("/ios/", "ios", False),
        ("/ios/", "iosapp/README.md", False),
        # A file-shaped pattern is still exact for files, and anchored.
        ("/AGENTS.md", "AGENTS.md", True),
        ("/AGENTS.md", "android/AGENTS.md", False),
        ("/AGENTS.md", "AGENTS.md.bak", False),
        ("/docs/CODEOWNERS", "docs/CODEOWNERS", True),
        ("/docs/CODEOWNERS", "docs/mobile-parity.md", False),
        # `*` stays within one segment.
        ("/Dockerfile*", "Dockerfile", True),
        ("/Dockerfile*", "Dockerfile.cloud", True),
        ("/Dockerfile*", "docker-compose.yml", False),
        ("/Dockerfile*", "scripts/Dockerfile", False),
        # A deeper directory pattern covers its own subtree only.
        ("/.github/workflows", ".github/workflows/cloud.yml", True),
        ("/.github/workflows", ".github/CODEOWNERS", False),
    ],
)
def test_matcher_follows_gitignore_directory_semantics(pattern, path, expected):
    assert _matches(pattern, path) is expected


@pytest.mark.parametrize("pattern", ["/ios", "/ios/", "/.github", "/.github/"])
def test_appended_ownerless_directory_rule_is_caught(pattern):
    # GitHub resolves the last match, so an ownerless directory rule at the end
    # strips ownership from the whole tree whether or not it ends in `/`.
    text = CODEOWNERS.read_text(encoding="utf-8") + f"\n{pattern}\n"
    rules = _rules(text)
    disowned = [path for path in OWNED if _owners(path, rules) != [OWNER]]
    tree = pattern.strip("/") + "/"
    assert disowned and all(path.startswith(tree) for path in disowned), disowned
    assert all(_owners(path, rules) == [OWNER] for path in OWNED if not path.startswith(tree))


def test_last_match_wins_in_both_directions():
    rules = _rules("/ios @a\n/ios/WatTracker\n")
    assert _owners("ios/README.md", rules) == ["@a"]
    assert _owners("ios/WatTracker/Config/Release.xcconfig", rules) == []
    rules = _rules("/ios\n/ios/WatTracker/ @a\n")
    assert _owners("ios/README.md", rules) == []
    assert _owners("ios/WatTracker/Config/Release.xcconfig", rules) == ["@a"]


@pytest.mark.parametrize("pattern", ["//", "/ios//WatTracker", "ios/", "/**/ios", "/*/ios"])
def test_unmodelled_pattern_is_rejected(pattern):
    with pytest.raises(AssertionError):
        _rules(f"{pattern} @a\n")
