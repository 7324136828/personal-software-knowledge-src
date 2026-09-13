#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"

echo "========================================================"
echo " Skill-Driven Content Generator - Project Setup"
echo "========================================================"
echo ""

if [ -n "${VIRTUAL_ENV:-}${CONDA_PREFIX:-}" ]; then
    PYTHON_BIN="$(command -v python || command -v python3)"
elif command -v python3 &> /dev/null; then
    PYTHON_BIN="python3"
elif command -v python &> /dev/null; then
    PYTHON_BIN="python"
else
    echo "[ERROR] Python was not found in PATH."
    exit 1
fi

echo "[SETUP] Launching setup.py using ${PYTHON_BIN}..."
${PYTHON_BIN} setup.py

echo ""
echo "[SETUP] Done! Run './run.sh' to start the application."
