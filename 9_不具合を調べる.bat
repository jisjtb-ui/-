@echo off
cd /d "%~dp0"
python autopost.py doctor %*
echo.
pause
