"""ローカルHTTPサーバでOAuthコールバックを受け取る。

ブラウザで認可 → 127.0.0.1 のこのサーバへリダイレクト → code を受け取る、という
デスクトップアプリ向けの標準的な流れ。Computer Use やブラウザ自動操作は使わない。

TikTok Desktop のリダイレクトURI要件（公式）:
  - http または https
  - ホストは localhost か 127.0.0.1 のみ
  - ポート番号が必要
  - パラメータやフラグメントを付けない固定URI
"""

from __future__ import annotations

import secrets
import threading
import urllib.parse
import webbrowser
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer

PAGE = """<!doctype html><html lang="ja"><head><meta charset="utf-8">
<title>{title}</title></head><body style="font-family:sans-serif;padding:40px;line-height:1.7">
<h2>{title}</h2><p>{body}</p><p>このタブは閉じて構いません。</p></body></html>"""

LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")


class CallbackError(RuntimeError):
    """コールバックを受け取れなかった場合。"""


@dataclass
class _Capture:
    """受信結果を保持する（スレッド間で共有）。"""

    params: dict = field(default_factory=dict)
    done: threading.Event = field(default_factory=threading.Event)


def _normalize(path: str) -> str:
    """末尾スラッシュの有無を吸収する（/callback と /callback/ を同一視）。"""
    return "/" + path.strip("/")


def _make_handler(expected_path: str, capture: _Capture):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def do_GET(self) -> None:  # noqa: N802 (http.server の規約)
            parsed = urllib.parse.urlparse(self.path)
            if _normalize(parsed.path) != expected_path:
                # favicon.ico など無関係なリクエストではサーバを止めない
                self.send_error(404)
                return

            params = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
            capture.params.update(params)
            ok = "code" in params
            self._respond(
                "認証に成功しました" if ok else "認証に失敗しました",
                "アプリの画面に戻ってください。"
                if ok
                else params.get("error_description") or params.get("error", "コードを取得できませんでした。"),
            )
            capture.done.set()
            threading.Thread(target=self.server.shutdown, daemon=True).start()

        def _respond(self, title: str, body: str) -> None:
            html = PAGE.format(title=title, body=body).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)

        def log_message(self, *args) -> None:
            # アクセスログにはcodeが残るため出力しない
            return

    return Handler


def new_state() -> str:
    return secrets.token_urlsafe(24)


def validate_redirect_uri(redirect_uri: str) -> urllib.parse.ParseResult:
    """デスクトップ用リダイレクトURIとして妥当かを確認する。"""
    parsed = urllib.parse.urlparse(redirect_uri)
    if parsed.scheme not in ("http", "https"):
        raise CallbackError(f"リダイレクトURIは http か https である必要があります: {redirect_uri}")
    if (parsed.hostname or "") not in LOOPBACK_HOSTS:
        raise CallbackError(
            f"リダイレクトURIのホストは 127.0.0.1 か localhost にしてください: {redirect_uri}"
        )
    if not parsed.port:
        raise CallbackError(f"リダイレクトURIにポート番号が必要です: {redirect_uri}")
    if parsed.query or parsed.fragment:
        raise CallbackError(
            f"リダイレクトURIにクエリやフラグメントを付けられません: {redirect_uri}"
        )
    return parsed


def wait_for_code(
    redirect_uri: str,
    authorize_url: str,
    timeout: int = 300,
    open_browser: bool = True,
) -> dict:
    """ブラウザを開き、リダイレクトされたクエリパラメータを返す。"""
    parsed = validate_redirect_uri(redirect_uri)
    host = parsed.hostname or "127.0.0.1"
    bind_host = "127.0.0.1" if host in ("localhost", "127.0.0.1") else host
    port = parsed.port
    expected_path = _normalize(parsed.path or "/")

    capture = _Capture()
    try:
        server = HTTPServer((bind_host, port), _make_handler(expected_path, capture))
    except OSError as exc:
        raise CallbackError(
            f"ポート {port} を使用できません（他のアプリが使用中の可能性があります）: {exc}"
        ) from exc

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        if open_browser:
            webbrowser.open(authorize_url)
        capture.done.wait(timeout)
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()

    if not capture.params:
        raise CallbackError(
            f"{timeout}秒以内にコールバックを受信できませんでした。"
            "ブラウザで認可を完了したか、リダイレクトURIの登録内容を確認してください"
        )
    return dict(capture.params)


# ----------------------------------------------------------------------
# 認可画面が返すエラーの読み取り
# ----------------------------------------------------------------------
AUTHORIZE_HINTS = {
    "1349168": (
        "リダイレクトURIがアプリに登録されていません。\n"
        "  Metaの管理画面で、次の値を**そのまま**「リダイレクトコールバックURL」\n"
        "  （有効なOAuthリダイレクトURI）に追加して保存してください:\n"
        "\n"
        "      {redirect_uri}\n"
        "\n"
        "  手で打ち直さず、コピーして貼ってください（末尾のスラッシュまで一致が必要）。\n"
        "  保存できない場合は、削除／アンインストールのコールバックURL欄が\n"
        "  原因のことがあります（空にするか、所有しているドメインを入れてください）。"
    ),
    "1349245": (
        "テスターの招待を承認していません。\n"
        "  Threadsアプリ → 設定 → アカウント → ウェブサイトの許可 → 招待 から承認してください。"
    ),
    "1": (
        "アプリの設定が原因のことが多いエラーです。次を確認してください:\n"
        "  ・client_id が Meta App ID ではなく、Threads App ID / Instagram App ID か\n"
        "  ・要求しているスコープがそのAPIに存在するか\n"
        "  ・自分がテスターとして承認済みか"
    ),
}


def authorize_error(params: dict, redirect_uri: str = "") -> str | None:
    """認可画面から返ってきたエラーを、対処できる文章にして返す。

    Meta系は error / error_message / error_code と形が揺れるので全部見る。
    """
    message = (
        params.get("error_message")
        or params.get("error_description")
        or params.get("error")
        or ""
    )
    code = str(params.get("error_code") or "")
    if not message and not code:
        return None

    text = f"認可されませんでした: {message}".strip()
    if code:
        text += f"（code {code}）"
    hint = AUTHORIZE_HINTS.get(code)
    if hint:
        text += "\n\n" + hint.format(redirect_uri=redirect_uri or "（.env の設定値）")
    return text
