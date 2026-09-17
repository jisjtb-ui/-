@echo off
chcp 65001 >nul
cd /d "%~dp0"
python autopost.py tiktok drafts
echo.
python autopost.py tiktok queue
echo.
pause
