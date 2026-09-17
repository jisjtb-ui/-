@echo off
chcp 65001 >nul
cd /d "%~dp0"
python tools\sns_setup.py
echo.
pause
