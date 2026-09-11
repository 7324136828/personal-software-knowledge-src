@echo off
setlocal
echo ========================================================
echo  Skill-Driven Content Generator - Starting Application
echo ========================================================
echo.

if exist .venv\Scripts\python.exe (
    .venv\Scripts\python.exe run.py
) else (
    echo [WARNING] .venv not found. Running with system python...
    python run.py
)

if errorlevel 1 (
    echo.
    echo [ERROR] Application terminated with an error. Run setup.bat first if dependencies are missing.
    pause
)
endlocal

