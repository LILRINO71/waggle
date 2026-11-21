@echo off
title Waggle Google sign-in
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "tools\google-login.ps1"
pause
