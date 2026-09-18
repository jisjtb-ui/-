"""プラットフォーム別の投稿処理。"""

from .base import (
    AnalyticsResult,
    ManualRequired,
    NotSupported,
    PermanentError,
    PublishError,
    PublishResult,
    Publisher,
    TransientError,
)

__all__ = [
    "Publisher",
    "PublishResult",
    "AnalyticsResult",
    "PublishError",
    "TransientError",
    "PermanentError",
    "ManualRequired",
    "NotSupported",
    "get_publisher",
]


def get_publisher(platform: str, settings, store):
    """プラットフォーム名から Publisher を返す。"""
    if platform == "threads":
        from .threads import ThreadsPublisher

        return ThreadsPublisher(settings, store)
    if platform == "pinterest":
        from .pinterest import PinterestPublisher

        return PinterestPublisher(settings, store)
    if platform == "tiktok":
        from .tiktok import TikTokPublisher

        return TikTokPublisher(settings, store)
    if platform == "instagram":
        from .instagram import InstagramPublisher

        return InstagramPublisher(settings, store)
    if platform == "instagram_reel":
        from .reel import ReelPublisher

        return ReelPublisher(settings, store)
    raise ValueError(f"未対応のプラットフォームです: {platform}")
