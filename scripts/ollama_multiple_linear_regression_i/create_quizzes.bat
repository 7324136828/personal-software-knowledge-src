@echo off
call "%~dp0run_action.bat" create_quizzes %*
exit /b %ERRORLEVEL%
