@echo off
chcp 65001 >nul
cd /d "%~dp0.."
python autopost.py sns run >> autopost.log 2>&1
python autopost.py experiment collect --due >> autopost.log 2>&1
exit /b %errorlevel%
