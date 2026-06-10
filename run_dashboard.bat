@echo off
title APPLY-NAV // LinkedIn Easy Apply Automation
echo Setting up PATH for uv...
set PATH=C:\Users\smdsa\.local\bin;%PATH%

echo Verifying dependencies...
call uv pip install google-genai fastapi uvicorn websockets

echo Launching Apply-Nav Dashboard...
start http://localhost:8000

echo Starting backend application server...
.\linkedin-mcp-server\.venv\Scripts\python.exe job_applier_dashboard.py

pause
