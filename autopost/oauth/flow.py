"""ローカルでOAuthコールバックを受け取る小さなHTTPサーバ。

ブラウザで認可 → リダイレクトされた code をこのサーバが受け取り、
アクセストークンへ交換する。Computer Use やブラウザ自動操作は使わない。
"""

from __future__ import annotations

import secrets
import threading
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

PAGE = """<!doctype html><html lang="ja"><head><meta charset="utf-8">
<title>認証完了</title></head><body style="font-family:sans-serif;padding:40px">
<h2>{title}</h2><p>{body}</p><p>このタブは閉じて構いません。</p></body></html>"""


class _Handler(BaseHTTPRequestHandler):
    result: dict = {}

    def do_GET(self) -> None:  # noqa: N802 (http.server の規約)
        parsed = urllib.parse.urlparse(self.path)
        params = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
        _Handler.result.update(params)
        ok = "code" in params
        html = PAGE.format(
            title="認証に成功しました" if ok else "認証に失敗しました",
            body="アプリの画面に戻ってください。" if ok else params.get("error_description", "コードを取得できませんでした。"),
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(html.encode("utf-8"))
        threading.Thread(target=self.server.shutdown, daemon=True).start()

    def log_message(self, *args) -> None:      # アクセスログを出さない（codeが残るため）
        return


def new_state() -> str:
    return secrets.token_urlsafe(24)


def wait_for_code(redirect_uri: str, authorize_url: str, timeout: int = 300) -> dict:
    """ブラウザを開き、リダイレクトされたクエリパラメータを返す。"""
    parsed = urllib.parse.urlparse(redirect_uri)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 80

    _Handler.result = {}
    server = HTTPServer((host, port), _Handler)
    server.timeout = timeout

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    webbrowser.open(authorize_url)
    thread.join(timeout)
    server.server_close()
    return dict(_Handler.result)
