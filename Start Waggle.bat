@echo off
title Waggle
set HERMES_HOME=%USERPROFILE%\.hermes
rem Make sure Ollama is running (starts the tray app if it isn't)
curl -s http://localhost:11434/api/tags >nul 2>&1 || start "" "%LOCALAPPDATA%\Programs\Ollama\ollama app.exe"
"%HERMES_HOME%\bin\hermes.exe" %*
