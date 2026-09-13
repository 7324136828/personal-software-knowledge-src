#!/usr/bin/env python3
"""Automated project setup script.

Uses the active Python environment or creates one, installs backend Python dependencies,
installs frontend npm packages, and creates initial configuration.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
import venv
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent
VENV_DIR = ROOT_DIR / ".venv"
FRONTEND_DIR = ROOT_DIR / "frontend"


def log(msg: str) -> None:
    print(f"\n[SETUP] === {msg} ===")


def run_cmd(cmd: list[str], cwd: Path | None = None, check: bool = True) -> int:
    display = " ".join(str(c) for c in cmd)
    print(f"--> Running: {display}")
    is_windows = platform.system() == "Windows"
    result = subprocess.run(cmd, cwd=str(cwd or ROOT_DIR), shell=is_windows)
    if check and result.returncode != 0:
        raise RuntimeError(f"Command failed with exit code {result.returncode}: {display}")
    return result.returncode


def get_venv_executables() -> tuple[Path, Path]:
    is_windows = platform.system() == "Windows"
    if is_windows:
        python_exe = VENV_DIR / "Scripts" / "python.exe"
        pip_exe = VENV_DIR / "Scripts" / "pip.exe"
    else:
        python_exe = VENV_DIR / "bin" / "python"
        pip_exe = VENV_DIR / "bin" / "pip"
    return python_exe, pip_exe


def main() -> int:
    log("Checking Python environment")
    version = sys.version_info
    print(f"Detected Python {version.major}.{version.minor}.{version.micro}")
    if version < (3, 10):
        print("ERROR: Python 3.10 or higher is required.", file=sys.stderr)
        return 1

    # 1. Python environment. Never create a nested environment when the user
    # has already activated virtualenv, venv, or Conda.
    active_environment = (
        sys.prefix != getattr(sys, "base_prefix", sys.prefix)
        or bool(os.environ.get("VIRTUAL_ENV"))
        or bool(os.environ.get("CONDA_PREFIX"))
    )
    if active_environment:
        python_exe = Path(sys.executable)
        print(f"Using active Python environment at {sys.prefix}")
    else:
        if not VENV_DIR.is_dir():
            log("Creating virtual environment in .venv")
            venv.create(VENV_DIR, with_pip=True)
        else:
            print(f"Virtual environment already exists at {VENV_DIR}")

        python_exe, _ = get_venv_executables()
        if not python_exe.exists():
            log("Re-initializing virtual environment with pip")
            venv.create(VENV_DIR, with_pip=True)
            python_exe, _ = get_venv_executables()

    # 2. Upgrade pip and install Python requirements
    log("Upgrading pip in the selected Python environment")
    run_cmd([str(python_exe), "-m", "pip", "install", "--upgrade", "pip"])

    log("Installing Python dependencies from requirements.txt")
    run_cmd([str(python_exe), "-m", "pip", "install", "-r", "requirements.txt"])

    # 3. Environment configuration template
    env_file = ROOT_DIR / ".env"
    env_example = ROOT_DIR / ".env.example"
    if not env_file.exists() and env_example.exists():
        log("Creating .env from .env.example")
        shutil.copy2(env_example, env_file)
        print("Created .env with default placeholders.")

    # 4. Frontend setup
    if FRONTEND_DIR.is_dir() and (FRONTEND_DIR / "package.json").exists():
        log("Setting up frontend dependencies with npm")
        npm_cmd = "npm.cmd" if platform.system() == "Windows" else "npm"
        try:
            run_cmd([npm_cmd, "--version"], check=False)
            run_cmd([npm_cmd, "install"], cwd=FRONTEND_DIR)
        except Exception as exc:
            print(f"Warning: Failed to install frontend packages: {exc}")
            print("Please ensure Node.js and npm are installed and in your PATH.")

    log("Setup completed successfully!")
    print("\nYou can now start the application with:")
    if platform.system() == "Windows":
        print("  run.bat")
    else:
        print("  ./run.sh")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
