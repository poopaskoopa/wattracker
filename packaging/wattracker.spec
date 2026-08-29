# -*- mode: python ; coding: utf-8 -*-
"""One spec, two platforms.

Analysis (entry point, datas, hidden imports) is shared: a Windows-only and a
macOS-only spec would drift, and the thing most likely to break a frozen build
is a missing hidden import or a missing template/static tree - exactly the parts
that must stay identical. Only what genuinely differs per OS is branched:

* bleak picks its backend at runtime, so each OS needs its own collected - WinRT
  on Windows, CoreBluetooth on macOS - and neither module exists on the other;
* macOS additionally wraps the onedir COLLECT in a BUNDLE so Finder sees a real
  wattracker.app.
"""
import importlib.util
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

root = Path(SPECPATH).parent
IS_WINDOWS = sys.platform.startswith("win")
IS_MACOS = sys.platform == "darwin"


def _load(name):
    """Load a sibling helper by path.

    Never ``import``: this directory is named ``packaging``, and so is a widely
    installed PyPI distribution, so an import would resolve to that one on most
    machines. wattracker-connector.spec loads the same helper the same way.
    """
    path = Path(SPECPATH) / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"_wattracker_{name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _project_version() -> str:
    """version from pyproject.toml, so the bundle cannot drift from the wheel."""
    return _load("_version").project_version(root)


hidden = ["wattracker.server"] + collect_submodules("uvicorn")
# scipy vendors array-api-compat, whose numpy shim pulls two of its own
# submodules through a string-built __import__ so the package stays vendorable:
#
#     __import__(__package__ + ".linalg")
#     __import__(__package__ + ".fft")
#
# PyInstaller's modulegraph is static, so it sees neither. linalg survives by
# accident - a plain `from .linalg import ...` follows two lines later - and fft
# has no such static twin, so it is the one module that goes missing. The build
# still succeeds and the frozen app then dies on first import of scipy.optimize,
# which server.py reaches via races -> metrics.curve. Collect the subpackage.
hidden += collect_submodules("scipy._external.array_api_compat")
# keyring resolves its backend at runtime, so nothing static imports the
# platform vault module. Collecting every backend covers Windows Credential
# Manager (keyring.backends.Windows) and the macOS Keychain
# (keyring.backends.macOS, whose api submodule is ctypes-loaded, not linked).
hidden += collect_submodules("keyring.backends")
# bleak is an optional extra and its backend is resolved at runtime, so nothing
# static imports it. Missing bleak is not a build error: the app feature-detects
# it and the ride page degrades to Simulate.
if IS_WINDOWS or IS_MACOS:
    ble_backend = "bleak.backends.winrt" if IS_WINDOWS else "bleak.backends.corebluetooth"
    try:
        hidden += collect_submodules(ble_backend)
    except Exception:
        pass

a = Analysis(
    [str(root / "packaging" / "wattracker_entry.py")],
    pathex=[str(root)],
    binaries=[],
    datas=[
        (str(root / "wattracker" / "web" / "templates"), "wattracker/web/templates"),
        (str(root / "wattracker" / "web" / "static"), "wattracker/web/static"),
    ],
    hiddenimports=hidden,
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="wattracker", console=True)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="wattracker")

if IS_MACOS:
    # console=True above is deliberate and applies to the bundled binary too:
    # dist/wattracker/wattracker stays a usable CLI, and inside the .app macOS
    # simply discards stdout when Finder launches it with no terminal attached.
    #
    # That discard used to be free, because wattracker only printed its URL and
    # then served. It is not free any more: on an install with no account the
    # app prints a one-time setup token which the first registration has to
    # present (wattracker/setuptoken.py), so a Finder-launched .app cannot
    # complete its own first run. docs/macos-packaging.md ("First run") carries
    # the workaround - launch once from a terminal, or via `open --stdout` - and
    # lists the gap. Do not "fix" it by writing the token to disk or by
    # weakening the check: the token's whole value is that it travels only where
    # the operator can see it.
    #
    # LSUIElement=True: this process runs a uvicorn server and hands the user
    # off to their browser. It has no Cocoa event loop, so a regular Dock icon
    # would never answer AppKit and macOS would flag it "Application Not
    # Responding" and offer a Force Quit that is really a kill. An agent app
    # gets no Dock icon and no unresponsive-app UI; the browser tab is the UI.
    # The cost is that quitting means Activity Monitor (documented in
    # docs/macos-packaging.md).
    app = BUNDLE(
        coll,
        name="wattracker.app",
        icon=None,
        bundle_identifier="com.wattracker.wattracker",
        version=_project_version(),
        info_plist={
            "CFBundleShortVersionString": _project_version(),
            "CFBundleVersion": _project_version(),
            "LSUIElement": True,
            "NSHighResolutionCapable": True,
            # Oldest macOS the arm64 runners and PyInstaller bootloader support.
            "LSMinimumSystemVersion": "11.0",
            # Required before CoreBluetooth may be used: without a purpose
            # string macOS terminates the process the moment the ride page
            # starts a scan, rather than showing a permission prompt.
            "NSBluetoothAlwaysUsageDescription":
                "wattracker connects to your trainer, power meter and heart rate "
                "monitor over Bluetooth during a ride.",
        },
    )
