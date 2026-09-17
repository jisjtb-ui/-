"""Pinterest OAuth（API v5 / Authorization Code）。

公式仕様（OpenAPI 5.28.0 / 公式ドキュメントで確認）:
  認可      https://www.pinterest.com/oauth/
  トークン  POST https://api.pinterest.com/v5/oauth/token
            - HTTP Basic 認証（base64("client_id:client_secret")）
            - body は application/x-www-form-urlencoded
  スコープ  Pin作成には boards:read, boards:write, pins:read, pins:write
            （アカウント表示に user_accounts:read を追加）
  トークン  access_token 約30日 / refresh_token 60日（継続更新可）
  PKCEは未対応（TikTokと異なり code_challenge は送らない）

リダイレクトURIは「登録した値と完全一致」が条件。
  1. ループバック（http://localhost:8730/callback/）を登録できる場合は自動で受け取る
  2. HTTPSのURL（例: Cloudflare Pagesのドメイン）しか登録できない場合は、
     リダイレクト後のURLを貼り付ける手動モードを使う
"""

from __future__ import annotations

import base64
import urllib.parse

import requests

from ..config import Settings
from .flow import CallbackError, new_state, validate_redirect_uri, wait_for_code
from .store import Token, TokenStore

AUTHORIZE_URL = "https://www.pinterest.com/oauth/"
API_BASE = "https://api.pinterest.com/v5"
SANDBOX_BASE = "https://api-sandbox.pinterest.com/v5"
SCOPES = (
    "user_accounts:read",
    "boards:read",
    "boards:write",
    "pins:read",
    "pins:write",
)
TIMEOUT = 30


class PinterestAuthError(RuntimeError):
    pass


def api_base(settings: Settings) -> str:
    """本番かサンドボックスかを返す。"""
    return SANDBOX_BASE if settings.pinterest_sandbox else API_BASE


def authorize_url(settings: Settings, state: str) -> str:
    params = {
        "client_id": settings.pinterest_app_id,
        "redirect_uri": settings.pinterest_redirect_uri,
        "response_type": "code",
        "scope": ",".join(SCOPES),
        "state": state,
    }
    return AUTHORIZE_URL + "?" + urllib.parse.urlencode(params)


def connect(settings: Settings, store: TokenStore, manual: bool = False) -> Token:
    """ブラウザで認可し、トークンを保存する。

    manual=True の場合はローカルサーバを使わず、リダイレクト先URLの貼り付けを待つ。
    """
    missing = settings.missing("pinterest")
    if missing:
        raise PinterestAuthError(".env の設定が不足しています: " + ", ".join(missing))

    state = new_state()
    url = authorize_url(settings, state)

    if manual or not _is_loopback(settings.pinterest_redirect_uri):
        params = _manual_prompt(url)
    else:
        try:
            validate_redirect_uri(settings.pinterest_redirect_uri)
            params = wait_for_code(settings.pinterest_redirect_uri, url)
        except CallbackError as exc:
            raise PinterestAuthError(str(exc)) from exc

    if params.get("error"):
        raise PinterestAuthError(
            params.get("error_description") or f"認可されませんでした（{params['error']}）"
        )
    if params.get("state") and params["state"] != state:
        raise PinterestAuthError("stateが一致しません（認証をやり直してください）")
    code = params.get("code")
    if not code:
        raise PinterestAuthError("認可コードを取得できませんでした")

    data = _post_token(
        settings,
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": settings.pinterest_redirect_uri,
        },
    )
    token = Token(
        platform="pinterest",
        access_token=data.get("access_token", ""),
        refresh_token=data.get("refresh_token", ""),
        expires_at=Token.expiry_from_seconds(data.get("expires_in")),
        refresh_expires_at=Token.expiry_from_seconds(data.get("refresh_token_expires_in")),
        scope=data.get("scope", ""),
    )
    token.account_name = _fetch_username(settings, token.access_token)
    token.extra = {"sandbox": settings.pinterest_sandbox}
    store.save(token)
    return token


def refresh(settings: Settings, store: TokenStore, token: Token) -> Token:
    if not token.can_refresh():
        raise PinterestAuthError("リフレッシュトークンが無効です。再認証してください")
    data = _post_token(
        settings,
        {
            "grant_type": "refresh_token",
            "refresh_token": token.refresh_token,
            "scope": ",".join(SCOPES),
        },
    )
    token.access_token = data.get("access_token", token.access_token)
    if data.get("refresh_token"):
        token.refresh_token = data["refresh_token"]
    token.expires_at = Token.expiry_from_seconds(data.get("expires_in"))
    if data.get("refresh_token_expires_in"):
        token.refresh_expires_at = Token.expiry_from_seconds(data["refresh_token_expires_in"])
    store.save(token)
    return token


def ensure_token(settings: Settings, store: TokenStore) -> Token:
    token = store.load("pinterest")
    if token is None:
        raise PinterestAuthError("Pinterestが未接続です。先に `connect pinterest` を実行してください")
    if token.is_expired():
        token = refresh(settings, store, token)
    return token


# ----------------------------------------------------------------------
def _post_token(settings: Settings, payload: dict) -> dict:
    """トークンエンドポイント（Basic認証 + form-urlencoded）。"""
    credentials = f"{settings.pinterest_app_id}:{settings.pinterest_app_secret}".encode()
    headers = {
        "Authorization": "Basic " + base64.b64encode(credentials).decode(),
        "Content-Type": "application/x-www-form-urlencoded",
    }
    try:
        response = requests.post(
            f"{api_base(settings)}/oauth/token", data=payload, headers=headers, timeout=TIMEOUT
        )
    except requests.RequestException as exc:
        raise PinterestAuthError(f"トークン取得に失敗しました（通信エラー）: {exc}") from exc

    try:
        data = response.json()
    except ValueError as exc:
        raise PinterestAuthError(
            f"トークン応答を解釈できません（HTTP {response.status_code}）"
        ) from exc
    if not data.get("access_token"):
        message = data.get("message") or data.get("error_description") or data.get("error", "")
        raise PinterestAuthError(f"トークンを取得できませんでした（HTTP {response.status_code}）: {message}")
    return data


def _fetch_username(settings: Settings, access_token: str) -> str:
    try:
        response = requests.get(
            f"{api_base(settings)}/user_account",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=TIMEOUT,
        )
        return response.json().get("username", "")
    except Exception:
        return ""


def _is_loopback(redirect_uri: str) -> bool:
    host = urllib.parse.urlparse(redirect_uri).hostname or ""
    return host in ("127.0.0.1", "localhost")


def _manual_prompt(url: str) -> dict:
    """HTTPSのリダイレクトURIしか登録できない場合の手動モード。"""
    print("\n--- Pinterest 認証（手動モード） ---")
    print("1. 次のURLをブラウザで開いて許可してください:\n")
    print(url)
    print("\n2. 許可後にリダイレクトされたURL全体をコピーして貼り付けてください。")
    print("   （例: https://example.com/?code=xxxxx&state=yyyyy）\n")
    raw = input("リダイレクト先URL: ").strip()
    if not raw:
        raise PinterestAuthError("URLが入力されませんでした")
    parsed = urllib.parse.urlparse(raw)
    params = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
    if not params:
        raise PinterestAuthError("URLから code を読み取れませんでした")
    return params
