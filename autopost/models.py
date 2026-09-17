"""投稿データの共通モデル。

1つの post フォルダ = 1つの PostBundle。TikTokとInstagramで同じ素材を使い、
プラットフォーム固有の差分（caption / hashtags）だけを上書きできるようにする。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

PLATFORMS = ("tiktok", "instagram")
# 実験エンジンで配信できるチャネル（Publisher Adapter を追加すればここに増える）
ALL_PLATFORMS = ("pinterest", "tiktok", "instagram")

# Pinterest の上限（API v5 / OpenAPI 5.28.0）
PINTEREST_TITLE_LIMIT = 100
PINTEREST_DESCRIPTION_LIMIT = 800
PINTEREST_LINK_LIMIT = 2048
PINTEREST_ALT_TEXT_LIMIT = 500

# TikTok / Instagram の文字数上限（公式ドキュメント基準）
TIKTOK_TITLE_LIMIT = 90        # UTF-16 runes
TIKTOK_DESCRIPTION_LIMIT = 4000
INSTAGRAM_CAPTION_LIMIT = 2200
INSTAGRAM_HASHTAG_LIMIT = 30
INSTAGRAM_MAX_CAROUSEL = 10
TIKTOK_MAX_PHOTOS = 35

HASHTAG_RE = re.compile(r"[#＃][^\s#＃]+")


def utf16_len(text: str) -> int:
    """TikTokの文字数カウント（UTF-16 code unit）に合わせた長さ。"""
    return len(text.encode("utf-16-le")) // 2


def extract_hashtags(text: str) -> list[str]:
    """本文からハッシュタグを順序を保って抽出する（重複除去）。"""
    seen: list[str] = []
    for tag in HASHTAG_RE.findall(text or ""):
        tag = "#" + tag[1:]
        if tag not in seen:
            seen.append(tag)
    return seen


def strip_hashtags(text: str) -> str:
    """本文からハッシュタグ行を取り除いた部分を返す。"""
    lines = [ln for ln in (text or "").splitlines() if not HASHTAG_RE.fullmatch(ln.strip() or "x")]
    return "\n".join(lines).strip()


def compose_caption(body: str, hashtags: list[str]) -> str:
    """本文に、まだ含まれていないハッシュタグだけを追記する。"""
    body = (body or "").strip()
    missing = [t for t in hashtags if t not in body]
    if not missing:
        return body
    return (body + "\n\n" + " ".join(missing)).strip()


@dataclass
class PlatformContent:
    """あるプラットフォームへ実際に送る文言。"""

    caption: str = ""
    hashtags: list[str] = field(default_factory=list)
    title: str = ""

    @property
    def text(self) -> str:
        return compose_caption(self.caption, self.hashtags)


@dataclass
class PostBundle:
    """1投稿分の素材と文言。ここから先はプラットフォームごとに分岐する。"""

    post_id: str
    folder: Path
    images: list[Path]
    caption: str = ""
    hashtags: list[str] = field(default_factory=list)
    title: str = ""
    music_mode: str = "auto"          # auto | none
    meta: dict = field(default_factory=dict)
    overrides: dict = field(default_factory=dict)
    scheduled_at: datetime | None = None
    enabled_platforms: tuple[str, ...] = PLATFORMS

    # ------------------------------------------------------------------
    def content_for(self, platform: str) -> PlatformContent:
        """プラットフォーム固有の文言を解決する（無ければ共通を使う）。"""
        caption = self.overrides.get(f"caption_{platform}") or self.caption
        hashtags = self.overrides.get(f"hashtags_{platform}") or self.hashtags
        title = self.overrides.get(f"title_{platform}") or self.title

        if platform == "tiktok":
            title = _truncate_utf16(title, TIKTOK_TITLE_LIMIT)
        return PlatformContent(caption=caption, hashtags=list(hashtags), title=title)

    @property
    def image_count(self) -> int:
        return len(self.images)


def _truncate_utf16(text: str, limit: int) -> str:
    if utf16_len(text) <= limit:
        return text
    out = ""
    for char in text:
        if utf16_len(out + char) > limit:
            break
        out += char
    return out
