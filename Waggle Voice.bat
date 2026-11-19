@echo off
title Waggle Voice  -  say "waggle", or hold Ctrl+B to talk
set HERMES_HOME=%USERPROFILE%\.hermes
rem Hermes's own ffmpeg (speech needs it) and Ollama (the local brain)
set PATH=%HERMES_HOME%\tools\ffmpeg-9.0.1-win32-x64\bin;%PATH%
curl -s http://127.0.0.1:11434/api/version >nul 2>&1 || start "" "%LOCALAPPDATA%\Programs\Ollama\ollama app.exe"
echo.
echo   Waggle voice is listening.
echo   - Say "waggle", then your question. It answers out loud.
echo   - Or press Ctrl+B to talk, and Ctrl+B again to stop.
echo   - Type /wake off to stop listening, /exit to close.
echo.
"%HERMES_HOME%\bin\hermes.exe" chat
