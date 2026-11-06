@echo off
setlocal
chcp 65001 >nul
title Lark Mail Sync - Run one sync
cd /d "%~dp0"

if not exist ".env" (
  echo [ERROR] .env not found. Double-click config-env.bat first
  echo         and fill in the credentials from the admin.
  pause
  exit /b 1
)
if not exist "python_venv\Scripts\python.exe" (
  echo [ERROR] Dependencies not installed. Double-click setup.bat first.
  pause
  exit /b 1
)

set "PYTHONPATH=%~dp0."
"python_venv\Scripts\python" -m lark_mail_sync --env-file .env sync
set "RC=%ERRORLEVEL%"
echo.
if "%RC%"=="0" (
  echo ============ sync finished ============
) else (
  echo ============ sync FAILED, exit code %RC% ============
  echo Show this window to your AI assistant or the admin.
)
echo.
pause
endlocal
