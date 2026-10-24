"""Git-derived project version shared by every desktop build artifact."""
from __future__ import annotations

from pathlib import Path


def project_version(root) -> str:
    """Return the PEP 440 version that setuptools-scm writes into the wheel."""
    from setuptools_scm import get_version

    return get_version(root=root)


def _release_parts(version: str) -> tuple[int, int, int, int]:
    """Normalize a PEP 440 version into numeric Windows resource fields."""
    from packaging.version import InvalidVersion, Version

    try:
        parsed = Version(version)
    except InvalidVersion:
        raise SystemExit(f"unsupported Git-derived version: {version}")

    major, minor, patch = (list(parsed.release) + [0, 0, 0])[:3]
    distance = parsed.dev
    if distance is None and parsed.local:
        first_local = parsed.local.split(".", 1)[0]
        if first_local.isdigit():
            distance = int(first_local)
    if distance is None:
        distance = parsed.post or 0
    patch += distance
    if any(part > 65535 for part in (major, minor, patch)):
        raise SystemExit(f"version is too large for Windows resources: {version}")
    return major, minor, patch, 0


def windows_file_version(version: str) -> tuple[int, int, int, int]:
    return _release_parts(version)


def macos_short_version(version: str) -> str:
    """Return a three-component numeric CFBundleShortVersionString."""
    from packaging.version import Version

    release = (list(Version(version).release) + [0, 0, 0])[:3]
    major, minor, patch = release
    return f"{major}.{minor}.{patch}"


def macos_build_version(version: str) -> str:
    """Return a numeric CFBundleVersion that distinguishes preview commits."""
    major, minor, patch, _ = _release_parts(version)
    return f"{major}.{minor}.{patch}"


if __name__ == "__main__":
    print(project_version(Path(__file__).resolve().parents[1]))
