@echo off
setlocal
if defined VIRTUAL_ENV (
    python "%~dp0study_set.py" %*
) else if defined CONDA_PREFIX (
    python "%~dp0study_set.py" %*
) else if exist "%~dp0.venv\Scripts\python.exe" (
    "%~dp0.venv\Scripts\python.exe" "%~dp0study_set.py" %*
) else (
    python "%~dp0study_set.py" %*
)
exit /b %ERRORLEVEL%
