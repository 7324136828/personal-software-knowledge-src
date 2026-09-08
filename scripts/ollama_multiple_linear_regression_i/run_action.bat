@echo off
setlocal EnableExtensions

rem Shared runner for the Multiple Linear Regression I Ollama examples.
rem Usage: run_action.bat create_datatables [run_id]

set "ACTION=%~1"
set "RUN_ID=%~2"
if "%ACTION%"=="" (
  echo [ERROR] Supply an action, for example: create_datatables
  exit /b 2
)
if "%RUN_ID%"=="" set "RUN_ID=20260907115400"

set "PROJECT_ROOT=%~dp0..\.."
pushd "%PROJECT_ROOT%" >nul || (
  echo [ERROR] Could not open the project root: %PROJECT_ROOT%
  exit /b 1
)

set "CONNECTOR=ollama"
set "INPUT_FILE=input\Regression Modeling with Actuarial and Financial Applications_014 - 3 Multiple Linear Regression - I.tex"
set "OUTPUT_FILE=tmp\output_%ACTION%_%RUN_ID%.txt"
if not defined PYTHON_CMD set "PYTHON_CMD=py -3.11"

if not exist "%INPUT_FILE%" (
  echo [ERROR] Input file not found: %INPUT_FILE%
  echo [INFO] Add the requested chapter as a .tex file, or edit INPUT_FILE in this script.
  popd
  exit /b 3
)

%PYTHON_CMD% --version >nul 2>&1
if errorlevel 1 (
  echo [ERROR] Python 3.11 was not found.
  echo [INFO] Install Python 3.11+ or set PYTHON_CMD to your interpreter command.
  popd
  exit /b 5
)

echo [INFO] Connector: %CONNECTOR%
echo [INFO] Action: %ACTION%
echo [INFO] Input: %INPUT_FILE%
echo [INFO] Output: %OUTPUT_FILE%
%PYTHON_CMD% orchestrator.py --connector "%CONNECTOR%" --input "%INPUT_FILE%" --action "%ACTION%" --output "%OUTPUT_FILE%"
set "EXIT_CODE=%ERRORLEVEL%"

popd
exit /b %EXIT_CODE%
