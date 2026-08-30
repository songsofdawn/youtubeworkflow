@echo off
rem Resolve one Python executable for both source development and Portable builds.
if not defined PROJECT_ROOT set "PROJECT_ROOT=%~dp0"
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

set "PYTHON_EXE="
if exist "%PROJECT_ROOT%runtime\python\python.exe" set "PYTHON_EXE=%PROJECT_ROOT%runtime\python\python.exe"
if not defined PYTHON_EXE if exist "%PROJECT_ROOT%.venv\Scripts\python.exe" set "PYTHON_EXE=%PROJECT_ROOT%.venv\Scripts\python.exe"
rem Keep the historical environment as a silent fallback for existing installations.
if not defined PYTHON_EXE if exist "%PROJECT_ROOT%.venv_stage3\Scripts\python.exe" set "PYTHON_EXE=%PROJECT_ROOT%.venv_stage3\Scripts\python.exe"

if defined PYTHON_EXE exit /b 0
echo [ERROR] Python runtime not found.
echo [ERROR] Portable package: runtime\python\python.exe
echo [ERROR] Source installation: .venv\Scripts\python.exe
exit /b 1
