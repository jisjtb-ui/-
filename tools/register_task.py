"""TikTokの下書き転送を毎日自動実行するタスクを登録する。

cmd.exe は .bat を端末のコードページで読むため、日本語を含む .bat は
文字化けや構文エラーを起こしやすい。日本語の案内はすべてPython側で表示する。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TASK_NAME = "HonneTest-TikTokDrafts"
LAUNCHER = ROOT / "scripts" / "tiktok_drafts_task.cmd"


def ask_time(default: str = "21:00") -> str:
    raw = input(f"送信する時刻を入力してください（例 21:00）[既定 {default}]: ").strip()
    if not raw:
        return default
    parts = raw.replace("：", ":").split(":")
    try:
        hour, minute = int(parts[0]), int(parts[1]) if len(parts) > 1 else 0
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError
    except (ValueError, IndexError):
        print(f"  時刻の形式が正しくないため、{default} を使います")
        return default
    return f"{hour:02d}:{minute:02d}"


def register(when: str) -> bool:
    """schtasks で登録する（Windows標準。PowerShellの実行ポリシーに影響されない）。"""
    if not LAUNCHER.is_file():
        print(f"[エラー] 起動ファイルが見つかりません: {LAUNCHER}")
        return False

    command = [
        "schtasks", "/Create",
        "/TN", TASK_NAME,
        "/TR", f'"{LAUNCHER}"',
        "/SC", "DAILY",
        "/ST", when,
        "/F",
    ]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode == 0:
        print(f"\n登録しました: {TASK_NAME}（毎日 {when}）")
        return True

    print("\n[エラー] タスクを登録できませんでした")
    for line in (result.stdout + result.stderr).splitlines():
        if line.strip():
            print("  " + line.strip())
    print("\n手動で登録する場合:")
    print("  1. Windowsキー → 「タスクスケジューラ」を開く")
    print("  2. 「基本タスクの作成」→ 毎日 → 開始時刻を指定")
    print("  3. 操作は「プログラムの開始」を選び、次を指定する")
    print(f"     {LAUNCHER}")
    return False


def main() -> int:
    print("=" * 46)
    print(" 毎日の自動送信を登録します")
    print("=" * 46)
    print("\nPCが起動していなかった日の分は、次に起動したときに実行されます。\n")

    when = ask_time()
    ok = register(when)

    print("\n確認・操作のコマンド:")
    print(f"  今すぐ実行 : schtasks /Run /TN {TASK_NAME}")
    print(f"  登録の確認 : schtasks /Query /TN {TASK_NAME}")
    print(f"  解除       : schtasks /Delete /TN {TASK_NAME} /F")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
