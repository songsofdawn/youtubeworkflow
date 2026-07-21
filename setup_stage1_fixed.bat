@echo off
setlocal EnableExtensions DisableDelayedExpansion

set "PROJECT_ROOT=%~dp0"
cd /d "%PROJECT_ROOT%" || goto :bad_root

py -3.13 --version >nul 2>&1
if not errorlevel 1 (
  set "PYTHON_CMD=py -3.13"
) else (
  python --version >nul 2>&1 || goto :no_python
  set "PYTHON_CMD=python"
)

if not exist ".venv\Scripts\python.exe" (
  echo [INFO] Creating Python virtual environment...
  %PYTHON_CMD% -m venv ".venv" || goto :failed
)
if not exist "requirements_stage1.txt" (
  echo [ERROR] requirements_stage1.txt was not found.
  goto :failed
)
echo [INFO] Installing Stage 1 dependencies...
".venv\Scripts\python.exe" -m pip install -r "requirements_stage1.txt" || goto :failed
if not exist ".env" (
  if exist ".env.example" copy /Y ".env.example" ".env" >nul
)
echo [SUCCESS] Environment is ready. Set YOUTUBE_API_KEY in: %PROJECT_ROOT%.env
pause
exit /b 0

:bad_root
echo [ERROR] Cannot enter project directory: %PROJECT_ROOT%
goto :failed
:no_python
echo [ERROR] Python 3.13 was not found.
:failed
pause
exit /b 1
