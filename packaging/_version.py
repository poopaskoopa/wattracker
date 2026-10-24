"""Git-derived project version shared by every desktop build artifact."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

_DESKTOP_TAG = re.compile(
    r"^(?:v|preview-)(?P<version>(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*))$"
)
DESCRIBE_COMMAND = [
    "git",
    "describe",
    "--dirty",
    "--tags",
    "--long",
    "--match",
    "v[0-9]*.[0-9]*.[0-9]*",
    "--match",
    "preview-[0-9]*.[0-9]*.[0-9]*",
    "--exclude",
    "v*[!0-9.]*",
    "--exclude",
    "preview-*[!0-9.]*",
    "--exclude",
    "v0[0-9]*.*",
    "--exclude",
    "v*.0[0-9]*.*",
    "--exclude",
    "v*.*.0[0-9]*",
    "--exclude",
    "preview-0[0-9]*.*",
    "--exclude",
    "preview-*.0[0-9]*.*",
    "--exclude",
    "preview-*.*.0[0-9]*",
]
_MAX_BUILD_ID = (1 << 32) - 1


def project_version(root) -> str:
    """Return the same configured PEP 440 version setuptools-scm writes to the wheel."""
    from setuptools_scm._get_version import _get_version
    from vcs_versioning import VcsEnvironment

    root = Path(root).resolve()
    # Loading pyproject.toml keeps the helper and wheel on the same tag regex,
    # git argv, and no-Git fallback configuration.
    environment = VcsEnvironment.from_env("SETUPTOOLS_SCM")
    config = environment.build_config(name=root / "pyproject.toml")
    if _has_desktop_tag(root):
        # setuptools-scm deliberately treats an unsuccessful custom describe
        # command as a signal to infer a fallback version. In a tagged desktop
        # checkout that would conceal a broken git invocation and report a
        # version older than the release tag, so fail before invoking it.
        subprocess.run(
            config.scm.git.describe_command,
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        )
    version = _get_version(config, force_write_version_files=False)
    if version is None:
        raise RuntimeError(f"setuptools-scm could not determine a version for {root}")
    return version


def _has_desktop_tag(root: Path) -> bool:
    """Return whether a recognized desktop version tag is reachable from HEAD."""
    try:
        result = subprocess.run(
            ["git", "tag", "--merged", "HEAD"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        # A source export without Git is handled by setuptools-scm's configured
        # fallback. Tagged repositories get a loud describe check above.
        return False
    return any(_DESKTOP_TAG.fullmatch(tag) for tag in result.stdout.splitlines())


def _release_major_minor(version: str) -> tuple[int, int]:
    """Return the release's major/minor pair for native version resources."""
    from packaging.version import InvalidVersion, Version

    try:
        parsed = Version(version)
    except InvalidVersion:
        raise SystemExit(f"unsupported Git-derived version: {version}")

    major, minor = (list(parsed.release) + [0, 0])[:2]
    if any(part > 65535 for part in (major, minor)):
        raise SystemExit(f"version is too large for native version resources: {version}")
    return major, minor


def git_build_id(root=None) -> int:
    """Count reachable commits and accepted desktop tags as a build sequence.

    Counting tags as well as commits gives a stable release a larger build id
    than a preview published from the same commit immediately before the
    release tag was created. iOS and malformed tags are excluded.
    """
    root = Path(root or Path.cwd()).resolve()
    try:
        shallow = subprocess.run(
            ["git", "rev-parse", "--is-shallow-repository"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        if shallow == "true":
            raise SystemExit(
                "desktop native version resources require a full-history Git checkout"
            )
        commits = subprocess.run(
            ["git", "rev-list", "--count", "HEAD"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        tags = subprocess.run(
            ["git", "tag", "--merged", "HEAD"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.splitlines()
        build_id = int(commits) + sum(_DESKTOP_TAG.fullmatch(tag) is not None for tag in tags)
    except (OSError, subprocess.CalledProcessError, ValueError):
        # An sdist or source export without Git uses setuptools-scm's configured
        # fallback version and a zero build id rather than crashing the build.
        return 0
    if build_id > _MAX_BUILD_ID:
        raise SystemExit(
            f"Git build id exceeds the 32-bit Windows resource limit: {build_id}"
        )
    return build_id


def windows_file_version(version: str, root=None) -> tuple[int, int, int, int]:
    """Return major/minor plus a 32-bit monotonic build id in the 4-part tuple."""
    major, minor = _release_major_minor(version)
    build_id = git_build_id(root)
    high, low = divmod(build_id, 1 << 16)
    return major, minor, high, low


def macos_short_version(version: str) -> str:
    """Return a three-component numeric CFBundleShortVersionString."""
    from packaging.version import Version

    release = (list(Version(version).release) + [0, 0, 0])[:3]
    major, minor, patch = release
    return f"{major}.{minor}.{patch}"


def macos_build_version(version: str, root=None) -> str:
    """Return three numeric CFBundleVersion components with a monotonic build id."""
    major, minor = _release_major_minor(version)
    return f"{major}.{minor}.{git_build_id(root)}"


if __name__ == "__main__":
    print(project_version(Path(__file__).resolve().parents[1]))
