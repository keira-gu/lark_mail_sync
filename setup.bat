@echo off
setlocal
chcp 65001 >nul
title Lark Mail Sync - Setup
cd /d "%~dp0"

echo ============================================================
echo   Lark Mail Sync - one-time setup
echo   Target folder: %CD%
echo ============================================================
echo.

rem ---------- 1. Python ----------
set "PYCMD="
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)" >nul 2>nul
if not errorlevel 1 set "PYCMD=py -3"
if not defined PYCMD (
  python -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)" >nul 2>nul
  if not errorlevel 1 set "PYCMD=python"
)
if not defined PYCMD (
  echo  [ERROR] Python 3.9+ was not found on this computer.
  echo          Please install Python from https://www.python.org/downloads/
  echo          IMPORTANT: tick "Add python.exe to PATH" during install,
  echo          then run this file again.
  echo.
  pause
  exit /b 1
)
for /f "delims=" %%v in ('%PYCMD% --version 2^>^&1') do echo  [OK] %%v found

rem ---------- 2. venv ----------
if exist "python_venv\Scripts\python.exe" (
  echo  [OK] python_venv already exists, reusing it.
) else (
  echo  Creating virtual environment python_venv ...
  %PYCMD% -m venv python_venv
  if errorlevel 1 (
    echo  [ERROR] Failed to create python_venv. See message above.
    pause
    exit /b 1
  )
)

rem ---------- 3. dependencies ----------
echo.
echo  Installing dependencies (requests + pypdf). May take 1-3 minutes ...
echo.
"python_venv\Scripts\python" -m pip install -q -r requirements-dev.txt
if errorlevel 1 (
  echo  [WARN] Default PyPI failed, retrying with Tsinghua mirror ...
  "python_venv\Scripts\python" -m pip install -q -r requirements-dev.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
  if errorlevel 1 (
    echo  [ERROR] Dependency install failed. Check network / proxy and retry.
    pause
    exit /b 1
  )
)
echo  [OK] Dependencies installed.

rem ---------- 4. offline self-test (no network needed) ----------
echo.
echo  Running offline self-test ...
set "PYTHONPATH=%~dp0."
"python_venv\Scripts\python" -m pytest tests -q
if errorlevel 1 (
  echo  [ERROR] Self-test FAILED. Do not use the tool; contact the admin.
  pause
  exit /b 1
)

echo.
echo ============================================================
echo   Setup finished successfully.
echo.
echo   NEXT STEPS:
echo     1. double-click  config-env.bat   - fill in the credentials
echo        (sent separately by the admin), then save and close
echo     2. ask your AI assistant (with the Mail-Sync SKILL) to run:
echo        probe  -  rebuild-state  -  sync
echo ============================================================
echo.
pause
endlocal
