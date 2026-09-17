#!/usr/bin/env python3
"""投稿ステータス管理ツール（output → ready → posted）。

使い方:
    python manage.py ready post_001        # output/post_001 → ready/post_001
    python manage.py posted post_001       # ready/post_001（なければ output/）→ posted/
    python manage.py back post_001         # ひとつ前の段階へ戻す
    python manage.py list                  # 各フォルダの状況を一覧表示
    python manage.py ready --all           # output の投稿をまとめて ready へ
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STAGES = ("output", "ready", "posted")
# 移動元の探索順（posted へ送るときは ready を優先して探す）
SOURCES = {
    "ready": ("output",),
    "posted": ("ready", "output"),
}
BACK_TARGET = {"posted": "ready", "ready": "output"}


def stage_dir(stage: str) -> Path:
    path = ROOT / stage
    path.mkdir(parents=True, exist_ok=True)
    return path


def find_post(name: str, stages: tuple[str, ...]) -> Path | None:
    for stage in stages:
        candidate = stage_dir(stage) / name
        if candidate.is_dir():
            return candidate
    return None


def move_post(name: str, target: str, force: bool = False) -> bool:
    source = find_post(name, SOURCES[target])
    if source is None:
        where = " / ".join(SOURCES[target])
        print(f"[スキップ] {name} が見つかりません（{where} を確認しました）", file=sys.stderr)
        return False

    destination = stage_dir(target) / name
    if destination.exists():
        if not force:
            print(f"[スキップ] 移動先にすでにあります: {destination}（上書きは --force）", file=sys.stderr)
            return False
        shutil.rmtree(destination)

    shutil.move(str(source), str(destination))
    print(f"{source.parent.name}/{name} → {target}/{name}")
    return True


def move_back(name: str, force: bool = False) -> bool:
    for stage, target in BACK_TARGET.items():
        source = stage_dir(stage) / name
        if not source.is_dir():
            continue
        destination = stage_dir(target) / name
        if destination.exists():
            if not force:
                print(f"[スキップ] 戻し先にすでにあります: {destination}（上書きは --force）", file=sys.stderr)
                return False
            shutil.rmtree(destination)
        shutil.move(str(source), str(destination))
        print(f"{stage}/{name} → {target}/{name}")
        return True
    print(f"[スキップ] {name} は ready / posted に見つかりません", file=sys.stderr)
    return False


def list_posts() -> None:
    for stage in STAGES:
        folders = sorted(p.name for p in stage_dir(stage).iterdir() if p.is_dir())
        print(f"[{stage}] {len(folders)} 件")
        for name in folders:
            images = len(list((stage_dir(stage) / name).glob("*_question.png"))) * 2
            print(f"  {name}  画像{images}枚")
        if not folders:
            print("  （なし）")


def collect_names(args: argparse.Namespace, target: str) -> list[str]:
    if args.all:
        stages = SOURCES[target] if target in SOURCES else ("ready", "posted")
        names: list[str] = []
        for stage in stages:
            names.extend(sorted(p.name for p in stage_dir(stage).iterdir() if p.is_dir()))
            if names:
                break
        return names
    return args.posts


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="生成した投稿フォルダを output → ready → posted へ移動して管理する",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "例:\n"
            "  python manage.py ready post_001\n"
            "  python manage.py posted post_001 post_002\n"
            "  python manage.py list\n"
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    for command, help_text in (
        ("ready", "output から ready へ移動する（投稿準備OK）"),
        ("posted", "ready（なければ output）から posted へ移動する（投稿済み）"),
        ("back", "ひとつ前の段階へ戻す"),
    ):
        sp = sub.add_parser(command, help=help_text)
        sp.add_argument("posts", nargs="*", help="投稿フォルダ名（例: post_001）")
        sp.add_argument("--all", action="store_true", help="対象をまとめて移動する")
        sp.add_argument("--force", action="store_true", help="移動先が存在する場合に上書きする")

    sub.add_parser("list", help="output / ready / posted の状況を表示する")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.command == "list":
        list_posts()
        return 0

    names = collect_names(args, args.command if args.command != "back" else "ready")
    if not names:
        print("[エラー] 対象の投稿がありません（フォルダ名を指定するか --all）", file=sys.stderr)
        return 1

    moved = 0
    for name in names:
        if args.command == "back":
            moved += int(move_back(name, args.force))
        else:
            moved += int(move_post(name, args.command, args.force))

    print(f"移動: {moved} 件")
    return 0 if moved else 1


if __name__ == "__main__":
    raise SystemExit(main())
