@echo off
echo ============================================================
echo   BGG Deal Monitor + GameNerdz DotD -- Starting Both
echo ============================================================
echo.

cd /d "%~dp0"

echo Installing/updating dependencies...
pip install -r requirements.txt --quiet

echo.
echo Starting BGG Hot Deals monitor in a new window...
start "BGG Hot Deals Monitor" cmd /k python monitor.py

echo Starting GameNerdz Deal of the Day monitor in a new window...
start "GameNerdz DotD Monitor" cmd /k python gamenerdz_dotd.py

echo.
echo Both monitors are running in separate windows.
echo Close those windows (or press Ctrl+C in each) to stop them.
pause
