@echo off
setlocal
cd /d "%~dp0"

echo.
echo  display-panel installer
echo  =======================
echo.

rem -- find a Python >= 3.10 -------------------------------------------------
set "PY="
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)" >nul 2>&1
if not errorlevel 1 set "PY=py -3"
if not defined PY (
    python -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)" >nul 2>&1
    if not errorlevel 1 set "PY=python"
)
if not defined PY (
    echo No Python 3.10 or newer found. Install it from https://python.org
    echo ^(the "py" launcher or "python" must be on PATH^), then run this again.
    exit /b 1
)

rem -- stop a running instance so files are not locked -----------------------
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='pythonw.exe' or Name='python.exe'\" | Where-Object { $_.CommandLine -match 'display_panel' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }" >nul 2>&1

rem -- virtualenv + package --------------------------------------------------
if not exist ".venv\Scripts\python.exe" (
    echo Creating virtualenv...
    %PY% -m venv .venv || exit /b 1
)
echo Installing package...
".venv\Scripts\python.exe" -m pip install -e . --quiet --disable-pip-version-check || exit /b 1

rem -- is the panel there? ---------------------------------------------------
echo.
".venv\Scripts\display-panel.exe" info
if errorlevel 1 (
    echo.
    echo WARNING: no panel answered. Is it plugged in? Is the vendor
    echo application still running? It holds the COM port exclusively.
    echo Continuing anyway - autostart will pick the panel up at next logon.
)

rem -- with or without the Claude sessions view ------------------------------
echo.
choice /C YN /T 30 /D Y /M "Show running Claude Code sessions next to the metrics"
set "MODEARGS=-m display_panel run"
if %errorlevel%==1 set "MODEARGS=-m display_panel run --claude"

rem -- autostart shortcut in the Startup folder (no admin needed) ------------
powershell -NoProfile -Command "$s=(New-Object -ComObject WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Startup')+'\display-panel.lnk'); $s.TargetPath='%CD%\.venv\Scripts\pythonw.exe'; $s.Arguments='%MODEARGS%'; $s.WorkingDirectory='%CD%'; $s.Save()" || exit /b 1

rem -- and start it right now ------------------------------------------------
start "" "%CD%\.venv\Scripts\pythonw.exe" %MODEARGS%

echo.
echo Done. The dashboard is running and will start again at every logon.
echo To change modes, run install.bat again. To remove, run uninstall.bat.
exit /b 0
