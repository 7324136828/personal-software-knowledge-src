@echo off
setlocal EnableExtensions EnableDelayedExpansion

rem Run all nine artifact generators with one consistent output timestamp.
rem Usage: run_all.bat [run_id]
set "RUN_ID=%~1"
if "%RUN_ID%"=="" set "RUN_ID=20260907115400"

for %%A in (
  create_datatables
  create_flashcards
  create_infographics
  create_mindmaps
  create_podcasts
  create_qandas
  create_quizzes
  create_reports
  create_slides
) do (
  call "%~dp0run_action.bat" %%A "%RUN_ID%"
  set "EXIT_CODE=!ERRORLEVEL!"
  if not "!EXIT_CODE!"=="0" (
    echo [ERROR] %%A failed. Stopping the batch run.
    exit /b !EXIT_CODE!
  )
)

echo [INFO] All requested artifacts completed.
exit /b 0
