#!/usr/bin/env python3
"""Fail when desktop app or packaging commits are newer than a published release."""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

REPOSITORY = "poopaskoopa/wattracker"
_VERSION_TAG = re.compile(
    r"^(?:v|preview-)\d+(?:\.\d+){1,2}(?:[-+][A-Za-z0-9.-]+)?$"
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


def _is_desktop_tag(tag: object) -> bool:
    return isinstance(tag, str) and (
        tag == "preview-latest" or bool(_VERSION_TAG.fullmatch(tag))
    )


def latest_published_release(root: Path) -> tuple[str, str]:
    """Choose the newest published desktop release reachable from this checkout."""
    try:
        records = json.loads(run_gh([
            "release", "list", "--repo", REPOSITORY, "--limit", "1000",
            "--exclude-drafts", "--json", "tagName,publishedAt,isDraft",
        ]))
    except json.JSONDecodeError as exc:
        raise DriftError("gh returned invalid release data") from exc
    if not isinstance(records, list):
        raise DriftError("gh returned invalid release data")

    candidates = []
    for record in records:
        if not isinstance(record, dict) or not _is_desktop_tag(record.get("tagName")):
            continue
        if record.get("isDraft") is True:
            continue
        tag = record["tagName"]
        sha = run_git(["rev-parse", "--verify", f"refs/tags/{tag}^{{commit}}"], root)
        if not sha:
            continue
        if run_git(["merge-base", "--is-ancestor", sha, "HEAD"], root) is None:
            continue
        published_at = record.get("publishedAt")
        candidates.append((published_at if isinstance(published_at, str) else "", tag, sha))

    if not candidates:
        raise DriftError("no published desktop release tag (preview-latest or v*) is reachable from HEAD")
    # The publisher replaces preview-latest on each main push, so its release
    # publication time advances with the assets. This selects the actual newest
    # published release, including a newer v* release that points at an older
    # main commit.
    _, tag, sha = max(candidates, key=lambda item: item[0])
    return tag, sha


def check(root: Path | str = ".") -> dict[str, object]:
    root = Path(root).resolve()
    tag, release_sha = latest_published_release(root)
    main_sha = run_git(["rev-parse", "HEAD"], root)
    if not main_sha:
        raise DriftError("git returned an invalid HEAD")
    count = run_git([
        "rev-list", "--count", f"{release_sha}..HEAD", "--", "wattracker", "packaging",
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
            f"{desktop_commits} commit(s) changed wattracker/ or packaging/ "
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
