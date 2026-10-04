@echo off
setlocal
rem LAN launch: UI 5110, API 8310. Use -Local for loopback only.
set "HOST=0.0.0.0"
if /i "%~1"=="-Local" (
    set "HOST=127.0.0.1"
    shift
)
if not "%~1"=="" (
    echo Usage: run_lan.bat [-Local]
    exit /b 1
)
set "FRONTEND_HOST=%HOST%"
if not defined FRONTEND_PORT set "FRONTEND_PORT=5110"
if not defined BACKEND_PORT set "BACKEND_PORT=8310"
if "%HOST%"=="0.0.0.0" (
    echo LAN mode: open http://YOUR-PC-IP:%FRONTEND_PORT% from another device.
    echo The startup output shows the actual port if the requested port is busy.
)
call "%~dp0run.bat"
exit /b %errorlevel%
