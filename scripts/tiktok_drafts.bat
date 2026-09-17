@echo off
chcp 65001 >nul
cd /d "%~dp0.."
python autopost.py tiktok drafts
python autopost.py tiktok queue
if "%1"=="--no-pause" exit /b %errorlevel%
pause
