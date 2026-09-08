@echo off
call "%~dp0run_action.bat" create_qandas %*
exit /b %ERRORLEVEL%
