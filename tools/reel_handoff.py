"""Reelの書き出しと受け渡しを、質問に答えるだけで進める。

  1. 予約済みのReelを書き出す
  2. 置き場所を表示する
  3. 投稿し終えたものがあれば、その場で記録する
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from autopost.config import Settings                      # noqa: E402
from autopost.experiments import ExperimentStore, MANUAL_REQUIRED  # noqa: E402
from autopost.oauth.store import TokenStore               # noqa: E402
from autopost.publishers.reel import PLATFORM, ReelPublisher  # noqa: E402

LINE = "=" * 52


def ask(prompt: str) -> str:
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        return ""


def main() -> int:
    settings = Settings.load()
    store = ExperimentStore(settings.experiments_db_path)
    publisher = ReelPublisher(settings, TokenStore(settings.token_dir))

    print(f"{LINE}\n Instagram Reel の書き出し\n{LINE}")
    print("Reelは自動投稿しません。音源をご自分で付けるためです。")
    print("ここでは動画を作り、投稿し終えたものを記録します。\n")

    try:
        publisher.preflight()
    except Exception as exc:
        print(f"[エラー] {exc}")
        print("\n次を実行してから、もう一度お試しください:")
        print("  pip install imageio-ffmpeg")
        return 1

    # 1. 未書き出しのものを作る
    waiting = [p for p in store.publications(platform=PLATFORM)
               if p.status == MANUAL_REQUIRED]
    if not waiting:
        print("手渡し待ちのReelはありません。")
        print("先に予約してください:")
        print("  7_SNS予約を実行.bat  （--platforms instagram_reel で予約した分が対象）")
        return 0

    print(f"手渡し待ち {len(waiting)}件\n")
    for publication in waiting:
        experiment = store.get(publication.experiment_id)
        if experiment is None:
            continue
        folder = publisher.output_dir(experiment.source_post_id)
        if (folder / "reel.mp4").is_file():
            print(f"  {publication.experiment_id}  {experiment.source_post_id}  書き出し済み")
            continue
        print(f"  {publication.experiment_id}  {experiment.source_post_id}  作成中…")
        try:
            publisher.build(experiment, log=lambda m: print(f"    {m}"))
        except Exception as exc:
            print(f"    [失敗] {exc}")

    base = publisher.output_dir("").parent
    print(f"\n置き場所: {base}")
    print("この中の reel.mp4 をスマホへ移し、Instagramアプリで")
    print("リールとして開いて音源を付け、caption.txt の文章を貼って投稿してください。\n")

    # 2. 投稿済みの記録
    print(LINE)
    answer = ask("投稿し終えたものはありますか？ [y/N] ").lower()
    if answer not in ("y", "yes"):
        print("\n終わります。投稿後にまたこのボタンを押してください。")
        return 0

    while True:
        experiment_id = ask("\n実験ID（空で終了）: ")
        if not experiment_id:
            break
        url = ask("投稿のURL（任意）: ")
        media_id = ask("メディアID（反応データを集めるなら必要 / 任意）: ")
        args = [sys.executable, "autopost.py", "reel", "posted", experiment_id]
        if url:
            args += ["--url", url]
        if media_id:
            args += ["--media-id", media_id]
        subprocess.run(args, cwd=ROOT)

    print("\n完了しました。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
