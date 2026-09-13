#!/usr/bin/env python3
"""Unified development and runtime coordinator.

Starts the FastAPI backend and Vite React frontend on the first available
ports, coordinating shutdown cleanly on Ctrl+C.
"""

from __future__ import annotations

import os
import platform
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent
FRONTEND_DIR = ROOT_DIR / "frontend"
VENV_DIR = ROOT_DIR / ".venv"


def get_venv_python() -> str:
    if (
        sys.prefix != getattr(sys, "base_prefix", sys.prefix)
        or os.environ.get("VIRTUAL_ENV")
        or os.environ.get("CONDA_PREFIX")
    ):
        return sys.executable
    is_windows = platform.system() == "Windows"
    if is_windows:
        candidate = VENV_DIR / "Scripts" / "python.exe"
    else:
        candidate = VENV_DIR / "bin" / "python"
    if candidate.exists():
        return str(candidate)
    return sys.executable


def requested_port(variable: str, default: int) -> int:
    value = os.environ.get(variable, str(default))
    try:
        port = int(value)
    except ValueError as error:
        raise RuntimeError(f"{variable} must be an integer, not {value!r}") from error
    if not 1 <= port <= 65535:
        raise RuntimeError(f"{variable} must be between 1 and 65535")
    return port


def available_port(start: int, reserved: set[int] | None = None) -> int:
    reserved = reserved or set()
    for port in range(start, 65536):
        if port in reserved:
            continue
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                continue
        return port
    raise RuntimeError(f"No available TCP port was found at or above {start}")


def main() -> int:
    is_windows = platform.system() == "Windows"
    python_bin = get_venv_python()
    npm_bin = "npm.cmd" if is_windows else "npm"

    print("=" * 60)
    print("  Starting Skill-Driven Content Generator Studio")
    print("=" * 60)
    print(f"[RUN] Using Python: {python_bin}")

    default_backend_port = requested_port("PORT", 8000)
    backend_port = available_port(
        requested_port("BACKEND_PORT", default_backend_port)
    )
    frontend_port = available_port(
        requested_port("FRONTEND_PORT", 5173), {backend_port}
    )
    host = os.getenv("HOST", "127.0.0.1")
    backend_url = f"http://{host}:{backend_port}"
    frontend_url = f"http://localhost:{frontend_port}"

    # 1. Start backend process
    backend_cmd = [
        python_bin,
        "-m",
        "uvicorn",
        "server:app",
        "--host",
        host,
        "--port",
        str(backend_port),
        "--reload",
    ]
    print(f"[RUN] Launching Backend on {backend_url} ...")
    backend_proc = subprocess.Popen(backend_cmd, cwd=str(ROOT_DIR))

    # 2. Start frontend process
    frontend_proc = None
    if FRONTEND_DIR.is_dir() and (FRONTEND_DIR / "package.json").exists():
        frontend_cmd = [
            npm_bin,
            "run",
            "dev",
            "--",
            "--port",
            str(frontend_port),
            "--strictPort",
        ]
        frontend_env = os.environ.copy()
        # Keep Vite's API proxy in sync when PORT or HOST is overridden.
        frontend_env["VITE_BACKEND_URL"] = backend_url
        print(f"[RUN] Launching Frontend on {frontend_url} ...")
        frontend_proc = subprocess.Popen(
            frontend_cmd,
            cwd=str(FRONTEND_DIR),
            env=frontend_env,
        )
    else:
        print("[RUN] Frontend directory not found; backend serving standalone.")

    print("\n" + "=" * 60)
    print("  Application is running!")
    print(f"  -> Web UI:  {frontend_url}")
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
