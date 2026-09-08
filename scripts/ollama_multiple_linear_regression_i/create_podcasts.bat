@echo off
call "%~dp0run_action.bat" create_podcasts %*
exit /b %ERRORLEVEL%
