"""画像とスマホ用ページを Cloudflare Pages へ公開する。

コマンドを覚えなくていいように、必要な手順をまとめて実行する。

  1. 実験に登録済みの画像を pages_media へ書き出す
  2. スマホ用ページとコールバック用ページを作る
  3. Cloudflare Pages へ公開する
  4. 本当に公開されたか確認する

初回は Cloudflare のログイン画面がブラウザで開く。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from autopost.config import Settings          # noqa: E402
from autopost import mobile                   # noqa: E402

LINE = "=" * 52
DEFAULT_PROJECT = "honeshinri-media"
MEDIA_DIR = "pages_media"


def ask(prompt: str, default: str = "") -> str:
    try:
        answer = input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        return default
    return answer or default


def has_node() -> bool:
    return bool(shutil.which("npx") or shutil.which("npx.cmd"))


def run_step(title: str, args: list[str]) -> bool:
    print(f"\n{title}\n{'-' * 52}")
    return subprocess.run([sys.executable, *args], cwd=ROOT).returncode == 0


def main() -> int:
    settings = Settings.load()
    print(f"{LINE}\n 画像とスマホ用ページを公開する\n{LINE}")

    if not has_node():
        print("\n[エラー] Node.js が見つかりません。")
        print("公開には Node.js が必要です。次からインストールしてください:")
        print("  https://nodejs.org/  （LTS版をダウンロードして実行するだけです）")
        print("\nインストール後、PCを再起動してからもう一度このボタンを押してください。")
        return 1

    # 1. 画像の書き出し
    if not run_step("[1/4] 画像を書き出しています",
                    ["autopost.py", "tiktok", "export-media", "--dest", MEDIA_DIR]):
        print("\n[エラー] 画像の書き出しに失敗しました")
        return 1

    # 2. ページの生成
    print(f"\n[2/4] ページを作っています\n{'-' * 52}")
    result = mobile.build(settings, Path(MEDIA_DIR))

    # 3. 公開
    command = settings.pages_deploy_command
    if not command:
        print(f"\n[3/4] 公開\n{'-' * 52}")
        print("Cloudflare Pages のプロジェクト名を入力してください。")
        print("（Cloudflareの画面 → Workers & Pages に出ている名前です）")
        project = ask(f"プロジェクト名 [{DEFAULT_PROJECT}]: ", DEFAULT_PROJECT)
        command = f"npx wrangler pages deploy {MEDIA_DIR} --project-name {project}"
        print(f"\n次回から自動にするには、.env に次の行を足してください:")
        print(f"  PAGES_DEPLOY_COMMAND={command}")
    else:
        print(f"\n[3/4] 公開\n{'-' * 52}")

    print(f"\n実行: {command}")
    print("※ 初回は Cloudflare のログイン画面がブラウザで開きます\n")
    if subprocess.run(command, shell=True, cwd=ROOT).returncode != 0:
        print("\n[エラー] 公開に失敗しました。")
        print("よくある原因:")
        print("  ・プロジェクト名が違う（Cloudflareの画面で確認してください）")
        print("  ・Cloudflareにログインしていない（画面の指示に従ってください）")
        return 1

    # 4. 確認
    print(f"\n[4/4] 確認しています\n{'-' * 52}")
    if result["url"].startswith("http"):
        if mobile.verify_published(result["url"]):
            print(f"\n{LINE}")
            print(" 公開できました")
            print(LINE)
            print(f"\nスマホでこのURLを開いてください:\n\n  {result['url']}\n")
            print("画像10枚をまとめて写真アプリに保存できます。")
            print("（このURLは人に教えないでください）")
            return 0
        print("\n公開は終わりましたが、まだ反映されていないようです。")
        print("1〜2分待ってから、もう一度このボタンを押してください。")
        return 0

    print("\n.env の LOCAL_HOST_BASE_URL が未設定のため、URLを組み立てられません。")
    print("次の行を .env に足してください:")
    print("  LOCAL_HOST_BASE_URL=https://（あなたのPagesのURL）")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
