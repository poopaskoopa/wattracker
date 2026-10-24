"""Git-derived project version shared by every desktop build artifact."""
from __future__ import annotations

from pathlib import Path


def project_version(root) -> str:
    """Return the same configured PEP 440 version setuptools-scm writes to the wheel."""
    from setuptools_scm import Configuration, get_version

    root = Path(root).resolve()
    # Loading pyproject.toml is essential here: its tag regex and describe
    # command deliberately ignore iOS tags, and its fallback version matches
    # the wheel build when Git metadata is unavailable.
    config = Configuration.from_file(name=root / "pyproject.toml")
    return get_version(
        root=root,
        version_scheme=config.version_scheme,
        local_scheme=config.local_scheme,
        fallback_version=config.fallback_version,
        fallback_root=config.fallback_root,
        tag_regex=config.tag.regex,
        dist_name=config.dist_name,
        search_parent_directories=config.search_parent_directories,
        scm={"git": {"describe_command": config.scm.git.describe_command}},
    )


_BUILDS_PER_PATCH = 10_000


def _release_parts(version: str) -> tuple[int, int, int, int]:
    """Map a PEP 440 version to release fields plus the commit distance."""
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
    distance = distance or 0
    if distance >= _BUILDS_PER_PATCH:
        raise SystemExit(
            f"version is too far from its desktop tag for macOS build numbering: {version}"
        )
    encoded_patch = patch * _BUILDS_PER_PATCH + distance
    if any(part > 65535 for part in (major, minor, encoded_patch)):
        raise SystemExit(f"version is too large for Windows resources: {version}")
    return major, minor, encoded_patch, 0


def windows_file_version(version: str) -> tuple[int, int, int, int]:
    return _release_parts(version)


def macos_short_version(version: str) -> str:
    """Return a three-component numeric CFBundleShortVersionString."""
    from packaging.version import Version

    release = (list(Version(version).release) + [0, 0, 0])[:3]
    major, minor, patch = release
    return f"{major}.{minor}.{patch}"


def macos_build_version(version: str) -> str:
    """Return monotonic three-part CFBundleVersion metadata.

    macOS uses only three numeric components from CFBundleVersion. Put the
    commit distance inside the patch component so a later tagged patch release
    always sorts after a preview built from an earlier patch tag. The reserved
    range allows up to 9,999 commits after one tag. Windows uses the same
    encoded third component in its four-part file version.
    """
    major, minor, encoded_patch, _ = _release_parts(version)
    return f"{major}.{minor}.{encoded_patch}"


if __name__ == "__main__":
    print(project_version(Path(__file__).resolve().parents[1]))
