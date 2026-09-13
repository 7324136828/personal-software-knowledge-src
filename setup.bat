@echo off
setlocal
cd /d "%~dp0"
echo ========================================================
echo  Skill-Driven Content Generator - Project Setup
echo ========================================================
echo.
echo [SETUP] Launching setup.py to configure virtual environment and dependencies...
python setup.py
if errorlevel 1 (
    echo.
    echo [ERROR] Setup encountered an error. Please verify Python is installed and accessible.
    pause
    exit /b %errorlevel%
)
echo.
echo [SETUP] Done! Run 'run.bat' to start the application.
endlocal
