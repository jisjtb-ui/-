"""コンテンツ生成 → 実験登録 → 配信用の書き出し を一度に行う。

すでに作ったものがある場合は、作り直すか足すかを聞く。
仕様が変わった後（占いの追加など）は作り直さないと古い形のまま残るため。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from autopost.config import Settings                       # noqa: E402
from autopost.experiments import DELIVERED, ExperimentStore  # noqa: E402

DEFAULT_POSTS = 100
LINE = "=" * 52


def ask(prompt: str, default: str = "") -> str:
    try:
        return input(prompt).strip() or default
    except (EOFError, KeyboardInterrupt):
        return default


def run(step: str, args: list[str]) -> bool:
    print(f"\n{step}\n{'-' * 52}")
    return subprocess.run([sys.executable, *args], cwd=ROOT).returncode == 0


def main() -> int:
    settings = Settings.load()
    posts = DEFAULT_POSTS
    base_url = (settings.local_host_base_url or "").rstrip("/")

    if len(sys.argv) > 1:
        try:
            posts = int(sys.argv[1])
        except ValueError:
            pass
    if len(sys.argv) > 2:
        base_url = sys.argv[2].rstrip("/")

    if not base_url or "example.com" in base_url:
        print("[エラー] 画像の公開先が設定されていません。")
        print("先に 12_画像を公開する.bat を実行してください（.env を自動で整えます）。")
        return 1

    store = ExperimentStore(settings.experiments_db_path)
    counts = store.status_counts()
    pending = sum(n for st, n in counts.items() if st not in DELIVERED)

    print(f"{LINE}\n セットアップ\n{LINE}")
    print(f" 公開先   : {base_url}")

    # 仕様が変わると古いものは作り直さないと直らない
    if pending:
        print(f"\n すでに未配信の投稿が {pending}件あります。")
        print("   1) 作り直す … 古いものを捨てて、いまの形で作り直す")
        print("   2) 足す     … 残したまま新しく追加する")
        print("   3) やめる")
        choice = ask("\n どうしますか？ [1/2/3]: ", "1")
        if choice == "3":
            print("やめました")
            return 0
        if choice == "1":
            if not run("[0/3] 古い投稿を捨てています",
                       ["autopost.py", "experiment", "discard", "--yes"]):
                return 1
            shutil.rmtree(ROOT / "output", ignore_errors=True)
            (ROOT / "history.json").unlink(missing_ok=True)
            print("  生成済みの画像と履歴も消しました")

    count = ask(f"\n 何投稿つくりますか？ [{posts}]: ", str(posts))
    try:
        posts = max(1, int(count))
    except ValueError:
        pass

    steps = (
        (f"[1/3] {posts}投稿ぶんの画像を作っています", ["generate.py", "--posts", str(posts)]),
        ("[2/3] 実験として登録し、公開URLを紐付けています",
         ["autopost.py", "sns", "enqueue", "--folder", "output", "--base-url", base_url]),
        ("[3/3] 配信用に書き出しています",
         ["autopost.py", "tiktok", "export-media", "--dest", "pages_media"]),
    )
    for label, args in steps:
        if not run(label, args):
            print("\n[エラー] 途中で失敗しました。上のメッセージを確認してください。")
            return 1

    print(f"\n{LINE}\n 次にやること\n{LINE}")
    print("  1. 12_画像を公開する.bat   … 画像とスマホ用ページを公開")
    print("  2. 11_スマホ用ページを作る.bat … URLを確認してスマホで開く")
    print("  3. 6_SNSセットアップ.bat   … 自動投稿を使う場合")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
