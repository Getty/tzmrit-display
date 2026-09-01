# -*- mode: python ; coding: utf-8 -*-
# Builds two executables from one analysis:
#
#   tzmrit-display.exe    console  - the CLI (info, run, preview, ...)
#   tzmrit-displayw.exe   windowed - same CLI, no console window; this is what
#                                   the autostart shortcut runs
#
# The fonts land in _internal/tzmrit_display/fonts because theme.py resolves
# them relative to the package (parent / "fonts") - the frozen layout mirrors
# the checkout layout, so no code change is needed.
#
# The Windows version resource is built here rather than passed in because
# build.bat runs pyinstaller BEFORE it extracts the version for NSIS - so the
# spec reads the version itself from the editable install that build.bat
# guarantees via "pip install -e ." right before pyinstaller runs.

import re
from importlib.metadata import version as _pkg_version

from PyInstaller.utils.win32.versioninfo import (
    FixedFileInfo,
    StringFileInfo,
    StringStruct,
    StringTable,
    VarFileInfo,
    VarStruct,
    VSVersionInfo,
)

_version = _pkg_version("tzmrit-display")
# filevers/prodvers must be exactly 4 ints, but a dev version looks like
# 0.1.1.dev3+g1234abc - take only the leading numeric release segment and
# zero-pad it; the full PEP440 string goes in the string table below.
_release = re.match(r"\d+(?:\.\d+)*", _version).group(0).split(".")
_filevers = tuple(int(n) for n in (_release + ["0"] * 4)[:4])


def _versioninfo(description):
    return VSVersionInfo(
        ffi=FixedFileInfo(filevers=_filevers, prodvers=_filevers),
        kids=[
            StringFileInfo(
                [
                    StringTable(
                        "040904B0",
                        [
                            StringStruct("CompanyName", "Torsten Raudssus"),
                            StringStruct("ProductName", "tzmrit-display"),
                            StringStruct("FileDescription", description),
                            StringStruct("FileVersion", _version),
                            StringStruct("ProductVersion", _version),
                            StringStruct(
                                "LegalCopyright",
                                "Copyright (c) 2026 Torsten Raudssus",
                            ),
                        ],
                    )
                ]
            ),
            VarFileInfo([VarStruct("Translation", [0x0409, 1200])]),
        ],
    )


a = Analysis(
    ["launcher.py"],
    pathex=[".."],
    datas=[("../tzmrit_display/fonts", "tzmrit_display/fonts")],
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
    # relative icon paths resolve against the spec's directory (SPECPATH)
    icon="icon.ico",
    version=_versioninfo(
        "tzmrit-display — system monitor for HONGTAI USB LCD panels"
    ),
)

exe_win = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="tzmrit-displayw",
    console=False,
    icon="icon.ico",
    version=_versioninfo(
        "tzmrit-display — system monitor for HONGTAI USB LCD panels (windowed)"
    ),
)

coll = COLLECT(
    exe_cli,
    exe_win,
    a.binaries,
    a.datas,
    name="tzmrit-display",
)
