@echo off
call "%~dp0run_action.bat" create_reports %*
exit /b %ERRORLEVEL%
