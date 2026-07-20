# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path
from PyInstaller.utils.hooks import collect_submodules

root = Path(SPECPATH).parent
hidden = collect_submodules("uvicorn") + collect_submodules("keyring.backends")
try:
    hidden += collect_submodules("bleak.backends.winrt")
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
