#!/usr/bin/env python3
"""Planning and follow-up helpers for .github/workflows/ios-release.yml.

Standard library only: this runs on hosted ubuntu runners with whatever
``python3`` the image ships, before anything is installed.

Two subcommands, one per job that needs a decision made in code rather than
in a YAML expression. Neither runs in the job that holds the signing secrets.

``plan``        (the ``plan`` job, contents: read)
    Decides whether this run releases at all and which marketing version it
    carries, and refuses a manual tag that would not be above what has
    already shipped. See ``plan()`` for the rules.

``notify``      (the ``notify`` job, issues: write)
    Comments "Shipped in TestFlight <version> (build <n>)" on each issue the
    shipped pull requests closed.

THE VERSION RULE, and why it is not "latest tag + 1".

    version = latest ios-v* tag reachable from HEAD, with its patch component
              increased by the number of first-parent commits since that tag
              that touch the app (minimum 1).

The workflow never creates tags: ``ios-v*`` tag creation is admin-only (the
"iOS release tag policy" ruleset), and that restriction is part of the signing
boundary. So the latest tag is whichever release an admin last tagged by hand,
and automatic releases count forward from it: with ios-v0.1.9 the latest tag,
the next three app merges ship 0.1.10, 0.1.11 and 0.1.12. On ``main`` every
pull request lands as one first-parent merge commit, so that is "+1" per
merge. The rule is a pure function of the commit, so re-running a run
recomputes the same version; a number can be skipped when two merges ship as
one build, which is accurate, because that version never shipped.

Because the tags lag behind what has shipped, a manual tag is refused unless
its commit is on main with no app change after it, and its version is
strictly above ``shipped_version()`` - the highest version the automatic
release can already have shipped (``check_manual_tag``). Without that, a
hand-pushed ios-v0.1.10 after 0.1.12 shipped would ship 0.1.10 again and make
the next merge ship 0.1.11.

The build number (``run_number.run_attempt``) is what makes every upload
unique to App Store Connect; it is set in the workflow, not here.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.request
from dataclasses import dataclass
from typing import Callable, Iterable, Sequence

TAG_PREFIX = "ios-v"

# Everything the App Store archive compiles, links, bundles, or signs with,
# derived from the WatTracker target in project.pbxproj (its Sources and
# Resources phases, its INFOPLIST_FILE, its baseConfigurationReference
# xcconfigs) plus the export options the .ipa is re-signed with.
#
# NOT here, deliberately: WatTrackerTests/ (test target only), tests/vectors/
# (bundled into the test target only), docs, and the workflow itself - a
# workflow change can be exercised with a non-uploading dispatch run. tests/test_ios_release_workflow.py asserts this tuple matches
# the workflow's `paths:` filter and covers every file the app target names.
APP_PATHS: tuple[str, ...] = (
    "ios/WatTracker/WatTracker",
    "ios/WatTracker/WatTracker.xcodeproj",
    "ios/WatTracker/Config",
    "ios/WatTracker/ExportOptions.plist",
)
APP_FILES = frozenset({"ios/WatTracker/ExportOptions.plist"})

# Tag names are pushed by people; bound every component so a silly tag cannot
# produce a version App Store Connect will reject after a full archive.
_TAG_RE = re.compile(r"^ios-v(\d{1,9})(?:\.(\d{1,9}))?(?:\.(\d{1,9}))?$")
_VERSION_RE = re.compile(r"^\d{1,9}(?:\.\d{1,9}){0,2}$")
_BUILD_RE = re.compile(r"^\d{1,9}(?:\.\d{1,9}){0,2}$")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")

DIAGNOSTIC_VERSION = "0.0.0"
MAX_NOTIFY_COMMITS = 30
NOTIFY_LOGIN = "github-actions[bot]"
MARKER_PREFIX = "<!-- wattracker-testflight-shipped pr=#"


class PlanError(RuntimeError):
    pass


# ---------------------------------------------------------------- versions


def workflow_path_filters() -> list[str]:
    """The `on.push.paths` globs the workflow must carry, in order."""
    return [p if p in APP_FILES else f"{p}/**" for p in APP_PATHS]


def touches_app(path: str) -> bool:
    for root in APP_PATHS:
        if root in APP_FILES:
            if path == root:
                return True
        elif path.startswith(root + "/"):
            return True
    return False


def parse_tag(tag: str) -> tuple[int, int, int] | None:
    match = _TAG_RE.match(tag)
    if not match:
        return None
    major, minor, patch = match.groups()
    return int(major), int(minor or 0), int(patch or 0)


def tag_version_string(tag: str) -> str:
    """The marketing version a manually pushed tag names, verbatim."""
    if parse_tag(tag) is None:
        raise PlanError(
            f"Tag '{tag}' does not carry a 1-3 component numeric version"
        )
    return tag[len(TAG_PREFIX):]


def latest_tag(tags: Iterable[str]) -> str | None:
    best: tuple[tuple[int, int, int], str] | None = None
    for tag in tags:
        version = parse_tag(tag)
        if version is None:
            continue
        if best is None or version > best[0]:
            best = (version, tag)
    return None if best is None else best[1]


def next_version(base_tag: str, app_commits: int) -> str:
    version = parse_tag(base_tag)
    if version is None:
        raise PlanError(f"'{base_tag}' is not a release tag")
    major, minor, patch = version
    return f"{major}.{minor}.{patch + max(app_commits, 1)}"


# ---------------------------------------------------------------- git

GitRunner = Callable[[Sequence[str]], str]


def run_git(args: Sequence[str]) -> str:
    return subprocess.run(
        ["git", *args], check=True, capture_output=True, text=True
    ).stdout


def _lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


def tags_at(git: GitRunner, rev: str) -> list[str]:
    return [
        t for t in _lines(git(["tag", "--points-at", rev, "--list", f"{TAG_PREFIX}*"]))
        if parse_tag(t) is not None
    ]


def base_tag_for(git: GitRunner, rev: str) -> str | None:
    """The highest release tag reachable from rev, not counting tags on rev."""
    at_rev = set(tags_at(git, rev))
    merged = _lines(git(["tag", "--merged", rev, "--list", f"{TAG_PREFIX}*"]))
    return latest_tag(t for t in merged if t not in at_rev)


def app_commits(git: GitRunner, base: str, head: str) -> list[str]:
    return _lines(
        git(["rev-list", "--first-parent", f"{base}..{head}", "--", *APP_PATHS])
    )


MAIN_REF = "refs/remotes/origin/main"


def ref_exists(git: GitRunner, ref: str) -> bool:
    try:
        git(["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"])
    except subprocess.CalledProcessError:
        return False
    return True


def shipped_version(git: GitRunner, rev: str, exclude: str) -> tuple[int, int, int] | None:
    """The highest version the automatic release can have shipped at rev.

    The latest release tag reachable from rev (other than `exclude`, the tag
    being judged) plus every first-parent app commit since it, each of which
    a merge to main would have shipped as the next patch. No minimum of one
    here: with no app commit since the tag, the tag's own version is the last
    one shipped. None when no release tag is reachable at all.
    """
    merged = _lines(git(["tag", "--merged", rev, "--list", f"{TAG_PREFIX}*"]))
    base = latest_tag(t for t in merged if t != exclude)
    if base is None:
        return None
    major, minor, patch = parse_tag(base)
    return major, minor, patch + len(app_commits(git, base, rev))


def _fmt(version: tuple[int, int, int]) -> str:
    return ".".join(str(part) for part in version)


# ---------------------------------------------------------------- plan


def is_ancestor(git: GitRunner, ancestor: str, descendant: str) -> bool:
    try:
        git(["merge-base", "--is-ancestor", ancestor, descendant])
    except subprocess.CalledProcessError:
        return False
    return True


def check_manual_tag(git: GitRunner, tag: str, head: str) -> tuple[int, int, int] | None:
    """Refuse a manual tag that would ship out of order. Returns the floor.

    1. The tagged commit must be on main. A tag on an unmerged commit would
       ship a version the automatic release never counts from, so main could
       later ship a lower one (ios-v0.2.0 on a hotfix, then 0.1.13 from main).
    2. No app change may have landed on main's first-parent line after it.
       If one has, the automatic release has already counted past this
       commit, and shipping it now (a re-run of an old tag run, say) would put
       an older build above a newer one.
    3. The version must be strictly above what the automatic release can
       already have shipped at this commit and at the tip of main
       (shipped_version, which excludes the tag being judged).

    Fails closed when refs/remotes/origin/main is missing: the plan job's
    full-history checkout always fetches it, so its absence means the
    checkout is not what this check assumes.
    """
    if not ref_exists(git, MAIN_REF):
        raise PlanError(f"{MAIN_REF} is not in this checkout, so '{tag}' cannot be "
                        "checked against main. Refusing to release it.")
    if not is_ancestor(git, head, MAIN_REF):
        raise PlanError(f"Tag '{tag}' is not on main. Merge the commit first, then tag "
                        "it (or let the merge ship it).")
    later = app_commits(git, head, MAIN_REF)
    if later:
        raise PlanError(
            f"Tag '{tag}' is behind main: {len(later)} app change(s) landed on main after "
            "it, so the automatic release has already shipped past it. Tag main instead."
        )
    floors = [shipped_version(git, head, tag), shipped_version(git, MAIN_REF, tag)]
    floor = max((f for f in floors if f is not None), default=None)
    if floor is not None and parse_tag(tag) <= floor:
        raise PlanError(
            f"Tag '{tag}' is not above {_fmt(floor)}, which the automatic "
            f"release may already have shipped. Delete it and tag a version "
            f"above {_fmt(floor)} instead."
        )
    return floor


@dataclass(frozen=True)
class Plan:
    release: bool
    mode: str
    version: str
    base_tag: str
    reason: str

    def outputs(self) -> dict[str, str]:
        return {
            "release": "true" if self.release else "false",
            "mode": self.mode,
            "version": self.version,
            "base_tag": self.base_tag,
        }


def plan(event_name: str, ref: str, git: GitRunner, head: str = "HEAD") -> Plan:
    """Decide what this run does.

    push of refs/tags/ios-v*   the manual escape hatch: ship the tag's version,
                               provided (all checked before any secret is in
                               scope, see check_manual_tag):
                               - the tagged commit is on main,
                               - no app change has landed on main after it,
                               - the version is above everything the automatic
                                 release can already have shipped.
    push of refs/heads/main    ship the derived version, unless a release tag
                               already points at this commit (its tag run
                               ships it).
    workflow_dispatch          archive and export only, as 0.0.0.
    """
    if event_name == "workflow_dispatch":
        return Plan(True, "dispatch", DIAGNOSTIC_VERSION, "", "manual dispatch, no upload")

    if event_name != "push":
        raise PlanError(f"Unexpected event '{event_name}'")

    if ref.startswith("refs/tags/"):
        tag = ref[len("refs/tags/"):]
        version = tag_version_string(tag)
        floor = check_manual_tag(git, tag, head)
        base = base_tag_for(git, head) or ""
        above = _fmt(floor) if floor is not None else "no earlier release"
        return Plan(True, "tag", version, base, f"tag {tag}, above {above}")

    if ref == "refs/heads/main":
        existing = tags_at(git, head)
        if existing:
            return Plan(
                False, "main", "", "",
                f"{', '.join(sorted(existing))} already points at this commit",
            )
        base = base_tag_for(git, head)
        if base is None:
            raise PlanError(
                "No ios-v* tag is reachable from this commit, so there is no "
                "version to count from. Push one release tag by hand first."
            )
        count = len(app_commits(git, base, head))
        version = next_version(base, count)
        return Plan(True, "main", version, base, f"{base} + {count} app commit(s)")

    raise PlanError(f"Refusing to plan a release for ref '{ref}'")


def write_outputs(outputs: dict[str, str]) -> None:
    lines = "".join(f"{k}={v}\n" for k, v in outputs.items())
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(lines)
    sys.stdout.write(lines)


# ---------------------------------------------------------------- GitHub API


class GitHub:
    """The smallest client that does the job: REST and GraphQL over urllib."""

    def __init__(self, token: str, api_url: str, graphql_url: str):
        for url in (api_url, graphql_url):
            if not url.startswith("https://"):
                raise ValueError("GitHub API URLs must be https")
        if not token:
            raise ValueError("GITHUB_TOKEN is empty")
        self._token = token
        self._api = api_url.rstrip("/")
        self._graphql = graphql_url

    @classmethod
    def from_env(cls) -> "GitHub":
        return cls(
            os.environ.get("GITHUB_TOKEN", ""),
            os.environ.get("GITHUB_API_URL", "https://api.github.com"),
            os.environ.get("GITHUB_GRAPHQL_URL", "https://api.github.com/graphql"),
        )

    def _send(self, method: str, url: str, body: object | None) -> object:
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("Authorization", f"Bearer {self._token}")
        request.add_header("Accept", "application/vnd.github+json")
        request.add_header("X-GitHub-Api-Version", "2022-11-28")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = response.read()
        return json.loads(payload) if payload else None

    def rest(self, method: str, path: str, body: object | None = None) -> object:
        return self._send(method, f"{self._api}{path}", body)

    def graphql(self, query: str, variables: dict[str, object]) -> dict:
        result = self._send("POST", self._graphql, {"query": query, "variables": variables})
        if not isinstance(result, dict) or result.get("errors"):
            raise RuntimeError("GraphQL request failed")
        return result["data"]


# ---------------------------------------------------------------- notify

_CLOSING_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      closingIssuesReferences(first: 50) {
        nodes { number repository { nameWithOwner } }
      }
    }
  }
}
"""


def marker(pr: int) -> str:
    return f"{MARKER_PREFIX}{pr} -->"


def merged_prs_for_commit(client, repo: str, sha: str) -> list[int]:
    """The pull request whose merge (or squash) commit is exactly sha."""
    pulls = client.rest("GET", f"/repos/{repo}/commits/{sha}/pulls") or []
    return sorted(
        int(p["number"]) for p in pulls
        if isinstance(p, dict) and p.get("merged_at") and p.get("merge_commit_sha") == sha
    )


def closing_issues(client, repo: str, pr: int) -> list[int]:
    owner, name = repo.split("/", 1)
    data = client.graphql(_CLOSING_QUERY, {"owner": owner, "name": name, "number": pr})
    nodes = (
        ((data.get("repository") or {}).get("pullRequest") or {})
        .get("closingIssuesReferences", {})
        .get("nodes", [])
    )
    return sorted({
        int(n["number"]) for n in nodes
        if (n.get("repository") or {}).get("nameWithOwner", "").lower() == repo.lower()
    })


def already_noted(client, repo: str, issue: int) -> set[int]:
    """PR numbers this workflow has already reported shipped on the issue.

    Only the workflow's own comments count: anyone can comment on a public
    issue, and a pasted marker must not be able to suppress a notification.
    """
    noted: set[int] = set()
    for page in range(1, 11):
        comments = client.rest(
            "GET", f"/repos/{repo}/issues/{issue}/comments?per_page=100&page={page}"
        ) or []
        for comment in comments:
            if (comment.get("user") or {}).get("login") != NOTIFY_LOGIN:
                continue
            for match in re.finditer(re.escape(MARKER_PREFIX) + r"(\d+) -->", comment.get("body") or ""):
                noted.add(int(match.group(1)))
        if len(comments) < 100:
            break
    return noted


def comment_body(version: str, build: str, prs: Sequence[int]) -> str:
    via = ", ".join(f"#{n}" for n in prs)
    markers = "\n".join(marker(n) for n in prs)
    return f"Shipped in TestFlight {version} (build {build}) via {via}.\n\n{markers}\n"


def notify(client, git: GitRunner, repo: str, base_tag: str, head: str,
           version: str, build: str) -> int:
    if not _REPO_RE.match(repo):
        raise ValueError("bad repository name")
    if not _SHA_RE.match(head):
        raise ValueError("head must be a full commit sha")
    if not _VERSION_RE.match(version) or not _BUILD_RE.match(build):
        raise ValueError("bad version or build number")
    if base_tag and parse_tag(base_tag) is None:
        raise ValueError("bad base tag")

    # The range is every app commit since the latest admin-created tag, so it
    # grows until the next manual tag. Only the newest MAX_NOTIFY_COMMITS are
    # looked at: this build's own pull requests are always the newest, older
    # ones were reported by the builds that shipped them (and are skipped by
    # the marker check while still in the window), and the bound keeps the
    # API calls per release constant. What falls out of the window unreported
    # is only a pull request whose own notify failed 30 app merges earlier.
    commits = app_commits(git, base_tag, head) if base_tag else [head]
    if len(commits) > MAX_NOTIFY_COMMITS:
        print(f"{len(commits)} app commits since {base_tag}; "
              f"checking the newest {MAX_NOTIFY_COMMITS}.")
        commits = commits[:MAX_NOTIFY_COMMITS]

    failures = 0
    by_issue: dict[int, set[int]] = {}
    for sha in commits:
        try:
            for pr in merged_prs_for_commit(client, repo, sha):
                for issue in closing_issues(client, repo, pr):
                    by_issue.setdefault(issue, set()).add(pr)
        except Exception as error:  # noqa: BLE001 - report, keep going
            failures += 1
            print(f"::warning::Could not resolve pull requests for {sha[:12]}: {type(error).__name__}")

    for issue in sorted(by_issue):
        try:
            pending = sorted(by_issue[issue] - already_noted(client, repo, issue))
            if not pending:
                print(f"#{issue}: already noted")
                continue
            client.rest("POST", f"/repos/{repo}/issues/{issue}/comments",
                        {"body": comment_body(version, build, pending)})
            print(f"#{issue}: noted {version} ({build})")
        except Exception as error:  # noqa: BLE001 - report, keep going
            failures += 1
            print(f"::warning::Could not comment on #{issue}: {type(error).__name__}")

    if not by_issue:
        print("No closed issues to notify.")
    return 1 if failures else 0


# ---------------------------------------------------------------- CLI


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = parser.add_subparsers(dest="command", required=True)

    p_plan = sub.add_parser("plan")
    p_plan.add_argument("--event", required=True)
    p_plan.add_argument("--ref", required=True)

    p_note = sub.add_parser("notify")
    p_note.add_argument("--repo", required=True)
    p_note.add_argument("--base-tag", default="")
    p_note.add_argument("--head", required=True)
    p_note.add_argument("--version", required=True)
    p_note.add_argument("--build", required=True)

    args = parser.parse_args(argv)

    if args.command == "plan":
        try:
            result = plan(args.event, args.ref, run_git)
        except PlanError as error:
            print(f"::error::{error}", file=sys.stderr)
            return 1
        print(f"Plan: {result.reason}")
        write_outputs(result.outputs())
        return 0

    return notify(GitHub.from_env(), run_git, args.repo, args.base_tag,
                  args.head, args.version, args.build)


if __name__ == "__main__":
    sys.exit(main())
