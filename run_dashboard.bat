@echo off
title APPLY-NAV // LinkedIn Job Application Dashboard

:: ─────────────────────────────────────────────────────────
:: Apply-Nav — Startup Script (Windows)
:: ─────────────────────────────────────────────────────────

echo Apply-Nav — LinkedIn Job Application Dashboard
echo ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

:: Check for config
if not exist "config.local.yaml" (
    echo.
    echo First-time setup: Creating config.local.yaml...
    copy config.yaml config.local.yaml >nul
    echo Created config.local.yaml — edit it with your details, or use the Settings tab in the UI.
    echo.
)

:: Create data directories
if not exist "data\resumes" mkdir data\resumes

:: Set up PATH for uv
set PATH=C:\Users\%USERNAME%\.local\bin;%PATH%

echo Verifying dependencies...
call uv pip install google-genai fastapi uvicorn websockets pyyaml pdfplumber httpx 2>nul

echo.
echo Launching Apply-Nav Dashboard...
start http://localhost:8000

echo Starting backend application server...
.\linkedin-mcp-server\.venv\Scripts\python.exe job_applier_dashboard.py

pause
