#!/usr/bin/env python3
"""Planning and follow-up helpers for .github/workflows/ios-release.yml.

Standard library only: this runs on hosted ubuntu and macOS runners with
whatever ``python3`` the image ships, before anything is installed.

Three subcommands, one per job that needs a decision made in code rather than
in a YAML expression:

``plan``        (the ``plan`` job, contents: read)
    Decides whether this run releases at all and which marketing version it
    carries. See ``plan()`` for the rules.

``superseded``  (the signing job, contents: read, just before upload)
    Asks whether ``main`` has already moved past this commit with another
    change to the app. If it has, that push started its own release, which is
    queued behind this one and ships everything this one would, so this run
    skips its upload. Fails open: any doubt means "upload".

``notify``      (the ``notify`` job, issues: write, never the signing job)
    Comments "Shipped in TestFlight <version> (build <n>)" on each issue the
    shipped pull requests closed.

THE VERSION RULE, and why it is not "latest tag + 1".

    version = latest ios-v* tag reachable from HEAD, with its patch component
              increased by the number of first-parent commits since that tag
              that touch the app (minimum 1).

On ``main`` every pull request lands as one first-parent merge commit, so in
the normal flow (one merge per push, the previous release tagged) this is
exactly "latest tag's patch + 1". It differs in the two cases where "+1" is
wrong:

* The post-upload tag could not be created (the tag ruleset refuses it, or the
  tag job failed). "+1" would then reuse the previous version forever. This
  rule is additive - patch(T) + count(T..H) is the same whether it is computed
  from the last tag that exists or from one that was never written - so the
  tag is a cache for humans and for the notify range, never load-bearing.
* A release was superseded (two merges, one build). The number skips, which
  is accurate: the skipped version was never shipped.

The version is therefore a pure function of the commit, so re-running a run
recomputes the same version. The build number (``run_number.run_attempt``) is
what makes every upload unique to App Store Connect; it is set in the workflow,
not here.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
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
# workflow change can be exercised with a non-uploading dispatch run or the
# manual tag. tests/test_ios_release_workflow.py asserts this tuple matches
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


# ---------------------------------------------------------------- plan


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

    push of refs/tags/ios-v*   the manual escape hatch: ship the tag's version.
    push of refs/heads/main    ship the derived version, unless a release tag
                               already points at this commit (a manual tag
                               run, or a re-run of a run that already shipped
                               and tagged it).
    workflow_dispatch          archive and export only, as 0.0.0.
    """
    if event_name == "workflow_dispatch":
        return Plan(True, "dispatch", DIAGNOSTIC_VERSION, "", "manual dispatch, no upload")

    if event_name != "push":
        raise PlanError(f"Unexpected event '{event_name}'")

    if ref.startswith("refs/tags/"):
        tag = ref[len("refs/tags/"):]
        version = tag_version_string(tag)
        base = base_tag_for(git, head) or ""
        return Plan(True, "tag", version, base, f"tag {tag}")

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


# ---------------------------------------------------------------- superseded


def is_superseded(client, repo: str, sha: str, branch: str = "main") -> bool:
    """True only when branch has a newer commit that touches the app.

    That newer push fired its own release (same path filter), which the
    workflow concurrency group has queued behind this run. Any uncertainty -
    an API error, a diverged history, a truncated file list with no app path
    in it - returns False: a redundant TestFlight build is harmless, a
    missing one is not.
    """
    try:
        result = client.rest("GET", f"/repos/{repo}/compare/{sha}...{branch}")
    except Exception as error:  # noqa: BLE001 - fail open, by design
        print(f"::warning::Could not compare with {branch} ({type(error).__name__}); uploading.")
        return False
    if not isinstance(result, dict) or result.get("status") != "ahead":
        return False
    files = result.get("files") or []
    return any(touches_app(str(f.get("filename", ""))) for f in files if isinstance(f, dict))


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

    commits = app_commits(git, base_tag, head) if base_tag else [head]
    if len(commits) > MAX_NOTIFY_COMMITS:
        print(f"::warning::{len(commits)} app commits since {base_tag}; "
              f"notifying for the newest {MAX_NOTIFY_COMMITS} only.")
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

    p_sup = sub.add_parser("superseded")
    p_sup.add_argument("--repo", required=True)
    p_sup.add_argument("--sha", required=True)

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

    if args.command == "superseded":
        if not _REPO_RE.match(args.repo) or not _SHA_RE.match(args.sha):
            print("::warning::Unexpected repository or sha; uploading.")
            write_outputs({"superseded": "false"})
            return 0
        try:
            client = GitHub.from_env()
        except ValueError:
            print("::warning::No usable GitHub token; uploading.")
            write_outputs({"superseded": "false"})
            return 0
        superseded = is_superseded(client, args.repo, args.sha)
        if superseded:
            print("main has a newer app change; its queued release ships this one too.")
        write_outputs({"superseded": "true" if superseded else "false"})
        return 0

    return notify(GitHub.from_env(), run_git, args.repo, args.base_tag,
                  args.head, args.version, args.build)


if __name__ == "__main__":
    sys.exit(main())
