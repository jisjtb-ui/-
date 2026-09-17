# TikTokの下書き転送を毎日1回自動実行するタスクを登録する。
#
# 実行方法（PowerShellで）:
#   powershell -ExecutionPolicy Bypass -File scripts\register_daily_task.ps1
# 時刻を変える場合:
#   powershell -ExecutionPolicy Bypass -File scripts\register_daily_task.ps1 -Time "09:00"

param(
    [string]$Time = "21:00",
    [string]$TaskName = "HonneTest-TikTokDrafts"
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$batch = Join-Path $PSScriptRoot "tiktok_drafts_task.cmd"

if (-not (Test-Path $batch)) {
    Write-Host "[エラー] $batch が見つかりません" -ForegroundColor Red
    exit 1
}

try {
    $action = New-ScheduledTaskAction -Execute $batch -WorkingDirectory $root
    $trigger = New-ScheduledTaskTrigger -Daily -At $Time
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopIfGoingOnBatteries `
        -AllowStartIfOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 1)

    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
        -Settings $settings -Description "本音心理テスト: TikTokへ1日分の下書きを転送する" -Force | Out-Null
}
catch {
    Write-Host "[エラー] タスクを登録できませんでした" -ForegroundColor Red
    Write-Host $_.Exception.Message
    Write-Host ""
    Write-Host "次のコマンドでも登録できます（コマンドプロンプトで実行）:"
    Write-Host "  schtasks /Create /TN $TaskName /TR `"$batch`" /SC DAILY /ST $Time /F"
    exit 1
}

Write-Host "登録しました: $TaskName（毎日 $Time）" -ForegroundColor Green
Write-Host ""
Write-Host "確認   : Get-ScheduledTask -TaskName $TaskName"
Write-Host "今すぐ : Start-ScheduledTask -TaskName $TaskName"
Write-Host "解除   : Unregister-ScheduledTask -TaskName $TaskName -Confirm:`$false"
Write-Host ""
Write-Host "PCが起動していなかった日の分は、次に起動したときに自動で実行されます。"
