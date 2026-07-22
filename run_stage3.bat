@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "STAGE3_PYTHON=.venv\Scripts\python.exe"

if not exist "%STAGE3_PYTHON%" (
  echo [ERROR] Missing .venv\Scripts\python.exe
  pause
  exit /b 1
)
if not exist "config\stage3_config.json" (
  echo [ERROR] Missing config\stage3_config.json
  pause
  exit /b 1
)
"%STAGE3_PYTHON%" -c "import openai, dotenv" >nul 2>&1
if errorlevel 1 (
  echo [ERROR] Missing Stage 3 dependencies. Run: .venv\Scripts\python.exe -m pip install -r requirements_stage3.txt
  pause
  exit /b 1
)

set "VIDEO_DIR=%~1"
set "RUN_MODE=%~2"
if not defined VIDEO_DIR set /p "VIDEO_DIR=Video task directory: "
if not defined VIDEO_DIR (
  echo [ERROR] Video task directory is required.
  pause
  exit /b 1
)
if not exist "%VIDEO_DIR%" (
  echo [ERROR] Directory does not exist: %VIDEO_DIR%
  pause
  exit /b 1
)

if defined RUN_MODE goto select_mode

echo 1. Clean and rebuild English subtitles
echo 2. Check translation settings and batch count ^(no API charge^)
echo 3. Translate cleaned English subtitles to Chinese ^(paid API, resume enabled^)
echo 4. Clean English subtitles, then translate to Chinese ^(paid API, resume enabled^)
echo 5. Translate and polish every Chinese subtitle ^(paid API^)
echo 6. Translate everything again from scratch ^(paid API, ignores checkpoints^)
set /p "STAGE3_CHOICE=Choose 1-6: "
if "%STAGE3_CHOICE%"=="1" set "RUN_MODE=clean"
if "%STAGE3_CHOICE%"=="2" set "RUN_MODE=check"
if "%STAGE3_CHOICE%"=="3" set "RUN_MODE=translate"
if "%STAGE3_CHOICE%"=="4" set "RUN_MODE=full"
if "%STAGE3_CHOICE%"=="5" set "RUN_MODE=polish"
if "%STAGE3_CHOICE%"=="6" set "RUN_MODE=retranslate"

:select_mode
set "RUN_ARGS="
set "PAID_MODE=0"
if /I "%RUN_MODE%"=="clean" set "RUN_ARGS=--steps clean --resume"
if /I "%RUN_MODE%"=="check" set "RUN_ARGS=--steps translate --resume"
if /I "%RUN_MODE%"=="translate" (
  set "RUN_ARGS=--steps translate --resume --allow-paid-api"
  set "PAID_MODE=1"
)
if /I "%RUN_MODE%"=="full" (
  set "RUN_ARGS=--steps clean,translate --resume --allow-paid-api"
  set "PAID_MODE=1"
)
if /I "%RUN_MODE%"=="polish" (
  set "RUN_ARGS=--steps translate --resume --allow-paid-api --polish-all"
  set "PAID_MODE=1"
)
if /I "%RUN_MODE%"=="retranslate" (
  set "RUN_ARGS=--steps translate --force --allow-paid-api"
  set "PAID_MODE=1"
)

if not defined RUN_ARGS (
  echo [ERROR] Invalid mode: %RUN_MODE%
  echo Valid modes: clean, check, translate, full, polish, retranslate
  pause
  exit /b 1
)

if not "%PAID_MODE%"=="1" goto paid_mode_checked
"%STAGE3_PYTHON%" -c "from src.stage3.translator_deepseek import load_deepseek_settings; raise SystemExit(0 if load_deepseek_settings()['api_key'] else 1)" >nul 2>&1
if errorlevel 1 (
  echo [ERROR] DEEPSEEK_API_KEY is missing. Add it to the project .env file.
  pause
  exit /b 1
)
echo [NOTICE] Paid DeepSeek API mode is enabled.
set "PAID_CONFIRM="
set /p "PAID_CONFIRM=Type YES to confirm paid translation: "
if /I "%PAID_CONFIRM%"=="YES" goto paid_mode_checked
echo [CANCELLED] Paid translation was not started.
pause
exit /b 0

:paid_mode_checked

echo [INFO] Video directory: %VIDEO_DIR%
echo [INFO] Mode: %RUN_MODE%
"%STAGE3_PYTHON%" src\run_stage3.py --video-dir "%VIDEO_DIR%" %RUN_ARGS%
set "STAGE3_EXIT_CODE=%ERRORLEVEL%"
if "%STAGE3_EXIT_CODE%"=="0" (
  echo [DONE] Subtitle processing completed.
) else (
  echo [ERROR] Subtitle processing failed with exit code %STAGE3_EXIT_CODE%.
)
pause
endlocal & exit /b %STAGE3_EXIT_CODE%
