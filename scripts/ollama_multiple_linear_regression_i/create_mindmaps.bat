@echo off
call "%~dp0run_action.bat" create_mindmaps %*
exit /b %ERRORLEVEL%
