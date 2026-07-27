@echo off
setlocal
cd /d "%~dp0"

set "PYTHON=.venv\Scripts\python.exe"
set "STAGE3_PYTHON=.venv_stage3\Scripts\python.exe"

if not exist "%PYTHON%" (
  echo [ERROR] Missing .venv. Run setup_stage1_fixed.bat first.
  exit /b 1
)

if not exist "%STAGE3_PYTHON%" (
  echo [ERROR] Missing .venv_stage3. Follow the stage 3 setup in README.md.
  exit /b 1
)

echo [1/6] Running the complete offline test suite...
"%PYTHON%" -m unittest discover -s tests -p "test_*.py"
if errorlevel 1 goto :failed

echo [2/6] Checking source syntax in the main environment...
"%PYTHON%" -m compileall -q src tests
if errorlevel 1 goto :failed

echo [3/6] Checking source syntax in the stage 3 environment...
"%STAGE3_PYTHON%" -m compileall -q src
if errorlevel 1 goto :failed

echo [4/6] Checking installed dependency consistency...
"%PYTHON%" -m pip check
if errorlevel 1 goto :failed
"%STAGE3_PYTHON%" -m pip check
if errorlevel 1 goto :failed

echo [5/6] Checking Git whitespace...
git diff --check
if errorlevel 1 goto :failed

echo [6/6] Checking that private and generated paths are not tracked...
for /f "delims=" %%F in ('git ls-files -- ".env" "models" "downloads" ".venv" ".venv_stage3" "private" "logs" "work"') do (
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
