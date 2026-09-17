@echo off
chcp 65001 >nul
rem タスクスケジューラから呼ばれる入口。画面を出さずに実行して終了する。
cd /d "%~dp0.."
call "%~dp0tiktok_drafts.bat" --no-pause
exit /b %errorlevel%
