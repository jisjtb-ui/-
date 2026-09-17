"""プラットフォーム別の投稿処理。"""

from .base import ManualRequired, PermanentError, PublishError, PublishResult, Publisher, TransientError

__all__ = [
    "Publisher",
    "PublishResult",
    "PublishError",
    "TransientError",
    "PermanentError",
    "ManualRequired",
    "get_publisher",
]


def get_publisher(platform: str, settings, store):
    """プラットフォーム名から Publisher を返す。"""
    if platform == "tiktok":
        from .tiktok import TikTokPublisher

        return TikTokPublisher(settings, store)
    if platform == "instagram":
        from .instagram import InstagramPublisher

        return InstagramPublisher(settings, store)
    raise ValueError(f"未対応のプラットフォームです: {platform}")
