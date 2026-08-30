@echo off
setlocal
set "BILIUP_UI=%~dp0biliup\bbup-app\tauri-app.exe"
if not exist "%BILIUP_UI%" (
  echo [ERROR] Missing biliup account application: %BILIUP_UI%
  pause
  exit /b 1
)
start "biliup" /D "%~dp0biliup\bbup-app" "%BILIUP_UI%"
exit /b 0
