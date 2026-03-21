@echo off
title BGG Hot Deals Monitor
cd /d "%~dp0"

echo.
echo  ========================================
echo   BGG Hot Deals Monitor
echo  ========================================
echo.

REM Check Python is installed
python --version >nul 2>&1
if errorlevel 1 (
    echo  ERROR: Python not found. Please install Python 3.9+ from python.org
    pause
    exit /b 1
)

REM Install dependencies if needed
echo  Checking dependencies...
pip install -r requirements.txt -q

echo.
echo  Starting monitor... (Press Ctrl+C to stop)
echo.

python monitor.py

pause
