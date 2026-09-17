"""絵文字（🌸など）の描画支援。

本文用の日本語フォントは絵文字を持たないため、そのまま描くと豆腐（□）になる。
このモジュールでカラー絵文字フォントを探し、絵文字だけを画像として合成する。
絵文字フォントが見つからない場合は、呼び出し側で絵文字を取り除いて描画する
（豆腐を出すよりも、文字だけで成立させる方針）。
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# 絵文字とみなす文字（異体字セレクタ・ゼロ幅接合子を含む）
EMOJI_PATTERN = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U00002B00-\U00002BFF"
    "\U0001F1E6-\U0001F1FF\U0000FE0F\U0000200D\U000024C2\U00002190-\U000021FF]"
)

CANDIDATE_PATHS: tuple[str, ...] = (
    "/System/Library/Fonts/Apple Color Emoji.ttc",
    "C:/Windows/Fonts/seguiemj.ttf",
    "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf",
    "/usr/share/fonts/truetype/noto/NotoEmoji-Regular.ttf",
    "/usr/share/fonts/opentype/noto/NotoColorEmoji.ttf",
    "/usr/share/fonts/truetype/ancient-scripts/Symbola_hint.ttf",
)

# ビットマップ絵文字フォントは固定サイズでしか開けないことがあるため候補を用意する
NATIVE_SIZES = (109, 137, 160, 96, 64, 32)


def has_emoji(text: str) -> bool:
    return bool(EMOJI_PATTERN.search(text))


def strip_emoji(text: str) -> str:
    """絵文字を取り除き、余分な空白を整える。"""
    return re.sub(r"\s+", " ", EMOJI_PATTERN.sub("", text)).strip()


def split_runs(text: str) -> list[tuple[str, bool]]:
    """(文字列, 絵文字かどうか) の並びに分解する。"""
    runs: list[tuple[str, bool]] = []
    for char in text:
        if char in "\uFE0F\u200D":  # 異体字セレクタ等は描画しない
            continue
        is_emoji = bool(EMOJI_PATTERN.match(char))
        if runs and runs[-1][1] == is_emoji and not is_emoji:
            runs[-1] = (runs[-1][0] + char, is_emoji)
        else:
            runs.append((char, is_emoji))
    return runs


@lru_cache(maxsize=1)
def resolve_emoji_font_path() -> str | None:
    """利用できるカラー絵文字フォントのパスを返す（無ければ None）。"""
    for path in CANDIDATE_PATHS:
        if not Path(path).is_file():
            continue
        if _load_any(path) is not None:
            return path
    return None


def _load_any(path: str) -> tuple[ImageFont.FreeTypeFont, int] | None:
    """開けるサイズでフォントを読み込む。"""
    for size in NATIVE_SIZES:
        try:
            return ImageFont.truetype(path, size), size
        except OSError:
            continue
    return None


@lru_cache(maxsize=128)
def emoji_image(char: str, size: int) -> Image.Image | None:
    """絵文字1文字を size px の RGBA 画像として返す（描けなければ None）。"""
    path = resolve_emoji_font_path()
    if path is None:
        return None
    loaded = _load_any(path)
    if loaded is None:
        return None
    font, native = loaded

    canvas = Image.new("RGBA", (native * 2, native * 2), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    try:
        draw.text((native // 2, native // 2), char, font=font, embedded_color=True)
    except Exception:  # pragma: no cover - 環境依存の描画失敗
        return None

    bbox = canvas.getbbox()
    if bbox is None:
        return None
    glyph = canvas.crop(bbox)
    ratio = size / max(glyph.width, glyph.height)
    return glyph.resize(
        (max(1, int(glyph.width * ratio)), max(1, int(glyph.height * ratio))),
        Image.LANCZOS,
    )


def available() -> bool:
    """絵文字を描画できる環境かどうか。"""
    return resolve_emoji_font_path() is not None
