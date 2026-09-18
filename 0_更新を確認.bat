@echo off
cd /d "%~dp0"
python autopost.py update --apply
echo.
pause
