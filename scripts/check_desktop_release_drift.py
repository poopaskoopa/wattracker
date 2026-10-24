#!/usr/bin/env python3
"""Fail when shipped desktop app or packaging commits are newer than a release."""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

REPOSITORY = "poopaskoopa/wattracker"
_VERSION_TAG = re.compile(
    r"^(?:v|preview-)(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)$"
)


class DriftError(Exception):
    """A safe, user-facing release drift failure."""


def run_gh(args: list[str]) -> str:
    try:
        result = subprocess.run(
            ["gh", *args], capture_output=True, text=True, check=False
        )
    except OSError as exc:
        raise DriftError("could not run gh; install and authenticate GitHub CLI") from exc
    if result.returncode:
        raise DriftError(f"gh failed while running {args[0]}")
    return result.stdout


def run_git(args: list[str], root: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", *args], cwd=root, capture_output=True, text=True, check=False
        )
    except OSError as exc:
        raise DriftError("could not run git") from exc
    if result.returncode:
        # `merge-base --is-ancestor` uses exit status 1 for a normal false.
        if args[:1] == ["merge-base"] and result.returncode == 1:
            return None
        raise DriftError(f"git failed while running {args[0]}")
    return result.stdout.strip()


def local_tag_commit(tag: str, root: Path) -> str | None:
    """Return a release tag's commit, or None when that tag is not checked out."""
    ref = f"refs/tags/{tag}"
    try:
        result = subprocess.run(
            ["git", "show-ref", "--verify", "--quiet", ref],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise DriftError("could not run git") from exc
    if result.returncode == 1:
        # GitHub may list a release whose tag wasn't fetched by a shallow or
        # selective checkout. Ignore that record and consider local tags.
        return None
    if result.returncode:
        raise DriftError("git failed while checking a release tag")
    return run_git(["rev-parse", "--verify", f"{ref}^{{commit}}"], root)


def _is_desktop_tag(tag: object) -> bool:
    return isinstance(tag, str) and (
        tag == "preview-latest" or bool(_VERSION_TAG.fullmatch(tag))
    )


def latest_published_release(root: Path) -> tuple[str, str]:
    """Choose the newest published desktop release by the commit it names.

    GitHub preserves a release's original ``publishedAt`` when its assets and
    tag are updated in place, so publication time cannot identify the newest
    ``preview-latest`` build. Git's topological log orders descendant commits
    before their ancestors and uses commit time to order independent history.
    When multiple names point at the same newest commit, prefer the rolling
    ``preview-latest`` name.
    """
    try:
        records = json.loads(run_gh([
            "release", "list", "--repo", REPOSITORY, "--limit", "1000",
            "--exclude-drafts", "--json", "tagName,isDraft",
        ]))
    except json.JSONDecodeError as exc:
        raise DriftError("gh returned invalid release data") from exc
    if not isinstance(records, list):
        raise DriftError("gh returned invalid release data")

    head_order = run_git(["rev-list", "--topo-order", "HEAD"], root)
    if head_order is None:
        raise DriftError("git returned an invalid HEAD history")
    # Lower indexes are newer: --topo-order places descendants before parents.
    commit_rank = {sha: index for index, sha in enumerate(head_order.splitlines())}
    candidates: list[tuple[str, str, int]] = []
    for record in records:
        if not isinstance(record, dict) or not _is_desktop_tag(record.get("tagName")):
            continue
        if record.get("isDraft") is True:
            continue
        tag = record["tagName"]
        sha = local_tag_commit(tag, root)
        if sha is None:
            continue
        rank = commit_rank.get(sha)
        if rank is None:
            continue
        candidates.append((tag, sha, rank))

    if not candidates:
        raise DriftError("no published desktop release tag (preview-latest or v*) is reachable from HEAD")
    # Git's order handles ancestry and commit-date tie breaking across
    # independent branches; the rolling alias breaks a same-commit tie.
    tag, sha, _ = max(
        candidates,
        key=lambda item: (-item[2], item[0] == "preview-latest", item[0]),
    )
    return tag, sha


def check(root: Path | str = ".") -> dict[str, object]:
    root = Path(root).resolve()
    tag, release_sha = latest_published_release(root)
    main_sha = run_git(["rev-parse", "HEAD"], root)
    if not main_sha:
        raise DriftError("git returned an invalid HEAD")
    count = run_git([
        "rev-list", "--count", f"{release_sha}..HEAD", "--",
        "wattracker", "wattracker_connector", "packaging", "pyproject.toml",
    ], root)
    if count is None:
        raise DriftError("git returned an invalid desktop change count")
    try:
        desktop_commits = int(count)
    except ValueError as exc:
        raise DriftError("git returned an invalid desktop change count") from exc
    result = {
        "release_tag": tag,
        "release_sha": release_sha,
        "head_sha": main_sha,
        "desktop_commits": desktop_commits,
    }
    if desktop_commits:
        raise DriftError(
            f"{desktop_commits} commit(s) changed shipped desktop code or packaging "
            f"after published release {tag} ({release_sha[:7]})"
        )
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    try:
        result = check(args.repo_root)
    except DriftError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"published desktop release: {result['release_tag']} ({result['release_sha']})")
    print(f"checked commit: {result['head_sha']}")
    print("status: current")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
