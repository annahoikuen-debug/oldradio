@echo off
chcp 65001 > nul

REM Retro Radio Time Machine Launcher (FastAPI + Pure SPA)
REM Double-click to start the application.
REM NOTE: This file must stay UTF-8 without BOM. `chcp 65001` above makes the
REM       Japanese text below render correctly in the console.
REM Streamlit was removed. The only entry point is uvicorn.

setlocal EnableDelayedExpansion

cd /d "%~dp0"

title Retro Radio Time Machine Launcher
color 0A

if not defined PORT set "PORT=8501"
if not defined HOST set "HOST=0.0.0.0"
REM Set RELOAD=1 before running this file to enable auto-reload during development.
if not defined RELOAD set "RELOAD=0"

echo.
echo ==========================================
echo   Retro Radio Time Machine v2.0
echo   FastAPI + Pure SPA
echo ==========================================
echo.

REM `.env` itself is read by pydantic-settings (model_config env_file=".env"),
REM so it must NOT be parsed into the environment here: that would re-introduce
REM the old hand-rolled quoting bugs. We only probe for the key below.
set "HAS_GEMINI_KEY=0"
if defined RETRO_RADIO_GEMINI_API_KEY set "HAS_GEMINI_KEY=1"
if defined GEMINI_API_KEY set "HAS_GEMINI_KEY=1"
if exist ".env" (
    findstr /B /C:"RETRO_RADIO_GEMINI_API_KEY=" .env > nul 2>&1
    if not errorlevel 1 (
        for /f "usebackq tokens=2 delims==" %%a in (".env") do (
            if /i "%%a"=="RETRO_RADIO_GEMINI_API_KEY" (
                if not "!%%a!"=="" set "HAS_GEMINI_KEY=1"
            )
        )
    )
)

if !HAS_GEMINI_KEY!==0 (
    echo [INFO] No Gemini API key detected.
    echo.
    echo [INPUT] To enable AI script generation, paste your Gemini API key.
    echo [INFO] Or just press Enter to start in fallback mode.
    echo.
    set /p GEMINI_API_KEY="API Key (Enter to skip): "
    if defined GEMINI_API_KEY (
        echo.
        set /p SAVE_KEY="Save this key to .env? (Y/N): "
        if /i "!SAVE_KEY!"=="Y" call :save_env RETRO_RADIO_GEMINI_API_KEY "!GEMINI_API_KEY!"
    ) else (
        echo [WARNING] No API key. Starting in fallback mode.
        echo [INFO] Get a free key at: https://aistudio.google.com/app/apikey
    )
) else (
    echo [OK] Gemini API key configured
)

echo.
echo ------------------------------------------

REM Check for Python installation (py launcher preferred)
set "PYTHON_OK=0"
where py > nul 2>&1
if not errorlevel 1 (
    py -3 -c "import sys" > nul 2>&1
    if not errorlevel 1 (
        set "PYTHON_OK=1"
        set "PYTHON_CMD=py -3"
    )
)

if "!PYTHON_OK!"=="0" (
    where python > nul 2>&1
    if not errorlevel 1 (
        python -c "import sys" > nul 2>&1
        if not errorlevel 1 (
            set "PYTHON_OK=1"
            set "PYTHON_CMD=python"
        )
    )
)

if "!PYTHON_OK!"=="0" goto :no_python

echo [OK] Python environment detected: !PYTHON_CMD!
echo.
echo [INFO] Checking runtime dependencies...
!PYTHON_CMD! -c "import fastapi, uvicorn" > nul 2>&1
if errorlevel 1 (
    echo [INFO] Installing required dependencies...
    !PYTHON_CMD! -m pip install -r requirements.txt
    if errorlevel 1 (
        echo [ERROR] Dependency installation failed.
        goto :failed
    )
)

echo [OK] All dependencies ready.
echo.
echo [START] Starting Retro Radio Server (uvicorn)...
echo [INFO] Open your browser to: http://localhost:8501
echo [INFO] Health check:         http://localhost:8501/health
echo.
echo [INFO] Press Ctrl+C to stop the server
echo.

start "" cmd /c "timeout /t 3 > nul && start http://localhost:8501"

if "!RELOAD!"=="1" (
    !PYTHON_CMD! -m uvicorn retro_radio.server:app --host !HOST! --port !PORT! --reload
) else (
    !PYTHON_CMD! -m uvicorn retro_radio.server:app --host !HOST! --port !PORT!
)
goto :eof

:save_env
REM %1 = key name, %2 = value. Never truncate an existing .env.
if not exist ".env" (
    > .env echo %~1=%~2
    goto :eof
)
findstr /B /C:"%~1=" .env > nul 2>&1
if not errorlevel 1 (
    echo [INFO] %~1 already exists in .env -- edit it manually to change the value.
    goto :eof
)
>> .env echo %~1=%~2
echo [OK] Saved %~1 to .env
goto :eof

:no_python
echo [ERROR] Python environment not found. Please install Python 3.11+.
echo [INFO] Download from: https://www.python.org/downloads/
echo.
pause
exit /b 1

:failed
pause
exit /b 1
