"""不具合の切り分け用レポート。

**秘密情報を一切出力しない**ことを最優先に設計している。

- .env の値は出さない。「設定済み / 未設定」だけを出す
- トークン本体は出さない。期限とスコープ名だけを出す
- アカウントIDは末尾4文字だけに縮める
- APIのエラーは、コードとメッセージだけを転記する（トークンを含む
  リクエストURLやヘッダは出さない）

出力はそのまま人に見せて相談できる。
"""

from __future__ import annotations

import platform as platform_mod
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

from .config import Settings
from .experiments import DELIVERED, ExperimentStore
from .models import ALL_PLATFORMS, MANUAL_PLATFORMS
from .hosting import get_host
from .oauth.store import TokenStore

LINE = "-" * 60

# 認証情報の持ち主を確認するだけの読み取り専用エンドポイント
PROBES: dict[str, str] = {
    "threads": "{base}/me?fields=id,username",
    "instagram": "{graph}/me?fields=id,username",
    "tiktok": "https://open.tiktokapis.com/v2/user/info/?fields=open_id,display_name",
    "pinterest": "{pin}/user_account",
}


def _tail(value: str, keep: int = 4) -> str:
    """IDなどを末尾だけに縮める。"""
    if not value:
        return "なし"
    if len(value) <= keep:
        return "…" + value
    return "…" + value[-keep:]


def _env_keys(settings: Settings, platform: str) -> dict[str, str]:
    """そのプラットフォームが使う .env 項目名 → 値（値は呼び出し側で捨てる）。"""
    table = {
        "threads": {
            "THREADS_APP_ID": settings.threads_app_id,
            "THREADS_APP_SECRET": settings.threads_app_secret,
            "THREADS_REDIRECT_URI": settings.threads_redirect_uri,
        },
        "instagram": {
            "META_APP_ID": settings.meta_app_id,
            "META_APP_SECRET": settings.meta_app_secret,
            "META_REDIRECT_URI": settings.meta_redirect_uri,
            "INSTAGRAM_ACCOUNT_ID": settings.instagram_account_id,
        },
        "tiktok": {
            "TIKTOK_CLIENT_KEY": settings.tiktok_client_key,
            "TIKTOK_CLIENT_SECRET": settings.tiktok_client_secret,
            "TIKTOK_REDIRECT_URI": settings.tiktok_redirect_uri,
        },
        "pinterest": {
            "PINTEREST_APP_ID": settings.pinterest_app_id,
            "PINTEREST_APP_SECRET": settings.pinterest_app_secret,
            "PINTEREST_REDIRECT_URI": settings.pinterest_redirect_uri,
        },
    }
    return table.get(platform, {})


def _probe_url(settings: Settings, plat: str) -> str:
    from .oauth import threads_oauth

    template = PROBES[plat]
    return template.format(
        base=threads_oauth.api_base(settings),
        graph=f"https://graph.instagram.com/{settings.meta_graph_version}",
        pin="https://api-sandbox.pinterest.com/v5"
        if settings.pinterest_sandbox
        else "https://api.pinterest.com/v5",
    )


def _probe(settings: Settings, plat: str, access_token: str) -> str:
    """読み取り専用の疎通確認。結果は1行で返す（秘密情報は含めない）。"""
    url = _probe_url(settings, plat)
    try:
        response = requests.get(
            url, headers={"Authorization": f"Bearer {access_token}"}, timeout=20
        )
    except requests.RequestException as exc:
        return f"到達できません（{type(exc).__name__}）"

    try:
        body = response.json()
    except ValueError:
        return f"HTTP {response.status_code} / 応答がJSONではありません"

    if response.status_code == 200:
        data = body.get("data", body)
        if isinstance(data, dict):
            name = data.get("username") or data.get("display_name") or ""
            ident = data.get("id") or data.get("open_id") or ""
            label = f"@{name}" if name else _tail(str(ident))
            return f"OK（{label}）"
        return "OK"

    # エラー形式はプラットフォームごとに違う。両方から拾う。
    error = body.get("error") if isinstance(body.get("error"), dict) else {}
    code = (
        body.get("error_code")
        or error.get("code")
        or error.get("error_code")
        or body.get("code")
        or ""
    )
    message = (
        body.get("error_message")
        or error.get("message")
        or error.get("error_user_msg")
        or body.get("message")
        or ""
    )
    detail = f"{message}".strip() or "（メッセージなし）"
    suffix = f" / code {code}" if code else ""
    return f"HTTP {response.status_code}{suffix} / {detail[:200]}"


def experiments_for_doctor(settings) -> ExperimentStore:
    return ExperimentStore(settings.experiments_db_path)


def _git_revision() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return "不明"
    return out.stdout.strip() or "不明"


def _check_image_url(url: str) -> str:
    """公開URLが本当に見えるかを確認する。ここが落ちていると投稿は必ず失敗する。"""
    try:
        response = requests.head(url, timeout=20, allow_redirects=True)
        if response.status_code == 405:  # HEAD非対応のサーバ
            response = requests.get(url, timeout=20, stream=True)
    except requests.RequestException as exc:
        return f"到達できません（{type(exc).__name__}）"

    kind = response.headers.get("Content-Type", "不明")
    if response.status_code == 200:
        # Cloudflare Pages は存在しないパスにも HTML を 200 で返すことがある。
        # 画像が返ってきていなければ「公開されていない」と同じ意味になる。
        if kind.startswith("image/"):
            return f"OK（{kind}）"
        return (
            f"画像ではありません（{kind}）"
            " / このパスに画像がありません。デプロイが必要です"
        )
    if response.status_code in (403, 404):
        return f"HTTP {response.status_code} / まだ公開されていません（デプロイが必要です）"
    return f"HTTP {response.status_code}"


# ----------------------------------------------------------------------
def build_report(settings: Settings, live: bool = True) -> str:
    """貼り付けて相談できる診断レポートを組み立てる。"""
    out: list[str] = []
    add = out.append

    now = datetime.now(timezone.utc).astimezone()
    add("===== 自動投稿システム 診断レポート =====")
    add("※ このレポートには秘密情報（キー・トークン）は含まれません")
    add("")
    add(f"日時       : {now.strftime('%Y-%m-%d %H:%M:%S %z')}")
    add(f"Python     : {sys.version.split()[0]}")
    add(f"OS         : {platform_mod.system()} {platform_mod.release()}")
    add(f"コード      : {_git_revision()}")
    add(f"タイムゾーン: {settings.timezone}")

    env_path = Path(".env")
    add(f".env       : {'あり' if env_path.exists() else '見つかりません'}")

    store = TokenStore(settings.token_dir)

    add("")
    add(LINE)
    add(" プラットフォームごとの状態")
    add(LINE)
    for plat in ALL_PLATFORMS:
        add("")
        add(f"[{plat}]")

        keys = _env_keys(settings, plat)
        if keys:
            filled = [name for name, value in keys.items() if value]
            empty = [name for name, value in keys.items() if not value]
            add(f"  .env 設定済み : {', '.join(filled) if filled else 'なし'}")
            add(f"  .env 未設定   : {', '.join(empty) if empty else 'なし'}")

        if plat in MANUAL_PLATFORMS:
            add("  認証          : 不要（予約時刻に書き出して手動で投稿する）")
            waiting = [
                pub for pub in experiments_for_doctor(settings).publications(platform=plat)
                if pub.status == "manual_required"
            ]
            add(f"  手渡し待ち    : {len(waiting)}件")
            continue

        token = store.load(plat)
        if token is None or not token.access_token:
            add("  トークン      : 未接続")
            continue

        expiry = token.expires_at[:16].replace("T", " ") if token.expires_at else "期限なし"
        remain = ""
        if token.expires_at:
            try:
                left = datetime.fromisoformat(token.expires_at) - datetime.now(timezone.utc)
                remain = f" / 残り{left.days}日"
            except ValueError:
                pass
        state = "期限切れ" if token.is_expired() else "有効"
        add(f"  トークン      : {state}（期限 {expiry}{remain}）")
        add(f"  アカウント    : {_tail(token.account_id)}")
        add(f"  スコープ      : {token.scope or '不明'}")

        if live:
            add(f"  API疎通       : {_probe(settings, plat, token.access_token)}")

    # ------------------------------------------------------------------
    add("")
    add(LINE)
    add(" 画像ホスティング")
    add(LINE)
    host = get_host(settings)
    add(f"  設定          : {host.describe()}")

    experiments = ExperimentStore(settings.experiments_db_path)
    sample = ""
    for experiment in experiments.list_experiments(limit=40):
        if experiment.image_url:
            sample = experiment.image_url
            break
    if sample:
        add(f"  確認したURL    : {sample}")
        if live:
            add(f"  結果          : {_check_image_url(sample)}")
    else:
        add("  確認したURL    : なし（まだ画像URLが登録されていません）")

    # ------------------------------------------------------------------
    add("")
    add(LINE)
    add(" キュー")
    add(LINE)
    counts = experiments.status_counts()
    if not counts:
        add("  実験なし")
    for status, count in sorted(counts.items()):
        add(f"  {status:<18} {count}件")

    pending_total = sum(
        count for status, count in counts.items() if status not in DELIVERED
    )
    add(f"  配信待ち合計       {pending_total}件")
    add(
        f"  自動補充           {'有効' if settings.auto_topup else '無効'}"
        f"（下限{settings.auto_topup_min}件 / {settings.auto_topup_count}件生成）"
    )
    if settings.auto_topup and not settings.pages_deploy_command:
        add("  ※ PAGES_DEPLOY_COMMAND が未設定です。補充した画像は未公開のままになります")

    add("")
    for plat in ALL_PLATFORMS:
        limit = settings.daily_limit(plat)
        if limit:
            add(f"  本日の配信 {plat:<10} {experiments.published_today(plat)} / {limit}件")

    # ------------------------------------------------------------------
    add("")
    add(LINE)
    add(" 直近の失敗")
    add(LINE)
    failures = [
        publication
        for publication in experiments.publications()
        if publication.status in ("failed", "manual_required")
        # Reelの手渡し待ちは異常ではないので失敗として並べない
        and publication.platform not in MANUAL_PLATFORMS
    ]
    if not failures:
        add("  なし")
    for publication in failures[:10]:
        message = (publication.error_message or "").replace("\n", " ")[:200]
        add(f"  {publication.experiment_id} / {publication.platform}")
        add(f"    {publication.status} : {message or '（メッセージなし）'}")

    add("")
    add("===== ここまで =====")
    return "\n".join(out)


def run(settings: Settings, live: bool = True, output: Path | None = None) -> int:
    report = build_report(settings, live=live)
    print(report)
    target = output or Path("診断結果.txt")
    target.write_text(report + "\n", encoding="utf-8")
    print(f"\nこの内容を {target} に保存しました。")
    print("そのまま貼り付けて相談できます（秘密情報は含まれていません）。")
    return 0
