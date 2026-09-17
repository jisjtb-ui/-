"""Publisher の共通部分。

共通処理（投稿データ読込・画像準備・キュー・ログ・リトライ・二重投稿防止）は
呼び出し側（scheduler）に置き、ここは「APIを叩いて結果を返す」ことに専念する。
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Callable

import requests

from ..models import PlatformContent, PostBundle


class PublishError(RuntimeError):
    """投稿時のエラー基底。"""

    transient = False


class TransientError(PublishError):
    """一時的なエラー（ネットワーク / タイムアウト / レート制限 / 5xx）。再試行する。"""

    transient = True


class PermanentError(PublishError):
    """恒久的なエラー（設定不備 / 権限不足 / 規約違反）。再試行しない。"""


class ManualRequired(PublishError):
    """APIでは完結できず、手作業が必要な場合（例: 音楽の手動設定）。"""


@dataclass
class PublishResult:
    """投稿結果。"""

    platform: str
    post_id: str
    platform_post_id: str = ""
    publish_id: str = ""
    detail: str = ""
    notes: list[str] = field(default_factory=list)


class Publisher(ABC):
    """1プラットフォーム分の投稿処理。"""

    name = "base"

    @abstractmethod
    def preflight(self) -> None:
        """認証や設定を確認する。問題があれば例外を送出する。"""

    @abstractmethod
    def publish(
        self,
        bundle: PostBundle,
        image_urls: list[str],
        content: PlatformContent,
        log: Callable[[str], None] = lambda message: None,
    ) -> PublishResult:
        """公開HTTPS URLの画像を使って投稿する。"""

    @abstractmethod
    def account_label(self) -> str:
        """接続中アカウントの表示名（秘密情報は含めない）。"""


def request_json(
    method: str,
    url: str,
    *,
    timeout: int = 60,
    retry_on_status: tuple[int, ...] = (429, 500, 502, 503, 504),
    **kwargs,
) -> dict:
    """HTTPリクエストを投げてJSONを返す。通信系の失敗は TransientError にする。"""
    try:
        response = requests.request(method, url, timeout=timeout, **kwargs)
    except requests.Timeout as exc:
        raise TransientError(f"タイムアウトしました: {url}") from exc
    except requests.ConnectionError as exc:
        raise TransientError(f"接続できませんでした: {exc}") from exc
    except requests.RequestException as exc:
        raise TransientError(f"通信エラー: {exc}") from exc

    if response.status_code in retry_on_status:
        raise TransientError(
            f"一時的なエラー（HTTP {response.status_code}）: {response.text[:200]}"
        )
    try:
        return response.json()
    except ValueError as exc:
        raise PermanentError(
            f"応答を解釈できません（HTTP {response.status_code}）: {response.text[:200]}"
        ) from exc


class Pacer:
    """レート制限を避けるための最小間隔（TikTokはユーザーあたり6req/分）。"""

    def __init__(self, min_interval: float) -> None:
        self.min_interval = min_interval
        self._last = 0.0

    def wait(self) -> None:
        elapsed = time.monotonic() - self._last
        if self._last and elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        self._last = time.monotonic()
