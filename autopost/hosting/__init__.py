"""画像ホスティング層。

TikTokの写真投稿は PULL_FROM_URL（公開HTTPS URL）のみ、Instagramも公開URLが必要。
そのため画像は一度だけアップロードし、**同じURLを両プラットフォームで使い回す**。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from ..config import Settings


class HostingError(RuntimeError):
    """アップロードに失敗した場合。"""


class ImageHost(ABC):
    """アップロード先の共通インターフェース。"""

    name = "base"

    @abstractmethod
    def upload(self, post_id: str, files: list[Path], content_hash: str) -> list[str]:
        """画像を順番どおりにアップロードし、公開URLを同じ順で返す。"""

    @abstractmethod
    def describe(self) -> str:
        """設定内容の要約（秘密情報を含めない）。"""

    def public_prefix(self) -> str:
        return ""


class NullHost(ImageHost):
    """未設定。検証は通るが投稿はできない。"""

    name = "none"

    def upload(self, post_id: str, files: list[Path], content_hash: str) -> list[str]:
        raise HostingError(
            "画像ホスティングが未設定です。.env の IMAGE_HOST を r2 または local にしてください"
        )

    def describe(self) -> str:
        return "未設定（IMAGE_HOST=none）"


def get_host(settings: Settings) -> ImageHost:
    """.env の IMAGE_HOST に応じたホストを返す。"""
    kind = (settings.image_host or "none").lower()
    if kind == "r2":
        from .r2 import R2Host

        return R2Host(settings)
    if kind in ("local", "pages"):
        # pages = Cloudflare Pages のローカルディレクトリへ置き、デプロイ後に公開URLで配信する
        from .local import LocalDirHost

        return LocalDirHost(settings)
    return NullHost()
