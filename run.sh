#!/usr/bin/env bash
set -e

echo "========================================================"
echo " Skill-Driven Content Generator - Starting Application"
echo "========================================================"
echo ""

if [ -f ".venv/bin/python" ]; then
    .venv/bin/python run.py
elif command -v python3 &> /dev/null; then
    echo "[WARNING] .venv not found. Running with system python3..."
    python3 run.py
else
    echo "[WARNING] .venv not found. Running with system python..."
    python run.py
fi

