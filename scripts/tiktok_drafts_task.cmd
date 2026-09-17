@echo off
chcp 65001 >nul
cd /d "%~dp0.."
python autopost.py tiktok drafts >> autopost.log 2>&1
exit /b %errorlevel%
