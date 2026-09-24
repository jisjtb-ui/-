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
from pathlib import Path
from typing import Callable

from ..config import Settings
from ..models import INSTAGRAM_MAX_CAROUSEL, PlatformContent, PostBundle
from ..oauth import meta_oauth
from ..oauth.store import TokenStore
from .base import (
    AnalyticsResult,
    MetricNotSupported,
    Pacer,
    PermanentError,
    PermissionDenied,
    PublishResult,
    Publisher,
    RateLimited,
    TransientError,
    request_json,
)

# 投稿のInsights。2024年7月以降に作成されたメディアでは impressions が views に置き換わったため、
# まず新しい指標で取得し、エラーになったら古い指標へ自動でフォールバックする
MEDIA_METRICS_PRIMARY = ("views", "reach", "likes", "comments", "shares", "saved", "total_interactions")
MEDIA_METRICS_FALLBACK = ("impressions", "reach", "engagement", "saved")
METRIC_MAP = {
    "views": "views",
    "impressions": "impressions",
    "reach": "reach",
    "likes": "likes",
    "comments": "comments",
    "shares": "shares",
    "saved": "saves",
}

PACE_SECONDS = 1.0
STATUS_POLL_SECONDS = 5
STATUS_MAX_POLLS = 36          # 最大3分待つ
PUBLISH_SETTLE_SECONDS = 3

# 再試行しても直らないエラーコード
PERMANENT_SUBCODES = {2207026, 2207003, 2207032, 2207020}
# 権限・スコープ・そのオブジェクトを見る権利が無い
PERMISSION_CODES = {10, 102, 200, 210, 803}
PERMISSION_SUBCODES = {33}


class InstagramPublisher(Publisher):
    name = "instagram"

    def __init__(self, settings: Settings, store: TokenStore) -> None:
        self.settings = settings
        self.store = store
        self.pacer = Pacer(PACE_SECONDS)
        self._token = None
        self._account_id = ""

    # ------------------------------------------------------------------
    def preflight(self) -> None:
        missing = self.settings.missing("instagram")
        if missing:
            raise PermanentError(".env の設定が不足しています: " + ", ".join(missing))
        self._token = meta_oauth.ensure_token(self.settings, self.store)
        self._account_id = (
            self.settings.instagram_account_id or self._token.account_id
        )
        if not self._account_id:
            raise PermanentError(
                "InstagramのアカウントIDを取得できません"
                "（.env の INSTAGRAM_ACCOUNT_ID を設定してください）"
            )

    def account_label(self) -> str:
        token = self.store.load("instagram")
        if token is None:
            return "未接続"
        return token.account_name or token.account_id or self.settings.instagram_account_id or "接続済み"

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
        ig_id = self._account_id

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
    # 実験単位の配信（画像1枚。extra["image_urls"] があればカルーセル）
    # ------------------------------------------------------------------
    def publish_content(
        self,
        experiment,
        image_url: str,
        log: Callable[[str], None] = lambda message: None,
    ) -> PublishResult:
        from ..models import PlatformContent, PostBundle

        image_urls = (experiment.extra or {}).get("image_urls") or ([image_url] if image_url else [])
        if not image_urls:
            raise PermanentError("画像URLがありません")

        caption = _compose_caption(experiment)
        if len(image_urls) == 1:
            return self._publish_single(experiment.experiment_id, image_urls[0], caption, log)

        pseudo = PostBundle(post_id=experiment.experiment_id, folder=Path("."), images=[])
        return self.publish(
            pseudo, image_urls, PlatformContent(caption=caption, hashtags=[]), log
        )

    def _publish_single(
        self, post_id: str, image_url: str, caption: str, log: Callable[[str], None]
    ) -> PublishResult:
        """画像1枚の通常投稿。"""
        self.preflight()
        token = self._token.access_token
        base = meta_oauth.graph_base(self.settings)
        ig_id = self._account_id

        log("画像コンテナを作成しています")
        container_id = self._create_container(
            base, ig_id, token, {"image_url": image_url, "caption": caption}
        )
        self._wait_ready(base, container_id, token, log, "画像")

        log("公開しています")
        self.pacer.wait()
        data = request_json(
            "POST",
            f"{base}/{ig_id}/media_publish",
            data={"creation_id": container_id, "access_token": token},
        )
        self._raise_for_error(data)
        media_id = str(data.get("id", ""))
        if not media_id:
            raise PermanentError(f"公開結果を取得できませんでした: {str(data)[:200]}")
        time.sleep(PUBLISH_SETTLE_SECONDS)
        return PublishResult(
            platform=self.name,
            post_id=post_id,
            platform_post_id=media_id,
            publish_id=container_id,
            detail="PUBLISHED",
            external_url=self._permalink(base, media_id, token),
        )

    # ------------------------------------------------------------------
    def get_post_status(self, external_post_id: str) -> str:
        self.preflight()
        base = meta_oauth.graph_base(self.settings)
        self.pacer.wait()
        data = request_json(
            "GET",
            f"{base}/{external_post_id}",
            params={"fields": "id,timestamp", "access_token": self._token.access_token},
        )
        self._raise_for_error(data)
        return "published" if data.get("id") else "unknown"

    def get_analytics(
        self, external_post_id: str, start_date: str = "", end_date: str = ""
    ) -> AnalyticsResult:
        """投稿のInsightsを共通指標へ正規化して返す。"""
        self.preflight()
        base = meta_oauth.graph_base(self.settings)
        token = self._token.access_token

        raw: dict[str, int] = {}
        metric_errors: list[str] = []
        for metrics in (MEDIA_METRICS_PRIMARY, MEDIA_METRICS_FALLBACK):
            try:
                self.pacer.wait()
                data = request_json(
                    "GET",
                    f"{base}/{external_post_id}/insights",
                    params={"metric": ",".join(metrics), "access_token": token},
                )
                self._raise_for_error(data)
            except MetricNotSupported as exc:
                # 指標名が古い/新しい場合だけ、もう一方の組で試す。
                # 権限不足やID不正は、ここで握りつぶさず呼び出し側へ返す。
                metric_errors.append(str(exc))
                continue
            for item in data.get("data", []):
                name = item.get("name", "")
                values = item.get("values") or []
                value = item.get("total_value", {}).get("value")
                if value is None and values:
                    value = values[0].get("value")
                if isinstance(value, (int, float)):
                    raw[name] = int(value)
            if raw:
                break

        if metric_errors and len(raw) < len(MEDIA_METRICS_PRIMARY):
            # まとめて頼むと、1つでもその投稿形式に無い指標が混ざった時点で
            # 応答全体がエラーになる（views が永久に入らない原因）。
            # まとめて頼んで断られたときだけ、足りない分を1つずつ聞き直す。
            # （断られていないのに毎回聞き直すと、無駄にAPIを叩いてしまう）
            for metric in MEDIA_METRICS_PRIMARY:
                if metric in raw:
                    continue
                value = self._single_metric(base, external_post_id, token, metric)
                if value is not None:
                    raw[metric] = value

        if not raw:
            # 1つも取れていないものを返すと、呼び出し側が「測定済み・全部NULL」
            # として保存してしまう。取得できなかったことを明示する。
            detail = "／".join(metric_errors) or "応答に指標が含まれていません"
            raise PermanentError(f"Insightsを取得できませんでした: {detail}")

        result = AnalyticsResult(
            platform=self.name, external_post_id=external_post_id, platform_metrics=raw
        )
        for metric, attribute in METRIC_MAP.items():
            if metric in raw and getattr(result, attribute) is None:
                setattr(result, attribute, raw[metric])
        return result

    def _single_metric(self, base: str, external_post_id: str, token: str,
                       metric: str) -> int | None:
        """指標を1つだけ聞く。使えない指標なら None を返す（例外にしない）。"""
        try:
            self.pacer.wait()
            data = request_json(
                "GET", f"{base}/{external_post_id}/insights",
                params={"metric": metric, "access_token": token},
            )
            self._raise_for_error(data)
        except (MetricNotSupported, PermanentError):
            return None                 # この投稿形式では使えない指標
        for item in data.get("data", []):
            value = item.get("total_value", {}).get("value")
            values = item.get("values") or []
            if value is None and values:
                value = values[0].get("value")
            if isinstance(value, (int, float)):
                return int(value)
        return None

    def describe_media(self, external_post_id: str) -> dict:
        """投稿の種類を確かめる（カルーセルか、Reelか）。

        ``media_product_type`` が REELS ならReelとして投稿されている。
        本システムのInstagram投稿は CAROUSEL_ALBUM / FEED になる。
        """
        self.preflight()
        base = meta_oauth.graph_base(self.settings)
        self.pacer.wait()
        data = request_json(
            "GET", f"{base}/{external_post_id}",
            params={"fields": "media_type,media_product_type,permalink,timestamp,like_count,comments_count",
                    "access_token": self._token.access_token},
        )
        self._raise_for_error(data)
        return {k: v for k, v in data.items() if k != "id"}

    def _permalink(self, base: str, media_id: str, token: str) -> str:
        try:
            self.pacer.wait()
            data = request_json(
                "GET", f"{base}/{media_id}",
                params={"fields": "permalink", "access_token": token},
            )
            return str(data.get("permalink", ""))
        except Exception:
            return ""

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
            raise RateLimited(f"レート制限（code={code}）: {message}")
        if _looks_like_metric_problem(code, message):
            # 指標名がこの投稿形式・APIバージョンに無いだけ。別の組で試せる
            raise MetricNotSupported(f"使えない指標（code={code}）: {message}")
        if code in (190,):                  # トークン期限切れ・無効（再接続で直る）
            raise TransientError(f"認証エラー（code=190）: {message}")
        if code in PERMISSION_CODES or subcode in PERMISSION_SUBCODES:
            # ここを一時的扱いにすると、権限不足に気づかないまま再試行し続ける
            raise PermissionDenied(f"権限が足りません（code={code}）: {message}")
        if subcode in PERMANENT_SUBCODES:
            raise PermanentError(f"Instagramエラー（subcode={subcode}）: {message}")
        if code in (1, 2):                  # 一時的な内部エラー
            raise TransientError(f"一時的なエラー（code={code}）: {message}")
        raise PermanentError(f"Instagramエラー（code={code}）: {message}")


# 「その指標はこの投稿形式では使えない」を表す言い回し。
# Metaはこれを code=100 の汎用エラーで返すため、文面でしか区別できない。
METRIC_PROBLEM_HINTS = (
    "metric",
    "metrics",
    "not valid for this media",
    "does not support",
)


def _looks_like_metric_problem(code, message: str) -> bool:
    """指標名だけの問題か。権限不足やID不正と混同しないようにする。"""
    if code not in (100,):
        return False
    lowered = (message or "").lower()
    if "permission" in lowered or "access token" in lowered:
        return False
    return any(hint in lowered for hint in METRIC_PROBLEM_HINTS)


def _compose_caption(experiment) -> str:
    """Hook・本文・ハッシュタグをキャプションに組み立てる。"""
    from ..models import INSTAGRAM_CAPTION_LIMIT

    tags = getattr(experiment, "tags", None) or []
    tag_line = " ".join(t if t.startswith("#") else f"#{t}" for t in tags)
    body = (experiment.text or "").strip()
    hook = (experiment.hook or "").strip()
    if hook and not body.startswith(hook):
        body = f"{hook}\n\n{body}".strip()
    caption = "\n\n".join(part for part in (body, tag_line) if part)
    if len(caption) > INSTAGRAM_CAPTION_LIMIT:
        caption = caption[: INSTAGRAM_CAPTION_LIMIT - 1] + "…"
    return caption
