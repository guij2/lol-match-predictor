@echo off
REM Run the native overlay directly (development mode)
REM Uses pythonw.exe to avoid console window

echo Starting LoL Win Probability Overlay...
start "" pythonw "%~dp0overlay.pyw"
