@echo off
call "%~dp0run_action.bat" create_datatables %*
exit /b %ERRORLEVEL%
