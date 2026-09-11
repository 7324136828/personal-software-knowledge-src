#!/usr/bin/env python3
"""Unified development and runtime coordinator.

Starts the FastAPI backend server on port 8000 and the Vite React frontend
development server on port 5173, coordinating shutdown cleanly on Ctrl+C.
"""

from __future__ import annotations

import os
import platform
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent
FRONTEND_DIR = ROOT_DIR / "frontend"
VENV_DIR = ROOT_DIR / ".venv"


def get_venv_python() -> str:
    is_windows = platform.system() == "Windows"
    if is_windows:
        candidate = VENV_DIR / "Scripts" / "python.exe"
    else:
        candidate = VENV_DIR / "bin" / "python"
    if candidate.exists():
        return str(candidate)
    return sys.executable


def main() -> int:
    is_windows = platform.system() == "Windows"
    python_bin = get_venv_python()
    npm_bin = "npm.cmd" if is_windows else "npm"

    print("=" * 60)
    print("  Starting Skill-Driven Content Generator Studio")
    print("=" * 60)
    print(f"[RUN] Using Python: {python_bin}")

    port = os.getenv("PORT", "8000")
    host = os.getenv("HOST", "127.0.0.1")
    backend_url = f"http://{host}:{port}"

    # 1. Start backend process
    backend_cmd = [
        python_bin,
        "-m",
        "uvicorn",
        "server:app",
        "--host",
        host,
        "--port",
        port,
        "--reload",
    ]
    print(f"[RUN] Launching Backend on {backend_url} ...")
    backend_proc = subprocess.Popen(backend_cmd, cwd=str(ROOT_DIR))

    # 2. Start frontend process
    frontend_proc = None
    if FRONTEND_DIR.is_dir() and (FRONTEND_DIR / "package.json").exists():
        frontend_cmd = [npm_bin, "run", "dev"]
        frontend_env = os.environ.copy()
        # Keep Vite's API proxy in sync when PORT or HOST is overridden.
        frontend_env["VITE_BACKEND_URL"] = backend_url
        print(f"[RUN] Launching Frontend on http://localhost:5173 ...")
        frontend_proc = subprocess.Popen(
            frontend_cmd,
            cwd=str(FRONTEND_DIR),
            env=frontend_env,
        )
    else:
        print("[RUN] Frontend directory not found; backend serving standalone.")

    print("\n" + "=" * 60)
    print("  Application is running!")
    print("  -> Web UI:  http://localhost:5173")
    print(f"  -> API Docs: {backend_url}/docs")
    print("  Press Ctrl+C to stop both servers.")
    print("=" * 60 + "\n")

    def handle_sigint(signum, frame):
        print("\n[RUN] Stopping services...")
        if frontend_proc:
            frontend_proc.terminate()
        backend_proc.terminate()
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_sigint)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, handle_sigint)

    try:
        while True:
            time.sleep(1)
            # Check if backend unexpectedly died
            if backend_proc.poll() is not None:
                print(f"[RUN] Backend exited with code {backend_proc.returncode}")
                if frontend_proc:
                    frontend_proc.terminate()
                break
            # Check if frontend unexpectedly died
            if frontend_proc and frontend_proc.poll() is not None:
                print(f"[RUN] Frontend exited with code {frontend_proc.returncode}")
                backend_proc.terminate()
                break
    except KeyboardInterrupt:
        handle_sigint(None, None)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
