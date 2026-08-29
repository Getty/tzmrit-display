@echo off
rem Builds tzmrit-display-setup-<version>.exe. Needs Python 3.10+ and NSIS
rem (winget install NSIS.NSIS). Run from anywhere; paths are script-relative.
setlocal
cd /d "%~dp0.."

if not exist ".venv\Scripts\python.exe" (
    py -3 -m venv .venv || python -m venv .venv || exit /b 1
)
".venv\Scripts\python.exe" -m pip install -e . pyinstaller --quiet --disable-pip-version-check || exit /b 1

rem The version has a single source: pyproject.toml. Read it from the
rem installed package and hand it to NSIS - installer.nsi refuses to build
rem without it, so the two can never drift apart. The numeric form is for
rem NSIS VIProductVersion, which needs strict X.X.X.X - a PEP440 dev string
rem is reduced to its leading release segment, zero-padded.
".venv\Scripts\python.exe" -c "from importlib.metadata import version; import pathlib, re; v = version('tzmrit-display'); pathlib.Path('packaging/_version.txt').write_text(v); n = re.match(r'\d+(?:\.\d+)*', v).group(0).split('.'); pathlib.Path('packaging/_version_numeric.txt').write_text('.'.join((n + ['0'] * 4)[:4]))" || exit /b 1

cd packaging
set /p VERSION=<_version.txt
set /p VERSION_NUMERIC=<_version_numeric.txt
del _version.txt _version_numeric.txt
echo Building version %VERSION% (%VERSION_NUMERIC%)

"..\.venv\Scripts\pyinstaller.exe" --noconfirm --clean tzmrit-display.spec || exit /b 1

set "MAKENSIS=%ProgramFiles(x86)%\NSIS\makensis.exe"
if not exist "%MAKENSIS%" set "MAKENSIS=%ProgramFiles%\NSIS\makensis.exe"
if not exist "%MAKENSIS%" (
    echo NSIS not found - install it with: winget install NSIS.NSIS
    exit /b 1
)
"%MAKENSIS%" /DVERSION=%VERSION% /DVERSION_NUMERIC=%VERSION_NUMERIC% installer.nsi || exit /b 1

echo.
echo Done. Installer is at:
dir /b "%~dp0tzmrit-display-setup-*.exe"
exit /b 0
