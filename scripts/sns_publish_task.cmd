@echo off
chcp 65001 >nul
cd /d "%~dp0.."
rem 1) top up the queue when it runs low  2) publish due posts  3) collect metrics
python autopost.py sns topup >> autopost.log 2>&1
python autopost.py sns run >> autopost.log 2>&1
python autopost.py experiment collect --due >> autopost.log 2>&1
exit /b %errorlevel%
