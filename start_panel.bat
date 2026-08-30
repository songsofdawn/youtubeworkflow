@echo off
setlocal EnableExtensions

set "PROJECT_ROOT=%~dp0"
cd /d "%PROJECT_ROOT%" || goto :bad_root

call "%PROJECT_ROOT%set_runtime.bat" || goto :failed
if not exist "src\run_control_panel.py" (
  echo [ERROR] Control panel entrypoint not found.
  goto :failed
)

"%PYTHON_EXE%" "src\run_control_panel.py" %*
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
