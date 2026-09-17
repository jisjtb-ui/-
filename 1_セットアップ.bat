@echo off
chcp 65001 >nul
cd /d "%~dp0"
python tools\setup_all.py %*
pause
