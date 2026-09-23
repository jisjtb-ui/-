@echo off
chcp 65001 >nul
cd /d "%~dp0.."
rem 1) top up  2) pull phone-published items  3) publish due posts
rem 4) metrics  5) update category weights  6) refresh phone page
python autopost.py sns topup >> autopost.log 2>&1
python autopost.py mobile --sync >> autopost.log 2>&1
python autopost.py sns run >> autopost.log 2>&1
python autopost.py experiment collect --due >> autopost.log 2>&1
python autopost.py weights update >> autopost.log 2>&1
python autopost.py mobile --deploy >> autopost.log 2>&1
exit /b %errorlevel%
