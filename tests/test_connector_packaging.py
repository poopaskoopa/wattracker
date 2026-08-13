"""Guards on the connector's frozen build.

Nothing here can build the artifact - the release job is hard-disabled and the
runners are the wrong OS anyway - so these assert from the checkout, in the
same style and for the same reason as test_windows_installer.py and
test_macos_packaging.py: the invariants that would otherwise be noticed only by
shipping a broken binary.

The tray and autostart guards the plan calls for (WP-B, WP-C) are deliberately
absent: those modules do not exist yet, and a test asserting things about a
file nobody has written is a test that passes for the wrong reason. They land
with the code they describe. What is here covers WP-E, WP-G, and the two
properties the smoke script depends on.
"""
import importlib.util
import re
from pathlib import Path

ROOT = Path(__file__).parents[1]
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


def test_the_app_installer_still_has_no_startup_entry():
    """Autostart is the connector's business, at runtime, in HKCU only.

    test_windows_installer.py asserts this too. Repeated here because WP-C is
    the change most likely to be "helpfully" implemented by adding a startup
    entry to the installer instead, which is what that assertion forbids.
    """
    assert "run at startup" not in ISS.lower()
    assert "WattrackerConnector" not in ISS
