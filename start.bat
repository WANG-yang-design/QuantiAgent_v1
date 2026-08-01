@echo off
rem ============================================================
rem  Multi-Agent Quant Trading System V1 - Launcher
rem  Usage (double-click or command line):
rem    start.bat           -> Web console + Scheduler + Browser
rem    start.bat dev       -> Web with Python hot reload
rem    start.bat nosched   -> Web console only
rem    start.bat front     -> Vite frontend dev server (5173)
rem ============================================================
setlocal enabledelayedexpansion
chcp 65001 >nul 2>&1
cd /d "%~dp0"

echo.
echo  [QuantiAgent V1] starting...
echo  - Web console : http://localhost:8080
echo  - Scheduler   : data collection / agent analysis / daily report
echo  - Ctrl+C in each window to stop
echo.

if not exist ".venv\Scripts\python.exe" (
    echo  [ERROR] .venv not found. Please run:  python -m venv .venv
    pause
    exit /b 1
)

if not exist ".env" (
    echo  [WARN] .env not found. Please copy .env.example to .env first.
)

rem ---- dev mode: Python hot reload ----
if /i "%~1"=="dev" (
    start "QuantiAgent-Web" cmd /k ".venv\Scripts\python.exe -m uvicorn web.api.main:app --reload --host 0.0.0.0 --port 8080"
    timeout /t 6 /nobreak >nul
    start "" "http://localhost:8080"
    echo  [OK] dev mode: Python code changes auto-reload. Press Ctrl+C to stop.
    exit /b 0
)

rem ---- frontend dev server ----
if /i "%~1"=="front" (
    cd /d "%~dp0frontend"
    call npm run dev
    exit /b 0
)

rem ---- normal mode ----
start "QuantiAgent-Web" cmd /k ".venv\Scripts\python.exe main.py serve"

if /i not "%~1"=="nosched" (
    start "QuantiAgent-Scheduler" cmd /k ".venv\Scripts\python.exe main.py scheduler"
)

timeout /t 6 /nobreak >nul
start "" "http://localhost:8080"
echo  [OK] windows opened. Check mailbox for daily reports.
exit /b 0
