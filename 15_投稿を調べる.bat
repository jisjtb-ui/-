@echo off
chcp 65001 >nul
cd /d "%~dp0"
python autopost.py probe --platform instagram --count 3
echo.
python autopost.py probe --platform threads --count 1
echo.
pause
