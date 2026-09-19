#!/usr/bin/env python3
"""夜の心理テスト｜TikTok投稿セット生成ツール

使い方:
    python generate.py --posts 10
    python generate.py --posts 10 --category love
    python generate.py --posts 3 --width 1350 --height 1800 --seed 42

1投稿 = 心理テスト5問 = 画像10枚（問題5枚 + 答え5枚）。
出力は output/post_001/ のように投稿単位のフォルダにまとまる。
"""

from __future__ import annotations

import argparse
import random
import sys
from pathlib import Path

from night_test.builder import build_post, post_folder_name
from night_test.captions import load_caption_data, load_header_data, pick_header
from night_test.actions import ActionConfig, ActionCtaError, empty as empty_actions
from night_test.cta import CtaConfig, CtaError, empty as empty_cta
from night_test.config import DEFAULT_SIZE, PRESET_SIZES, TESTS_PER_POST, Layout
from night_test.content import (
    RANDOM_CATEGORY,
    ContentError,
    apply_header,
    available_categories,
    build_post_tests,
    load_items,
)
from night_test.fonts import FontNotFoundError, resolve_font_path
from night_test.history import History
from night_test.renderer import Renderer

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
TESTS_DIR = DATA_DIR / "tests"
CAPTIONS_PATH = DATA_DIR / "captions.json"
HEADERS_PATH = DATA_DIR / "headers.json"
CTA_PATH = DATA_DIR / "cta.json"
ACTION_CTA_PATH = DATA_DIR / "cta_actions.json"
DEFAULT_OUTPUT = ROOT / "output"
DEFAULT_HISTORY = ROOT / "history.json"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="夜の心理テストのTikTok投稿セット（1投稿=10枚）を大量生成する",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "例:\n"
            "  python generate.py --posts 10\n"
            "  python generate.py --posts 10 --category love\n"
            "  python generate.py --posts 5 --seed 7 --no-preview\n"
        ),
    )
    parser.add_argument("--posts", type=int, default=1, help="生成する投稿数（既定: 1）")
    parser.add_argument(
        "--category",
        default=RANDOM_CATEGORY,
        help="カテゴリ（love / relationship / dark / loneliness / personality / jealousy / values / random）",
    )
    parser.add_argument("--width", type=int, default=DEFAULT_SIZE[0], help="画像の幅（既定: 1080）")
    parser.add_argument("--height", type=int, default=DEFAULT_SIZE[1], help="画像の高さ（既定: 1440）")
    parser.add_argument(
        "--size",
        choices=sorted(PRESET_SIZES),
        help="プリセットサイズ指定（--width/--height より優先）",
    )
    parser.add_argument("--seed", type=int, help="乱数シード（同じ値なら同じ組み合わせ）")
    parser.add_argument("--font", help="日本語フォントのパス（未指定なら自動検出）")
    parser.add_argument(
        "--header",
        help="画像上部の見出し文言を固定する（未指定なら投稿ごとにパターンから選ぶ）",
    )
    parser.add_argument(
        "--cta-set",
        help="使用するCTAセット（data/cta.json で管理。未指定なら active のセット）",
    )
    parser.add_argument("--no-cta", action="store_true", help="CTAを表示しない")
    parser.add_argument(
        "--action-set",
        help="アクション別CTAのセット（data/cta_actions.json で管理）",
    )
    parser.add_argument(
        "--no-action-cta",
        action="store_true",
        help="アクション別CTAを使わず、従来の保存→共有→コメントにする",
    )
    parser.add_argument(
        "--list-action-cta", action="store_true", help="アクション別CTAの重みを表示して終了"
    )
    parser.add_argument("--list-cta", action="store_true", help="CTAセット一覧を表示して終了")
    parser.add_argument(
        "--tests-per-post", type=int, default=TESTS_PER_POST, help="1投稿の問題数（既定: 4／占いで2枚使うため）"
    )
    parser.add_argument("--start-index", type=int, help="投稿番号の開始値（既定: 履歴の続きから）")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="出力先（既定: output/）")
    parser.add_argument("--history", type=Path, default=DEFAULT_HISTORY, help="履歴ファイルのパス")
    parser.add_argument("--paper", action="store_true", help="白い紙のようなごく薄い質感を加える")
    parser.add_argument(
        "--preview",
        dest="preview",
        action="store_true",
        default=True,
        help="preview.jpg（10枚の一覧）を作る（既定: 作る）",
    )
    parser.add_argument("--no-preview", dest="preview", action="store_false", help="preview.jpg を作らない")
    parser.add_argument("--overwrite", action="store_true", help="既存フォルダを上書きする")
    parser.add_argument("--dry-run", action="store_true", help="画像を書き出さず、構成だけ表示する")
    parser.add_argument("--list-categories", action="store_true", help="利用できるカテゴリを表示して終了")
    parser.add_argument(
        "--validate",
        action="store_true",
        help="ネタ元をチェックして終了（文字数が多すぎる項目を警告する）",
    )
    return parser


# 画像に収めやすい文字数の目安（超えると自動縮小がかかる）
LIMITS = {"title": 14, "question": 70, "choice": 14, "answer": 52, "closing": 34}


def validate_items(items) -> int:
    """ネタ元の文字数をチェックして、長すぎる箇所を警告する。"""
    warnings = 0
    for item in items:
        def warn(kind: str, text: str) -> None:
            nonlocal warnings
            limit = LIMITS[kind]
            if len(text.replace("\n", "")) > limit:
                warnings += 1
                print(f"  [長い] {item.id} {kind}({len(text)}字 > {limit}): {text[:28]}…")

        for title in item.titles:
            warn("title", title)
        for question in item.questions:
            warn("question", question)
        for text in item.choices.values():
            warn("choice", text)
        for text in item.answers.values():
            warn("answer", text)
        for closing in item.closings:
            warn("closing", closing)

    variants = sum(i.variant_count for i in items)
    print(f"ネタ {len(items)} 件 / 言い回しの組み合わせ {variants} 通り")
    print(f"警告 {warnings} 件" if warnings else "警告なし（すべて画像に収まる長さです）")
    return warnings


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        items = load_items(TESTS_DIR)
    except ContentError as exc:
        print(f"[エラー] {exc}", file=sys.stderr)
        return 1

    if args.validate:
        validate_items(items)
        return 0

    try:
        cta_config = CtaConfig.load(CTA_PATH)
        cta = empty_cta() if args.no_cta else cta_config.select(args.cta_set)
        action_config = ActionConfig.load(ACTION_CTA_PATH)
        actions = (
            empty_actions()
            if (args.no_action_cta or args.no_cta)
            else action_config.get(args.action_set)
        )
    except (CtaError, ActionCtaError) as exc:
        print(f"[エラー] {exc}", file=sys.stderr)
        return 1

    if args.list_cta:
        print(f"CTAセット（既定: {cta_config.active}）:")
        for name in cta_config.names:
            texts = cta_config.sets[name]
            print(f"  {name:<10} {texts.label}")
            print(f"    1枚目   : {texts.first_page or '（なし）'}")
            for line in texts.final_lines or ["（なし）"]:
                print(f"    最終ページ: {line}")
        return 0

    categories = available_categories(items)
    if args.list_action_cta:
        print(f"アクション別CTA（既定: {action_config.active}）\n")
        for name, action_set in action_config.sets.items():
            mark = "*" if name == action_config.active else " "
            print(f"{mark} {name} … {action_set.label}")
            print(f"    見出し: {action_set.headline}")
            header = "".join(f"{t:>8}" for t in action_config.tiers)
            print(f"    {'':12}{header}   ← 結果の強さ（%）")
            for action in action_set.actions:
                weights = action_set.weights.get(action, {})
                total = sum(float(v) for v in weights.values()) or 1.0
                row = "".join(f"{float(weights.get(t, 0)) / total * 100:7.0f}%"
                              for t in action_config.tiers)
                label = action_set.action_labels.get(action, action)
                print(f"    {label:12}{row}")
            counts = {t: len(action_set.results.get(t, [])) for t in action_config.tiers}
            print(f"    文言の数: {counts}\n")
        return 0

    if args.list_categories:
        print("利用できるカテゴリ:")
        for name in categories + [RANDOM_CATEGORY]:
            count = len(items) if name == RANDOM_CATEGORY else sum(1 for i in items if i.category == name)
            print(f"  {name:<14} ネタ {count} 件")
        return 0

    if args.category not in categories and args.category != RANDOM_CATEGORY:
        print(
            f"[エラー] 不明なカテゴリ: {args.category}\n"
            f"        指定できるのは: {', '.join(categories + [RANDOM_CATEGORY])}",
            file=sys.stderr,
        )
        return 1
    if args.posts < 1:
        print("[エラー] --posts は1以上を指定してください", file=sys.stderr)
        return 1

    width, height = (PRESET_SIZES[args.size] if args.size else (args.width, args.height))
    layout = Layout(width=width, height=height, paper=args.paper)

    try:
        font_path = resolve_font_path(args.font)
    except FontNotFoundError as exc:
        print(f"[エラー] {exc}", file=sys.stderr)
        return 1

    rng = random.Random(args.seed)
    history = History.load(args.history)
    caption_data = load_caption_data(CAPTIONS_PATH)
    header_data = load_header_data(HEADERS_PATH)
    renderer = Renderer(layout, font_path)

    output_dir: Path = args.output
    output_dir.mkdir(parents=True, exist_ok=True)

    start_id = args.start_index if args.start_index is not None else history.next_post_id()
    print(f"ネタ {len(items)} 件 / カテゴリ: {args.category} / サイズ: {width}x{height}")
    print(f"CTA: {cta.name}" + (f"（{cta.first_page}）" if cta.first_page else "（なし）"))
    print(f"フォント: {font_path}")
    print(f"出力先: {output_dir}")
    print("-" * 56)

    created = 0
    for offset in range(args.posts):
        post_id = start_id + offset
        try:
            tests = build_post_tests(
                items, history, rng, args.category, args.tests_per_post
            )
        except ContentError as exc:
            print(f"[中断] {exc}", file=sys.stderr)
            break

        header = args.header or pick_header(header_data, rng, args.category, tests)
        apply_header(tests, header)

        if args.dry_run:
            print(f"{post_folder_name(post_id)}  （dry-run）  見出し: {header}")
            for test in tests:
                head = (test["question"].replace("\n", " ")).strip()
                print(
                    f"  Q{test['number']} L{test.get('level', 1)}"
                    f" [{test.get('theme', test['category'])}/{test['form_label']}]"
                    f" {test['title']}"
                )
                print(f"        {head}")
                print(f"        A {test['choices'].get('A','')} / B {test['choices'].get('B','')}"
                      f" / C {test['choices'].get('C','')} / D {test['choices'].get('D','')}")
            created += 1
            history.record_post(
                post_id, args.category, post_folder_name(post_id), tests, cta_set=cta.name
            )
            continue

        try:
            result = build_post(
                post_id=post_id,
                tests=tests,
                category=args.category,
                output_dir=output_dir,
                renderer=renderer,
                caption_data=caption_data,
                rng=rng,
                layout=layout,
                make_preview=args.preview,
                overwrite=args.overwrite,
                cta=cta,
                actions=actions,
            )
        except FileExistsError as exc:
            print(f"[スキップ] {exc}（上書きするなら --overwrite）", file=sys.stderr)
            continue

        history.record_post(
            post_id, args.category, result.folder.name, tests, cta_set=cta.name
        )
        created += 1
        titles = " / ".join(t["title"] for t in tests)
        print(f"{result.folder.name}  画像{len(result.images)}枚  [{header}]")
        print(f"          {titles}")

    if args.dry_run:
        print("-" * 56)
        print(f"（dry-run）{created} 投稿分の構成を確認しました。履歴は保存していません。")
        return 0

    history.save()
    print("-" * 56)
    print(f"完了: {created} 投稿 / 画像 {created * 10} 枚")
    if created:
        print(f"確認: {output_dir / post_folder_name(start_id)} を開いて preview.jpg を見てください")
        print("投稿後: python manage.py posted post_001 のように移動できます")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
