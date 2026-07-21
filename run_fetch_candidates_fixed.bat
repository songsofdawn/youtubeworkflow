@echo off
setlocal EnableExtensions DisableDelayedExpansion

set "PROJECT_ROOT=%~dp0"
cd /d "%PROJECT_ROOT%" || goto :bad_root

if not exist ".venv\Scripts\python.exe" (
  echo [ERROR] Virtual environment not found. Run setup_stage1_fixed.bat first.
  goto :failed
)
if not exist ".env" (
  echo [ERROR] .env was not found. Create it and set YOUTUBE_API_KEY.
  goto :failed
)
findstr /R /C:"^[ ]*YOUTUBE_API_KEY[ ]*=[ ]*[^ ][^ ]*" ".env" >nul 2>&1
if errorlevel 1 (
  echo [ERROR] YOUTUBE_API_KEY is missing or empty in .env.
  goto :failed
)
if not exist "src\fetch_daily_candidates.py" (
  echo [ERROR] Candidate fetcher was not found.
  goto :failed
)

echo [INFO] Fetching US YouTube candidates...
".venv\Scripts\python.exe" "src\fetch_daily_candidates.py" --config "config\trending_config.json" --limit 50
if errorlevel 1 goto :fetch_failed

echo [SUCCESS] Candidate files are in: %PROJECT_ROOT%candidates
start "" "%PROJECT_ROOT%candidates"
exit /b 0

:bad_root
echo [ERROR] Cannot enter project directory: %PROJECT_ROOT%
goto :failed

:fetch_failed
echo [ERROR] Fetch failed. Check: %PROJECT_ROOT%logs

:failed
pause
exit /b 1
