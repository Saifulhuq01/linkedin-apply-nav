@echo off
echo.
echo  ╔══════════════════════════════════════════════╗
echo  ║          Apply-Nav — Dashboard Launcher       ║
echo  ╚══════════════════════════════════════════════╝
echo.
cd /d "%~dp0"

set PYTHON="%~dp0linkedin-mcp-server\.venv\Scripts\python.exe"
set PORT=8000

echo Starting Apply-Nav on http://localhost:%PORT%
echo (Press Ctrl+C to stop)
echo.

%PYTHON% job_applier_dashboard.py

pause
