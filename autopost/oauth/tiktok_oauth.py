"""TikTok OAuth（Login Kit v2）。

公式ドキュメント:
  認可      https://www.tiktok.com/v2/auth/authorize/
  トークン  https://open.tiktokapis.com/v2/oauth/token/
必要スコープ: user.info.basic（アカウント表示用）, video.publish（Direct Post）
"""

from __future__ import annotations

import urllib.parse

import requests

from ..config import Settings
from .flow import new_state, wait_for_code
from .store import Token, TokenStore

AUTHORIZE_URL = "https://www.tiktok.com/v2/auth/authorize/"
TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"
USER_INFO_URL = "https://open.tiktokapis.com/v2/user/info/"
SCOPES = ("user.info.basic", "video.publish")
TIMEOUT = 30


class TikTokAuthError(RuntimeError):
    pass


def authorize_url(settings: Settings, state: str) -> str:
    params = {
        "client_key": settings.tiktok_client_key,
        "scope": ",".join(SCOPES),
        "response_type": "code",
        "redirect_uri": settings.tiktok_redirect_uri,
        "state": state,
    }
    return AUTHORIZE_URL + "?" + urllib.parse.urlencode(params)


def connect(settings: Settings, store: TokenStore) -> Token:
    """ブラウザで認可し、トークンを保存する。"""
    if not settings.has_tiktok_credentials():
        raise TikTokAuthError(
            ".env の TIKTOK_CLIENT_KEY / TIKTOK_CLIENT_SECRET / TIKTOK_REDIRECT_URI を設定してください"
        )
    state = new_state()
    params = wait_for_code(settings.tiktok_redirect_uri, authorize_url(settings, state))
    if params.get("state") != state:
        raise TikTokAuthError("stateが一致しません（認証をやり直してください）")
    code = params.get("code")
    if not code:
        raise TikTokAuthError(params.get("error_description") or "認可コードを取得できませんでした")

    payload = {
        "client_key": settings.tiktok_client_key,
        "client_secret": settings.tiktok_client_secret,
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": settings.tiktok_redirect_uri,
    }
    data = _post_token(payload)
    token = Token(
        platform="tiktok",
        access_token=data.get("access_token", ""),
        refresh_token=data.get("refresh_token", ""),
        expires_at=Token.expiry_from_seconds(data.get("expires_in")),
        refresh_expires_at=Token.expiry_from_seconds(data.get("refresh_expires_in")),
        scope=data.get("scope", ""),
        account_id=data.get("open_id", ""),
    )
    token.account_name = _fetch_display_name(token.access_token)
    store.save(token)
    return token


def refresh(settings: Settings, store: TokenStore, token: Token) -> Token:
    """リフレッシュトークンでアクセストークンを更新する。"""
    if not token.can_refresh():
        raise TikTokAuthError("リフレッシュトークンが無効です。再認証してください")
    payload = {
        "client_key": settings.tiktok_client_key,
        "client_secret": settings.tiktok_client_secret,
        "grant_type": "refresh_token",
        "refresh_token": token.refresh_token,
    }
    data = _post_token(payload)
    token.access_token = data.get("access_token", token.access_token)
    token.refresh_token = data.get("refresh_token", token.refresh_token)
    token.expires_at = Token.expiry_from_seconds(data.get("expires_in"))
    if data.get("refresh_expires_in"):
        token.refresh_expires_at = Token.expiry_from_seconds(data["refresh_expires_in"])
    store.save(token)
    return token


def ensure_token(settings: Settings, store: TokenStore) -> Token:
    """有効なトークンを返す（期限切れなら自動更新）。"""
    token = store.load("tiktok")
    if token is None:
        raise TikTokAuthError("TikTokが未接続です。先にOAuth認証を実行してください")
    if token.is_expired():
        token = refresh(settings, store, token)
    return token


def _post_token(payload: dict) -> dict:
    response = requests.post(
        TOKEN_URL,
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=TIMEOUT,
    )
    try:
        data = response.json()
    except ValueError as exc:
        raise TikTokAuthError(f"トークン応答を解釈できません（HTTP {response.status_code}）") from exc
    if data.get("error"):
        raise TikTokAuthError(f"{data.get('error')}: {data.get('error_description', '')}")
    if not data.get("access_token"):
        raise TikTokAuthError(f"アクセストークンを取得できませんでした（HTTP {response.status_code}）")
    return data


def _fetch_display_name(access_token: str) -> str:
    try:
        response = requests.get(
            USER_INFO_URL,
            params={"fields": "display_name"},
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=TIMEOUT,
        )
        return response.json().get("data", {}).get("user", {}).get("display_name", "")
    except Exception:
        return ""
