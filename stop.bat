@echo off
rem ============================================================
rem  QuantiAgent V1 - Stop (kill Web + Scheduler processes)
rem  Usage: double-click stop.bat
rem  Purpose: closing windows (X) or Ctrl+C may leave orphan
rem  processes (occupying port 8080 / duplicate scheduling).
rem  This script matches by command line and executable path to
rem  precisely clean up this project's python processes.
rem  ASCII only. Do NOT add Chinese characters here.
rem ============================================================
chcp 65001 >nul 2>&1
cd /d "%~dp0"

echo.
echo  [QuantiAgent V1] stopping all services...
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -like '*main.py*' -or $_.CommandLine -like '*uvicorn*' -or $_.CommandLine -like '*spawn_main*' -or $_.CommandLine -like '*multiprocessing-fork*' -or $_.ExecutablePath -like '*QuantiAgent*' } | ForEach-Object { Write-Host ('  stopped PID ' + $_.ProcessId); Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"

timeout /t 2 /nobreak >nul

rem ---- fallback: force release port 8080 (leftover listeners) ----
powershell -NoProfile -Command "Get-NetTCPConnection -LocalPort 8080 -State Listen -ErrorAction SilentlyContinue | ForEach-Object { $pid2 = $_.OwningProcess; Write-Host ('  port 8080 held by PID ' + $pid2 + ', killing'); Stop-Process -Id $pid2 -Force -ErrorAction SilentlyContinue }"

rem ---- clean scheduler lock file ----
if exist "data\scheduler.pid" del /q "data\scheduler.pid" >nul 2>&1

set /a LEFT=0
for /f "delims=" %%i in ('powershell -NoProfile -Command "(Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -like '*main.py*' -or $_.CommandLine -like '*uvicorn*' -or $_.CommandLine -like '*spawn_main*' -or $_.CommandLine -like '*multiprocessing-fork*' -or $_.ExecutablePath -like '*QuantiAgent*' } | Measure-Object).Count"') do set LEFT=%%i
if "%LEFT%"=="0" (
    echo  [OK] All QuantiAgent processes stopped, port 8080 released.
) else (
    echo  [WARN] %LEFT% process(es) still running, check Task Manager.
)
echo.
pause
