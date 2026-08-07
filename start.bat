@echo off
rem ============================================================
rem  Multi-Agent Quant Trading System V1 - Launcher
rem
rem  Usage (double-click or command line):
rem    start.bat           -> build frontend + Web (hot reload)
rem                           + Scheduler + Browser
rem    start.bat nosched   -> build frontend + Web only
rem    start.bat front     -> Vite dev server (5173, HMR instant
rem                           update) + Web API (8080) + Browser
rem    start.bat watch     -> also run "vite build --watch" window:
rem                           frontend changes auto-rebuild
rem
rem  Frontend updates:
rem    - Normal mode: this script builds frontend before start;
rem      after editing frontend code run "start.bat watch" once,
rem      then every change auto-rebuilds (refresh browser to see).
rem    - "start.bat front": Vite HMR - changes appear instantly.
rem
rem  Python backend: hot reload ON by default (uvicorn --reload),
rem  .py changes take effect automatically.
rem  config/*.yaml changes do NOT hot-reload; run restart.bat.
rem
rem  How to stop:
rem    1) Recommended: run stop.bat (kills all processes)
rem    2) Or press Ctrl+C in each window
rem    3) Closing windows (X) triggers exit, but leftovers may
rem       remain - run stop.bat to clean up.
rem  ASCII only. Do NOT add Chinese characters here.
rem ============================================================
setlocal enabledelayedexpansion
chcp 65001 >nul 2>&1
cd /d "%~dp0"

echo.
echo  [QuantiAgent V1] starting...
echo  - Web console : http://localhost:8080  (Python hot reload ON)
echo  - Scheduler   : embedded in Web process (data collection /
echo                   agent analysis / daily report)
echo  - Stop: run stop.bat, or press Ctrl+C in the windows.
echo.

if not exist ".venv\Scripts\python.exe" (
    echo  [ERROR] .venv not found. Please run:  python -m venv .venv
    pause
    exit /b 1
)

if not exist ".env" (
    echo  [WARN] .env not found. Please copy .env.example to .env first.
)

rem ---- frontend dev server (HMR, instant update) ----
if /i "%~1"=="front" (
    echo  [MODE] Vite HMR on http://localhost:5173 (API proxied to 8080)
    start "QuantiAgent-Web" cmd /k ".venv\Scripts\python.exe main.py serve"
    timeout /t 4 /nobreak >nul
    cd /d "%~dp0frontend"
    start "" "http://localhost:5173"
    call npm run dev
    exit /b 0
)

rem ---- build frontend (make sure dist is up to date) ----
if not exist "frontend\node_modules" (
    echo  [WARN] frontend\node_modules missing, skipping build.
    goto :skip_build
)
echo  - building frontend (npm run build)...
pushd frontend
call npm run build
set BUILD_OK=%ERRORLEVEL%
popd
if not "%BUILD_OK%"=="0" (
    echo  [WARN] frontend build failed, using previous dist.
)
:skip_build

rem ---- optional frontend watch (auto rebuild on change) ----
if /i "%~1"=="watch" (
    echo  [MODE] frontend watch: edits auto-rebuild (refresh browser to see)
    start "QuantiAgent-FrontendWatch" cmd /k "cd /d %~dp0frontend && npx vite build --watch"
    timeout /t 2 /nobreak >nul
)

rem ---- normal mode: Web (hot reload) + embedded Scheduler ----
rem NOTE: do NOT start a separate scheduler process. The Web process
rem embeds the scheduler (web.embed_scheduler=true). Two processes
rem holding their own broker state caused position/order/cash
rem corruption before.
start "QuantiAgent-Web" cmd /k ".venv\Scripts\python.exe main.py serve"

timeout /t 6 /nobreak >nul
start "" "http://localhost:8080"
echo  [OK] windows opened. Python hot reload ON, frontend built.
echo  [NOTE] Frontend: run "start.bat front" for HMR or "start.bat watch"
echo         for auto-rebuild. After config/*.yaml changes run restart.bat.
exit /b 0
