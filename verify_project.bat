@echo off
setlocal
cd /d "%~dp0"

set "PROJECT_ROOT=%CD%\"
call "%PROJECT_ROOT%set_runtime.bat"
if errorlevel 1 exit /b 1

echo [1/7] Running the complete offline test suite...
"%PYTHON_EXE%" -m unittest discover -s tests -p "test_*.py"
if errorlevel 1 goto :failed

echo [2/7] Checking source syntax...
"%PYTHON_EXE%" -m compileall -q src tests
if errorlevel 1 goto :failed

echo [3/7] Checking application entrypoints...
for %%M in (src.download_video src.run_stage3 src.run_dubbing src.run_stage4 src.run_control_panel) do (
  "%PYTHON_EXE%" -m %%M --help >nul
  if errorlevel 1 goto :failed
)

echo [4/7] Checking required local tools and Whisper model...
for %%T in (yt-dlp.exe ffmpeg.exe ffprobe.exe deno.exe) do (
  if not exist "tools\bin\%%T" (
    echo [ERROR] Missing required tool: tools\bin\%%T
    goto :failed
  )
)
if not exist "models\faster-whisper-large-v3\model.bin" (
  echo [ERROR] Missing Whisper model: models\faster-whisper-large-v3\model.bin
  goto :failed
)

echo [5/7] Checking installed dependency consistency...
"%PYTHON_EXE%" -m pip check
if errorlevel 1 goto :failed

echo [6/7] Checking Git whitespace...
git diff --check
if errorlevel 1 goto :failed

echo [7/7] Checking that private and generated paths are not tracked...
for /f "delims=" %%F in ('git ls-files -- ".env" "models" "downloads" ".venv" ".venv_stage3" "private" "logs" "work" "candidates" "dist" "biliup"') do (
  echo [ERROR] Private or generated path is tracked: %%F
  goto :failed
)

echo.
echo [PASS] Project verification completed successfully.
exit /b 0

:failed
echo.
echo [FAIL] Project verification stopped at a failed check.
exit /b 1
