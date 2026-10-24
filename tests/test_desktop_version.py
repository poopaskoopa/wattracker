from __future__ import annotations

import importlib.util
import os
import re
import runpy
import shutil
import subprocess
import sys
import types
import zipfile
from email.parser import Parser
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


def _fast_import_commits(root: Path, count: int) -> None:
    parent = _git(root, "rev-parse", "HEAD")
    commands = []
    for index in range(count):
        content = f"fast-import commit {index}\n"
        mark = index + 1
        commands.append(
            f"commit refs/heads/main\nmark :{mark}\n"
            f"author Version Test <version@example.invalid> {1_700_000_000 + index} +0000\n"
            f"committer Version Test <version@example.invalid> {1_700_000_000 + index} +0000\n"
            f"data {len(content.encode())}\n{content}"
            f"from {parent if index == 0 else ':' + str(mark - 1)}\n"
            f"M 100644 inline README.md\ndata {len(content.encode())}\n{content}\n"
        )
    subprocess.run(
        ["git", "-C", str(root), "fast-import", "--quiet"],
        input="".join(commands),
        check=True,
        capture_output=True,
        text=True,
    )
    _git(root, "reset", "--hard", "HEAD")


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
    return _read_wheel_versions(wheel_directory)


def _pip_wheel_versions(
    root: Path, wheel_directory: Path, env: dict[str, str]
) -> tuple[str, str]:
    """Build through pip, with its PEP 517 frontend and the supplied environment."""
    wheel_directory.mkdir()
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            "--no-build-isolation",
            "--no-deps",
            "--wheel-dir",
            str(wheel_directory),
            ".",
        ],
        cwd=root,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    return _read_wheel_versions(wheel_directory)


def _read_wheel_versions(wheel_directory: Path) -> tuple[str, str]:
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


def _minimal_path(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    """PATH with only git and a versioned interpreter name, no python aliases."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    git = shutil.which("git")
    assert git is not None
    git_bin = tmp_path / "git-bin"
    interpreter_bin = tmp_path / "interpreter-bin"
    git_bin.mkdir()
    interpreter_bin.mkdir()
    (git_bin / "git").symlink_to(git)
    (interpreter_bin / "python3.12").symlink_to(sys.executable)
    path = os.pathsep.join((str(git_bin), str(interpreter_bin)))
    assert shutil.which("python", path=path) is None
    assert shutil.which("python3", path=path) is None
    env = {"HOME": str(tmp_path / "home"), "PATH": path, "PYTHONDONTWRITEBYTECODE": "1"}
    Path(env["HOME"]).mkdir()
    return tmp_path, env


def test_package_version_is_dynamic_and_only_uses_desktop_tags():
    import tomllib

    config = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    project = config["project"]
    scm = config["tool"]["setuptools_scm"]
    assert project["dynamic"] == ["version"]
    assert "version" not in project
    assert config["build-system"]["requires"][-1] == "setuptools-scm>=10.0.4"
    assert "setuptools-scm>=10.0.4" in project["optional-dependencies"]["dev"][-1]
    assert "setuptools-scm>=10.0.4" in project["optional-dependencies"]["package"][-1]
    assert scm["version_scheme"] == "no-guess-dev"
    assert scm["version_file"] == "wattracker/_scm_version.py"
    assert scm["write_to_source"] is True
    assert scm["tag"]["regex"] == (
        r"^(?:v|preview-)(?P<version>(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\."
        r"(?:0|[1-9]\d*))$"
    )
    command = scm["scm"]["git"]["describe_command"]
    assert command == _load_version_helper().DESCRIBE_COMMAND
    assert command[0] == "git"
    assert "python" not in command and "python3" not in command
    assert "strict" not in scm["tag"]


def test_desktop_tag_contract_matches_scm_describe_and_drift_checker(tmp_path):
    pytest.importorskip("setuptools_scm")
    import tomllib

    config = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    scm_regex = re.compile(config["tool"]["setuptools_scm"]["tag"]["regex"])
    command = config["tool"]["setuptools_scm"]["scm"]["git"]["describe_command"]
    helper = _load_version_helper()
    drift_spec = importlib.util.spec_from_file_location(
        "_desktop_drift", ROOT / "scripts" / "check_desktop_release_drift.py"
    )
    drift = importlib.util.module_from_spec(drift_spec)
    drift_spec.loader.exec_module(drift)
    valid = ("v0.4.0", "preview-0.4.0", "v1.0.10")
    invalid = (
        "v0.4.0-rc1",
        "v0.4.0rc1",
        "v0.5.0-beta.1",
        "v0.4.0-rc1-suffix",
        "preview-0.4.0-rc1x",
        "v1.0",
        "v01.0.0",
        "v0.1.00",
        "ios-v9.9.9",
        "preview-latest",
    )
    for tag in valid:
        assert scm_regex.fullmatch(tag)
        assert helper._DESKTOP_TAG.fullmatch(tag)
        assert drift._VERSION_TAG.fullmatch(tag)
    for tag in invalid:
        assert scm_regex.fullmatch(tag) is None
        assert helper._DESKTOP_TAG.fullmatch(tag) is None
        assert drift._VERSION_TAG.fullmatch(tag) is None

    repo = _git_repo(tmp_path / "describe-contract")
    _git(repo, "tag", "v0.2.7")
    for index, tag in enumerate(
        (
            "ios-v9.9.9",
            "v0.4.0-rc1",
            "v0.4.0rc1",
            "v0.5.0-beta.1",
            "v1.0",
            "v01.0.0",
            "v0.1.00",
            "preview-latest",
        )
    ):
        _commit(repo, f"ignored tag {index}")
        _git(repo, "tag", tag)
        output = subprocess.run(
            command,
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert output.startswith("v0.2.7-")
    _commit(repo, "valid preview tag")
    _git(repo, "tag", "preview-0.5.0")
    _commit(repo, "annotated iOS tag past desktop tag")
    _git(repo, "tag", "-a", "ios-v10.0.0", "-m", "iOS only")
    output = subprocess.run(
        command,
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert output.startswith("preview-0.5.0-")
    assert (
        Version(_load_version_helper().project_version(repo)).public
        == Version("0.5.0.post1.dev1").public
    )


def test_helper_and_pip_wheel_work_without_python_names_on_path(tmp_path, monkeypatch):
    pytest.importorskip("setuptools_scm")
    helper = _load_version_helper()
    repo = _git_repo(tmp_path / "minimal-path-repo")
    _git(repo, "tag", "v0.2.7")
    _, env = _minimal_path(tmp_path / "minimal-path")

    # Exercise the helper with only the two deliberate PATH entries. The pip
    # subprocess receives a clean environment with that same minimal PATH.
    monkeypatch.setenv("PATH", env["PATH"])
    helper_version = helper.project_version(repo)
    wheel_version, runtime_version = _pip_wheel_versions(
        repo, tmp_path / "minimal-path-wheel", env
    )

    assert helper_version == wheel_version == runtime_version == "0.2.7"


def test_helper_raises_when_the_configured_describe_command_fails(tmp_path):
    pytest.importorskip("setuptools_scm")
    helper = _load_version_helper()
    repo = _git_repo(tmp_path / "broken-describe")
    _git(repo, "tag", "v0.2.7")
    pyproject = repo / "pyproject.toml"
    pyproject.write_text(
        pyproject.read_text(encoding="utf-8").replace(
            '"describe",', '"deliberately-invalid-describe-subcommand",', 1
        ),
        encoding="utf-8",
    )

    with pytest.raises(subprocess.CalledProcessError, match="returned non-zero exit status"):
        helper.project_version(repo)


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
    _git(repo, "tag", "v0.4.0junk1")
    _git(repo, "tag", "v0.2.0")
    helper_version = helper.project_version(repo)
    assert helper_version == "0.2.0"
    wheel_version, runtime_version = _wheel_versions(repo, tmp_path / "wheel-v-tag")
    assert wheel_version == runtime_version == helper_version

    _commit(repo, "preview release")
    _git(repo, "tag", "preview-0.3.0")
    helper_version = helper.project_version(repo)
    assert Version(helper_version) == Version("0.3.0")
    wheel_version, runtime_version = _wheel_versions(repo, tmp_path / "wheel-preview-tag")
    assert wheel_version == runtime_version == helper_version

    _commit(repo, "unreleased desktop change")
    _git(repo, "tag", "v0.3.0-rc1-suffix")
    _git(repo, "tag", "ios-v99.99.99")
    helper_version = helper.project_version(repo)
    assert re.fullmatch(r"0\.3\.0\.post1\.dev1\+g[0-9a-f]+", helper_version)
    wheel_version, runtime_version = _wheel_versions(repo, tmp_path / "wheel-ios-tag")
    assert wheel_version == runtime_version == helper_version


def test_native_build_versions_are_monotonic_and_tag_independent(tmp_path):
    pytest.importorskip("setuptools_scm")
    helper = _load_version_helper()
    repo = _git_repo(tmp_path / "build-sequence")
    _git(repo, "tag", "ios-v99.99.99")
    _git(repo, "tag", "v0.2.7")
    preview_id = helper.git_build_id(repo)
    preview_version = helper.project_version(repo)
    preview_windows = helper.windows_file_version(preview_version, root=repo)
    assert preview_windows == (0, 2, 0, preview_id)
    assert helper.macos_short_version(preview_version) == "0.2.7"
    preview_macos = tuple(map(int, helper.macos_build_version(preview_version, root=repo).split(".")))

    # A stable release tag can be added to the exact SHA that published a
    # preview. The added reachable desktop tag must still advance the sequence.
    _git(repo, "tag", "v1.0.10")
    stable_id = helper.git_build_id(repo)
    stable_version = helper.project_version(repo)
    assert stable_id == preview_id + 1
    stable_windows = helper.windows_file_version(stable_version, root=repo)
    assert stable_windows > preview_windows
    stable_macos = tuple(map(int, helper.macos_build_version(stable_version, root=repo).split(".")))
    assert stable_macos > preview_macos

    _commit(repo, "next preview commit")
    next_id = helper.git_build_id(repo)
    assert next_id == stable_id + 1
    assert helper.windows_file_version(helper.project_version(repo), root=repo) > stable_windows
    assert helper.windows_file_version("0.2.7", root=repo)[0:2] == (0, 2)
    assert helper.windows_file_version("1.0.10", root=repo)[0:2] == (1, 0)



def test_project_version_matches_wheel_fallback_without_git_or_with_shallow_clone(tmp_path):
    pytest.importorskip("setuptools_scm")
    helper = _load_version_helper()

    source = tmp_path / "source-without-git"
    _project_tree(source)
    helper_version = helper.project_version(source)
    assert helper_version == "0.0.0"
    wheel_version, runtime_version = _wheel_versions(source, tmp_path / "wheel-no-git")
    assert wheel_version == runtime_version == helper_version

    ios_only = _git_repo(tmp_path / "ios-only-tags")
    _git(ios_only, "tag", "ios-v9.9.9")
    ios_only_version = helper.project_version(ios_only)
    assert Version(ios_only_version).release == (0, 0, 0)
    assert "9.9.9" not in ios_only_version
    wheel_version, runtime_version = _wheel_versions(ios_only, tmp_path / "wheel-ios-only")
    assert wheel_version == runtime_version == ios_only_version

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
    with pytest.raises(SystemExit, match="require a full-history Git checkout"):
        helper.git_build_id(shallow)


def test_real_specs_encode_exact_release_tags_and_large_dev_distance(tmp_path, monkeypatch):
    pytest.importorskip("setuptools_scm")
    repo = _git_repo(tmp_path / "spec-repo")
    packaging = repo / "packaging"
    packaging.mkdir(exist_ok=True)
    for name in ("_version.py", "_connector_excludes.py", "wattracker.spec", "wattracker-connector.spec"):
        shutil.copyfile(ROOT / "packaging" / name, packaging / name)
    _git(repo, "add", "packaging")
    _git(repo, "commit", "--quiet", "-m", "add actual specs")
    _git(repo, "tag", "v0.2.7")

    class Record:
        def __init__(self, *args, **kwargs):
            self.args, self.kwargs = args, kwargs

    class Analysis(Record):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.pure = []
            self.scripts = self.binaries = self.datas = []

    hooks = types.ModuleType("PyInstaller.utils.hooks")
    hooks.collect_submodules = lambda _name: []
    pyinstaller = types.ModuleType("PyInstaller")
    utils = types.ModuleType("PyInstaller.utils")
    pyinstaller.utils = utils
    utils.hooks = hooks
    monkeypatch.setitem(sys.modules, "PyInstaller", pyinstaller)
    monkeypatch.setitem(sys.modules, "PyInstaller.utils", utils)
    monkeypatch.setitem(sys.modules, "PyInstaller.utils.hooks", hooks)
    monkeypatch.setattr(sys, "platform", "darwin")
    mac_globals = {
        "SPECPATH": str(packaging), "Analysis": Analysis, "PYZ": Record,
        "EXE": Record, "COLLECT": Record,
        "BUNDLE": lambda *args, **kwargs: Record(*args, **kwargs),
    }
    mac = runpy.run_path(str(packaging / "wattracker.spec"), init_globals=mac_globals)
    assert mac["app"].kwargs["info_plist"]["CFBundleShortVersionString"] == "0.2.7"
    exact_macos = tuple(map(int, mac["app"].kwargs["info_plist"]["CFBundleVersion"].split(".")))

    monkeypatch.setattr(sys, "platform", "win32")
    versioninfo = types.ModuleType("PyInstaller.utils.win32.versioninfo")
    for name in ("FixedFileInfo", "StringFileInfo", "StringStruct", "StringTable", "VarFileInfo", "VarStruct", "VSVersionInfo"):
        setattr(versioninfo, name, type(name, (Record,), {}))
    win32 = types.ModuleType("PyInstaller.utils.win32")
    win32.versioninfo = versioninfo
    monkeypatch.setitem(sys.modules, "PyInstaller.utils.win32", win32)
    monkeypatch.setitem(sys.modules, "PyInstaller.utils.win32.versioninfo", versioninfo)
    connector_globals = {
        "SPECPATH": str(packaging), "Analysis": Analysis, "PYZ": Record, "EXE": Record,
    }
    connector = runpy.run_path(str(packaging / "wattracker-connector.spec"), init_globals=connector_globals)
    resource = connector["_version_resource"]()
    assert resource.kwargs["ffi"].kwargs["filevers"][:2] == (0, 2)

    # Exercise a real 10,001-commit describe distance through both specs.
    _fast_import_commits(repo, 10_001)
    monkeypatch.setattr(sys, "platform", "darwin")
    preview_mac = runpy.run_path(str(packaging / "wattracker.spec"), init_globals=mac_globals)
    preview_macos = tuple(map(int, preview_mac["app"].kwargs["info_plist"]["CFBundleVersion"].split(".")))
    assert Version(preview_mac["project_version"]).dev == 10_001
    monkeypatch.setattr(sys, "platform", "win32")
    preview_connector = runpy.run_path(str(packaging / "wattracker-connector.spec"), init_globals=connector_globals)
    preview_windows = preview_connector["_version_resource"]().kwargs["ffi"].kwargs["filevers"]
    assert preview_windows[0:2] == (0, 2)
    assert preview_macos > exact_macos

    # A stable release tag added to this same SHA advances the build sequence.
    _git(repo, "tag", "v1.0.10")
    stable_id = _load_version_helper().git_build_id(repo)
    monkeypatch.setattr(sys, "platform", "darwin")
    stable_mac = runpy.run_path(str(packaging / "wattracker.spec"), init_globals=mac_globals)
    assert stable_mac["project_version"] == "1.0.10"
    stable_macos = tuple(map(int, stable_mac["app"].kwargs["info_plist"]["CFBundleVersion"].split(".")))
    assert stable_macos > preview_macos
    monkeypatch.setattr(sys, "platform", "win32")
    stable_connector = runpy.run_path(str(packaging / "wattracker-connector.spec"), init_globals=connector_globals)
    stable_windows = stable_connector["_version_resource"]().kwargs["ffi"].kwargs["filevers"]
    assert stable_windows[0:2] == (1, 0)
    assert stable_windows > preview_windows
    assert stable_id > 0


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
    assert '--force-with-lease=refs/tags/preview-latest:${expected_preview_ref}' in publisher
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
