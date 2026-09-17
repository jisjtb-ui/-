@echo off
chcp 65001 >nul
rem ============================================================
rem  毎日決まった時刻に自動で下書きを送る設定をするボタン
rem  1回押せば、あとは放置で構いません
rem ============================================================
cd /d "%~dp0"

echo ========================================
echo  毎日の自動送信を登録します
echo ========================================
echo.
set /p TIME_INPUT=送信する時刻を入力してください（例 21:00）[既定 21:00]:
if "%TIME_INPUT%"=="" set TIME_INPUT=21:00

echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\register_daily_task.ps1" -Time "%TIME_INPUT%"
set PSRESULT=%errorlevel%

if %PSRESULT% neq 0 (
    echo.
    echo PowerShellでの登録に失敗したため、標準の方法で登録します...
    schtasks /Create /TN "HonneTest-TikTokDrafts" /TR "\"%~dp0scripts\tiktok_drafts_task.cmd\"" /SC DAILY /ST %TIME_INPUT% /F
    if errorlevel 1 (
        echo.
        echo [エラー] 自動登録できませんでした。
        echo タスクスケジューラを手動で開き、次を1日1回実行するよう登録してください:
        echo   %~dp0scripts\tiktok_drafts_task.cmd
    ) else (
        echo 登録しました（毎日 %TIME_INPUT%）
    )
)

echo.
echo 解除したいときは、タスクスケジューラで HonneTest-TikTokDrafts を削除してください。
pause
