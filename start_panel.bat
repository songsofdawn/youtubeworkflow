@echo off
setlocal EnableExtensions

set "PROJECT_ROOT=%~dp0"
cd /d "%PROJECT_ROOT%" || goto :bad_root

if not exist ".venv\Scripts\python.exe" (
  echo [ERROR] Python environment not found. Run setup_stage1_fixed.bat first.
  goto :failed
)
if not exist "src\run_control_panel.py" (
  echo [ERROR] Control panel entrypoint not found.
  goto :failed
)

".venv\Scripts\python.exe" "src\run_control_panel.py" %*
set "EXIT_CODE=%ERRORLEVEL%"
if "%EXIT_CODE%"=="0" exit /b 0
echo.
echo [ERROR] Control panel exited with code %EXIT_CODE%.
goto :failed

:bad_root
echo [ERROR] Cannot enter project directory: %PROJECT_ROOT%

:failed
pause
exit /b 1
