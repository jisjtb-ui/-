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
powershell -ExecutionPolicy Bypass -File "%~dp0scripts\register_daily_task.ps1" -Time "%TIME_INPUT%"

echo.
echo 解除したいときは、このファイルをもう一度開かずに
echo タスクスケジューラで HonneTest-TikTokDrafts を削除してください。
pause
