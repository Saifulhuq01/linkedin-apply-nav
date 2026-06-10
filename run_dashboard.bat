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

:: Ensure virtual environment exists in linkedin-mcp-server
if not exist "linkedin-mcp-server\.venv" (
    echo.
    echo Virtual environment not found. Creating one in linkedin-mcp-server...
    cd linkedin-mcp-server
    call uv venv
    if errorlevel 1 (
        echo [Warning] uv venv failed, falling back to python -m venv...
        python -m venv .venv
    )
    
    echo Installing MCP server in editable mode...
    call uv pip install -e . 2>nul
    if errorlevel 1 (
        echo [Warning] uv pip failed, falling back to standard pip...
        .venv\Scripts\pip install -e .
    )
    cd ..
)

echo Verifying dependencies...
cd linkedin-mcp-server
call uv pip install google-genai fastapi uvicorn websockets pyyaml pdfplumber httpx 2>nul
if errorlevel 1 (
    echo [Warning] uv pip failed, falling back to standard pip...
    .venv\Scripts\pip install google-genai fastapi uvicorn websockets pyyaml pdfplumber httpx
)

:: Install patchright browser binaries if not already installed
echo Verifying automated browser binaries...
.venv\Scripts\python.exe -m patchright install chromium 2>nul
if errorlevel 1 (
    echo [Warning] Patchright browser installation failed, please check network connection.
)
cd ..

echo.
echo Launching Apply-Nav Dashboard...
start http://localhost:8000

echo Starting backend application server...
.\linkedin-mcp-server\.venv\Scripts\python.exe job_applier_dashboard.py

pause
