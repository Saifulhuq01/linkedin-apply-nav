#!/bin/bash
# ─────────────────────────────────────────────────────────
# Apply-Nav — Startup Script (Linux/macOS)
# ─────────────────────────────────────────────────────────

set -e

echo "Apply-Nav — LinkedIn Job Application Dashboard"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# Check for config
if [ ! -f "config.local.yaml" ]; then
    echo ""
    echo "⚠️  First-time setup: Creating config.local.yaml..."
    cp config.yaml config.local.yaml
    echo "✓  Created config.local.yaml — edit it with your details, or use the Settings tab in the UI."
    echo ""
fi

# Create data directories
mkdir -p data/resumes

# Ensure virtual environment exists in linkedin-mcp-server
if [ ! -d "linkedin-mcp-server/.venv" ]; then
    echo "Virtual environment not found. Creating one in linkedin-mcp-server..."
    cd linkedin-mcp-server
    if command -v uv &> /dev/null; then
        uv venv
        uv pip install -e .
    else
        python3 -m venv .venv
        .venv/bin/pip install -e .
    fi
    cd ..
fi

PYTHON="./linkedin-mcp-server/.venv/bin/python"
echo "Using Python: $PYTHON"

# Install dependencies
echo "Verifying dependencies..."
if command -v uv &> /dev/null; then
    cd linkedin-mcp-server
    uv pip install google-genai fastapi uvicorn websockets pyyaml pdfplumber httpx
    cd ..
else
    $PYTHON -m pip install google-genai fastapi uvicorn websockets pyyaml pdfplumber httpx
fi

# Install patchright browser binaries
echo "Verifying automated browser binaries..."
$PYTHON -m patchright install chromium || echo "[Warning] Patchright browser installation failed."

# Open browser
echo "Opening dashboard at http://localhost:8000 ..."
if command -v xdg-open &> /dev/null; then
    xdg-open http://localhost:8000 &
elif command -v open &> /dev/null; then
    open http://localhost:8000 &
fi

# Start server
echo "Starting backend..."
$PYTHON job_applier_dashboard.py
