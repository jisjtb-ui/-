"""Threads Publisher（Meta公式 Threads API）。

公式仕様（2026年9月時点で確認）:
  投稿      1. POST /{user-id}/threads          media_type=IMAGE, image_url, text → creation_id
            2. POST /{user-id}/threads_publish  creation_id → 投稿ID
            カルーセルは is_carousel_item=true の子コンテナを2〜20件作り、
            media_type=CAROUSEL の親コンテナを publish する
  取得      GET /{media-id}?fields=id,permalink,timestamp,media_type
  Insights  GET /{media-id}/insights?metric=views,likes,replies,reposts,quotes,shares

制約:
  本文500文字 / 画像はJPEG・PNG、8MB以下、幅320〜1440px、アスペクト比10:1まで
  1日250投稿まで / コンテナ作成後は公開まで少し待つ必要がある
"""

from __future__ import annotations

import time
from typing import Callable

from ..config import Settings
from ..models import PlatformContent, PostBundle
from ..oauth import threads_oauth
from ..oauth.store import TokenStore
from .base import (
    AnalyticsResult,
    ManualRequired,
    Pacer,
    PermanentError,
    PublishResult,
    Publisher,
    TransientError,
    request_json,
)

PACE_SECONDS = 1.0
TEXT_LIMIT = 500
MAX_CAROUSEL = 20
# コンテナ作成後、公開できる状態になるまでの待ち時間（公式の推奨は約30秒）
CONTAINER_WAIT_SECONDS = 30
STATUS_POLL_SECONDS = 5
STATUS_MAX_POLLS = 12

MEDIA_METRICS = ("views", "likes", "replies", "reposts", "quotes", "shares")

# Threads固有のエラーコードと、利用者がすぐ動ける対処
THREADS_ERROR_HINTS = {
    1349245: (
        "Threadsアプリでテスターの招待が承認されていません。\n"
        "      Threadsアプリ → 設定 → アカウント → ウェブサイトの許可（Website permissions）\n"
        "      → 招待（Invites）を開いて承認してください。\n"
        "      承認後にもう一度 `python autopost.py connect threads --manual` を実行します"
    ),
    1349138: "画像の形式または大きさが要件を満たしていません（JPEG/PNG・8MB以下・幅320〜1440px）",
    1349125: "画像URLへアクセスできません（公開HTTPSのURLか確認してください）",
    1349048: "このThreadsアカウントではAPI投稿が許可されていません",
}
# 共通指標へのマッピング（存在しない指標は platform_metrics にだけ残す）
METRIC_MAP = {
    "views": "views",
    "likes": "likes",
    "replies": "comments",
    "reposts": "shares",
    "quotes": "shares",
}


class ThreadsPublisher(Publisher):
    name = "threads"

    def __init__(self, settings: Settings, store: TokenStore) -> None:
        self.settings = settings
        self.store = store
        self.pacer = Pacer(PACE_SECONDS)
        self._token = None
        self._user_id = ""

    # ------------------------------------------------------------------
    def preflight(self) -> None:
        missing = self.settings.missing("threads")
        if missing:
            raise PermanentError(".env の設定が不足しています: " + ", ".join(missing))
        self._token = threads_oauth.ensure_token(self.settings, self.store)
        self._user_id = threads_oauth.resolve_user_id(self.settings, self._token)
        if not self._user_id:
            raise PermanentError(
                "ThreadsのユーザーIDを取得できません（.env の THREADS_USER_ID を設定してください）"
            )

    def account_label(self) -> str:
        token = self.store.load("threads")
        if token is None:
            return "未接続"
        return f"@{token.account_name}" if token.account_name else "接続済み"

    @property
    def base(self) -> str:
        return threads_oauth.api_base(self.settings)

    # ------------------------------------------------------------------
    # 実験単位の配信
    # ------------------------------------------------------------------
    def publish_content(
        self,
        experiment,
        image_url: str,
        log: Callable[[str], None] = lambda message: None,
    ) -> PublishResult:
        image_urls = (experiment.extra or {}).get("image_urls") or ([image_url] if image_url else [])
        text = _compose_text(experiment)
        return self._publish_images(experiment.experiment_id, image_urls, text, log)

    # ------------------------------------------------------------------
    # フォルダ単位の配信
    # ------------------------------------------------------------------
    def publish(
        self,
        bundle: PostBundle,
        image_urls: list[str],
        content: PlatformContent,
        log: Callable[[str], None] = lambda message: None,
    ) -> PublishResult:
        return self._publish_images(bundle.post_id, image_urls, _clip(content.text), log)

    # ------------------------------------------------------------------
    def _publish_images(
        self,
        post_id: str,
        image_urls: list[str],
        text: str,
        log: Callable[[str], None],
    ) -> PublishResult:
        if not image_urls:
            raise PermanentError("画像URLがありません")
        if len(image_urls) > MAX_CAROUSEL:
            raise PermanentError(
                f"Threadsのカルーセルは最大{MAX_CAROUSEL}枚です（{len(image_urls)}枚）"
            )
        self.preflight()

        if len(image_urls) == 1:
            log("コンテナを作成しています（画像1枚）")
            creation_id = self._create_container(
                {"media_type": "IMAGE", "image_url": image_urls[0], "text": text}
            )
        else:
            log(f"子コンテナを作成しています（画像{len(image_urls)}枚）")
            children = [
                self._create_container(
                    {"media_type": "IMAGE", "image_url": url, "is_carousel_item": "true"}
                )
                for url in image_urls
            ]
            log("カルーセルコンテナを作成しています")
            creation_id = self._create_container(
                {"media_type": "CAROUSEL", "children": ",".join(children), "text": text}
            )

        log(f"公開待ち（{CONTAINER_WAIT_SECONDS}秒）")
        self._wait_container(creation_id, log)

        log("公開しています")
        self.pacer.wait()
        data = request_json(
            "POST",
            f"{self.base}/{self._user_id}/threads_publish",
            data={"creation_id": creation_id, "access_token": self._token.access_token},
        )
        self._raise_for_error(data)
        media_id = str(data.get("id", ""))
        if not media_id:
            raise PermanentError(f"投稿IDを取得できませんでした: {str(data)[:200]}")

        permalink = self._permalink(media_id)
        log(f"公開しました: {media_id}")
        return PublishResult(
            platform=self.name,
            post_id=post_id,
            platform_post_id=media_id,
            publish_id=creation_id,
            detail="published",
            external_url=permalink,
            extra={"image_count": len(image_urls)},
        )

    def _create_container(self, params: dict) -> str:
        self.pacer.wait()
        payload = dict(params)
        payload["access_token"] = self._token.access_token
        data = request_json("POST", f"{self.base}/{self._user_id}/threads", data=payload)
        self._raise_for_error(data)
        container_id = str(data.get("id", ""))
        if not container_id:
            raise PermanentError(f"コンテナIDを取得できませんでした: {str(data)[:200]}")
        return container_id

    def _wait_container(self, creation_id: str, log: Callable[[str], None]) -> None:
        """コンテナが FINISHED になるまで待つ。"""
        time.sleep(CONTAINER_WAIT_SECONDS)
        for _ in range(STATUS_MAX_POLLS):
            self.pacer.wait()
            data = request_json(
                "GET",
                f"{self.base}/{creation_id}",
                params={"fields": "status,error_message",
                        "access_token": self._token.access_token},
            )
            self._raise_for_error(data)
            status = (data.get("status") or "").upper()
            if status in ("FINISHED", "PUBLISHED", ""):
                return
            if status == "ERROR":
                raise PermanentError(
                    f"コンテナの処理に失敗しました: {data.get('error_message', '')}"
                )
            if status == "EXPIRED":
                raise PermanentError("コンテナが期限切れです（24時間以内に公開が必要）")
            log(f"  処理中（{status}）")
            time.sleep(STATUS_POLL_SECONDS)
        raise TransientError("コンテナの処理が終わりませんでした（あとで再試行します）")

    def _permalink(self, media_id: str) -> str:
        try:
            self.pacer.wait()
            data = request_json(
                "GET",
                f"{self.base}/{media_id}",
                params={"fields": "permalink", "access_token": self._token.access_token},
            )
            return str(data.get("permalink", ""))
        except Exception:
            return ""

    # ------------------------------------------------------------------
    def get_post_status(self, external_post_id: str) -> str:
        self.preflight()
        self.pacer.wait()
        data = request_json(
            "GET",
            f"{self.base}/{external_post_id}",
            params={"fields": "id,timestamp", "access_token": self._token.access_token},
        )
        self._raise_for_error(data)
        return "published" if data.get("id") else "unknown"

    def get_analytics(
        self, external_post_id: str, start_date: str = "", end_date: str = ""
    ) -> AnalyticsResult:
        """投稿のInsightsを共通指標へ正規化して返す。"""
        self.preflight()
        self.pacer.wait()
        data = request_json(
            "GET",
            f"{self.base}/{external_post_id}/insights",
            params={
                "metric": ",".join(MEDIA_METRICS),
                "access_token": self._token.access_token,
            },
        )
        self._raise_for_error(data)

        raw: dict[str, int] = {}
        for item in data.get("data", []):
            name = item.get("name", "")
            values = item.get("values") or []
            value = item.get("total_value", {}).get("value")
            if value is None and values:
                value = values[0].get("value")
            if isinstance(value, (int, float)):
                raw[name] = int(value)

        result = AnalyticsResult(
            platform=self.name,
            external_post_id=external_post_id,
            platform_metrics=raw,
        )
        # reposts と quotes はどちらも「拡散」なので shares にまとめる
        shares = sum(raw.get(key, 0) for key in ("reposts", "quotes", "shares"))
        for metric, attribute in METRIC_MAP.items():
            if metric in raw and attribute != "shares":
                setattr(result, attribute, raw[metric])
        if shares:
            result.shares = shares
        return result

    # ------------------------------------------------------------------
    def _raise_for_error(self, data: dict) -> None:
        """Threadsの2種類のエラー形式を両方扱う。

          1. {"error": {"message": ..., "code": ...}}          … Graph API共通
          2. {"error_message": ..., "error_code": 1349245}     … Threads固有
        """
        if not isinstance(data, dict):
            return

        # 形式2（Threads固有）
        if data.get("error_message") or data.get("error_code"):
            code = data.get("error_code")
            message = data.get("error_message", "")
            hint = THREADS_ERROR_HINTS.get(code)
            if hint:
                raise ManualRequired(f"{hint}\n      （Threads error_code={code}）")
            raise PermanentError(f"Threadsエラー（error_code={code}）: {message}")

        error = data.get("error")
        if not error:
            return
        if not isinstance(error, dict):
            raise PermanentError(f"Threadsエラー: {error}")
        message = error.get("error_user_msg") or error.get("message", "")
        code = error.get("code")
        hint = THREADS_ERROR_HINTS.get(code)
        if hint:
            raise ManualRequired(f"{hint}\n      （Threads code={code}）")
        if code in (4, 17, 32, 613):
            raise TransientError(f"レート制限（code={code}）: {message}")
        if code == 190:
            raise TransientError(f"認証エラー（code=190）: {message}")
        if code in (1, 2):
            raise TransientError(f"一時的なエラー（code={code}）: {message}")
        raise PermanentError(f"Threadsエラー（code={code}）: {message}")


def _compose_text(experiment) -> str:
    """Hook・本文・タグを500文字に収めて組み立てる。"""
    tags = getattr(experiment, "tags", None) or []
    tag_line = " ".join(t if t.startswith("#") else f"#{t}" for t in tags)
    body = (experiment.text or "").strip()
    hook = (experiment.hook or "").strip()
    if hook and not body.startswith(hook):
        body = f"{hook}\n\n{body}".strip()
    return _clip("\n\n".join(part for part in (body, tag_line) if part))


def _clip(text: str, limit: int = TEXT_LIMIT) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"
