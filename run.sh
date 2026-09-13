#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"

echo "========================================================"
echo " Skill-Driven Content Generator - Starting Application"
echo "========================================================"
echo ""

if [ -n "${VIRTUAL_ENV:-}${CONDA_PREFIX:-}" ]; then
    PYTHON_BIN="$(command -v python || command -v python3)"
else
    if [ ! -x ".venv/bin/python" ]; then
        echo "[RUN.SH] No active environment or local .venv. Running setup.sh..."
        ./setup.sh
    fi
    PYTHON_BIN=".venv/bin/python"
fi

exec "$PYTHON_BIN" run.py "$@"
