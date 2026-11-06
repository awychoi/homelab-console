# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the console backend, not an .app.

Produces ``dist/backend/HomelabConsole`` plus its ``_internal`` tree. The Swift
shell copies that directory into
``Homelab Console.app/Contents/Resources/backend`` and runs it as a child
process. Building it separately keeps the app bundle about the shell, and the
backend about the console.

    ./.venv/bin/python -m PyInstaller --clean --noconfirm desktop/HomelabConsoleBackend.spec
"""

from pathlib import Path

ROOT = Path(SPECPATH).resolve().parent
APP = ROOT / "app"

a = Analysis(
    [str(ROOT / "desktop" / "launch.py")],
    pathex=[str(APP)],
    binaries=[],
    # server.py resolves web assets from util.bundle_root(); inside the bundle
    # that is sys._MEIPASS, so the directory must land at its root as "web".
    datas=[(str(APP / "web"), "web")],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "pydoc", "_pytest"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="HomelabConsole",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="backend",
)
