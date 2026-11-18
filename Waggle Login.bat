@echo off
title Waggle login
cd /d "%~dp0"
python -m waggle.cli login
pause
