"""投稿前の機械的検証（AI不使用）。

「投稿してから失敗する」を避けるため、APIを呼ぶ前に落ちる条件をすべて潰す。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .config import Settings
from .db import Queue, STATUS_POSTED
from .imageprep import MAX_BYTES, MAX_RATIO, MAX_WIDTH, MIN_RATIO, MIN_WIDTH, prepare_post_images
from .models import (
    INSTAGRAM_CAPTION_LIMIT,
    INSTAGRAM_HASHTAG_LIMIT,
    INSTAGRAM_MAX_CAROUSEL,
    PLATFORMS,
    PostBundle,
    TIKTOK_DESCRIPTION_LIMIT,
    TIKTOK_MAX_PHOTOS,
    TIKTOK_TITLE_LIMIT,
    utf16_len,
)

IMAGE_NAME_RE = re.compile(r"^(\d{2})_(question|answer)\.png$", re.IGNORECASE)

ERROR = "error"
WARN = "warn"


@dataclass
class Issue:
    """検証結果1件。"""

    level: str
    post_id: str
    message: str
    platform: str = ""

    def __str__(self) -> str:
        mark = "NG" if self.level == ERROR else "注意"
        target = f"{self.post_id}" + (f"/{self.platform}" if self.platform else "")
        return f"[{mark}] {target}: {self.message}"


def validate_post(
    bundle: PostBundle,
    settings: Settings,
    platforms: tuple[str, ...] = PLATFORMS,
    queue: Queue | None = None,
    check_images: bool = True,
) -> list[Issue]:
    """1投稿を検証する。error が1件でもあれば投稿しない。"""
    issues: list[Issue] = []
    add = issues.append

    # --- フォルダと画像 ---
    if not bundle.folder.is_dir():
        add(Issue(ERROR, bundle.post_id, f"フォルダがありません: {bundle.folder}"))
        return issues
    if not (bundle.folder / "meta.json").is_file():
        add(Issue(WARN, bundle.post_id, "meta.json がありません（caption.txt から文言を取得します）"))
    if not bundle.images:
        add(Issue(ERROR, bundle.post_id, "画像が1枚もありません"))
        return issues

    issues.extend(_check_image_sequence(bundle))

    if "instagram" in platforms and len(bundle.images) > INSTAGRAM_MAX_CAROUSEL:
        add(Issue(ERROR, bundle.post_id,
                  f"Instagramのカルーセルは最大{INSTAGRAM_MAX_CAROUSEL}枚です（現在{len(bundle.images)}枚）",
                  "instagram"))
    if "tiktok" in platforms and len(bundle.images) > TIKTOK_MAX_PHOTOS:
        add(Issue(ERROR, bundle.post_id,
                  f"TikTokの写真投稿は最大{TIKTOK_MAX_PHOTOS}枚です（現在{len(bundle.images)}枚）",
                  "tiktok"))

    # --- 文言 ---
    if not bundle.caption.strip():
        add(Issue(ERROR, bundle.post_id, "caption が空です"))
    if not bundle.hashtags:
        add(Issue(WARN, bundle.post_id, "ハッシュタグがありません"))

    for platform in platforms:
        content = bundle.content_for(platform)
        text = content.text
        if platform == "tiktok":
            if utf16_len(content.title) > TIKTOK_TITLE_LIMIT:
                add(Issue(ERROR, bundle.post_id,
                          f"TikTokのタイトルが{TIKTOK_TITLE_LIMIT}文字を超えています", platform))
            if utf16_len(text) > TIKTOK_DESCRIPTION_LIMIT:
                add(Issue(ERROR, bundle.post_id,
                          f"TikTokの説明文が{TIKTOK_DESCRIPTION_LIMIT}文字を超えています", platform))
            if not content.title.strip():
                add(Issue(WARN, bundle.post_id, "TikTokのタイトルが空です", platform))
        else:
            if len(text) > INSTAGRAM_CAPTION_LIMIT:
                add(Issue(ERROR, bundle.post_id,
                          f"Instagramのキャプションが{INSTAGRAM_CAPTION_LIMIT}文字を超えています", platform))
            if len(content.hashtags) > INSTAGRAM_HASHTAG_LIMIT:
                add(Issue(ERROR, bundle.post_id,
                          f"Instagramのハッシュタグは最大{INSTAGRAM_HASHTAG_LIMIT}個です"
                          f"（現在{len(content.hashtags)}個）", platform))

    # --- 画像仕様（変換後） ---
    if check_images:
        issues.extend(_check_prepared_images(bundle, settings))

    # --- 設定と重複投稿 ---
    issues.extend(_check_platform_settings(bundle, settings, platforms))
    if queue is not None:
        for platform in platforms:
            job = queue.get(bundle.post_id, platform)
            if job and job.status == STATUS_POSTED:
                add(Issue(WARN, bundle.post_id,
                          f"すでに投稿済みです（{job.posted_at}）。通常実行では再投稿しません", platform))
    return issues


def _check_image_sequence(bundle: PostBundle) -> list[Issue]:
    """01_question → 02_answer … の順序と対応を確認する。"""
    issues: list[Issue] = []
    names = [p.name for p in bundle.images]
    for index, name in enumerate(names, start=1):
        match = IMAGE_NAME_RE.match(name)
        if not match:
            issues.append(Issue(ERROR, bundle.post_id, f"想定外のファイル名です: {name}"))
            continue
        number = int(match.group(1))
        kind = match.group(2).lower()
        if number != index:
            issues.append(Issue(ERROR, bundle.post_id,
                                f"画像の連番が飛んでいます: {name}（{index:02d} を期待）"))
        expected = "question" if index % 2 == 1 else "answer"
        if kind != expected:
            issues.append(Issue(ERROR, bundle.post_id,
                                f"問題と答えの順番が崩れています: {name}（{expected} を期待）"))
    if len(names) % 2 != 0:
        issues.append(Issue(WARN, bundle.post_id, f"画像が奇数枚です（{len(names)}枚）"))
    return issues


def _check_prepared_images(bundle: PostBundle, settings: Settings) -> list[Issue]:
    """変換後の画像が各プラットフォームの要件を満たすか確認する。"""
    issues: list[Issue] = []
    try:
        prepared = prepare_post_images(bundle.post_id, bundle.images, settings.cache_dir)
    except Exception as exc:  # 破損画像など
        return [Issue(ERROR, bundle.post_id, f"画像を変換できません: {exc}")]

    for item in prepared:
        ratio = item.width / item.height
        if not (MIN_RATIO - 0.001 <= ratio <= MAX_RATIO + 0.001):
            issues.append(Issue(ERROR, bundle.post_id,
                                f"{item.name} のアスペクト比が要件外です（{ratio:.3f}）"))
        if not (MIN_WIDTH <= item.width <= MAX_WIDTH):
            issues.append(Issue(ERROR, bundle.post_id,
                                f"{item.name} の幅が要件外です（{item.width}px）"))
        if item.size_bytes > MAX_BYTES:
            issues.append(Issue(ERROR, bundle.post_id,
                                f"{item.name} が8MBを超えています（{item.size_bytes/1024/1024:.1f}MB）"))
    return issues


def _check_platform_settings(
    bundle: PostBundle, settings: Settings, platforms: tuple[str, ...]
) -> list[Issue]:
    """.env とトークンの状態を確認する（値そのものは扱わない）。"""
    from .oauth.store import TokenStore

    issues: list[Issue] = []
    store = TokenStore(settings.token_dir)

    missing_host = settings.missing("hosting")
    if missing_host:
        issues.append(Issue(ERROR, bundle.post_id,
                            "画像ホスティングが未設定です（.env: " + ", ".join(missing_host) + "）"))

    for platform in platforms:
        missing = settings.missing(platform)
        if missing:
            issues.append(Issue(ERROR, bundle.post_id,
                                ".env の設定が不足しています: " + ", ".join(missing), platform))
            continue
        token = store.load(platform)
        if token is None:
            issues.append(Issue(ERROR, bundle.post_id,
                                "未接続です（OAuth認証を実行してください）", platform))
        elif token.is_expired():
            if token.refresh_token:
                issues.append(Issue(WARN, bundle.post_id,
                                    "アクセストークンの期限切れです（自動更新を試みます）", platform))
            else:
                issues.append(Issue(ERROR, bundle.post_id,
                                    "アクセストークンの期限切れです（再認証が必要）", platform))
    return issues


def summarize(issues: list[Issue]) -> tuple[int, int]:
    errors = sum(1 for i in issues if i.level == ERROR)
    warns = sum(1 for i in issues if i.level == WARN)
    return errors, warns
