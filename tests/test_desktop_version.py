from __future__ import annotations

import importlib.metadata
import importlib.util
import re
import sys
import types
from pathlib import Path

import pytest
from packaging.version import Version

ROOT = Path(__file__).parents[1]
PYPROJECT = ROOT / "pyproject.toml"
WINDOWS = ROOT / ".github" / "workflows" / "windows.yml"
DRIFT_WORKFLOW = ROOT / ".github" / "workflows" / "desktop-release-drift.yml"


def _load_version_helper():
    path = ROOT / "packaging" / "_version.py"
    spec = importlib.util.spec_from_file_location("_desktop_version", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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
    assert "0+unknown" in source


def test_packaging_version_helper_delegates_to_setuptools_scm(monkeypatch):
    helper = _load_version_helper()
    expected = "0.1.0.post1.dev29+g25e7220"
    fake_scm = types.ModuleType("setuptools_scm")
    calls = []

    def get_version(*, root):
        calls.append(root)
        return expected

    fake_scm.get_version = get_version
    monkeypatch.setitem(sys.modules, "setuptools_scm", fake_scm)
    assert helper.project_version(ROOT) == expected
    assert calls == [ROOT]


def test_git_version_can_be_normalized_for_windows_and_macos_metadata():
    helper = _load_version_helper()
    for version, build_patch in (
        ("0.1.0.post1.dev29+g25e7220", 29),
        ("0.1.0+29.g25e7220", 29),
        ("0.1.0", 0),
    ):
        parsed = Version(version)
        assert parsed.release[:3] == (0, 1, 0)
        assert helper.windows_file_version(version) == (0, 1, build_patch, 0)
        assert helper.macos_short_version(version) == "0.1.0"
        assert helper.macos_build_version(version) == f"0.1.{build_patch}"


def test_setuptools_scm_current_checkout_version_matches_distribution_metadata():
    pytest.importorskip("setuptools_scm")
    helper = _load_version_helper()
    derived = helper.project_version(ROOT)
    installed = importlib.metadata.version("wattracker")
    assert Version(derived) == Version(installed)
    assert re.fullmatch(r"\d+\.\d+\.\d+\.post1\.dev\d+\+g[0-9a-f]+(?:\.d\d{8})?", derived)


def test_rolling_publish_has_one_write_job_and_short_artifact_handoff():
    workflow = WINDOWS.read_text(encoding="utf-8")
    before_publisher, publisher = workflow.split("  publish-preview:\n", 1)
    assert "permissions:\n  contents: read" in workflow
    assert "contents: write" not in before_publisher
    assert "permissions:\n      contents: write" in publisher
    assert "pattern: desktop-preview-*" in publisher
    assert "retention-days: 1" in workflow
    assert "preview-latest" in publisher and "--prerelease" in publisher
    assert "git ls-remote --exit-code origin refs/tags/preview-latest" in publisher
    assert 'repos/$GITHUB_REPOSITORY/git/refs/tags/preview-latest' in publisher
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
