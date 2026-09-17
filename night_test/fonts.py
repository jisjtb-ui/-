"""日本語フォントの自動検出とキャッシュ付きロード。

優先順位:
  1. --font で明示指定されたパス
  2. 環境変数 NIGHT_TEST_FONT
  3. リポジトリ同梱の fonts/ ディレクトリ内の .ttf/.otf/.ttc
  4. OS 標準の日本語ゴシック体（mac / Windows / Linux）
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from PIL import ImageFont

# OS 標準フォントの候補（上が優先）。ゴシック体で静かに見えるものを優先。
CANDIDATE_PATHS: tuple[str, ...] = (
    # macOS
    "/System/Library/Fonts/ヒラギノ角ゴシック W3.ttc",
    "/System/Library/Fonts/ヒラギノ角ゴシック W4.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/Library/Fonts/NotoSansJP-Regular.otf",
    "/System/Library/Fonts/AquaKana.ttc",
    # Windows
    "C:/Windows/Fonts/YuGothR.ttc",
    "C:/Windows/Fonts/YuGothM.ttc",
    "C:/Windows/Fonts/meiryo.ttc",
    "C:/Windows/Fonts/msgothic.ttc",
    # Linux（Noto 系 → IPA 系）
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJKjp-Regular.otf",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansJP-Regular.ttf",
    "/usr/share/fonts/opentype/ipafont-gothic/ipagp.ttf",
    "/usr/share/fonts/opentype/ipafont-gothic/ipag.ttf",
    "/usr/share/fonts/truetype/fonts-japanese-gothic.ttf",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
)

BUNDLED_DIR = Path(__file__).resolve().parent.parent / "fonts"

_JP_PROBE = "夜の心理テスト"


class FontNotFoundError(RuntimeError):
    """日本語フォントが見つからなかった場合に送出する。"""


def _renders_japanese(path: str) -> bool:
    """そのフォントで日本語が描けるか（豆腐にならないか）を簡易チェックする。"""
    try:
        font = ImageFont.truetype(path, 32)
    except OSError:
        return False
    try:
        # 未収録文字だと 0 幅や例外になることがある
        return font.getlength(_JP_PROBE) > 0
    except Exception:  # pragma: no cover - 壊れたフォント対策
        return False


def _bundled_candidates() -> list[str]:
    if not BUNDLED_DIR.is_dir():
        return []
    found: list[str] = []
    for ext in ("*.otf", "*.ttf", "*.ttc"):
        found.extend(str(p) for p in sorted(BUNDLED_DIR.glob(ext)))
    return found


def resolve_font_path(explicit: str | None = None) -> str:
    """使用する日本語フォントのパスを決定する。"""
    tried: list[str] = []

    if explicit:
        if not Path(explicit).is_file():
            raise FontNotFoundError(f"指定されたフォントが見つかりません: {explicit}")
        return explicit

    env = os.environ.get("NIGHT_TEST_FONT")
    if env:
        if Path(env).is_file() and _renders_japanese(env):
            return env
        tried.append(env)

    for path in _bundled_candidates() + list(CANDIDATE_PATHS):
        if Path(path).is_file() and _renders_japanese(path):
            return path
        tried.append(path)

    raise FontNotFoundError(
        "日本語フォントが見つかりませんでした。\n"
        "  --font /path/to/font.ttf で指定するか、\n"
        f"  {BUNDLED_DIR} に日本語フォント(.ttf/.otf)を置いてください。\n"
        "  例: Noto Sans JP / ヒラギノ角ゴシック / 游ゴシック / IPAゴシック"
    )


@lru_cache(maxsize=256)
def load_font(path: str, size: int) -> ImageFont.FreeTypeFont:
    """サイズ指定でフォントを読み込む（同一指定はキャッシュする）。"""
    return ImageFont.truetype(path, size)
