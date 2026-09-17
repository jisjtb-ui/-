"""投稿フォルダ（output/post_001 など）の読み込み。

既存の meta.json / caption.txt をそのまま再利用する。
文言が足りない場合は meta.json → caption.txt → 固定テンプレート の順で
決定論的にフォールバックする（LLMは使わない）。
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from .models import (
    PLATFORMS,
    PostBundle,
    extract_hashtags,
    strip_hashtags,
)

POST_DIR_RE = re.compile(r"^post_\d+$")
IMAGE_RE = re.compile(r"^(\d{2})_(question|answer)\.png$", re.IGNORECASE)
OVERRIDE_FILE = "publish.json"

# 文言がどこにも無かった場合の最終フォールバック（固定文・LLM不使用）
FALLBACK_CAPTION = "本音が出る心理テスト5問\n\n何問当たった？"
FALLBACK_HASHTAGS = ["#心理テスト", "#恋愛心理", "#性格診断"]


class LoaderError(RuntimeError):
    """投稿フォルダを読み込めなかった場合。"""


def find_post_dirs(root: Path) -> list[Path]:
    """フォルダ配下の post_XXX を昇順で返す。単一postフォルダ指定にも対応。"""
    root = Path(root)
    if not root.is_dir():
        raise LoaderError(f"フォルダが見つかりません: {root}")
    if POST_DIR_RE.match(root.name) or (root / "meta.json").is_file():
        return [root]
    dirs = [p for p in sorted(root.iterdir()) if p.is_dir() and POST_DIR_RE.match(p.name)]
    if not dirs:
        raise LoaderError(f"post_001 のようなフォルダが見つかりません: {root}")
    return dirs


def load_post(folder: Path) -> PostBundle:
    """1つの post フォルダを読み込む。"""
    folder = Path(folder)
    meta = _read_json(folder / "meta.json")
    overrides = _read_overrides(folder, meta)

    images = sorted(
        (p for p in folder.iterdir() if p.is_file() and IMAGE_RE.match(p.name)),
        key=lambda p: p.name,
    )

    caption_body, hashtags = _resolve_text(folder, meta, overrides)
    title = overrides.get("title") or _first_line(caption_body)

    music = overrides.get("music") or {}
    music_mode = str(music.get("mode", "auto")).lower()
    if music_mode not in ("auto", "none"):
        music_mode = "auto"

    scheduled_at = _parse_schedule(overrides.get("schedule"))
    enabled = _resolve_platforms(overrides)

    return PostBundle(
        post_id=folder.name,
        folder=folder,
        images=images,
        caption=caption_body,
        hashtags=hashtags,
        title=title,
        music_mode=music_mode,
        meta=meta,
        overrides=overrides,
        scheduled_at=scheduled_at,
        enabled_platforms=enabled,
    )


def load_posts(root: Path) -> list[PostBundle]:
    return [load_post(d) for d in find_post_dirs(root)]


# ----------------------------------------------------------------------
def _read_json(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise LoaderError(f"{path.name} を読み込めません: {exc}") from exc
    return data if isinstance(data, dict) else {}


def _read_overrides(folder: Path, meta: dict) -> dict:
    """投稿設定の上書き。meta.json の "publish" セクションを優先する。"""
    overrides = {}
    if isinstance(meta.get("publish"), dict):
        overrides.update(meta["publish"])
    side = _read_json(folder / OVERRIDE_FILE)
    overrides.update(side)
    return overrides


def _resolve_text(folder: Path, meta: dict, overrides: dict) -> tuple[str, list[str]]:
    """本文とハッシュタグを決定論的に解決する。"""
    raw = (
        overrides.get("caption")
        or _read_text(folder / "caption.txt")
        or meta.get("caption")
        or FALLBACK_CAPTION
    )
    hashtags = overrides.get("hashtags") or extract_hashtags(raw) or list(FALLBACK_HASHTAGS)
    body = strip_hashtags(raw) or raw.strip()
    return body, list(hashtags)


def _read_text(path: Path) -> str:
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8").strip()


def _first_line(text: str) -> str:
    for line in (text or "").splitlines():
        line = line.strip()
        if line:
            return line
    return ""


def _parse_schedule(schedule: object) -> datetime | None:
    if not isinstance(schedule, dict):
        return None
    raw = schedule.get("scheduled_at")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw))
    except ValueError:
        return None


def _resolve_platforms(overrides: dict) -> tuple[str, ...]:
    platforms = overrides.get("platforms")
    if not isinstance(platforms, dict):
        return PLATFORMS
    enabled = [p for p in PLATFORMS if platforms.get(p, {}).get("enabled", True)]
    return tuple(enabled) or PLATFORMS
