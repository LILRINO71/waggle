@echo off
title Waggle phone setup
cd /d "%~dp0"
set HERMES_HOME=%USERPROFILE%\.hermes
set H="%HERMES_HOME%\bin\hermes.exe"
echo.
echo  Step 1 of 3: connect Telegram
echo  -----------------------------
echo  Choose 21 (Telegram), then 1 (Automatic). Scan the QR code, tap Create Bot, then go back to
echo  @NousHostedHermesBot and tap "Connect this bot". Allowed users: ONLY your own Telegram ID.
echo  Choose 27 (Done) when the menu comes back.
echo.
%H% gateway setup
echo.
echo  Step 2 of 3: proactive check-ins (hourly) and digest (8am + 5pm)
python install.py --cron
echo.
echo  Step 3 of 3: start the gateway now and on every login (no admin needed)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install-startup.ps1"
wscript "%~dp0Waggle Gateway.vbs"
timeout /t 15 /nobreak >nul
%H% send -t telegram "Waggle is connected. Talk to me here any time."
echo.
echo  Done. Check Telegram for a message from your bot.
pause
