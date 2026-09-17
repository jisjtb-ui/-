"""Instagram のカルーセル投稿（Instagram Platform / Graph API）。

公式仕様（2026年9月時点で確認）:
  1. POST /{ig-id}/media          … 画像ごとに is_carousel_item=true でコンテナ作成
  2. GET  /{container-id}?fields=status_code … FINISHED になるまで待つ
  3. POST /{ig-id}/media          … media_type=CAROUSEL, children=[...], caption
  4. POST /{ig-id}/media_publish  … creation_id を公開

制約:
  - カルーセルは最大10枚
  - 画像は JPEG・8MB以下・アスペクト比 4:5〜1.91:1・幅320〜1440px・公開HTTPS URL
  - キャプションは2200文字、ハッシュタグ30個まで
  - 24時間あたり 投稿100件 / コンテナ作成400件
  - 予約投稿・音楽のAPI指定は非対応（予約はローカル側で行う）
"""

from __future__ import annotations

import time
from typing import Callable

from ..config import Settings
from ..models import INSTAGRAM_MAX_CAROUSEL, PlatformContent, PostBundle
from ..oauth import meta_oauth
from ..oauth.store import TokenStore
from .base import (
    Pacer,
    PermanentError,
    PublishResult,
    Publisher,
    TransientError,
    request_json,
)

PACE_SECONDS = 1.0
STATUS_POLL_SECONDS = 5
STATUS_MAX_POLLS = 36          # 最大3分待つ
PUBLISH_SETTLE_SECONDS = 3

# 再試行しても直らないエラーコード
PERMANENT_SUBCODES = {2207026, 2207003, 2207032, 2207020}


class InstagramPublisher(Publisher):
    name = "instagram"

    def __init__(self, settings: Settings, store: TokenStore) -> None:
        self.settings = settings
        self.store = store
        self.pacer = Pacer(PACE_SECONDS)
        self._token = None

    # ------------------------------------------------------------------
    def preflight(self) -> None:
        missing = self.settings.missing("instagram")
        if missing:
            raise PermanentError(".env の設定が不足しています: " + ", ".join(missing))
        self._token = meta_oauth.ensure_token(self.settings, self.store)

    def account_label(self) -> str:
        token = self.store.load("instagram")
        if token is None:
            return "未接続"
        return token.account_name or self.settings.instagram_account_id or "接続済み"

    # ------------------------------------------------------------------
    def publish(
        self,
        bundle: PostBundle,
        image_urls: list[str],
        content: PlatformContent,
        log: Callable[[str], None] = lambda message: None,
    ) -> PublishResult:
        if not image_urls:
            raise PermanentError("画像URLがありません")
        if len(image_urls) > INSTAGRAM_MAX_CAROUSEL:
            raise PermanentError(
                f"Instagramのカルーセルは最大{INSTAGRAM_MAX_CAROUSEL}枚です（{len(image_urls)}枚）"
            )
        self.preflight()
        token = self._token.access_token
        base = meta_oauth.graph_base(self.settings)
        ig_id = self.settings.instagram_account_id

        log(f"画像コンテナを作成しています（{len(image_urls)}枚）")
        children: list[str] = []
        for order, url in enumerate(image_urls, start=1):
            container_id = self._create_container(
                base, ig_id, token, {"image_url": url, "is_carousel_item": "true"}
            )
            children.append(container_id)
            log(f"  {order}/{len(image_urls)} 作成")

        for order, container_id in enumerate(children, start=1):
            self._wait_ready(base, container_id, token, log, f"画像{order}")

        log("カルーセルコンテナを作成しています")
        carousel_id = self._create_container(
            base,
            ig_id,
            token,
            {
                "media_type": "CAROUSEL",
                "children": ",".join(children),
                "caption": content.text,
            },
        )
        self._wait_ready(base, carousel_id, token, log, "カルーセル")

        log("公開しています")
        self.pacer.wait()
        data = request_json(
            "POST",
            f"{base}/{ig_id}/media_publish",
            data={"creation_id": carousel_id, "access_token": token},
        )
        self._raise_for_error(data)
        media_id = str(data.get("id", ""))
        if not media_id:
            raise PermanentError(f"公開結果を取得できませんでした: {str(data)[:200]}")
        time.sleep(PUBLISH_SETTLE_SECONDS)

        return PublishResult(
            platform=self.name,
            post_id=bundle.post_id,
            platform_post_id=media_id,
            publish_id=carousel_id,
            detail="PUBLISHED",
            notes=["音楽はInstagram APIでは設定できません（not_supported）"],
        )

    # ------------------------------------------------------------------
    def _create_container(self, base: str, ig_id: str, token: str, params: dict) -> str:
        self.pacer.wait()
        payload = dict(params)
        payload["access_token"] = token
        data = request_json("POST", f"{base}/{ig_id}/media", data=payload)
        self._raise_for_error(data)
        container_id = str(data.get("id", ""))
        if not container_id:
            raise PermanentError(f"コンテナIDを取得できませんでした: {str(data)[:200]}")
        return container_id

    def _wait_ready(
        self, base: str, container_id: str, token: str, log: Callable[[str], None], label: str
    ) -> None:
        """コンテナが FINISHED になるまで待つ。"""
        for attempt in range(STATUS_MAX_POLLS):
            self.pacer.wait()
            data = request_json(
                "GET",
                f"{base}/{container_id}",
                params={"fields": "status_code,status", "access_token": token},
            )
            self._raise_for_error(data)
            status = data.get("status_code", "")
            if status in ("FINISHED", "PUBLISHED"):
                return
            if status == "ERROR":
                raise PermanentError(f"{label}の処理に失敗しました: {data.get('status', '')}")
            if status == "EXPIRED":
                raise PermanentError(f"{label}のコンテナが期限切れです（24時間以内に公開が必要）")
            if attempt == 0:
                log(f"  {label}の処理を待っています")
            time.sleep(STATUS_POLL_SECONDS)
        raise TransientError(f"{label}の処理が終わりませんでした（あとで再試行します）")

    def _raise_for_error(self, data: dict) -> None:
        error = data.get("error")
        if not error:
            return
        if not isinstance(error, dict):
            raise PermanentError(f"Instagramエラー: {error}")
        message = error.get("error_user_msg") or error.get("message", "")
        code = error.get("code")
        subcode = error.get("error_subcode")
        if code in (4, 17, 32, 613):        # レート制限系
            raise TransientError(f"レート制限（code={code}）: {message}")
        if code in (190,):                  # トークン期限切れ・無効
            raise TransientError(f"認証エラー（code=190）: {message}")
        if subcode in PERMANENT_SUBCODES:
            raise PermanentError(f"Instagramエラー（subcode={subcode}）: {message}")
        if code in (1, 2):                  # 一時的な内部エラー
            raise TransientError(f"一時的なエラー（code={code}）: {message}")
        raise PermanentError(f"Instagramエラー（code={code}）: {message}")
