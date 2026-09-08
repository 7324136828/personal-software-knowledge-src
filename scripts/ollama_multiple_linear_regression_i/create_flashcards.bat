@echo off
call "%~dp0run_action.bat" create_flashcards %*
exit /b %ERRORLEVEL%
