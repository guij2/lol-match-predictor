@echo off
REM Build the native overlay executable

echo Installing dependencies...
pip install -r "%~dp0requirements.txt"

echo.
echo Building executable...
python "%~dp0build_overlay.py"

pause
