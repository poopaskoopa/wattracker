from __future__ import annotations

import importlib.metadata
import importlib.util
import re
import shutil
import subprocess
import sys
import zipfile
from email.parser import Parser
from pathlib import Path

import pytest
from packaging.version import Version

ROOT = Path(__file__).parents[1]
PYPROJECT = ROOT / "pyproject.toml"
WINDOWS = ROOT / ".github" / "workflows" / "windows.yml"
DRIFT_WORKFLOW = ROOT / ".github" / "workflows" / "desktop-release-drift.yml"
BUILDS_PER_PATCH = 10_000


def _load_version_helper():
    path = ROOT / "packaging" / "_version.py"
    spec = importlib.util.spec_from_file_location("_desktop_version", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _project_tree(root: Path) -> Path:
    root.mkdir()
    shutil.copyfile(PYPROJECT, root / "pyproject.toml")
    shutil.copyfile(ROOT / ".gitignore", root / ".gitignore")
    for package in ("wattracker", "wattracker_connector"):
        package_root = root / package
        package_root.mkdir()
        (package_root / "__init__.py").write_text('"""Version fixture."""\n', encoding="utf-8")
    return root


def _git_repo(root: Path) -> Path:
    _project_tree(root)
    _git(root, "init", "--quiet")
    _git(root, "config", "user.name", "Desktop version test")
    _git(root, "config", "user.email", "desktop-version@example.invalid")
    (root / "README.md").write_text("version fixture\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "--quiet", "-m", "initial")
    _git(root, "branch", "-M", "main")
    return root


def _commit(root: Path, message: str) -> None:
    (root / "README.md").write_text(f"{message}\n", encoding="utf-8")
    _git(root, "add", "README.md")
    _git(root, "commit", "--quiet", "-m", message)


def _wheel_versions(root: Path, wheel_directory: Path) -> tuple[str, str]:
    """Build with the configured PEP 517 backend and read its version outputs."""
    wheel_directory.mkdir()
    subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from setuptools.build_meta import build_wheel; "
            "print(build_wheel(sys.argv[1]))",
            str(wheel_directory),
        ],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    wheels = list(wheel_directory.glob("*.whl"))
    assert len(wheels) == 1, f"expected one wheel, found {wheels}"
    with zipfile.ZipFile(wheels[0]) as wheel:
        metadata_path = next(
            name for name in wheel.namelist() if name.endswith(".dist-info/METADATA")
        )
        metadata = Parser().parsestr(wheel.read(metadata_path).decode("utf-8"))
        scm_version = wheel.read("wattracker/_scm_version.py").decode("utf-8")
    match = re.search(
        r"(?m)^__version__\s*=\s*(?:version\s*=\s*)?['\"]([^'\"]+)['\"]",
        scm_version,
    )
    assert match, f"no __version__ assignment in generated module:\n{scm_version}"
    return metadata["Version"], match.group(1)


def test_package_version_is_dynamic_and_only_uses_desktop_tags():
    import tomllib

    config = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    project = config["project"]
    scm = config["tool"]["setuptools_scm"]
    assert project["dynamic"] == ["version"]
    assert "version" not in project
    assert "setuptools-scm" in config["build-system"]["requires"][-1]
    assert scm["version_scheme"] == "no-guess-dev"
    assert scm["version_file"] == "wattracker/_scm_version.py"
    assert scm["write_to_source"] is True
    assert scm["tag"]["regex"] == r"^(?:v|preview-)(?P<version>\d+\.\d+\.\d+)$"
    assert "preview-[0-9]*" in scm["scm"]["git"]["describe_command"]
    assert "v[0-9]*" in scm["scm"]["git"]["describe_command"]


def test_runtime_packages_use_the_generated_version_with_metadata_fallback():
    source = (ROOT / "wattracker" / "__init__.py").read_text(encoding="utf-8")
    connector_source = (ROOT / "wattracker_connector" / "__init__.py").read_text(encoding="utf-8")
    assert "from ._scm_version import __version__" in source
    assert "from wattracker._scm_version import __version__" in connector_source
    assert 'version("wattracker")' in source
    assert 'version("wattracker")' in connector_source
    assert "__version__ = \"0.1.0\"" not in source
    assert "__version__ = \"0.1.0\"" not in connector_source
    assert "__version__ = \"0.1.0.0\"" not in connector_source
    assert "from wattracker._scm_version import __version__" in connector_source
    assert '"0.0.0"' in source
    assert '"0.0.0"' in connector_source


def test_packaging_version_matches_real_desktop_tag_and_ignores_ios_tags(tmp_path):
    pytest.importorskip("setuptools_scm")
    helper = _load_version_helper()
    repo = _git_repo(tmp_path / "desktop-tags")
    _git(repo, "tag", "ios-v9.9.9")
    _git(repo, "tag", "v0.2.0")
    helper_version = helper.project_version(repo)
    assert helper_version == "0.2.0"
    wheel_version, runtime_version = _wheel_versions(repo, tmp_path / "wheel-v-tag")
    assert wheel_version == runtime_version == helper_version

    _commit(repo, "preview release")
    _git(repo, "tag", "preview-0.3.0")
    helper_version = helper.project_version(repo)
    assert helper_version == "0.3.0"
    wheel_version, runtime_version = _wheel_versions(repo, tmp_path / "wheel-preview-tag")
    assert wheel_version == runtime_version == helper_version

    _commit(repo, "unreleased desktop change")
    _git(repo, "tag", "ios-v99.99.99")
    helper_version = helper.project_version(repo)
    assert re.fullmatch(r"0\.3\.0\.post1\.dev1\+g[0-9a-f]+", helper_version)
    wheel_version, runtime_version = _wheel_versions(repo, tmp_path / "wheel-ios-tag")
    assert wheel_version == runtime_version == helper_version


def test_git_version_can_be_normalized_for_windows_and_macos_metadata():
    helper = _load_version_helper()
    for version, release, encoded_patch in (
        ("0.1.0.post1.dev31+g25e7220", (0, 1, 0), 31),
        ("0.1.0+31.g25e7220", (0, 1, 0), 31),
        ("0.1.0", (0, 1, 0), 0),
        ("0.1.5", (0, 1, 5), 5 * BUILDS_PER_PATCH),
    ):
        parsed = Version(version)
        assert parsed.release[:3] == release
        assert helper.windows_file_version(version) == (0, 1, encoded_patch, 0)
        assert helper.macos_short_version(version) == ".".join(map(str, release))
        assert helper.macos_build_version(version) == f"{release[0]}.{release[1]}.{encoded_patch}"

    preview = helper.windows_file_version("0.1.0.post1.dev31+g25e7220")
    next_patch_release = helper.windows_file_version("0.1.1")
    assert preview < next_patch_release
    preview_macos = tuple(
        map(int, helper.macos_build_version("0.1.0.post1.dev31+g25e7220").split("."))
    )
    next_patch_macos = tuple(map(int, helper.macos_build_version("0.1.1").split(".")))
    assert preview_macos < next_patch_macos


def test_project_version_matches_wheel_fallback_without_git_or_with_shallow_clone(tmp_path):
    pytest.importorskip("setuptools_scm")
    helper = _load_version_helper()

    source = tmp_path / "source-without-git"
    _project_tree(source)
    helper_version = helper.project_version(source)
    assert helper_version == "0.0.0"
    wheel_version, runtime_version = _wheel_versions(source, tmp_path / "wheel-no-git")
    assert wheel_version == runtime_version == helper_version

    origin = _git_repo(tmp_path / "shallow-origin")
    _git(origin, "tag", "v0.2.0")
    _commit(origin, "commit hidden by shallow clone")
    shallow = tmp_path / "shallow-clone"
    subprocess.run(
        ["git", "clone", "--quiet", "--depth", "1", "--no-tags", f"file://{origin}", str(shallow)],
        check=True,
        capture_output=True,
        text=True,
    )
    shallow_version = helper.project_version(shallow)
    wheel_version, runtime_version = _wheel_versions(shallow, tmp_path / "wheel-shallow")
    assert wheel_version == runtime_version == shallow_version


def test_setuptools_scm_current_checkout_version_matches_distribution_metadata():
    pytest.importorskip("setuptools_scm")
    helper = _load_version_helper()
    derived = helper.project_version(ROOT)
    installed = importlib.metadata.version("wattracker")
    import wattracker
    import wattracker_connector

    assert Version(derived) == Version(installed)
    assert Version(wattracker.__version__) == Version(derived)
    assert Version(wattracker_connector.__version__) == Version(derived)
    assert re.fullmatch(
        r"\d+\.\d+\.\d+(?:\.post1\.dev\d+\+g[0-9a-f]+(?:\.d\d{8})?)?",
        derived,
    )


def test_rolling_publish_has_one_write_job_and_short_artifact_handoff():
    workflow = WINDOWS.read_text(encoding="utf-8")
    before_publisher, publisher = workflow.split("  publish-preview:\n", 1)
    assert "permissions:\n  contents: read" in workflow
    assert "contents: write" not in before_publisher
    assert "permissions:\n      contents: write" in publisher
    assert "pattern: desktop-preview-*" in publisher
    assert "retention-days: 1" in workflow
    assert "preview-latest" in publisher and "--prerelease" in publisher
    assert 'git ls-remote --heads origin main' in publisher
    assert 'git ls-remote --refs origin refs/tags/preview-latest' in publisher
    assert '"$GITHUB_SHA" != "$main_sha"' in publisher
    assert '"$GITHUB_SHA" != "$main_sha_now"' in publisher
    assert '"$GITHUB_SHA" != "$preview_sha"' in publisher
    assert '"$GITHUB_SHA" != "$observed_preview_sha"' in publisher
    assert '--force-with-lease=refs/tags/preview-latest:${observed_preview_ref}' in publisher
    assert "gh release upload preview-latest" in publisher
    assert "gh release edit preview-latest" in publisher
    assert "--target \"$GITHUB_SHA\"" in publisher
    assert "gh release create preview-latest" in publisher
    assert "--verify-tag" in publisher
    assert "gh release delete preview-latest" not in publisher
    assert 'tags:\n      - "v*"' in (
        ROOT / ".github" / "workflows" / "macos-release.yml"
    ).read_text(encoding="utf-8")
    assert 'tags:\n      - "v*"' in (
        ROOT / ".github" / "workflows" / "windows-release.yml"
    ).read_text(encoding="utf-8")


def test_desktop_drift_check_is_scheduled_and_read_only():
    workflow = DRIFT_WORKFLOW.read_text(encoding="utf-8")
    assert "schedule:" in workflow
    assert "scripts/check_desktop_release_drift.py" in workflow
    assert "contents: read" in workflow
    assert "contents: write" not in workflow
