"""Guards on the connector's frozen build.

Nothing here can build the artifact - the release job is hard-disabled and the
runners are the wrong OS anyway - so these assert from the checkout, in the
same style and for the same reason as test_windows_installer.py and
test_macos_packaging.py: the invariants that would otherwise be noticed only by
shipping a broken binary.

The tray and autostart guards the plan called for (WP-B, WP-C) landed with the
code they describe, which is the section at the end. What came first covers
WP-E, WP-G, and the two properties the smoke script depends on.
"""
import ast
import importlib.util
import io
import re
import tokenize
from pathlib import Path

ROOT = Path(__file__).parents[1]


def _code(text: str) -> str:
    """The module with its prose stripped out.

    A guard that forbids ``HKLM`` must not be tripped by a comment explaining
    why HKLM is not used - otherwise the only way to keep the test passing is
    to stop writing down the reason, which is backwards.
    """
    lines = text.splitlines()
    for token in tokenize.generate_tokens(io.StringIO(text).readline):
        if token.type == tokenize.COMMENT:
            row, column = token.start
            lines[row - 1] = lines[row - 1][:column]
        elif token.type == tokenize.STRING and token.line.lstrip().startswith(
            ('"""', "'''")
        ):
            # A docstring: whole lines, so the code around it keeps its shape
            # and an assertion can still look for a phrase spanning two words.
            for row in range(token.start[0], token.end[0] + 1):
                lines[row - 1] = ""
    return "\n".join(lines)


AUTOSTART = _code(
    (ROOT / "wattracker_connector" / "autostart.py").read_text(encoding="utf-8")
)
TRAY = _code(
    (ROOT / "wattracker_connector" / "tray_win32.py").read_text(encoding="utf-8")
)


def _module_ast(name: str) -> ast.Module:
    path = ROOT / "wattracker_connector" / f"{name}.py"
    return ast.parse(path.read_text(encoding="utf-8"))


def _assert_defines_only(statement, module: str) -> None:
    """Fail if a module-scope statement does anything beyond defining a name.

    Both of these modules are imported by processes that must not be affected
    by having imported them - the connector core on Linux, and any run at all
    for autostart, whose whole promise is that it touches HKCU only when a
    rider asks. Definitions, imports and constants are fine; a call is not.
    """
    if isinstance(statement, (ast.FunctionDef, ast.ClassDef, ast.Import,
                              ast.ImportFrom)):
        return
    if isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Constant):
        return  # the docstring
    assert isinstance(statement, (ast.Assign, ast.AnnAssign)), (
        f"{module}.py runs {type(statement).__name__} at import time"
    )
    for node in ast.walk(statement):
        if not isinstance(node, ast.Call):
            continue
        assert _called_name(node) in _ALLOWED_AT_IMPORT, (
            f"{module}.py calls {_called_name(node)}() at import time, "
            f"line {statement.lineno}"
        )


# Getting a logger is the one thing every module in this repository does at
# import time, and the only thing either of these two may do.
_ALLOWED_AT_IMPORT = {"logging.getLogger"}


def _called_name(node: ast.Call) -> str:
    parts = []
    target = node.func
    while isinstance(target, ast.Attribute):
        parts.append(target.attr)
        target = target.value
    if isinstance(target, ast.Name):
        parts.append(target.id)
    return ".".join(reversed(parts))
SPEC = (ROOT / "packaging" / "wattracker-connector.spec").read_text(encoding="utf-8")
APP_SPEC = (ROOT / "packaging" / "wattracker.spec").read_text(encoding="utf-8")
SMOKE = (ROOT / "packaging" / "smoke_frozen_connector.py").read_text(encoding="utf-8")
WORKFLOW = (
    ROOT / ".github" / "workflows" / "windows-release.yml"
).read_text(encoding="utf-8")
ISS = (ROOT / "packaging" / "wattracker.iss").read_text(encoding="utf-8")


def _load(name):
    """Load a packaging helper by path, as both specs do.

    Never by import: the directory is called "packaging" and so is an
    installed PyPI distribution.
    """
    path = ROOT / "packaging" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"_test_{name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ------------------------------------------------------------------ the shape
def test_the_connector_builds_as_one_windowed_file():
    """Three properties, each of which the rider would feel if it flipped.

    onefile is the entire point (drop one file, tick a box); console=False is
    what a tray app is; and a name without a version in it is what lets WP-C
    put a path in the registry that survives an upgrade.
    """
    assert 'name="WattrackerConnector"' in SPEC
    assert "console=False" in SPEC
    # onefile is "binaries and datas passed to EXE", as against the app spec's
    # onedir shape, which holds them back for COLLECT.
    assert "COLLECT(" not in SPEC
    assert "exclude_binaries" not in SPEC
    assert "a.binaries," in SPEC and "a.datas," in SPEC
    # The app spec is the onedir counter-example, so this stays a real contrast
    # rather than a coincidence.
    assert "COLLECT(" in APP_SPEC


def test_the_frozen_entry_point_is_a_script_and_not_the_package_main():
    """The binary could not start at all until this was true.

    PyInstaller runs its entry file as the top-level ``__main__`` with no
    package around it, so ``from .client import ...`` raises ImportError before
    anything else happens. In a ``console=False`` build that surfaces as a
    modal dialog on a machine nobody is looking at - which is why the first
    build of this spec did not fail a smoke run, it hung one.
    """
    assert "wattracker_connector_entry.py" in SPEC
    assert '"wattracker_connector" / "__main__.py"' not in SPEC
    entry = (
        ROOT / "packaging" / "wattracker_connector_entry.py"
    ).read_text(encoding="utf-8")
    assert "from wattracker_connector.__main__ import main" in entry
    assert "raise SystemExit(main())" in entry
    # The app spec has always done this. Asserted here so the two stay one
    # rule rather than one habit and one accident.
    assert "wattracker_entry.py" in APP_SPEC


def test_the_spec_refuses_to_build_on_the_wrong_os():
    """A onefile windowed binary built elsewhere is not a Windows connector."""
    assert 'if not sys.platform.startswith("win"):' in SPEC
    assert "raise SystemExit(" in SPEC


def test_the_exclude_list_is_the_one_the_import_test_enforces():
    """The spec and the import-weight test must not drift apart.

    The dangerous direction is the test drifting ahead: it would keep passing
    while the exclude list quietly stopped matching what the code imports, and
    the first symptom would be an artifact four times the size it should be.
    """
    from test_connector_client import FORBIDDEN

    assert FORBIDDEN == _load("_connector_excludes").FORBIDDEN
    # Read, not repeated: a literal list in the spec is exactly the drift this
    # is here to prevent.
    assert "excludes=FORBIDDEN" in SPEC
    assert '"numpy"' not in SPEC


def test_nothing_quietly_falls_off_the_exclude_list():
    """The content, not just the agreement.

    Now that one list feeds both the spec and the import-weight test, the two
    cannot drift from each other - but they can drift *together*. Deleting an
    entry would make both keep passing while the connector grew a licence to
    import the thing that was removed, and the import test only fails on
    modules the list still names. So the entries that matter are named here.
    """
    forbidden = set(_load("_connector_excludes").FORBIDDEN)
    # The analysis stack. Any one of these roughly quadruples the artifact.
    assert {"numpy", "pandas", "scipy"} <= forbidden
    # The web stack: the connector is a client and serves nothing.
    assert {"fastapi", "starlette", "uvicorn", "jinja2"} <= forbidden
    # The server's own halves. wattracker.db in particular would put schema
    # migrations inside a process that must never touch the database.
    assert {"wattracker.db", "wattracker.server", "wattracker.ingest"} <= forbidden


def test_the_version_helper_is_shared_with_the_app_spec():
    version = _load("_version").project_version(ROOT)
    assert re.match(r"^\d+\.\d+", version), version
    for spec in (SPEC, APP_SPEC):
        assert '_load("_version")' in spec
    # Read from pyproject, never typed into a spec.
    assert version not in SPEC


def test_optional_halves_are_collected_best_effort():
    """Neither is reachable by static analysis, and neither is worth a failure.

    bleak picks its backend at runtime; webviewpy loads a native. A connector
    missing either still runs - without a radio, or with the browser fallback
    instead of a window - so the build must not die when they are absent.
    """
    assert 'collect_submodules("bleak.backends.winrt")' in SPEC
    assert "import webviewpy" in SPEC
    assert SPEC.count("except Exception:") >= 3


def test_the_tray_icon_is_the_apps_own_favicon():
    """The icon in the notification area is the one in the browser tab."""
    assert '"favicon.ico"' in SPEC
    assert "icon=str(root" in SPEC


# ------------------------------------------------------------------ the smoke
def test_the_smoke_script_cannot_touch_real_configuration():
    """The bar test_macos_packaging.py sets for its own smoke script."""
    assert 'environment["WATTRACKER_CONNECTOR_DIR"] = str(config_dir)' in SMOKE
    assert "tempfile.TemporaryDirectory()" in SMOKE


def test_the_smoke_script_is_stdlib_only():
    """It runs against whatever python is on the build box, not the venv."""
    imports = set(re.findall(r"(?m)^(?:import|from) (\w+)", SMOKE))
    third_party = imports - {
        "base64", "hashlib", "json", "os", "pathlib", "socket", "struct",
        "subprocess", "sys", "tempfile", "threading", "time", "__future__",
    }
    assert not third_party, third_party


def test_the_smoke_script_checks_what_the_spec_collects_best_effort():
    """The two things a frozen build can silently lose."""
    assert '("bleak", "Bluetooth")' in SMOKE
    assert '("webviewpy", "the tray window")' in SMOKE


def test_the_entry_point_answers_what_the_smoke_script_asks():
    """A windowed binary has no other way to be asked a question."""
    from wattracker_connector.__main__ import _SMOKE_IMPORTABLE, _parser

    options = _parser()._option_string_actions
    assert "--smoke-import" in options
    assert "--headless" in options
    assert "--smoke-import" in SMOKE or "--headless" in SMOKE
    # Not an arbitrary-import gadget on a binary that autostarts.
    assert set(_SMOKE_IMPORTABLE) == {"bleak", "webviewpy"}


# --------------------------------------------------------------- the workflow
def test_the_release_workflow_stays_tag_only_and_hard_disabled():
    """Mirrors test_macos_packaging.py: no certificate, so nothing may run."""
    assert 'tags:\n      - "v*"' in WORKFLOW
    assert "workflow_dispatch" not in WORKFLOW
    job = WORKFLOW.split("  build-test-sign:\n", 1)[1]
    assert "    if: ${{ false }}\n" in job.split("\n    steps:", 1)[0]


def test_the_workflow_builds_smokes_and_signs_the_connector():
    """Whenever it is enabled, the connector must go through the same gate."""
    assert "packaging\\wattracker-connector.spec" in WORKFLOW
    assert "smoke_frozen_connector.py" in WORKFLOW
    assert '-ArtifactPath "dist\\WattrackerConnector.exe"' in WORKFLOW
    # Uploaded with a checksum beside it, like the app.
    assert "WattrackerConnector.exe.sha256" in WORKFLOW


# ------------------------------------------------- the tray and the autostart
def test_autostart_asks_for_no_elevation_anywhere():
    """HKCU and only HKCU.

    HKLM's Run key, a service and a scheduled task would all each work, and
    each would put a UAC prompt between a rider and a checkbox - which is the
    promise docs/windows-security.md makes in every other place it can.
    """
    assert "HKEY_CURRENT_USER" in AUTOSTART
    assert "HKEY_LOCAL_MACHINE" not in AUTOSTART
    assert "HKLM" not in AUTOSTART
    # Not a service and not a scheduled task either, both of which would be
    # the "helpful" way to make autostart survive a logoff.
    assert "schtasks" not in AUTOSTART.lower()
    assert "CreateService" not in AUTOSTART


def test_autostart_writes_nothing_when_it_is_merely_imported():
    """Nothing at module scope may reach the registry - or reach anything.

    Read from the syntax tree rather than the text, because the question is
    precisely "what runs at import time", and that is a question about
    statements at module scope rather than about which words appear where.
    """
    for statement in _module_ast("autostart").body:
        _assert_defines_only(statement, "autostart")


def test_autostart_leaves_winreg_out_of_the_import_graph():
    """It is imported inside the functions that use it, and must stay there.

    A module-scope ``import winreg`` stops this module importing on Linux,
    which is where most of the suite that covers it runs.
    """
    for statement in _module_ast("autostart").body:
        if isinstance(statement, ast.Import):
            assert "winreg" not in {alias.name for alias in statement.names}


def test_only_the_frozen_build_may_register_itself():
    """A Run value naming a venv's python.exe fails silently once it moves."""
    from wattracker_connector import autostart

    assert "is_frozen()" in AUTOSTART
    assert autostart.supported() is (
        __import__("os").name == "nt" and autostart.is_frozen()
    )


def test_the_tray_is_importable_everywhere_and_constructs_only_on_windows():
    """The shape webview.py already has, so the Linux suite can hold it."""
    import wattracker_connector.tray_win32 as tray  # imports on any OS at all

    assert tray.TrayIcon is not None
    # Nothing at import time may load a DLL or build a WINFUNCTYPE, and
    # ctypes.wintypes may not be touched at all: it raises on Linux the moment
    # it is imported, which is why every type in that module is spelled out.
    assert "wintypes" not in TRAY
    for statement in _module_ast("tray_win32").body:
        _assert_defines_only(statement, "tray_win32")
    assert 'if os.name != "nt":' in TRAY


def test_the_tray_reads_connector_status_rather_than_keeping_its_own():
    """Two copies of "are we connected" is one copy too many."""
    assert "from .client import" not in TRAY
    assert "self._status" in TRAY
    assert "status.connected" in TRAY


def test_the_icon_the_tray_loads_is_the_one_the_spec_bundles():
    """The spec's datas entry and the tray's lookup must name the same path."""
    assert '"wattracker/web/static"' in SPEC
    assert '"wattracker", "web", "static", "favicon.ico"' in TRAY
    # onefile unpacks datas under _MEIPASS, so nothing else can find it.
    assert "_MEIPASS" in TRAY


def test_the_tray_re_adds_its_icon_when_explorer_restarts():
    """Without this the icon is gone for good, and explorer does restart.

    tests/test_connector_tray.py posts the real message to a real window and
    watches the icon come back; this is the half that can be asserted from a
    checkout, and it names the trap: a message-only window is cheaper, more
    obvious, and is never sent a broadcast.
    """
    assert 'RegisterWindowMessageW(_TASKBAR_CREATED)' in TRAY
    assert "message == self._taskbar_created" in TRAY
    assert "HWND_MESSAGE" not in TRAY


def test_the_app_installer_still_has_no_startup_entry():
    """Autostart is the connector's business, at runtime, in HKCU only.

    test_windows_installer.py asserts this too. Repeated here because WP-C is
    the change most likely to be "helpfully" implemented by adding a startup
    entry to the installer instead, which is what that assertion forbids.
    """
    assert "run at startup" not in ISS.lower()
    assert "WattrackerConnector" not in ISS
