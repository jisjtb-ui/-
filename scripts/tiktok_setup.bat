@echo off
chcp 65001 >nul
rem 100投稿の生成 → 実験登録 → Pages配信用の書き出し までを一度に行う。
setlocal
cd /d "%~dp0.."

set POSTS=%1
if "%POSTS%"=="" set POSTS=100

set BASEURL=%2
if "%BASEURL%"=="" set BASEURL=https://honeshinri-media.pages.dev

echo ========================================
echo  セットアップ: %POSTS% 投稿
echo  公開先: %BASEURL%
echo ========================================

echo.
echo [1/3] コンテンツと画像を生成します...
python generate.py --posts %POSTS%
if %errorlevel% neq 0 goto :failed

echo.
echo [2/3] 実験として登録し、公開URLを紐付けます...
python autopost.py tiktok enqueue --folder output --base-url %BASEURL%
if %errorlevel% neq 0 goto :failed

echo.
echo [3/3] Cloudflare Pages へ配信する形で画像を書き出します...
python autopost.py tiktok export-media --dest pages_media
if %errorlevel% neq 0 goto :failed

echo.
echo ========================================
echo  次にやること
echo ========================================
echo  1. 画像を公開する:
echo       npx wrangler pages deploy pages_media --project-name honeshinri-media
echo  2. TikTokへ接続する（初回のみ）:
echo       python autopost.py connect tiktok
echo  3. 毎日の送信を登録する:
echo       powershell -ExecutionPolicy Bypass -File scripts\register_daily_task.ps1
pause
exit /b 0

:failed
echo.
echo [エラー] 途中で失敗しました。上のメッセージを確認してください。
pause
exit /b 1
