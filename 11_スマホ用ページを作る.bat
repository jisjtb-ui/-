@echo off
cd /d "%~dp0"
python autopost.py mobile %*
echo.
pause
