@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo [ERROR] Stage 1 virtual environment is missing: .venv\Scripts\python.exe
  echo Run setup_stage1_fixed.bat first.
  pause
  exit /b 2
)
for %%T in (yt-dlp.exe ffmpeg.exe ffprobe.exe) do (
  if not exist "tools\bin\%%T" (
    echo [ERROR] Missing project-local tool: tools\bin\%%T
    pause
    exit /b 2
  )
)
if not exist "config\download_config.json" (
  echo [ERROR] Missing config\download_config.json
  pause
  exit /b 2
)

if not exist "downloads\candidates" mkdir "downloads\candidates"
if not exist "downloads\manual" mkdir "downloads\manual"
if not exist "private" mkdir "private"

echo Stage 2 is ready. No system PATH or Conda tools are required.
echo Optional cookies file: private\cookies.txt in Netscape format.
pause
exit /b 0
