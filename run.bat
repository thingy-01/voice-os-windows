@echo off
REM ===================================================================
REM  Voice OS (Windows) launcher.
REM  Creates a venv, installs deps, loads .env, runs in HOLD-TO-TALK.
REM  Pass through any flags, e.g.:  run.bat --push-to-talk
REM ===================================================================
setlocal enabledelayedexpansion
cd /d "%~dp0"

REM --- venv ---
if not exist ".venv\Scripts\python.exe" (
    echo Creating virtual environment...
    py -3 -m venv .venv 2>nul || python -m venv .venv
)
call ".venv\Scripts\activate.bat"

REM --- deps (only if a marker is missing) ---
if not exist ".venv\.installed" (
    echo Installing dependencies...
    python -m pip install --upgrade pip >nul
    python -m pip install -r requirements-windows.txt
    if errorlevel 1 (
        echo.
        echo Dependency install failed. Fix the error above and re-run.
        exit /b 1
    )
    echo done > ".venv\.installed"
)

REM --- load .env into the environment ---
if exist ".env" (
    for /f "usebackq tokens=1,* delims==" %%a in (".env") do (
        set "line=%%a"
        if not "!line:~0,1!"=="#" if not "%%a"=="" set "%%a=%%b"
    )
)

if "%OPENAI_API_KEY%"=="" (
    echo.
    echo  OPENAI_API_KEY is not set. Copy .env.example to .env and paste your key.
    exit /b 1
)

REM --- run (default mode = hold-to-talk on Right Ctrl) ---
python voice_agent.py %*
endlocal
