@echo off
setlocal
title Lark Mail Sync - Configure
cd /d "%~dp0"

if not exist ".env" (
  echo Creating .env from .env.example ...
  copy /y ".env.example" ".env" >nul
)

echo Opening .env in Notepad ...
echo.
echo   Fill in the credentials sent separately by the admin,
echo   especially: LARK_APP_ID / LARK_APP_SECRET / MAIL_USER_MAILBOX_ID
echo               BITABLE_APP_TOKEN / BITABLE_TABLE_ID
echo   then SAVE and close Notepad.
echo.
start "" notepad.exe ".env"
endlocal
