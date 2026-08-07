@echo off
rem ============================================================
rem  QuantiAgent V1 - Restart (Web + Scheduler + Browser)
rem  Usage:  restart.bat          -> restart everything
rem          restart.bat nosched  -> restart Web only
rem  NOTE: Web embeds the scheduler (web.embed_scheduler=true),
rem        no separate scheduler process is started. Running two
rem        processes (web + scheduler) caused cross-process
rem        position/order/cash corruption before.
rem  ASCII only. Do NOT add Chinese characters here.
rem ============================================================
chcp 65001 >nul 2>&1
cd /d "%~dp0"

echo.
echo  [QuantiAgent V1] restarting...
echo  - stopping old services...
echo.

rem ---- stop all QuantiAgent python processes ----
rem The uvicorn --reload worker is a multiprocessing.spawn child
rem (command line contains "spawn_main", NOT "main.py"), so we must
rem match it too, otherwise old workers keep listening on port 8080
rem and requests hit stale code (404 / missing features).
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -like '*main.py*' -or $_.CommandLine -like '*uvicorn*' -or $_.CommandLine -like '*spawn_main*' -or $_.CommandLine -like '*multiprocessing-fork*' -or $_.ExecutablePath -like '*QuantiAgent*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"

timeout /t 3 /nobreak >nul

rem ---- fallback: force release port 8080 if anything still holds it ----
powershell -NoProfile -Command "Get-NetTCPConnection -LocalPort 8080 -State Listen -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }"

timeout /t 2 /nobreak >nul

rem ---- clean scheduler lock file ----
if exist "data\scheduler.pid" del /q "data\scheduler.pid" >nul 2>&1

echo  - starting services (Web hot reload ON, scheduler embedded)...
start "QuantiAgent-Web" cmd /k ".venv\Scripts\python.exe main.py serve"

timeout /t 8 /nobreak >nul
start "" "http://localhost:8080"
echo  [OK] restarted. Web + Scheduler(embedded) running.
echo  [NOTE] Python .py changes auto-reload; after config/*.yaml changes run restart.bat again.
exit /b 0
