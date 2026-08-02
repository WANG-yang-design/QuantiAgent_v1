@echo off
rem ============================================================
rem  QuantiAgent V1 - Restart (Web + Scheduler + Browser)
rem  Usage:  restart.bat          -> restart everything
rem          restart.bat nosched  -> restart Web only
rem ============================================================
chcp 65001 >nul 2>&1
cd /d "%~dp0"

echo.
echo  [QuantiAgent V1] restarting...
echo  - stopping old services...
echo.

rem ---- stop all QuantiAgent python processes (via PowerShell, reliable) ----
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -like '*QuantiAgent*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"

timeout /t 3 /nobreak >nul

echo  - starting services...
start "QuantiAgent-Web" cmd /k ".venv\Scripts\python.exe main.py serve"
if /i not "%~1"=="nosched" (
    start "QuantiAgent-Scheduler" cmd /k ".venv\Scripts\python.exe main.py scheduler"
)

timeout /t 8 /nobreak >nul
start "" "http://localhost:8080"
echo  [OK] restarted. Web + Scheduler running.
echo  [NOTE] Python code changes require restart; use "start.bat dev" for auto-reload.
exit /b 0
