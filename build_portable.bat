@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul

set "PROJECT_ROOT=%~dp0"
cd /d "%PROJECT_ROOT%" || goto :bad_root

if /I "%~1"=="--help" goto :usage
if /I "%~1"=="/?" goto :usage

set "EDITION=%~1"
set "VERSION=%~2"
set "ARCHIVE_OPTION=%~3"

if not defined EDITION set "EDITION=all"
if not defined VERSION set "VERSION=0.4.0"

if /I "%EDITION%"=="cpu" goto :edition_ok
if /I "%EDITION%"=="gpu" goto :edition_ok
if /I "%EDITION%"=="all" goto :edition_ok
echo [ERROR] Invalid edition: %EDITION%
echo Usage: build_portable.bat [all^|cpu^|gpu] [version] [--skip-archive]
goto :failed

:edition_ok
set "SKIP_ARCHIVE="
if not defined ARCHIVE_OPTION goto :run
if /I "%ARCHIVE_OPTION%"=="--skip-archive" (
  set "SKIP_ARCHIVE=-SkipArchive"
  goto :run
)
echo [ERROR] Invalid option: %ARCHIVE_OPTION%
echo Usage: build_portable.bat [all^|cpu^|gpu] [version] [--skip-archive]
goto :failed

:run
if not exist "%PROJECT_ROOT%build_portable.ps1" (
  echo [ERROR] Missing build_portable.ps1
  goto :failed
)

echo ================================================================
echo Building YouTube Workflow Portable
echo Edition: %EDITION%
echo Version: %VERSION%
echo Output:  %PROJECT_ROOT%dist
echo ================================================================

powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%PROJECT_ROOT%build_portable.ps1" -Edition "%EDITION%" -Version "%VERSION%" %SKIP_ARCHIVE%
set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%"=="0" goto :build_failed

echo.
echo [DONE] Portable package is ready in:
echo %PROJECT_ROOT%dist
goto :finish

:bad_root
echo [ERROR] Cannot enter project directory: %PROJECT_ROOT%
goto :failed

:build_failed
echo.
echo [ERROR] Portable build failed with exit code %EXIT_CODE%.
goto :failed_with_code

:failed
set "EXIT_CODE=1"

:failed_with_code
echo.
pause
endlocal & exit /b %EXIT_CODE%

:finish
echo.
pause
endlocal & exit /b 0

:usage
echo Build Windows Portable packages into the dist directory.
echo.
echo Usage: build_portable.bat [all^|cpu^|gpu] [version] [--skip-archive]
echo.
echo Examples:
echo   build_portable.bat
echo   build_portable.bat cpu 0.4.0
echo   build_portable.bat all 0.4.0 --skip-archive
endlocal & exit /b 0
