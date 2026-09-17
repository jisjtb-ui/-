@echo off
chcp 65001 >nul
rem TikTokへ今日の分（既定5件）の下書きを送る。
rem ダブルクリックするか、タスクスケジューラから1日1回実行する。
setlocal
cd /d "%~dp0.."

if not exist ".env" (
    echo [エラー] .env がありません。SETUP_AUTOPOST.md を参照してください。
    pause
    exit /b 1
)

echo ========================================
echo  TikTok 下書き転送  %date% %time%
echo ========================================
python autopost.py tiktok drafts
set RESULT=%errorlevel%

echo.
python autopost.py tiktok queue

echo.
if %RESULT% neq 0 (
    echo [注意] エラーが発生しました。上のメッセージを確認してください。
) else (
    echo 完了しました。
)

rem タスクスケジューラから実行された場合は待たずに終了する
if "%1"=="--no-pause" exit /b %RESULT%
pause
exit /b %RESULT%
