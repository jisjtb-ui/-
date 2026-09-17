"""Publisher Adapter の共通インターフェース。

投稿先ごとの処理をここに閉じ込め、上位（実験エンジン / scheduler）は
プラットフォームを意識せずに扱えるようにする。

  publish(...)            コンテンツを配信して外部IDを返す
  get_post_status(id)     配信済みコンテンツの状態を取得する
  get_analytics(id, ...)  反応データを共通指標へ正規化して返す

新しいチャネル（YouTube / X / Threads など）は、このクラスを実装して
publishers/__init__.py の get_publisher に1行足すだけで追加できる。
共通処理（キュー・ログ・リトライ・二重投稿防止）は呼び出し側にある。
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


class NotSupported(PublishError):
    """そのプラットフォームのAPIに機能が存在しない場合。"""


@dataclass
class PublishResult:
    """投稿結果。"""

    platform: str
    post_id: str
    platform_post_id: str = ""
    publish_id: str = ""
    detail: str = ""
    external_url: str = ""
    notes: list[str] = field(default_factory=list)
    extra: dict = field(default_factory=dict)


@dataclass
class AnalyticsResult:
    """反応データ。共通指標に無いものは platform_metrics へ入れる。"""

    platform: str
    external_post_id: str
    impressions: int | None = None
    views: int | None = None
    likes: int | None = None
    comments: int | None = None
    shares: int | None = None
    saves: int | None = None
    clicks: int | None = None
    followers_gained: int | None = None
    period_start: str = ""
    period_end: str = ""
    platform_metrics: dict = field(default_factory=dict)


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

    # --- 任意実装。対応していないチャネルは NotSupported を送出する ---
    def publish_content(
        self,
        experiment,
        image_url: str,
        log: Callable[[str], None] = lambda message: None,
    ) -> PublishResult:
        """実験1件（画像1枚＋テキスト）を配信する。

        画像フォルダ単位ではなく「実験単位」で配信するチャネル向けの入口。
        """
        raise NotSupported(f"{self.name} は実験単位の配信に未対応です")

    def get_post_status(self, external_post_id: str) -> str:
        """配信済みコンテンツの状態を返す。"""
        raise NotSupported(f"{self.name} は投稿状態の取得に未対応です")

    def get_analytics(
        self, external_post_id: str, start_date: str = "", end_date: str = ""
    ) -> AnalyticsResult:
        """反応データを取得する。"""
        raise NotSupported(f"{self.name} は反応データの取得に未対応です")


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
