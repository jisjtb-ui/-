"""caption.txt の文面生成（毎回同じにならないよう複数パターンから合成する）。"""

from __future__ import annotations

import json
import random
from pathlib import Path

DEFAULT_TAG_COUNT = 6


def load_caption_data(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def load_header_data(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def pick_header(data: dict, rng: random.Random, category: str, tests: list[dict]) -> str:
    """投稿ごとの見出し文言を選ぶ（毎回「夜の心理テスト」にしない）。

    カテゴリ指定があればその寄りの文言から、random なら全体から選ぶ。
    """
    by_category = data.get("by_category", {})
    pool = list(by_category.get(category, []))
    if not pool and tests:
        for test in tests:
            pool.extend(by_category.get(test.get("category", ""), []))
    if not pool:
        pool = list(data.get("headers", []))
    return rng.choice(pool) if pool else ""


def build_caption(data: dict, rng: random.Random, category: str, tests: list[dict]) -> str:
    """1投稿分のキャプションを組み立てる。"""
    opener = rng.choice(data["openers"])
    middle = rng.choice(data["middles"])
    closer = rng.choice(data["closers"])

    lines = [opener, "", middle]
    if closer:
        lines += ["", closer]
    lines += ["", _hashtags(data, rng, category, tests)]
    return "\n".join(lines).strip() + "\n"


def _hashtags(data: dict, rng: random.Random, category: str, tests: list[dict]) -> str:
    conf = data["hashtags"]
    tags: list[str] = list(conf["base"])

    # 投稿カテゴリ＋実際に使われた問題のカテゴリからタグを寄せる
    by_category = conf.get("by_category", {})
    sources = [category] + [t.get("category", "") for t in tests]
    for key in sources:
        for tag in by_category.get(key, []):
            if tag not in tags:
                tags.append(tag)

    pool = [t for t in conf["pool"] if t not in tags]
    rng.shuffle(pool)
    while len(tags) < DEFAULT_TAG_COUNT and pool:
        tags.append(pool.pop())

    tags = tags[: max(DEFAULT_TAG_COUNT, 5)]
    rng.shuffle(tags)
    return "\n".join(tags)
