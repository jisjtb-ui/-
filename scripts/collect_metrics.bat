@echo off
chcp 65001 >nul
rem 取得時期（1h/6h/24h/72h）が来た反応データだけを集める。
setlocal
cd /d "%~dp0.."
echo ========================================
echo  反応データの取得  %date% %time%
echo ========================================
python autopost.py experiment collect --due
if "%1"=="--no-pause" exit /b %errorlevel%
echo.
pause
