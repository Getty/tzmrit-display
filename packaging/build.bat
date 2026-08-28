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
rem without it, so the two can never drift apart.
".venv\Scripts\python.exe" -c "from importlib.metadata import version; import pathlib; pathlib.Path('packaging/_version.txt').write_text(version('tzmrit-display'))" || exit /b 1

cd packaging
set /p VERSION=<_version.txt
del _version.txt
echo Building version %VERSION%

"..\.venv\Scripts\pyinstaller.exe" --noconfirm --clean tzmrit-display.spec || exit /b 1

set "MAKENSIS=%ProgramFiles(x86)%\NSIS\makensis.exe"
if not exist "%MAKENSIS%" set "MAKENSIS=%ProgramFiles%\NSIS\makensis.exe"
if not exist "%MAKENSIS%" (
    echo NSIS not found - install it with: winget install NSIS.NSIS
    exit /b 1
)
"%MAKENSIS%" /DVERSION=%VERSION% installer.nsi || exit /b 1

echo.
echo Done. Installer is at:
dir /b "%~dp0tzmrit-display-setup-*.exe"
exit /b 0
