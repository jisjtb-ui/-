"""Instagram / Meta OAuth。

2通りのログイン方式に対応する（.env の META_LOGIN_MODE）:

  instagram : Instagram Login（Instagramアカウントで直接認可）
              scope = instagram_business_basic, instagram_business_content_publish,
                      instagram_business_manage_insights
              APIホスト = graph.instagram.com
  facebook  : Facebook Login for Business（FacebookページとIGプロアカウント連携）
              scope = instagram_basic, instagram_content_publish,
                      instagram_manage_insights, pages_read_engagement
              APIホスト = graph.facebook.com

いずれも長期トークン（約60日）へ交換し、期限前に自動更新する。
"""

from __future__ import annotations

import urllib.parse

import requests

from ..config import Settings
from .flow import new_state, wait_for_code
from .store import Token, TokenStore

TIMEOUT = 30

IG_AUTHORIZE_URL = "https://www.instagram.com/oauth/authorize"
IG_TOKEN_URL = "https://api.instagram.com/oauth/access_token"
IG_GRAPH = "https://graph.instagram.com"
# instagram_business_manage_insights が無いと反応データを取得できない
IG_SCOPES = (
    "instagram_business_basic",
    "instagram_business_content_publish",
    "instagram_business_manage_insights",
)

FB_AUTHORIZE_URL = "https://www.facebook.com/{version}/dialog/oauth"
FB_GRAPH = "https://graph.facebook.com"
FB_SCOPES = (
    "instagram_basic",
    "instagram_content_publish",
    "instagram_manage_insights",
    "pages_show_list",
    "pages_read_engagement",
)


class MetaAuthError(RuntimeError):
    pass


def graph_base(settings: Settings) -> str:
    """投稿APIのベースURL（ログイン方式で変わる）。"""
    host = IG_GRAPH if settings.meta_login_mode == "instagram" else FB_GRAPH
    return f"{host}/{settings.meta_graph_version}"


def authorize_url(settings: Settings, state: str) -> str:
    if settings.meta_login_mode == "instagram":
        params = {
            "client_id": settings.meta_app_id,
            "redirect_uri": settings.meta_redirect_uri,
            "response_type": "code",
            "scope": ",".join(IG_SCOPES),
            "state": state,
        }
        return IG_AUTHORIZE_URL + "?" + urllib.parse.urlencode(params)
    params = {
        "client_id": settings.meta_app_id,
        "redirect_uri": settings.meta_redirect_uri,
        "response_type": "code",
        "scope": ",".join(FB_SCOPES),
        "state": state,
    }
    url = FB_AUTHORIZE_URL.format(version=settings.meta_graph_version)
    return url + "?" + urllib.parse.urlencode(params)


def _is_loopback(redirect_uri: str) -> bool:
    host = urllib.parse.urlparse(redirect_uri).hostname or ""
    return host in ("127.0.0.1", "localhost")


def _manual_prompt(url: str) -> dict:
    """HTTPSのリダイレクトURIしか登録できない場合の手動モード。

    Meta は http:// のリダイレクトURIを受け付けないことがある。その場合
    ループバックで待ち受けられないので、リダイレクト先のURLを貼ってもらう。
    """
    print("\n--- Instagram 認証（手動モード） ---")
    print("1. 次のURLをブラウザで開いて許可してください:\n")
    print(url)
    print("\n2. 許可後にリダイレクトされたURL全体をコピーして貼り付けてください。")
    print("   ページが表示されなくても、アドレス欄のURLをそのままコピーすれば大丈夫です。")
    print("   （例: https://example.com/?code=AQB... ）\n")
    raw = input("リダイレクト先URL: ").strip()
    if not raw:
        raise MetaAuthError("URLが入力されませんでした")
    parsed = urllib.parse.urlparse(raw)
    params = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
    if not params:
        raise MetaAuthError("URLから code を読み取れませんでした")
    return params


def connect(settings: Settings, store: TokenStore, manual: bool = False) -> Token:
    """ブラウザで認可し、長期トークンを保存する。"""
    if not (settings.meta_app_id and settings.meta_app_secret and settings.meta_redirect_uri):
        raise MetaAuthError(
            ".env の META_APP_ID / META_APP_SECRET / META_REDIRECT_URI を設定してください"
        )
    state = new_state()
    url = authorize_url(settings, state)

    # HTTPSのリダイレクトURIではループバックで待ち受けられないため手動にする
    if manual or not _is_loopback(settings.meta_redirect_uri):
        params = _manual_prompt(url)
    else:
        params = wait_for_code(settings.meta_redirect_uri, url)
    if params.get("error"):
        raise MetaAuthError(
            params.get("error_description") or f"認可されませんでした（{params['error']}）"
        )
    if params.get("state") != state:
        raise MetaAuthError("stateが一致しません（認証をやり直してください）")
    code = params.get("code")
    if not code:
        raise MetaAuthError("認可コードを取得できませんでした")

    if settings.meta_login_mode == "instagram":
        short = _ig_exchange_code(settings, code)
        long_lived = _ig_long_lived(settings, short["access_token"])
        token = Token(
            platform="instagram",
            access_token=long_lived["access_token"],
            expires_at=Token.expiry_from_seconds(long_lived.get("expires_in")),
            scope=",".join(IG_SCOPES),
            account_id=str(short.get("user_id", settings.instagram_account_id)),
        )
        profile = _ig_profile(settings, token.access_token)
        token.account_name = profile.get("username", "")
        token.account_id = str(profile.get("user_id") or profile.get("id") or token.account_id)
    else:
        short = _fb_exchange_code(settings, code)
        long_lived = _fb_long_lived(settings, short["access_token"])
        token = Token(
            platform="instagram",
            access_token=long_lived["access_token"],
            expires_at=Token.expiry_from_seconds(long_lived.get("expires_in")),
            scope=",".join(FB_SCOPES),
            account_id=settings.instagram_account_id,
        )
    token.extra = {"login_mode": settings.meta_login_mode}
    store.save(token)
    return token


def refresh(settings: Settings, store: TokenStore, token: Token) -> Token:
    """長期トークンを延長する（Instagram Login のみ自動更新に対応）。"""
    if settings.meta_login_mode != "instagram":
        raise MetaAuthError(
            "Facebook Login の長期トークンは自動更新できません。再認証してください"
        )
    response = requests.get(
        f"{IG_GRAPH}/refresh_access_token",
        params={"grant_type": "ig_refresh_token", "access_token": token.access_token},
        timeout=TIMEOUT,
    )
    data = _json(response)
    token.access_token = data.get("access_token", token.access_token)
    token.expires_at = Token.expiry_from_seconds(data.get("expires_in"))
    store.save(token)
    return token


def ensure_token(settings: Settings, store: TokenStore) -> Token:
    token = store.load("instagram")
    if token is None:
        raise MetaAuthError("Instagramが未接続です。先にOAuth認証を実行してください")
    if token.is_expired():
        token = refresh(settings, store, token)
    return token


# ----------------------------------------------------------------------
def _ig_exchange_code(settings: Settings, code: str) -> dict:
    response = requests.post(
        IG_TOKEN_URL,
        data={
            "client_id": settings.meta_app_id,
            "client_secret": settings.meta_app_secret,
            "grant_type": "authorization_code",
            "redirect_uri": settings.meta_redirect_uri,
            "code": code,
        },
        timeout=TIMEOUT,
    )
    return _json(response)


def _ig_long_lived(settings: Settings, short_token: str) -> dict:
    response = requests.get(
        f"{IG_GRAPH}/access_token",
        params={
            "grant_type": "ig_exchange_token",
            "client_secret": settings.meta_app_secret,
            "access_token": short_token,
        },
        timeout=TIMEOUT,
    )
    return _json(response)


def _fb_exchange_code(settings: Settings, code: str) -> dict:
    response = requests.get(
        f"{FB_GRAPH}/{settings.meta_graph_version}/oauth/access_token",
        params={
            "client_id": settings.meta_app_id,
            "client_secret": settings.meta_app_secret,
            "redirect_uri": settings.meta_redirect_uri,
            "code": code,
        },
        timeout=TIMEOUT,
    )
    return _json(response)


def _fb_long_lived(settings: Settings, short_token: str) -> dict:
    response = requests.get(
        f"{FB_GRAPH}/{settings.meta_graph_version}/oauth/access_token",
        params={
            "grant_type": "fb_exchange_token",
            "client_id": settings.meta_app_id,
            "client_secret": settings.meta_app_secret,
            "fb_exchange_token": short_token,
        },
        timeout=TIMEOUT,
    )
    return _json(response)


def _ig_profile(settings: Settings, access_token: str) -> dict:
    """接続中アカウントのユーザー名とIDを取得する（表示と自動設定に使う）。"""
    try:
        response = requests.get(
            f"{IG_GRAPH}/{settings.meta_graph_version}/me",
            params={"fields": "user_id,username", "access_token": access_token},
            timeout=TIMEOUT,
        )
        data = response.json()
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _json(response: requests.Response) -> dict:
    try:
        data = response.json()
    except ValueError as exc:
        raise MetaAuthError(f"応答を解釈できません（HTTP {response.status_code}）") from exc
    if isinstance(data, dict) and data.get("error"):
        error = data["error"]
        message = error.get("message") if isinstance(error, dict) else str(error)
        raise MetaAuthError(f"Meta APIエラー: {message}")
    if not data.get("access_token"):
        raise MetaAuthError(f"アクセストークンを取得できませんでした（HTTP {response.status_code}）")
    return data
