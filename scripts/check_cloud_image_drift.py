#!/usr/bin/env python3
"""Check that the deployed cloud image was published from the intended commit."""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

IMAGE = "ghcr.io/poopaskoopa/wattracker-cloud"
REPOSITORY = "poopaskoopa/wattracker"
WORKFLOW = "cloud-publish.yml"
_PARAM_RE = re.compile(r"^\s*param\s+(readImage|syncImage)\s*=\s*(['\"])(.*?)\2\s*$")
_IMAGE_RE = re.compile(rf"^{re.escape(IMAGE)}@sha256:([0-9a-fA-F]{{64}})$")
_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")


class DriftError(Exception):
    """A safe, user-facing checker failure."""


def run_gh(args):
    try:
        result = subprocess.run(
            ["gh", *args], capture_output=True, text=True, check=False
        )
    except OSError as exc:
        raise DriftError("could not run gh; install and authenticate GitHub CLI") from exc
    if result.returncode:
        raise DriftError(f"gh failed while running {args[0]}")
    return result.stdout


def read_image_reference(parameter_file):
    values = {}
    counts = {"readImage": 0, "syncImage": 0}
    try:
        lines = Path(parameter_file).read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise DriftError(f"cannot read parameter file: {exc.strerror or 'I/O error'}") from exc
    for line in lines:
        match = _PARAM_RE.match(line)
        if match:
            values[match.group(1)] = match.group(3)
            counts[match.group(1)] += 1
    if set(values) != {"readImage", "syncImage"} or any(count != 1 for count in counts.values()):
        raise DriftError("parameter file must define readImage and syncImage")
    if values["readImage"] != values["syncImage"]:
        raise DriftError("readImage and syncImage must be the same image reference")
    match = _IMAGE_RE.fullmatch(values["readImage"])
    if not match:
        raise DriftError("image reference must be the exact GHCR image with a full sha256 digest")
    return values["readImage"], match.group(1).lower()


def _run_records():
    try:
        records = json.loads(run_gh([
            "run", "list", "--workflow", WORKFLOW, "--branch", "main",
            "--status", "success", "--limit", "1000", "--json", "databaseId,headSha",
        ]))
    except json.JSONDecodeError as exc:
        raise DriftError("gh returned invalid workflow-run data") from exc
    if not isinstance(records, list):
        raise DriftError("gh returned invalid workflow-run data")
    return records


def resolve_published_run(digest):
    for record in _run_records():
        run_id = record.get("databaseId")
        head_sha = record.get("headSha")
        if not isinstance(run_id, int) or not isinstance(head_sha, str) or not _SHA_RE.fullmatch(head_sha):
            continue
        log = run_gh(["run", "view", str(run_id), "--log"])
        if re.search(
            rf"{re.escape(IMAGE)}@sha256:{digest}(?![0-9a-fA-F])", log
        ):
            return run_id, head_sha.lower()
    raise DriftError("pinned digest was not found in a successful main publishing run")


def compare_to_main(published_sha):
    try:
        main_sha = json.loads(run_gh(["api", f"repos/{REPOSITORY}/commits/main"]))["sha"]
        comparison = json.loads(run_gh(["api", f"repos/{REPOSITORY}/compare/{published_sha}...main"]))
        ahead, behind = comparison["ahead_by"], comparison["behind_by"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise DriftError("gh returned invalid commit comparison data") from exc
    if not isinstance(main_sha, str) or not _SHA_RE.fullmatch(main_sha):
        raise DriftError("gh returned an invalid main commit")
    if not isinstance(ahead, int) or not isinstance(behind, int):
        raise DriftError("gh returned invalid commit comparison data")
    return main_sha.lower(), ahead, behind


def check(parameter_file, deployment_commit=None):
    image_ref, digest = read_image_reference(parameter_file)
    if deployment_commit is not None and not _SHA_RE.fullmatch(deployment_commit):
        raise DriftError("deployment commit must be a full 40-character commit SHA")
    run_id, published_sha = resolve_published_run(digest)
    if deployment_commit is not None:
        if published_sha != deployment_commit.lower():
            raise DriftError("pinned digest was published from a different commit")
    main_sha, main_ahead, main_behind = compare_to_main(published_sha)
    return {"image_ref": image_ref, "run_id": run_id, "published_sha": published_sha,
            "main_sha": main_sha, "main_ahead": main_ahead, "main_behind": main_behind}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("parameter_file", type=Path)
    parser.add_argument("--deployment-commit", help="full 40-character commit SHA")
    args = parser.parse_args(argv)
    try:
        result = check(args.parameter_file, args.deployment_commit)
    except DriftError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"image: {result['image_ref']}")
    print(f"published commit: {result['published_sha']}")
    print(f"main commit: {result['main_sha']}")
    print(f"publishing run: {result['run_id']}")
    print(f"main ahead: {result['main_ahead']}; main behind: {result['main_behind']}")
    print("status: drift" if result["main_ahead"] or result["main_behind"] else "status: current")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
