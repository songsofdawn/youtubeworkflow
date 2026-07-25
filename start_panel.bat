@echo off
setlocal EnableExtensions
chcp 65001 >nul

set "PROJECT_ROOT=%~dp0"
cd /d "%PROJECT_ROOT%" || goto :bad_root

if not exist ".venv\Scripts\python.exe" (
  echo [错误] 下载环境不存在，请先运行 setup_stage1_fixed.bat。
  goto :failed
)
if not exist "src\run_control_panel.py" (
  echo [错误] 控制面板入口不存在。
  goto :failed
)

".venv\Scripts\python.exe" "src\run_control_panel.py"
set "EXIT_CODE=%ERRORLEVEL%"
if "%EXIT_CODE%"=="0" exit /b 0
echo.
echo [错误] 控制面板已退出，代码：%EXIT_CODE%
goto :failed

:bad_root
echo [错误] 无法进入项目目录：%PROJECT_ROOT%

:failed
pause
exit /b 1
