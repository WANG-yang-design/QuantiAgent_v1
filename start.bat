@echo off
rem ============================================
rem   Multi-Agent Quant Trading System V1
rem   Start Web Console + Scheduler + Browser
rem ============================================
chcp 65001 >nul
cd /d "%~dp0"

echo.
echo  [QuantiAgent V1] starting...
echo  - Web console : http://localhost:8080
echo  - Scheduler   : data collection / agent analysis / daily report
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

rem start web console
start "QuantiAgent-Web" cmd /k ".venv\Scripts\python.exe main.py serve"

rem start scheduler (skip with: start.bat nosched)
if /i not "%~1"=="nosched" (
    start "QuantiAgent-Scheduler" cmd /k ".venv\Scripts\python.exe main.py scheduler"
)

rem wait for server then open browser
timeout /t 6 /nobreak >nul
start "" http://localhost:8080

echo  [OK] windows opened. Check mailbox for daily reports.
