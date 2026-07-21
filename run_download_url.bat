@echo off
setlocal DisableDelayedExpansion
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

echo ================================================================
echo Copyright notice: only download videos you own, license, or have
echo explicit permission to download and use.
echo ================================================================
set /p "VIDEO_URL=Video URL: "
if not defined VIDEO_URL (
  echo URL is empty. Nothing was downloaded.
  goto :failed
)
set /p "RIGHTS_CONFIRM=Do you confirm you have the required rights? [Y/N]: "
if /I not "%RIGHTS_CONFIRM%"=="Y" (
  echo Rights were not confirmed. Nothing was downloaded.
  goto :failed
)

".venv\Scripts\python.exe" "src\download_video.py" --url "%VIDEO_URL%" --confirm-rights
set "EXIT_CODE=%ERRORLEVEL%"
echo.
echo Download finished with exit code %EXIT_CODE%.
pause
exit /b %EXIT_CODE%

:failed
pause
exit /b 2
