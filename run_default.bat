@echo off
rem Local launch: UI 5110, API 8310.
call "%~dp0run_lan.bat" -Local %*
exit /b %errorlevel%
