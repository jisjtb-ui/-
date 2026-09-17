"""コンテンツ生成 → 実験登録 → Pages配信用の書き出し を一度に行う。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_POSTS = 100
DEFAULT_BASE_URL = "https://honeshinri-media.pages.dev"


def run(step: str, args: list[str]) -> bool:
    print(f"\n{step}")
    print("-" * 46)
    result = subprocess.run([sys.executable, *args], cwd=ROOT)
    return result.returncode == 0


def main() -> int:
    posts = DEFAULT_POSTS
    base_url = DEFAULT_BASE_URL
    if len(sys.argv) > 1:
        try:
            posts = int(sys.argv[1])
        except ValueError:
            pass
    if len(sys.argv) > 2:
        base_url = sys.argv[2].rstrip("/")

    print("=" * 46)
    print(f" セットアップ: {posts} 投稿")
    print(f" 公開先: {base_url}")
    print("=" * 46)

    steps = (
        ("[1/3] コンテンツと画像を生成します...", ["generate.py", "--posts", str(posts)]),
        ("[2/3] 実験として登録し、公開URLを紐付けます...",
         ["autopost.py", "tiktok", "enqueue", "--folder", "output", "--base-url", base_url]),
        ("[3/3] Cloudflare Pages へ配信する形で書き出します...",
         ["autopost.py", "tiktok", "export-media", "--dest", "pages_media"]),
    )
    for label, args in steps:
        if not run(label, args):
            print("\n[エラー] 途中で失敗しました。上のメッセージを確認してください。")
            return 1

    print("\n" + "=" * 46)
    print(" 次にやること")
    print("=" * 46)
    print("  1. 画像を公開する:")
    print("       npx wrangler pages deploy pages_media --project-name honeshinri-media")
    print("  2. TikTokへ接続する（初回のみ）:")
    print("       python autopost.py connect tiktok")
    print("  3. 毎日の送信を登録する:")
    print("       3_毎日自動で送る.bat をダブルクリック")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
