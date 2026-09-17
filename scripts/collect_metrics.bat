@echo off
chcp 65001 >nul
cd /d "%~dp0.."
python autopost.py experiment collect --due
if "%1"=="--no-pause" exit /b %errorlevel%
pause
