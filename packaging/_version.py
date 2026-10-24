"""Git-derived project version shared by every desktop build artifact."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

_DESKTOP_TAG = re.compile(
    r"^(?:v|preview-)(?P<version>(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)(?:-(?:a|b|rc)\d+)?)$"
)
_MAX_BUILD_ID = (1 << 32) - 1


def project_version(root) -> str:
    """Return the same configured PEP 440 version setuptools-scm writes to the wheel."""
    from setuptools_scm import Configuration
    from setuptools_scm._get_version import _get_version

    root = Path(root).resolve()
    # Loading pyproject.toml is essential here: its tag regex and describe
    # command deliberately ignore iOS tags, and its fallback version matches
    # the wheel build when Git metadata is unavailable.
    try:
        from vcs_versioning import VcsEnvironment
    except ImportError:  # compatibility with earlier setuptools-scm 9.x installs
        config = Configuration.from_file(name=root / "pyproject.toml")
    else:
        environment = VcsEnvironment.from_env("SETUPTOOLS_SCM")
        config = environment.build_config(name=root / "pyproject.toml")
    version = _get_version(config, force_write_version_files=False)
    if version is None:
        raise RuntimeError(f"setuptools-scm could not determine a version for {root}")
    return version


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
