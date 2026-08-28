@echo off
setlocal
cd /d "%~dp0"

echo.
echo  tzmrit-display uninstaller
echo  =========================
echo.

rem -- stop the running instance ---------------------------------------------
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='pythonw.exe' or Name='python.exe'\" | Where-Object { $_.CommandLine -match 'tzmrit_display' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }" >nul 2>&1

rem -- blank the panel so no stale image stays behind ------------------------
if exist ".venv\Scripts\tzmrit-display.exe" (
    ".venv\Scripts\tzmrit-display.exe" clear >nul 2>&1
)

rem -- remove the autostart shortcut -----------------------------------------
powershell -NoProfile -Command "Remove-Item -LiteralPath ([Environment]::GetFolderPath('Startup')+'\tzmrit-display.lnk') -ErrorAction SilentlyContinue"

rem -- remove the virtualenv -------------------------------------------------
if exist ".venv" rmdir /s /q ".venv"

echo Done. Autostart and virtualenv removed.
echo Delete this folder if you also want the source gone.
exit /b 0
