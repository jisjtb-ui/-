@echo off
chcp 65001 >nul
cd /d "%~dp0"
python autopost.py sns queue
echo.
python autopost.py status
echo.
pause
