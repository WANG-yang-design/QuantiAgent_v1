@echo off
setlocal
cd /d "%~dp0"
set "APP_PORT=8080"

if /i not "%~1"=="quiet" (
    echo.
    echo [QuantiAgent] Stopping...
)

set "STOP_RESULT=0"
if exist "data\web.pid" (
    for /f "usebackq delims=" %%P in ("data\web.pid") do (
        taskkill /PID %%P /T /F >nul 2>&1
        if errorlevel 1 (
            tasklist /FI "PID eq %%P" 2>nul | find "%%P" >nul
            if not errorlevel 1 set "STOP_RESULT=1"
        ) else (
            if /i not "%~1"=="quiet" echo [OK] Stopped PID %%P and its child processes.
        )
    )
)

del /q "data\web.pid" >nul 2>&1
del /q "data\scheduler.pid" >nul 2>&1

powershell -NoProfile -Command "if (Get-NetTCPConnection -LocalPort %APP_PORT% -State Listen -ErrorAction SilentlyContinue) { exit 1 } else { exit 0 }"
if errorlevel 1 set "STOP_RESULT=1"

if "%STOP_RESULT%"=="0" (
    if /i not "%~1"=="quiet" echo [OK] All QuantiAgent processes were stopped.
) else (
    echo [ERROR] One or more QuantiAgent processes are still running.
)

if /i not "%~1"=="quiet" pause
exit /b %STOP_RESULT%
