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

# Detect Python executable
PYTHON=""
if [ -f "./linkedin-mcp-server/.venv/bin/python" ]; then
    PYTHON="./linkedin-mcp-server/.venv/bin/python"
elif command -v python3 &> /dev/null; then
    PYTHON="python3"
elif command -v python &> /dev/null; then
    PYTHON="python"
else
    echo "❌ Python not found. Install Python 3.10+ first."
    exit 1
fi

echo "Using Python: $PYTHON"

# Install dependencies
echo "Verifying dependencies..."
$PYTHON -m pip install --quiet google-genai fastapi uvicorn websockets pyyaml pdfplumber httpx 2>/dev/null || true

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
