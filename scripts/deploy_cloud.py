#!/usr/bin/env python3
"""Reconcile the published cloud image and explicitly deploy the cloud."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Sequence


SCRIPT_DIR = Path(__file__).resolve().parent
REPOSITORY_ROOT = SCRIPT_DIR.parent
GH_PATH = Path("/opt/homebrew/bin/gh")
_IMAGE_PATHS = ("Dockerfile.cloud", "wattracker/")
_PARAM_LINE_RE = re.compile(
    rb"(?m)^(?P<prefix>[ \t]*param[ \t]+(?P<name>readImage|syncImage)"
    rb"[ \t]*=[ \t]*)(?P<quote>['\"])(?P<value>[^'\"]*)"
    rb"(?P=quote)(?P<suffix>[ \t]*(?:\r?\n|$))"
)
_COMMIT_RE = re.compile(r"\A[0-9a-f]{40}\Z")


class DeployError(RuntimeError):
    """A safe, user-facing deployment failure."""


def _load_drift_checker():
    try:
        import check_cloud_image_drift as checker
    except ModuleNotFoundError:
        path = SCRIPT_DIR / "check_cloud_image_drift.py"
        spec = importlib.util.spec_from_file_location("cloud_image_drift", path)
        if spec is None or spec.loader is None:
            raise DeployError("could not load the cloud image drift checker")
        checker = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(checker)
    return checker


drift_checker = _load_drift_checker()
_IMAGE_REF_IN_LOG_RE = re.compile(
    (
        re.escape(drift_checker.IMAGE)
        + r"@sha256:[0-9a-fA-F]{64}"
    ).encode("ascii")
)


def _run_process(
    command: Sequence[str], *, label: str, cwd: Path = REPOSITORY_ROOT
) -> str:
    try:
        result = subprocess.run(
            list(command),
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise DeployError(f"{label} is unavailable") from exc
    except OSError as exc:
        raise DeployError(f"could not run {label}") from exc
    if result.returncode:
        raise DeployError(f"{label} failed; inspect its diagnostics and retry")
    return result.stdout


def _run_gh(args: Sequence[str]) -> str:
    if not GH_PATH.is_file() or not os.access(GH_PATH, os.X_OK):
        raise DeployError(
            "GitHub CLI is unavailable at /opt/homebrew/bin/gh; "
            "install the arm64 Homebrew build"
        )
    try:
        architecture = subprocess.run(
            ["file", "-b", str(GH_PATH)],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise DeployError("could not verify the architecture of /opt/homebrew/bin/gh") from exc
    if architecture.returncode or "arm64" not in architecture.stdout.lower():
        raise DeployError(
            "/opt/homebrew/bin/gh is not an arm64 executable; refusing to use another gh"
        )
    return _run_process([str(GH_PATH), *args], label="GitHub CLI")


def _checker_run_gh(args: Sequence[str]) -> str:
    """Give the existing checker the explicitly selected, verified gh binary."""

    return _run_gh(args)


drift_checker.run_gh = _checker_run_gh


def _git_output(args: Sequence[str]) -> str:
    return _run_process(["git", *args], label="git")


def _require_clean_main(parameter_file: Path) -> str:
    branch = _git_output(["branch", "--show-current"]).strip()
    if branch != "main":
        raise DeployError("refusing to deploy: the current checkout is not main")
    try:
        allowed_parameter = parameter_file.resolve().relative_to(REPOSITORY_ROOT)
    except ValueError as exc:
        raise DeployError("refusing to deploy: the parameter file must be inside the checkout") from exc
    status = _git_output(["status", "--porcelain=v1", "-z", "--untracked-files=all"])
    for entry in status.split("\0"):
        if not entry:
            continue
        code = entry[:2]
        changed_path = entry[3:] if len(entry) > 3 else ""
        if code == "??" and Path(changed_path) == allowed_parameter:
            continue
        raise DeployError("refusing to deploy: the working tree is dirty")
    head = _git_output(["rev-parse", "HEAD"]).strip().lower()
    if not _COMMIT_RE.fullmatch(head):
        raise DeployError("refusing to deploy: the checkout commit is invalid")
    return head


def _check_drift(parameter_file: Path, deployment_commit: str | None = None) -> dict[str, Any]:
    try:
        return drift_checker.check(parameter_file, deployment_commit)
    except drift_checker.DriftError as exc:
        raise DeployError(f"cloud image drift check failed: {exc}") from exc


def _changed_paths(published_sha: str, main_sha: str) -> list[str]:
    output = _git_output(
        ["diff", "--name-only", "--no-renames", f"{published_sha}..{main_sha}"]
    )
    return [line for line in output.splitlines() if line]


def _is_image_path(path: str) -> bool:
    return path == _IMAGE_PATHS[0] or path.startswith(_IMAGE_PATHS[1])


def _resolve_published_image(main_sha: str) -> tuple[str, int]:
    """Resolve, never synthesize, the image from a successful run for main."""

    try:
        records = drift_checker._run_records()
    except drift_checker.DriftError as exc:
        raise DeployError(f"could not inspect successful cloud publishing runs: {exc}") from exc

    for record in records:
        run_id = record.get("databaseId")
        head_sha = record.get("headSha")
        if (
            not isinstance(run_id, int)
            or run_id <= 0
            or not isinstance(head_sha, str)
            or not drift_checker._SHA_RE.fullmatch(head_sha)
            or head_sha.lower() != main_sha.lower()
        ):
            continue
        try:
            log = _checker_run_gh(["run", "view", str(run_id), "--log"])
        except DeployError:
            raise
        refs = list(dict.fromkeys(_IMAGE_REF_IN_LOG_RE.findall(log.encode("utf-8"))))
        if len(refs) != 1:
            raise DeployError(
                "the successful cloud publishing run did not expose exactly one image digest"
            )
        image_ref = refs[0].decode("ascii")
        if drift_checker._IMAGE_RE.fullmatch(image_ref) is None:
            raise DeployError("the publishing run exposed an invalid image reference")
        return image_ref, run_id

    raise DeployError("no successful cloud publishing run exists for the current main commit")


def _atomic_write(path: Path, contents: bytes, mode: int) -> None:
    temporary_name: str | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
        )
        with os.fdopen(descriptor, "wb") as temporary:
            temporary.write(contents)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.chmod(temporary_name, mode)
        os.replace(temporary_name, path)
        temporary_name = None
    except OSError as exc:
        raise DeployError("could not update the deployment parameter file") from exc
    finally:
        if temporary_name is not None:
            try:
                os.unlink(temporary_name)
            except OSError:
                pass


def _replace_image_pins(path: Path, image_ref: str) -> tuple[bytes, int]:
    try:
        if path.is_symlink() or not path.is_file():
            raise DeployError("deployment parameter file must be a regular file")
        original = path.read_bytes()
        mode = stat.S_IMODE(path.stat().st_mode)
    except DeployError:
        raise
    except OSError as exc:
        raise DeployError("could not read the deployment parameter file") from exc

    matches = list(_PARAM_LINE_RE.finditer(original))
    names = [match.group("name").decode("ascii") for match in matches]
    if sorted(names) != ["readImage", "syncImage"]:
        raise DeployError("deployment parameter file must define readImage and syncImage once")

    updated = bytearray(original)
    replacement = image_ref.encode("ascii")
    for match in reversed(matches):
        start, end = match.span("value")
        updated[start:end] = replacement
    _atomic_write(path, bytes(updated), mode)
    return original, mode


def _run_azure_deployment(parameter_file: Path, resource_group: str, deployment_name: str) -> None:
    common = [
        "--resource-group",
        resource_group,
        "--parameters",
        str(parameter_file),
    ]
    _run_process(
        ["az", "deployment", "group", "validate", *common],
        label="Azure deployment validation",
    )
    _run_process(
        [
            "az",
            "deployment",
            "group",
            "create",
            "--name",
            deployment_name,
            *common,
        ],
        label="Azure deployment",
    )


def _running_commit() -> str:
    try:
        result = subprocess.run(
            [sys.executable, "-m", "wattracker.cloud.admin", "version"],
            cwd=REPOSITORY_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise DeployError("could not run the cloud admin version command") from exc
    if result.returncode:
        raise DeployError("cloud admin version command failed; inspect its diagnostics and retry")
    try:
        payload = json.loads(result.stdout)
    except (TypeError, json.JSONDecodeError) as exc:
        raise DeployError("cloud admin version command returned invalid data") from exc
    commit = payload.get("commit") if isinstance(payload, dict) else None
    if not isinstance(commit, str) or (commit != "source" and _COMMIT_RE.fullmatch(commit) is None):
        raise DeployError("cloud admin version command returned invalid data")
    return commit


def _validate_cli_value(value: str, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value.startswith("-")
        or any(ord(char) < 0x21 for char in value)
    ):
        raise DeployError(f"invalid {label}")
    return value


def deploy(parameter_file: Path, resource_group: str, deployment_name: str) -> int:
    resource_group = _validate_cli_value(resource_group, "resource group")
    deployment_name = _validate_cli_value(deployment_name, "deployment name")
    local_head = _require_clean_main(parameter_file)
    initial = _check_drift(parameter_file)
    main_sha = str(initial["main_sha"]).lower()
    if local_head != main_sha:
        raise DeployError("refusing to deploy: local main is not at the current remote main commit")
    if not initial["main_ahead"] and not initial["main_behind"]:
        print("cloud image is current; no deployment needed")
        return 0

    changed = _changed_paths(initial["published_sha"], main_sha)
    image_paths = [path for path in changed if _is_image_path(path)]
    print(
        f"drift: {len(changed)} changed path(s) between the published image commit "
        f"and current main; image changes: {'yes' if image_paths else 'no'}"
    )
    for path in changed:
        print(f"  {path}")
    if not image_paths:
        print("drift contains no image changes; docs/tests/infra-only drift needs no deployment")
        return 0

    image_ref, run_id = _resolve_published_image(main_sha)
    print(f"image drift detected; using the successful cloud publish run {run_id}")
    print("updating both image pins, then running Azure validate and create")
    original: bytes | None = None
    mode: int | None = None
    updated = False
    try:
        original, mode = _replace_image_pins(parameter_file, image_ref)
        updated = True
        confirmed = _check_drift(parameter_file, deployment_commit=main_sha)
        if confirmed["main_ahead"] or confirmed["main_behind"]:
            raise DeployError("updated image did not reconcile the cloud image drift")
        _run_azure_deployment(parameter_file, resource_group, deployment_name)
        running = _running_commit()
        print(f"cloud deployment completed; running commit: {running}")
        return 0
    except BaseException:
        if updated and original is not None and mode is not None:
            _atomic_write(parameter_file, original, mode)
        raise


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, _message: str) -> None:
        raise DeployError("invalid command-line arguments")


def main(argv: Sequence[str] | None = None) -> int:
    parser = _ArgumentParser(description=__doc__)
    parser.add_argument("parameter_file", type=Path)
    parser.add_argument("--resource-group", default=os.environ.get("RESOURCE_GROUP", ""))
    parser.add_argument("--deployment-name", default="wattracker-cloud")
    try:
        args = parser.parse_args(argv)
        if not args.resource_group:
            raise DeployError("resource group is required via --resource-group or RESOURCE_GROUP")
        return deploy(args.parameter_file.resolve(), args.resource_group, args.deployment_name)
    except DeployError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
