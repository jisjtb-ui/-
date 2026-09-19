"""Threads OAuth（Meta公式 Threads API）。

公式仕様（2026年9月時点で確認）:
  認可        https://threads.com/oauth/authorize
              params: client_id, redirect_uri, scope, response_type=code, state
  短期トークン POST https://graph.threads.net/oauth/access_token
              form: client_id, client_secret, code, grant_type=authorization_code, redirect_uri
              → {access_token, user_id}（1時間有効）
  長期トークン GET  https://graph.threads.net/access_token
              params: grant_type=th_exchange_token, client_secret, access_token
              → 60日間有効
  更新        GET  https://graph.threads.net/refresh_access_token
              params: grant_type=th_refresh_token, access_token
              （24時間以上経過していれば更新できる）

スコープ:
  threads_basic            すべてのエンドポイントに必須
  threads_content_publish  投稿
  threads_manage_insights  Insights取得

リダイレクトURIはHTTPSである必要があるため、
Cloudflare Pages のURLなどを登録して手動貼り付けモードで認証する。
"""

from __future__ import annotations

import urllib.parse

import requests

from ..config import Settings
from .flow import (CallbackError, authorize_error, new_state,
                   validate_redirect_uri, wait_for_code)
from .store import Token, TokenStore

AUTHORIZE_URL = "https://threads.com/oauth/authorize"
SCOPES = ("threads_basic", "threads_content_publish", "threads_manage_insights")
TIMEOUT = 30
LONG_LIVED_DAYS = 60


class ThreadsAuthError(RuntimeError):
    pass


def graph_host(settings: Settings) -> str:
    """APIホスト（graph.threads.net / graph.threads.com のどちらでも動く）。"""
    return settings.threads_api_host.rstrip("/")


def api_base(settings: Settings) -> str:
    return f"{graph_host(settings)}/{settings.threads_api_version}"


def authorize_url(settings: Settings, state: str) -> str:
    params = {
        "client_id": settings.threads_app_id,
        "redirect_uri": settings.threads_redirect_uri,
        "scope": ",".join(SCOPES),
        "response_type": "code",
        "state": state,
    }
    return AUTHORIZE_URL + "?" + urllib.parse.urlencode(params)


def connect(settings: Settings, store: TokenStore, manual: bool = False) -> Token:
    """ブラウザで認可し、長期トークン（60日）を保存する。"""
    missing = settings.missing("threads")
    if missing:
        raise ThreadsAuthError(".env の設定が不足しています: " + ", ".join(missing))

    state = new_state()
    url = authorize_url(settings, state)

    if manual or not _is_loopback(settings.threads_redirect_uri):
        params = _manual_prompt(url)
    else:
        try:
            validate_redirect_uri(settings.threads_redirect_uri)
            params = wait_for_code(settings.threads_redirect_uri, url)
        except CallbackError as exc:
            raise ThreadsAuthError(str(exc)) from exc

    problem = authorize_error(params, settings.threads_redirect_uri)
    if problem:
        raise ThreadsAuthError(problem)
    if params.get("state") and params["state"] != state:
        raise ThreadsAuthError("stateが一致しません（認証をやり直してください）")
    code = params.get("code")
    if not code:
        raise ThreadsAuthError("認可コードを取得できませんでした")
    # Threadsの認可コードは末尾に #_ が付いて返ることがある
    code = code.split("#")[0]

    short = _exchange_code(settings, code)
    long_lived = _exchange_long_lived(settings, short["access_token"])

    token = Token(
        platform="threads",
        access_token=long_lived["access_token"],
        expires_at=Token.expiry_from_seconds(long_lived.get("expires_in")),
        scope=",".join(SCOPES),
        account_id=str(short.get("user_id", "")),
    )
    profile = _fetch_profile(settings, token.access_token)
    token.account_name = profile.get("username", "")
    token.account_id = token.account_id or str(profile.get("id", ""))
    store.save(token)
    return token


def refresh(settings: Settings, store: TokenStore, token: Token) -> Token:
    """長期トークンを延長する（24時間以上経過していれば可能）。"""
    response = requests.get(
        f"{graph_host(settings)}/refresh_access_token",
        params={"grant_type": "th_refresh_token", "access_token": token.access_token},
        timeout=TIMEOUT,
    )
    data = _json(response)
    token.access_token = data.get("access_token", token.access_token)
    token.expires_at = Token.expiry_from_seconds(data.get("expires_in"))
    store.save(token)
    return token


def ensure_token(settings: Settings, store: TokenStore) -> Token:
    token = store.load("threads")
    if token is None:
        raise ThreadsAuthError("Threadsが未接続です。先に `connect threads` を実行してください")
    if token.is_expired():
        token = refresh(settings, store, token)
    return token


def resolve_user_id(settings: Settings, token: Token) -> str:
    """投稿先のThreadsユーザーID。"""
    if settings.threads_user_id:
        return settings.threads_user_id
    if token.account_id:
        return token.account_id
    profile = _fetch_profile(settings, token.access_token)
    return str(profile.get("id", ""))


# ----------------------------------------------------------------------
def _exchange_code(settings: Settings, code: str) -> dict:
    response = requests.post(
        f"{graph_host(settings)}/oauth/access_token",
        data={
            "client_id": settings.threads_app_id,
            "client_secret": settings.threads_app_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": settings.threads_redirect_uri,
        },
        timeout=TIMEOUT,
    )
    return _json(response)


def _exchange_long_lived(settings: Settings, short_token: str) -> dict:
    response = requests.get(
        f"{graph_host(settings)}/access_token",
        params={
            "grant_type": "th_exchange_token",
            "client_secret": settings.threads_app_secret,
            "access_token": short_token,
        },
        timeout=TIMEOUT,
    )
    return _json(response)


def _fetch_profile(settings: Settings, access_token: str) -> dict:
    try:
        response = requests.get(
            f"{api_base(settings)}/me",
            params={"fields": "id,username", "access_token": access_token},
            timeout=TIMEOUT,
        )
        data = response.json()
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


# エラーコードごとの対処（利用者がすぐ動けるように具体的に書く）
ERROR_HINTS = {
    1349245: (
        "Threadsアプリでテスターの招待が承認されていません。\n"
        "  1. Threadsアプリを開く（またはブラウザで https://www.threads.net/ ）\n"
        "  2. 設定 → アカウント → ウェブサイトの許可（Website permissions）\n"
        "  3. 招待（Invites）から、このアプリの招待を承認する\n"
        "  4. 承認後にもう一度このコマンドを実行してください"
    ),
    1349048: "このThreadsアカウントではAPIの利用が許可されていません",
}


def _json(response: requests.Response) -> dict:
    try:
        data = response.json()
    except ValueError as exc:
        raise ThreadsAuthError(f"応答を解釈できません（HTTP {response.status_code}）") from exc

    # Threads固有のエラー形式 {"error_message": ..., "error_code": ...}
    if isinstance(data, dict) and (data.get("error_message") or data.get("error_code")):
        code = data.get("error_code")
        hint = ERROR_HINTS.get(code)
        if hint:
            raise ThreadsAuthError(hint + f"\n  （Threads error_code={code}）")
        raise ThreadsAuthError(
            f"Threadsエラー（error_code={code}）: {data.get('error_message', '')}"
        )

    if isinstance(data, dict) and data.get("error"):
        error = data["error"]
        message = error.get("message") if isinstance(error, dict) else str(error)
        code = error.get("code") if isinstance(error, dict) else None
        hint = ERROR_HINTS.get(code)
        if hint:
            raise ThreadsAuthError(hint + f"\n  （Threads code={code}）")
        raise ThreadsAuthError(f"Threads APIエラー: {message}")
    if not data.get("access_token"):
        raise ThreadsAuthError(
            f"アクセストークンを取得できませんでした（HTTP {response.status_code}）"
        )
    return data


def _is_loopback(redirect_uri: str) -> bool:
    host = urllib.parse.urlparse(redirect_uri).hostname or ""
    return host in ("127.0.0.1", "localhost")


def _manual_prompt(url: str) -> dict:
    """HTTPSのリダイレクトURIしか登録できない場合の手動モード。"""
    print("\n--- Threads 認証（手動モード） ---")
    print("1. 次のURLをブラウザで開いて許可してください:\n")
    print(url)
    print("\n2. 許可後にリダイレクトされたURL全体をコピーして貼り付けてください。")
    print("   （例: https://example.com/?code=AQB...#_ ）\n")
    raw = input("リダイレクト先URL: ").strip()
    if not raw:
        raise ThreadsAuthError("URLが入力されませんでした")
    parsed = urllib.parse.urlparse(raw)
    params = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
    if not params:
        raise ThreadsAuthError("URLから code を読み取れませんでした")
    return params
