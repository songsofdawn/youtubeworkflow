@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"
set "PYTHONUTF8=1"

set "PROJECT_ROOT=%~dp0"
call "%PROJECT_ROOT%set_runtime.bat" || goto failed
set "SUBTITLE_PYTHON=%PYTHON_EXE%"
if not exist "config\stage3_config.json" (
  echo [ERROR] Missing subtitle configuration.
  pause
  exit /b 1
)

set "VIDEO_DIR=%~1"
set "RUN_MODE=%~2"
set "REVIEW_FILE=%~3"
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
echo 3. Translate the selected English subtitles to Chinese ^(selected AI API, resume enabled^)
echo 4. Clean English subtitles, then translate to Chinese ^(selected AI API, resume enabled^)
echo 5. Translate and polish every Chinese subtitle ^(selected AI API^)
echo 6. Translate everything again from scratch ^(selected AI API, ignores checkpoints^)
echo 7. Test local GPU speech recognition on the first 30 seconds ^(no translation API^)
echo 8. Build both English sources and select automatically ^(no translation API, resume enabled^)
echo 9. Automatically select English subtitles, then translate to Chinese ^(selected AI API, resume enabled^)
echo 10. Export TSV and read-only HTML for human review
echo 11. Import the edited review TSV and create zh.reviewed.srt
set /p "SUBTITLE_CHOICE=Choose 1-11: "
if "%SUBTITLE_CHOICE%"=="1" set "RUN_MODE=clean"
if "%SUBTITLE_CHOICE%"=="2" set "RUN_MODE=check"
if "%SUBTITLE_CHOICE%"=="3" set "RUN_MODE=translate"
if "%SUBTITLE_CHOICE%"=="4" set "RUN_MODE=full"
if "%SUBTITLE_CHOICE%"=="5" set "RUN_MODE=polish"
if "%SUBTITLE_CHOICE%"=="6" set "RUN_MODE=retranslate"
if "%SUBTITLE_CHOICE%"=="7" set "RUN_MODE=asr30"
if "%SUBTITLE_CHOICE%"=="8" set "RUN_MODE=autoselect"
if "%SUBTITLE_CHOICE%"=="9" set "RUN_MODE=autotranslate"
if "%SUBTITLE_CHOICE%"=="10" set "RUN_MODE=reviewexport"
if "%SUBTITLE_CHOICE%"=="11" set "RUN_MODE=reviewimport"

:select_mode
set "RUN_ARGS="
set "PAID_MODE=0"
set "NEEDS_WHISPER=0"
set "TRANSLATE_AFTER_SELECT=0"
if /I "%RUN_MODE%"=="clean" set "RUN_ARGS=--steps clean --resume"
if /I "%RUN_MODE%"=="check" set "RUN_ARGS=--steps translate --resume"
if /I "%RUN_MODE%"=="translate" (
  set "RUN_ARGS=--steps translate --resume --allow-paid-api"
  set "PAID_MODE=1"
)
if /I "%RUN_MODE%"=="full" (
  set "RUN_ARGS=--steps clean,translate --resume --allow-paid-api"
  set "PAID_MODE=1"
  set "NEEDS_WHISPER=1"
)
if /I "%RUN_MODE%"=="polish" (
  set "RUN_ARGS=--steps translate --resume --allow-paid-api --polish-all"
  set "PAID_MODE=1"
)
if /I "%RUN_MODE%"=="retranslate" (
  set "RUN_ARGS=--steps translate --force --allow-paid-api"
  set "PAID_MODE=1"
)
if /I "%RUN_MODE%"=="asr30" (
  set "RUN_ARGS=--steps asr --subtitle-source whisper --asr-max-seconds 30 --force"
  set "NEEDS_WHISPER=1"
)
if /I "%RUN_MODE%"=="autoselect" (
  set "RUN_ARGS=--steps select --subtitle-source auto --resume"
  set "NEEDS_WHISPER=1"
)
if /I "%RUN_MODE%"=="autotranslate" (
  set "RUN_ARGS=--steps select --subtitle-source auto --resume"
  set "PAID_MODE=1"
  set "NEEDS_WHISPER=1"
  set "TRANSLATE_AFTER_SELECT=1"
)
if /I "%RUN_MODE%"=="reviewexport" set "RUN_ARGS=--steps review-export --resume"
if /I "%RUN_MODE%"=="reviewimport" (
  if not defined REVIEW_FILE set /p "REVIEW_FILE=Edited review TSV path: "
  if not defined REVIEW_FILE (
    echo [ERROR] Review TSV path is required.
    pause
    exit /b 1
  )
  set "RUN_ARGS=--steps review-import --resume"
)

if not defined RUN_ARGS (
  echo [ERROR] Invalid mode: %RUN_MODE%
  echo Valid modes: clean, check, translate, full, polish, retranslate, asr30, autoselect, autotranslate, reviewexport, reviewimport
  pause
  exit /b 1
)

if not exist "%SUBTITLE_PYTHON%" (
  echo [ERROR] Missing Python environment: %SUBTITLE_PYTHON%
  pause
  exit /b 1
)

if "%NEEDS_WHISPER%"=="1" (
  "%SUBTITLE_PYTHON%" -c "import faster_whisper, ctranslate2" >nul 2>&1
  if errorlevel 1 (
    echo [ERROR] Missing local speech recognition dependencies in the unified runtime.
    pause
    exit /b 1
  )
 ) else (
  "%SUBTITLE_PYTHON%" -c "import openai, dotenv" >nul 2>&1
  if errorlevel 1 (
    echo [ERROR] Missing subtitle processing dependencies in the unified runtime.
    pause
    exit /b 1
  )
)

if not "%TRANSLATE_AFTER_SELECT%"=="1" goto auto_selection_finished
echo [INFO] Video directory: %VIDEO_DIR%
echo [INFO] Automatically evaluating English subtitle sources...
"%SUBTITLE_PYTHON%" src\run_stage3.py --video-dir "%VIDEO_DIR%" %RUN_ARGS%
set "SELECTION_EXIT_CODE=%ERRORLEVEL%"
if not "%SELECTION_EXIT_CODE%"=="0" (
  echo [WARNING] Some videos could not produce a usable English subtitle.
  echo [WARNING] API confirmation will still be shown, and ready videos can continue translating.
)
set "RUN_ARGS=--steps translate --resume --allow-paid-api"

:auto_selection_finished
if not "%PAID_MODE%"=="1" goto paid_mode_checked
"%SUBTITLE_PYTHON%" -c "import openai, dotenv; from src.stage3.translator_deepseek import load_deepseek_settings; raise SystemExit(0 if load_deepseek_settings()['api_key'] else 1)" >nul 2>&1
if errorlevel 1 (
  echo [ERROR] The selected provider API key is missing. Configure it in the panel or project .env.
  pause
  exit /b 1
)
echo [NOTICE] The selected AI translation API is enabled.
set "PAID_CONFIRM="
set /p "PAID_CONFIRM=Type YES to confirm the API call: "
if /I "%PAID_CONFIRM%"=="YES" goto paid_mode_checked
echo [CANCELLED] API translation was not started.
pause
exit /b 0

:paid_mode_checked

echo [INFO] Video directory: %VIDEO_DIR%
echo [INFO] Mode: %RUN_MODE%
if /I "%RUN_MODE%"=="reviewimport" (
  "%SUBTITLE_PYTHON%" src\run_stage3.py --video-dir "%VIDEO_DIR%" %RUN_ARGS% --review-file "%REVIEW_FILE%"
) else (
  "%SUBTITLE_PYTHON%" src\run_stage3.py --video-dir "%VIDEO_DIR%" %RUN_ARGS%
)
set "SUBTITLE_EXIT_CODE=%ERRORLEVEL%"
if "%SUBTITLE_EXIT_CODE%"=="0" (
  echo [DONE] Subtitle processing completed.
) else (
  echo [ERROR] Subtitle processing failed with exit code %SUBTITLE_EXIT_CODE%.
)
pause
endlocal & exit /b %SUBTITLE_EXIT_CODE%
