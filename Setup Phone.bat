@echo off
title Waggle phone setup
cd /d "%~dp0"
set HERMES_HOME=%USERPROFILE%\.hermes
set H="%HERMES_HOME%\bin\hermes.exe"
echo.
echo  Step 1 of 3: connect Telegram
echo  -----------------------------
echo  In Telegram, message @BotFather, send /newbot, pick a name, and copy the token it gives you.
echo  Then choose Telegram below and paste the token. When it asks for allowed users, give YOUR
echo  Telegram user ID only (message @userinfobot to get it), so nobody else can talk to Waggle.
echo.
%H% gateway setup
echo.
echo  Step 2 of 3: proactive check-ins (hourly) and digest (8am + 5pm)
python install.py --cron
echo.
echo  Step 3 of 3: start the gateway now and on every login
%H% gateway install
%H% gateway start
%H% send -t telegram "Waggle is connected. Talk to me here any time."
echo.
echo  Done. Check Telegram for a message from your bot.
pause
