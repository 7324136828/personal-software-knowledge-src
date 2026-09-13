@echo off
setlocal
cd /d "%~dp0"
echo ========================================================
echo  Skill-Driven Content Generator - Starting Application
echo ========================================================
echo.

if defined VIRTUAL_ENV goto active
if defined CONDA_PREFIX goto active
if not exist ".venv\Scripts\python.exe" call setup.bat
if errorlevel 1 exit /b %ERRORLEVEL%
".venv\Scripts\python.exe" run.py %*
goto done

:active
python run.py %*

:done

if errorlevel 1 (
    echo.
    echo [ERROR] Application terminated with an error. Run setup.bat first if dependencies are missing.
    pause
)
endlocal
