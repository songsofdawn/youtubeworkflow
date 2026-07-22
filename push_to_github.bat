@echo off
setlocal EnableExtensions
cd /d "%~dp0"

where git >nul 2>&1
if errorlevel 1 (
  echo [ERROR] Git is not installed or is not available in PATH.
  pause
  exit /b 1
)

for /f "delims=" %%B in ('git branch --show-current 2^>nul') do set "CURRENT_BRANCH=%%B"
if not defined CURRENT_BRANCH (
  echo [ERROR] Cannot determine the current Git branch.
  pause
  exit /b 1
)

git remote get-url origin >nul 2>&1
if errorlevel 1 (
  echo [ERROR] Git remote "origin" is not configured.
  pause
  exit /b 1
)

echo [INFO] Pushing committed changes from branch: %CURRENT_BRANCH%
git push origin "%CURRENT_BRANCH%"
set "PUSH_EXIT_CODE=%ERRORLEVEL%"

if "%PUSH_EXIT_CODE%"=="0" (
  echo [DONE] GitHub upload completed.
) else (
  echo [ERROR] Git push failed with exit code %PUSH_EXIT_CODE%.
)

pause
endlocal & exit /b %PUSH_EXIT_CODE%
