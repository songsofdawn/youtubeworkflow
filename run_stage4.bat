@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul

set "PROJECT_ROOT=%~dp0"
set "PYTHON_EXE=%PROJECT_ROOT%.venv_stage3\Scripts\python.exe"
set "FFMPEG_EXE=%PROJECT_ROOT%tools\bin\ffmpeg.exe"
set "FFPROBE_EXE=%PROJECT_ROOT%tools\bin\ffprobe.exe"

if not exist "%PYTHON_EXE%" (
  echo [ERROR] Stage 3 Python environment not found: %PYTHON_EXE%
  goto :failed
)
if not exist "%FFMPEG_EXE%" (
  echo [ERROR] FFmpeg not found: %FFMPEG_EXE%
  goto :failed
)
if not exist "%FFPROBE_EXE%" (
  echo [ERROR] FFprobe not found: %FFPROBE_EXE%
  goto :failed
)

set "VIDEO_DIR=%~1"
if not defined VIDEO_DIR set /p "VIDEO_DIR=Video task directory: "
if not exist "%VIDEO_DIR%\" (
  echo [ERROR] Video task directory not found: %VIDEO_DIR%
  goto :failed
)

if /I "%~2"=="ass" set "MODE=ass"
if /I "%~2"=="softsub" set "MODE=softsub"
if /I "%~2"=="hardsub" set "MODE=hardsub"
if /I "%~2"=="both" set "MODE=both"
if /I "%~2"=="dry-run" set "MODE=dry-run"
if defined MODE goto :run

echo.
echo 1. Build bilingual ASS only
echo 2. Build soft-subtitle MKV
echo 3. Build hard-subtitle MP4
echo 4. Build both video outputs
echo 5. Dry-run validation
choice /C 12345 /N /M "Choose [1-5]: "
if errorlevel 5 set "MODE=dry-run"
if errorlevel 4 if not defined MODE set "MODE=both"
if errorlevel 3 if not defined MODE set "MODE=hardsub"
if errorlevel 2 if not defined MODE set "MODE=softsub"
if errorlevel 1 if not defined MODE set "MODE=ass"

:run
set "EXTRA_ARGS="
if /I "%MODE%"=="dry-run" (
  set "MODE=both"
  set "EXTRA_ARGS=--dry-run"
)
"%PYTHON_EXE%" "%PROJECT_ROOT%src\run_stage4.py" --video-dir "%VIDEO_DIR%" --mode "%MODE%" %EXTRA_ARGS%
if errorlevel 1 goto :failed
echo.
echo Stage 4 completed.
echo Output directory: %VIDEO_DIR%\stage4
goto :done

:failed
echo.
echo Stage 4 failed.
echo FFmpeg log: %VIDEO_DIR%\stage4\logs\ffmpeg_commands.log

:done
pause
endlocal
