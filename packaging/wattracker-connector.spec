# -*- mode: python ; coding: utf-8 -*-
"""The connector, frozen as one portable Windows executable.

Separate from wattracker.spec on purpose: a different entry point, a different
subsystem, and an exclude list that is the opposite of the app's. Sharing one
spec between them would mean branching on nearly every line, which is how the
app spec's two *platforms* are handled only because they genuinely share their
Analysis. These two do not.

Three deliberate differences from wattracker.spec:

* **onefile**, not onedir. The rider drops one file wherever they like and
  ticks "start with Windows"; a folder of 400 files is not that. The price is
  that every launch re-extracts the payload to ``%TEMP%\\_MEIxxxx`` and deletes
  it on exit - slower to start, and the single most common reason antivirus
  heuristics take an interest in a small unsigned binary.
* **console=False**. There is no terminal behind a tray icon, which is why
  ``wattracker_connector.__main__`` logs to a file and only adds a stream
  handler when ``sys.stderr`` is not None.
* **Windows only**. macOS and Linux keep the pip console script; the tray, the
  registry autostart and the named mutex are all Win32.

Build (on Windows, from the repository root):

    python -m pip install ".[dev,ble,package,connector,webview]"
    python -m PyInstaller --clean --noconfirm packaging\\wattracker-connector.spec
    python packaging\\smoke_frozen_connector.py dist\\WattrackerConnector.exe
"""
import importlib.util
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

root = Path(SPECPATH).parent

if not sys.platform.startswith("win"):
    # Refused rather than built badly. A onefile windowed binary produced on
    # another OS is not a Windows connector, and letting it build would hand
    # somebody an artifact that cannot possibly work.
    raise SystemExit(
        "wattracker-connector.spec builds a Windows executable; run it on Windows."
    )


def _load(name):
    """Load a sibling helper by path.

    Never ``import``: this directory is named ``packaging``, and so is a
    widely installed PyPI distribution, so an import would resolve to that one
    on most machines.
    """
    path = Path(SPECPATH) / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"_wattracker_{name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_project_version = lambda: _load("_version").project_version(root)  # noqa: E731
FORBIDDEN = _load("_connector_excludes").FORBIDDEN

# bleak resolves its backend at runtime, so nothing static imports the WinRT
# one. Best-effort for the same reason wattracker.spec gives: a connector
# without bleak still serves files, and the ride page reports Bluetooth as
# unavailable rather than failing to start.
hidden = []
try:
    hidden += collect_submodules("bleak.backends.winrt")
except Exception:
    pass

# The tray's window. webviewpy ships its own PyInstaller hook, which copies the
# native webview.dll in as data, so hookspath is all that is needed here - and
# the hook is the authority on where that DLL lives, which a hand-written datas
# entry would duplicate and then get wrong on the next release.
hookspath = []
try:
    import webviewpy

    hookspath.append(str(Path(webviewpy.__file__).parent / "tools" / "__pyinstaller"))
except Exception:
    # Absent is survivable: the window falls back to the rider's browser.
    pass

def _version_resource():
    """Windows version resource, so Explorer can say what this binary is.

    Best-effort, like the two collectors above: an unsigned executable that
    also has no version information is that much harder to identify on a
    rider's machine, but losing the resource is not worth losing the build.
    """
    try:
        from PyInstaller.utils.win32.versioninfo import (
            FixedFileInfo, StringFileInfo, StringStruct, StringTable,
            VarFileInfo, VarStruct, VSVersionInfo,
        )

        version = _project_version()
        parts = [int(p) for p in version.split(".")[:3]] + [0, 0, 0, 0]
        numbers = tuple(parts[:4])
        return VSVersionInfo(
            ffi=FixedFileInfo(filevers=numbers, prodvers=numbers),
            kids=[
                StringFileInfo([StringTable("040904B0", [
                    StringStruct("CompanyName", "wattracker"),
                    StringStruct("FileDescription",
                                 "wattracker connector - Zwift folders and BLE"),
                    StringStruct("FileVersion", version),
                    StringStruct("InternalName", "WattrackerConnector"),
                    StringStruct("OriginalFilename", "WattrackerConnector.exe"),
                    StringStruct("ProductName", "wattracker connector"),
                    StringStruct("ProductVersion", version),
                ])]),
                VarFileInfo([VarStruct("Translation", [1033, 1200])]),
            ],
        )
    except Exception:
        return None


a = Analysis(
    [str(root / "wattracker_connector" / "__main__.py")],
    pathex=[str(root)],
    binaries=[],
    datas=[
        # The tray icon. The app's favicon rather than a second artwork file,
        # so the icon in the notification area is the one in the browser tab.
        (str(root / "wattracker" / "web" / "static" / "favicon.ico"),
         "wattracker/web/static"),
    ],
    hiddenimports=hidden,
    hookspath=hookspath,
    excludes=FORBIDDEN,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    # A stable name, deliberately unversioned: WP-C stores this executable's
    # path in HKCU\...\Run, so a filename carrying the version would break
    # autostart on every upgrade.
    name="WattrackerConnector",
    console=False,
    icon=str(root / "wattracker" / "web" / "static" / "favicon.ico"),
    version=_version_resource(),
    strip=False,
    upx=False,
)
