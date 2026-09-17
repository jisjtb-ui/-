"""Threads / Instagram のセットアップから予約投稿までを一括で行うウィザード。

  .env確認 → 接続 → コンテンツ生成 → 実験登録 → 画像書き出し
  → 予約割り当て → 毎日の自動実行タスク登録

途中で止めても、もう一度実行すれば続きから進められる。
"""

from __future__ import annotations

import subprocess
import sys
from datetime import datetime, time as dtime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from autopost.config import Settings                       # noqa: E402
from autopost.experiments import ExperimentStore           # noqa: E402
from autopost.oauth.store import TokenStore                # noqa: E402
from autopost.queueing import enqueue_folder, overview, schedule_publications  # noqa: E402

PLATFORMS = ["threads", "instagram"]
LINE = "=" * 52
TASK_NAME = "HonneTest-SNSPublish"
LAUNCHER = ROOT / "scripts" / "sns_publish_task.cmd"


def ask(question: str, default: str = "") -> str:
    suffix = f"[{default}]" if default else ""
    answer = input(f"{question} {suffix}: ").strip()
    return answer or default


def ask_yes(question: str, default: bool = True) -> bool:
    mark = "Y/n" if default else "y/N"
    answer = input(f"{question} ({mark}): ").strip().lower()
    if not answer:
        return default
    return answer in ("y", "yes", "はい")


def run_cli(args: list[str]) -> int:
    return subprocess.run([sys.executable, "autopost.py", *args], cwd=ROOT).returncode


# ----------------------------------------------------------------------
def step_env(settings: Settings) -> bool:
    """必要な設定がそろっているか確認する。"""
    print(f"\n{LINE}\n [1/6] 設定の確認\n{LINE}")
    if not (ROOT / ".env").is_file():
        print("  .env がありません。.env.example をコピーして作成してください。")
        return False

    ready = True
    for platform in PLATFORMS:
        missing = settings.missing(platform)
        if missing:
            ready = False
            print(f"  {platform:<10} 未設定 → .env に {', '.join(missing)} を記入してください")
        else:
            print(f"  {platform:<10} 設定OK")
    if not ready:
        print("\n  設定方法は SETUP_AUTOPOST.md の B1（Threads）と C（Instagram）を参照してください。")
    return ready


def step_connect(settings: Settings) -> list[str]:
    """未接続のプラットフォームをつなぐ。"""
    print(f"\n{LINE}\n [2/6] アカウント接続\n{LINE}")
    store = TokenStore(settings.token_dir)
    connected = []

    for platform in PLATFORMS:
        token = store.load(platform)
        if token and not token.is_expired():
            name = token.account_name or token.account_id or "接続済み"
            print(f"  {platform:<10} 接続済み（{name}）")
            connected.append(platform)
            continue

        print(f"  {platform:<10} 未接続")
        if not ask_yes(f"    いま {platform} に接続しますか？"):
            continue
        print("    ブラウザが開きます。許可後、表示されたURLを貼り付けてください。")
        if run_cli(["connect", platform, "--manual"]) == 0:
            connected.append(platform)
        else:
            print(f"    {platform} の接続に失敗しました。あとで再実行できます。")

    return connected


def step_generate() -> bool:
    """コンテンツと画像を生成する。"""
    print(f"\n{LINE}\n [3/6] コンテンツ生成\n{LINE}")
    existing = len(list((ROOT / "output").glob("post_*"))) if (ROOT / "output").is_dir() else 0
    if existing:
        print(f"  すでに {existing} 投稿が output にあります")
        if not ask_yes("  追加で生成しますか？", default=False):
            return True

    count = ask("  生成する投稿数", "30")
    try:
        number = max(1, int(count))
    except ValueError:
        number = 30
    return subprocess.run(
        [sys.executable, "generate.py", "--posts", str(number)], cwd=ROOT
    ).returncode == 0


def step_enqueue(settings: Settings, platforms: list[str]) -> bool:
    """実験として登録し、画像の公開URLを紐付ける。"""
    print(f"\n{LINE}\n [4/6] 実験として登録\n{LINE}")
    base_url = ask("  画像の公開URLの先頭", "https://honeshinri-media.pages.dev")
    store = ExperimentStore(settings.experiments_db_path)
    report = enqueue_folder(
        settings, store, ROOT / "output", platforms or PLATFORMS, base_url=base_url, log=print
    )
    print(f"  {report.summary()}")

    print("\n  Cloudflare Pages へ配信する形で画像を書き出します...")
    if run_cli(["tiktok", "export-media", "--dest", "pages_media"]) != 0:
        return False
    print("\n  次のコマンドで画像を公開してください（未実施だと投稿が失敗します）:")
    print("    npx wrangler pages deploy pages_media --project-name honeshinri-media")
    input("\n  デプロイが終わったら Enter を押してください...")
    return True


def step_schedule(settings: Settings, platforms: list[str]) -> bool:
    """予約時刻を割り当てる。"""
    print(f"\n{LINE}\n [5/6] 予約の割り当て\n{LINE}")
    print(f"  1日の上限: Threads {settings.threads_daily_limit}件"
          f" / Instagram {settings.instagram_daily_limit}件")

    start_raw = ask("  開始日 (YYYY-MM-DD)", (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d"))
    times_raw = ask("  投稿時刻（カンマ区切り。例 09:00,21:00）", "21:00")
    try:
        start = datetime.strptime(start_raw, "%Y-%m-%d")
        times = []
        for raw in times_raw.split(","):
            hour, _, minute = raw.strip().partition(":")
            times.append(dtime(int(hour), int(minute or 0)))
    except ValueError:
        print("  形式が正しくないため、明日21:00から1日1件で設定します")
        start = datetime.now() + timedelta(days=1)
        times = [dtime(21, 0)]

    store = ExperimentStore(settings.experiments_db_path)
    result = schedule_publications(
        settings, store, platforms or PLATFORMS, start, times, log=print
    )
    print(f"  {result['assigned']} 件に予約時刻を割り当てました")

    for platform, info in overview(store, settings, platforms or PLATFORMS).items():
        when = info["next_at"][:16].replace("T", " ") if info["next_at"] else "未設定"
        print(f"    {platform:<10} 待ち{info['queued']}件 / 次回 {when} / 約{info['days_needed']}日で完了")
    return True


def step_task() -> bool:
    """毎日の自動実行を登録する。"""
    print(f"\n{LINE}\n [6/6] 毎日の自動実行\n{LINE}")
    if not ask_yes("  毎日決まった時刻に自動配信しますか？"):
        print("  あとで『7_SNS予約を実行.bat』を手動で押しても配信できます")
        return True
    if not LAUNCHER.is_file():
        print(f"  [エラー] {LAUNCHER} が見つかりません")
        return False

    when = ask("  チェックする時刻（この時刻に予約分をまとめて配信）", "21:05")
    command = [
        "schtasks", "/Create", "/TN", TASK_NAME,
        "/TR", f'"{LAUNCHER}"', "/SC", "DAILY", "/ST", when, "/F",
    ]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode == 0:
        print(f"  登録しました: {TASK_NAME}（毎日 {when}）")
        print(f"  解除: schtasks /Delete /TN {TASK_NAME} /F")
        return True

    print("  [エラー] タスクを登録できませんでした")
    for line in (result.stdout + result.stderr).splitlines():
        if line.strip():
            print("    " + line.strip())
    print(f"\n  手動で登録する場合はタスクスケジューラで次を実行するよう設定してください:")
    print(f"    {LAUNCHER}")
    return False


# ----------------------------------------------------------------------
def main() -> int:
    print(LINE)
    print(" Threads / Instagram セットアップ")
    print(LINE)
    print(" 設定の確認 → 接続 → 生成 → 登録 → 予約 → 自動実行")

    settings = Settings.load()

    if not step_env(settings):
        print("\n.env を設定してから、もう一度実行してください。")
        return 1

    connected = step_connect(settings)
    if not connected:
        print("\n接続できたプラットフォームがありません。")
        print("SETUP_AUTOPOST.md の手順で認証を済ませてから再実行してください。")
        return 1

    if not step_generate():
        print("\nコンテンツ生成に失敗しました。")
        return 1
    if not step_enqueue(settings, connected):
        return 1
    if not step_schedule(settings, connected):
        return 1
    step_task()

    print(f"\n{LINE}\n 完了\n{LINE}")
    print(" 状況の確認 : 8_SNS状況を確認.bat")
    print(" 今すぐ配信 : 7_SNS予約を実行.bat")
    print(" 反応データ : 5_反応データを集める.bat")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
