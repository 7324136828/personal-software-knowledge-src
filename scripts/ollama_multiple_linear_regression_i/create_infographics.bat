@echo off
call "%~dp0run_action.bat" create_infographics %*
exit /b %ERRORLEVEL%
