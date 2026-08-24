@echo off
setlocal
cd /d "%~dp0"

set "PYTHON_EXE=%CD%\.venv\Scripts\python.exe"
set "PID_FILE=%CD%\data\web.pid"
set "APP_PORT=8080"

echo.
echo [QuantiAgent] Starting...

if not exist "%PYTHON_EXE%" (
    echo [ERROR] Python virtual environment was not found.
    echo [ERROR] Expected: %PYTHON_EXE%
    pause
    exit /b 1
)

if not exist ".env" (
    echo [ERROR] .env was not found.
    echo [ERROR] Copy .env.example to .env and configure it first.
    pause
    exit /b 1
)

call "%CD%\stop.bat" quiet
if errorlevel 1 (
    echo [ERROR] Existing QuantiAgent processes could not be stopped.
    pause
    exit /b 1
)

if not exist "frontend\node_modules" (
    echo [ERROR] frontend\node_modules was not found.
    echo [ERROR] Run npm install in the frontend directory first.
    pause
    exit /b 1
)

echo [1/3] Building frontend...
pushd frontend
call npm run build
set "BUILD_RESULT=%ERRORLEVEL%"
popd
if not "%BUILD_RESULT%"=="0" (
    echo [ERROR] Frontend build failed. Service was not started.
    pause
    exit /b %BUILD_RESULT%
)
if not exist "frontend\dist\index.html" (
    echo [ERROR] Frontend build did not create frontend\dist\index.html.
    pause
    exit /b 1
)

if not exist "data" mkdir "data"
if not exist "logs" mkdir "logs"

echo [2/3] Starting web API and embedded scheduler on port %APP_PORT%...
powershell -NoProfile -Command "$p = Start-Process -FilePath '%PYTHON_EXE%' -ArgumentList @('main.py','serve','--port','%APP_PORT%','--no-reload') -WorkingDirectory '%CD%' -WindowStyle Hidden -RedirectStandardOutput '%CD%\logs\launcher.stdout.log' -RedirectStandardError '%CD%\logs\launcher.stderr.log' -PassThru; Set-Content -LiteralPath '%PID_FILE%' -Value $p.Id -Encoding ascii; Write-Output $p.Id"
if errorlevel 1 (
    echo [ERROR] Failed to create the server process.
    pause
    exit /b 1
)

echo [3/3] Waiting for the API and frontend to become ready...
for /l %%I in (1,1,60) do (
    powershell -NoProfile -Command "try { $h = Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 'http://127.0.0.1:%APP_PORT%/api/health'; $p = Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 'http://127.0.0.1:%APP_PORT%/'; if ($h.StatusCode -eq 200 -and $p.StatusCode -eq 200) { exit 0 }; exit 1 } catch { exit 1 }"
    if not errorlevel 1 goto ready
    timeout /t 1 /nobreak >nul
)

echo [ERROR] Service did not become ready within 60 seconds.
echo [ERROR] Check logs\launcher.stderr.log and logs\system.log.
call "%CD%\stop.bat" quiet
pause
exit /b 1

:ready
echo [OK] QuantiAgent is ready at http://127.0.0.1:%APP_PORT%/
start "" "http://127.0.0.1:%APP_PORT%/"
exit /b 0
