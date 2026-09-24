@echo off
chcp 65001 >nul
cd /d "%~dp0"
python autopost.py cloud health
echo.
python autopost.py cloud push
echo.
pause
