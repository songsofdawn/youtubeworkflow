@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul
cd /d "%~dp0"

set "CHECK_ONLY=0"
if /I "%~1"=="--check" set "CHECK_ONLY=1"
set "PYTHON_EXE=.venv\Scripts\python.exe"
set "COMMIT_MESSAGE=fix: resume stage4 renders with larger subtitles"

where git >nul 2>&1
if errorlevel 1 (
  echo [ERROR] Git is not installed or is not available in PATH.
  goto :failed
)
if not exist "%PYTHON_EXE%" (
  echo [ERROR] Test environment not found: %PYTHON_EXE%
  goto :failed
)
git rev-parse --show-toplevel >nul 2>&1
if errorlevel 1 (
  echo [ERROR] This directory is not a Git repository.
  goto :failed
)
git remote get-url origin >nul 2>&1
if errorlevel 1 (
  echo [ERROR] Git remote "origin" is not configured.
  goto :failed
)
for /f "delims=" %%B in ('git branch --show-current 2^>nul') do set "CURRENT_BRANCH=%%B"
if not defined CURRENT_BRANCH (
  echo [ERROR] Cannot determine the current Git branch.
  goto :failed
)

echo [1/4] Running all offline tests...
"%PYTHON_EXE%" -m unittest discover -s tests
if errorlevel 1 goto :failed

echo [2/4] Checking Python syntax and Git whitespace...
"%PYTHON_EXE%" -m compileall -q src tests
if errorlevel 1 goto :failed
git diff --check
if errorlevel 1 goto :failed

if "%CHECK_ONLY%"=="1" (
  echo [DONE] Validation passed. No files were staged, committed, or pushed.
  goto :success
)

git diff --cached --quiet
if errorlevel 1 (
  echo [ERROR] Existing staged changes were found.
  echo Commit or unstage them before using this one-click publisher.
  goto :failed
)

echo [3/4] Staging the Stage 4 source allowlist...
git add -- .gitignore README.md config\stage4_config.json run_stage4.bat publish_stage4_to_github.bat src\run_stage4.py src\stage4 tests\stage4
if errorlevel 1 goto :failed

set "UNSAFE_FILE="
for /f "delims=" %%F in ('git diff --cached --name-only -- .env private downloads candidates logs work models tools') do set "UNSAFE_FILE=%%F"
if defined UNSAFE_FILE (
  echo [ERROR] Unsafe generated or private file was staged: %UNSAFE_FILE%
  echo Nothing was committed or pushed.
  goto :failed
)

git diff --cached --quiet
if not errorlevel 1 (
  echo [INFO] No new Stage 4 source changes to commit.
) else (
  git commit -m "%COMMIT_MESSAGE%"
  if errorlevel 1 goto :failed
)

echo [4/4] Pushing branch %CURRENT_BRANCH% to GitHub...
git push origin "%CURRENT_BRANCH%"
if errorlevel 1 goto :failed
echo [DONE] GitHub upload completed.
goto :success

:failed
set "RESULT=1"
echo.
echo [FAILED] One-click publishing stopped. No push was reported as successful.
goto :finish

:success
set "RESULT=0"

:finish
echo.
pause
endlocal & exit /b %RESULT%
