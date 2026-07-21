@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo [ERROR] Missing .venv\Scripts\python.exe. Run setup_stage2.bat first.
  goto :failed
)
for %%T in (yt-dlp.exe ffmpeg.exe ffprobe.exe) do (
  if not exist "tools\bin\%%T" (
    echo [ERROR] Missing tools\bin\%%T
    goto :failed
  )
)

if "%~1"=="" (
  ".venv\Scripts\python.exe" "src\download_selected_candidates.py"
) else (
  ".venv\Scripts\python.exe" "src\download_selected_candidates.py" --input "%~1"
)
set "EXIT_CODE=%ERRORLEVEL%"
echo.
echo Stage 2 finished with exit code %EXIT_CODE%.
pause
exit /b %EXIT_CODE%

:failed
pause
exit /b 2
