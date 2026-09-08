@echo off
call "%~dp0run_action.bat" create_slides %*
exit /b %ERRORLEVEL%
