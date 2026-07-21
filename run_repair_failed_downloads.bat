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

echo Scanning incomplete Stage 2 candidate downloads...
".venv\Scripts\python.exe" "src\repair_failed_downloads.py" %*
set "EXIT_CODE=%ERRORLEVEL%"
echo.
echo Repair finished with exit code %EXIT_CODE%.
pause
exit /b %EXIT_CODE%

:failed
pause
exit /b 2
