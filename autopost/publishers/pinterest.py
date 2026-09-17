"""Pinterest Publisher（API v5）。

公式仕様（OpenAPI 5.28.0 で確認）:
  Pin作成   POST /v5/pins
            body: board_id, media_source{source_type:"image_url", url}, title, description, link, alt_text
            scope: boards:read, boards:write, pins:read, pins:write
  Pin取得   GET  /v5/pins/{pin_id}
  分析      GET  /v5/pins/{pin_id}/analytics?start_date&end_date&metric_types=...
            指標: IMPRESSION, OUTBOUND_CLICK, PIN_CLICK, SAVE, SAVE_RATE,
                  TOTAL_COMMENTS, TOTAL_REACTIONS, USER_FOLLOW, PROFILE_VISIT
  ボード    GET  /v5/boards

上限: title 100 / description 800 / link 2048 / alt_text 500 文字
画像は公開HTTPS URL（Cloudflare Pages など）を指定する。
"""

from __future__ import annotations

from typing import Callable

from ..config import Settings
from ..models import (
    PINTEREST_ALT_TEXT_LIMIT,
    PINTEREST_DESCRIPTION_LIMIT,
    PINTEREST_LINK_LIMIT,
    PINTEREST_TITLE_LIMIT,
    PlatformContent,
    PostBundle,
)
from ..oauth import pinterest_oauth
from ..oauth.store import TokenStore
from .base import (
    AnalyticsResult,
    Pacer,
    PermanentError,
    PublishResult,
    Publisher,
    TransientError,
    request_json,
)

PACE_SECONDS = 1.0
DEFAULT_METRICS = (
    "IMPRESSION",
    "PIN_CLICK",
    "OUTBOUND_CLICK",
    "SAVE",
    "SAVE_RATE",
    "TOTAL_COMMENTS",
    "TOTAL_REACTIONS",
    "USER_FOLLOW",
    "PROFILE_VISIT",
)
# 共通指標へのマッピング（Pinterest固有の値は platform_metrics に残す）
METRIC_MAP = {
    "IMPRESSION": "impressions",
    "SAVE": "saves",
    "PIN_CLICK": "clicks",
    "TOTAL_COMMENTS": "comments",
    "TOTAL_REACTIONS": "likes",
    "USER_FOLLOW": "followers_gained",
}


class PinterestPublisher(Publisher):
    name = "pinterest"

    def __init__(self, settings: Settings, store: TokenStore) -> None:
        self.settings = settings
        self.store = store
        self.pacer = Pacer(PACE_SECONDS)
        self.base = pinterest_oauth.api_base(settings)
        self._token = None

    # ------------------------------------------------------------------
    def preflight(self) -> None:
        missing = self.settings.missing("pinterest")
        if missing:
            raise PermanentError(".env の設定が不足しています: " + ", ".join(missing))
        self._token = pinterest_oauth.ensure_token(self.settings, self.store)

    def account_label(self) -> str:
        token = self.store.load("pinterest")
        if token is None:
            return "未接続"
        return token.account_name or "接続済み"

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._token.access_token}",
            "Content-Type": "application/json",
        }

    # ------------------------------------------------------------------
    # 実験単位の配信（このシステムの主経路）
    # ------------------------------------------------------------------
    def publish_content(
        self,
        experiment,
        image_url: str,
        log: Callable[[str], None] = lambda message: None,
    ) -> PublishResult:
        """実験1件をPinとして公開する。"""
        self.preflight()
        board_id = (experiment.extra or {}).get("board_id") or self.settings.pinterest_board_id
        if not board_id:
            raise PermanentError(
                "ボードIDが未設定です（.env の PINTEREST_BOARD_ID、"
                "または `autopost.py pinterest boards` で確認）"
            )
        if not image_url:
            raise PermanentError("画像の公開URLがありません")

        title = _clip(experiment.hook or experiment.text, PINTEREST_TITLE_LIMIT)
        description = _clip(
            "\n".join(x for x in (experiment.text, _tag_line(experiment)) if x),
            PINTEREST_DESCRIPTION_LIMIT,
        )
        link = _clip(experiment.link or self.settings.pinterest_default_link, PINTEREST_LINK_LIMIT)

        payload = {
            "board_id": board_id,
            "media_source": {"source_type": "image_url", "url": image_url},
            "title": title,
            "description": description,
        }
        if link:
            payload["link"] = link
        alt_text = _clip(experiment.image_prompt, PINTEREST_ALT_TEXT_LIMIT)
        if alt_text:
            payload["alt_text"] = alt_text

        log(f"Pin作成を要求します（board={board_id}）")
        self.pacer.wait()
        data = request_json("POST", f"{self.base}/pins", headers=self._headers(), json=payload)
        self._raise_for_error(data)

        pin_id = str(data.get("id", ""))
        if not pin_id:
            raise PermanentError(f"Pin IDを取得できませんでした: {str(data)[:200]}")
        log(f"Pinを公開しました: {pin_id}")
        return PublishResult(
            platform=self.name,
            post_id=experiment.experiment_id,
            platform_post_id=pin_id,
            detail="published",
            external_url=f"https://www.pinterest.com/pin/{pin_id}/",
            extra={"board_id": board_id, "sandbox": self.settings.pinterest_sandbox},
        )

    # ------------------------------------------------------------------
    # フォルダ単位の配信（既存スケジューラからの入口。1枚目をPinにする）
    # ------------------------------------------------------------------
    def publish(
        self,
        bundle: PostBundle,
        image_urls: list[str],
        content: PlatformContent,
        log: Callable[[str], None] = lambda message: None,
    ) -> PublishResult:
        from ..experiments import Experiment

        if not image_urls:
            raise PermanentError("画像URLがありません")
        pseudo = Experiment(
            experiment_id=bundle.post_id,
            hook=content.title or bundle.title,
            text=content.text,
            link=self.settings.pinterest_default_link,
        )
        return self.publish_content(pseudo, image_urls[0], log)

    # ------------------------------------------------------------------
    # 状態と反応データ
    # ------------------------------------------------------------------
    def get_post_status(self, external_post_id: str) -> str:
        self.preflight()
        self.pacer.wait()
        data = request_json(
            "GET", f"{self.base}/pins/{external_post_id}", headers=self._headers()
        )
        self._raise_for_error(data)
        return "published" if data.get("id") else "unknown"

    def get_analytics(
        self, external_post_id: str, start_date: str = "", end_date: str = ""
    ) -> AnalyticsResult:
        """Pinの反応データを共通指標へ正規化して返す。"""
        from ..experiments import utc_date

        self.preflight()
        start_date = start_date or utc_date(30)
        end_date = end_date or utc_date(0)
        params = {
            "start_date": start_date,
            "end_date": end_date,
            "metric_types": ",".join(DEFAULT_METRICS),
        }
        self.pacer.wait()
        data = request_json(
            "GET",
            f"{self.base}/pins/{external_post_id}/analytics",
            headers=self._headers(),
            params=params,
        )
        self._raise_for_error(data)

        totals = _extract_totals(data)
        result = AnalyticsResult(
            platform=self.name,
            external_post_id=external_post_id,
            period_start=start_date,
            period_end=end_date,
            platform_metrics=totals,
        )
        for metric, attribute in METRIC_MAP.items():
            if metric in totals and totals[metric] is not None:
                setattr(result, attribute, int(totals[metric]))
        return result

    # ------------------------------------------------------------------
    def list_boards(self) -> list[dict]:
        """ボード一覧（board_id を確認するため）。"""
        self.preflight()
        self.pacer.wait()
        data = request_json(
            "GET", f"{self.base}/boards", headers=self._headers(), params={"page_size": 50}
        )
        self._raise_for_error(data)
        return [
            {"id": item.get("id", ""), "name": item.get("name", ""),
             "privacy": item.get("privacy", ""), "pin_count": item.get("pin_count")}
            for item in data.get("items", [])
        ]

    # ------------------------------------------------------------------
    def _raise_for_error(self, data: dict) -> None:
        """Pinterestのエラー応答を一時／恒久に振り分ける。"""
        if not isinstance(data, dict):
            return
        code = data.get("code")
        message = data.get("message") or data.get("error_description") or ""
        if code is None and not message:
            return
        if code in (7, 8, 29):            # レート制限・一時的な制限
            raise TransientError(f"Pinterestのレート制限（code={code}）: {message}")
        if code in (2, 3):                # 認証系
            raise TransientError(f"Pinterestの認証エラー（code={code}）: {message}")
        if code is not None:
            raise PermanentError(f"Pinterestエラー（code={code}）: {message}")
        if message:
            raise PermanentError(f"Pinterestエラー: {message}")


def _extract_totals(payload: dict) -> dict:
    """analytics応答から指標の合計値を取り出す（形が変わっても壊れないように）。"""
    if not isinstance(payload, dict):
        return {}
    for key in ("all", "ALL"):
        if isinstance(payload.get(key), dict):
            payload = payload[key]
            break
    summary = payload.get("summary_metrics")
    if isinstance(summary, dict):
        return {k: v for k, v in summary.items() if isinstance(v, (int, float))}
    return {k: v for k, v in payload.items() if isinstance(v, (int, float))}


def _clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _tag_line(experiment) -> str:
    tags = getattr(experiment, "tags", None) or []
    return " ".join(t if t.startswith("#") else f"#{t}" for t in tags)
