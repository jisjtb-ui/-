@echo off
chcp 65001 >nul
cd /d "%~dp0"
python autopost.py cloud status
echo.
python autopost.py cloud sync
echo.
pause
