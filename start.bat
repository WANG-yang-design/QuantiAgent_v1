@echo off
rem ============================================
rem   Multi-Agent Quant Trading System V1
rem   start.bat           -> Web + Scheduler + Browser
rem   start.bat dev       -> Web with auto-reload (Python code hot reload)
rem   start.bat nosched   -> Web only
rem   start.bat front     -> Vite frontend dev server (hot reload, port 5173)
rem ============================================
chcp 65001 >nul
cd /d "%~dp0"

echo.
echo  [QuantiAgent V1] starting...
echo  - Web console : http://localhost:8080
echo  - Ctrl+C in each window to stop
echo.

if not exist ".venv\Scripts\python.exe" (
    echo  [ERROR] .venv not found. Run:  python -m venv .venv
    pause
    exit /b 1
)

if not exist ".env" (
    echo  [WARN] .env not found. Copy .env.example to .env first.
)

rem ---- dev mode: Python hot reload (uvicorn --reload) ----
if /i "%~1"=="dev" (
    start "QuantiAgent-Web" cmd /k ".venv\Scripts\python.exe -m uvicorn web.api.main:app --reload --host 0.0.0.0 --port 8080"
    timeout /t 6 /nobreak >nul
    start "" http://localhost:8080
    echo  [OK] dev mode: Python code changes auto-reload (Ctrl+C to stop)
    exit /b 0
)

rem ---- frontend dev server (Vite hot reload) ----
if /i "%~1"=="front" (
    cd frontend
    call npm run dev
    exit /b 0
)

rem ---- normal mode ----
start "QuantiAgent-Web" cmd /k ".venv\Scripts\python.exe main.py serve"

if /i not "%~1"=="nosched" (
    start "QuantiAgent-Scheduler" cmd /k ".venv\Scripts\python.exe main.py scheduler"
)

timeout /t 6 /nobreak >nul
start "" http://localhost:8080
echo  [OK] windows opened. Check mailbox for daily reports.
