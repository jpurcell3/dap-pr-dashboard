@echo off
title DAP PR Dashboard
echo ============================================
echo   DAP PR Dashboard - Windows Launcher
echo ============================================
echo.

:: Check for Python
python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python is not installed or not in PATH.
    echo Download from https://www.python.org/downloads/
    pause
    exit /b 1
)

:: Install dependencies if needed
if not exist "venv" (
    echo Creating virtual environment...
    python -m venv venv
    call venv\Scripts\activate.bat
    echo Installing dependencies...
    pip install -r requirements.txt
) else (
    call venv\Scripts\activate.bat
)

:: Check for .env
if not exist ".env" (
    echo.
    echo WARNING: No .env file found!
    echo Copy .env.example to .env and fill in your settings:
    echo   copy .env.example .env
    echo   notepad .env
    echo.
    pause
    exit /b 1
)

echo.
echo Starting dashboard on http://localhost:5000
echo Press Ctrl+C to stop.
echo.
python app.py
pause
