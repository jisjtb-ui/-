"""TikTok Content Posting API（写真投稿）。

公式仕様（2026年9月時点で確認）:
  1. POST /v2/post/publish/creator_info/query/   … Direct Post前に必須
  2. POST /v2/post/publish/content/init/         … media_type=PHOTO, source=PULL_FROM_URL
  3. POST /v2/post/publish/status/fetch/         … publish_id で状態確認

制約:
  - 写真は PULL_FROM_URL のみ（所有権を検証したドメインの公開HTTPS URLが必要）
  - 1投稿あたり最大35枚
  - title は90文字(UTF-16)、description は4000文字まで
  - auto_add_music で写真投稿に音楽を自動付与できる
  - アクセストークンあたり 6リクエスト/分
  - 審査前(unaudited)のクライアントが投稿した内容は非公開扱いになる
"""

from __future__ import annotations

from typing import Callable

from ..config import Settings
from ..models import PlatformContent, PostBundle
from ..oauth import tiktok_oauth
from ..oauth.store import TokenStore
from .base import (
    ManualRequired,
    Pacer,
    PermanentError,
    PublishResult,
    Publisher,
    TransientError,
    request_json,
)

API = "https://open.tiktokapis.com/v2"
CREATOR_INFO_URL = f"{API}/post/publish/creator_info/query/"
INIT_URL = f"{API}/post/publish/content/init/"
STATUS_URL = f"{API}/post/publish/status/fetch/"

# ユーザーあたり6リクエスト/分 → 余裕をみて11秒間隔
PACE_SECONDS = 11.0
STATUS_POLL_SECONDS = 10
STATUS_MAX_POLLS = 30

# 恒久エラーとして扱うTikTokのエラーコード
PERMANENT_CODES = {
    "url_ownership_unverified",
    "invalid_file_upload",
    "picture_size_check_failed",
    "privacy_level_option_mismatch",
    "spam_risk_too_many_posts",
    "spam_risk_user_banned_from_posting",
    "reached_active_user_cap",
    "unaudited_client_can_only_post_to_private_accounts",
}
TOKEN_CODES = {"access_token_invalid", "scope_not_authorized", "scope_permission_missed"}


class TikTokPublisher(Publisher):
    name = "tiktok"

    def __init__(self, settings: Settings, store: TokenStore) -> None:
        self.settings = settings
        self.store = store
        self.pacer = Pacer(PACE_SECONDS)
        self._token = None

    # ------------------------------------------------------------------
    def preflight(self) -> None:
        missing = self.settings.missing("tiktok")
        if missing:
            raise PermanentError(".env の設定が不足しています: " + ", ".join(missing))
        self._token = tiktok_oauth.ensure_token(self.settings, self.store)

    def account_label(self) -> str:
        token = self.store.load("tiktok")
        if token is None:
            return "未接続"
        return token.account_name or token.account_id or "接続済み"

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
        self.preflight()
        token = self._token
        headers = {
            "Authorization": f"Bearer {token.access_token}",
            "Content-Type": "application/json; charset=UTF-8",
        }

        log("creator_info を取得しています")
        creator = self._creator_info(headers)
        privacy = self._resolve_privacy(creator)

        post_info = {
            "title": content.title,
            "description": content.text,
            "privacy_level": privacy,
            "disable_comment": bool(creator.get("comment_disabled", False)),
            "auto_add_music": bool(
                self.settings.tiktok_auto_add_music and bundle.music_mode == "auto"
            ),
            "brand_content_toggle": False,
            "brand_organic_toggle": False,
        }
        payload = {
            "post_info": post_info,
            "source_info": {
                "source": "PULL_FROM_URL",
                "photo_cover_index": 0,
                "photo_images": image_urls,
            },
            "post_mode": "DIRECT_POST",
            "media_type": "PHOTO",
        }

        log(f"投稿を初期化しています（画像{len(image_urls)}枚 / 公開範囲 {privacy}）")
        self.pacer.wait()
        data = request_json("POST", INIT_URL, headers=headers, json=payload)
        self._raise_for_error(data)
        publish_id = (data.get("data") or {}).get("publish_id", "")
        if not publish_id:
            raise PermanentError(f"publish_id を取得できませんでした: {str(data)[:200]}")

        log(f"アップロード中（publish_id: {publish_id[:12]}…）")
        status, detail = self._wait_for_publish(headers, publish_id, log)

        notes = []
        if privacy != self.settings.tiktok_privacy_level:
            notes.append(
                f"公開範囲を {privacy} に調整しました"
                f"（希望: {self.settings.tiktok_privacy_level}）"
            )
        if post_info["auto_add_music"]:
            notes.append("auto_add_music=true で投稿しました")
        return PublishResult(
            platform=self.name,
            post_id=bundle.post_id,
            platform_post_id=detail.get("post_id", "") or publish_id,
            publish_id=publish_id,
            detail=status,
            notes=notes,
        )

    # ------------------------------------------------------------------
    def _creator_info(self, headers: dict) -> dict:
        self.pacer.wait()
        data = request_json("POST", CREATOR_INFO_URL, headers=headers, json={})
        self._raise_for_error(data)
        return data.get("data") or {}

    def _resolve_privacy(self, creator: dict) -> str:
        """希望の公開範囲が使えなければ、クリエイターが選べる範囲へ落とす。"""
        options = creator.get("privacy_level_options") or []
        wanted = self.settings.tiktok_privacy_level
        if not options:
            return wanted
        if wanted in options:
            return wanted
        for fallback in ("SELF_ONLY", "FOLLOWER_OF_CREATOR", "MUTUAL_FOLLOW_FRIENDS"):
            if fallback in options:
                return fallback
        return options[0]

    def _wait_for_publish(
        self, headers: dict, publish_id: str, log: Callable[[str], None]
    ) -> tuple[str, dict]:
        import time

        for _ in range(STATUS_MAX_POLLS):
            self.pacer.wait()
            data = request_json(
                "POST", STATUS_URL, headers=headers, json={"publish_id": publish_id}
            )
            self._raise_for_error(data)
            payload = data.get("data") or {}
            status = payload.get("status", "")
            if status in ("PUBLISH_COMPLETE", "SEND_TO_USER_INBOX"):
                log(f"完了（{status}）")
                post_ids = payload.get("publicaly_available_post_id") or payload.get(
                    "publicly_available_post_id"
                ) or []
                return status, {"post_id": str(post_ids[0]) if post_ids else ""}
            if status == "FAILED":
                reason = payload.get("fail_reason", "不明")
                raise PermanentError(f"TikTok側で失敗しました: {reason}")
            log(f"処理中（{status or '状態取得中'}）")
            time.sleep(STATUS_POLL_SECONDS)
        raise TransientError("TikTokの投稿状態が確定しませんでした（あとで再確認します）")

    def _raise_for_error(self, data: dict) -> None:
        error = data.get("error") or {}
        code = (error.get("code") or "").lower()
        if not code or code == "ok":
            return
        message = error.get("message", "")
        if code in TOKEN_CODES:
            raise TransientError(f"認証エラー（{code}）: {message}")
        if code in PERMANENT_CODES:
            if code == "url_ownership_unverified":
                raise PermanentError(
                    "画像URLのドメインがTikTokで未検証です。"
                    "TikTok開発者ポータルでドメインまたはURLプレフィックスを検証してください"
                )
            if code == "unaudited_client_can_only_post_to_private_accounts":
                raise ManualRequired(
                    "審査前のクライアントのため、公開投稿できません"
                    "（.env の TIKTOK_PRIVACY_LEVEL=SELF_ONLY で検証するか、審査を申請してください）"
                )
            raise PermanentError(f"TikTokエラー（{code}）: {message}")
        if "rate_limit" in code or "too_many" in code:
            raise TransientError(f"レート制限（{code}）: {message}")
        raise PermanentError(f"TikTokエラー（{code}）: {message}")
