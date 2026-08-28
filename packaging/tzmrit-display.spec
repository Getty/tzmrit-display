# -*- mode: python ; coding: utf-8 -*-
# Builds two executables from one analysis:
#
#   tzmrit-display.exe    console  - the CLI (info, run, preview, ...)
#   tzmrit-displayw.exe   windowed - same CLI, no console window; this is what
#                                   the autostart shortcut runs
#
# The fonts land in _internal/fonts because theme.py resolves them relative
# to the package (parent.parent / "fonts") - the frozen layout mirrors the
# checkout layout, so no code change is needed.

a = Analysis(
    ["launcher.py"],
    pathex=[".."],
    datas=[("../fonts", "fonts")],
    hiddenimports=[],
    excludes=["tkinter"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe_cli = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="tzmrit-display",
    console=True,
)

exe_win = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="tzmrit-displayw",
    console=False,
)

coll = COLLECT(
    exe_cli,
    exe_win,
    a.binaries,
    a.datas,
    name="tzmrit-display",
)
