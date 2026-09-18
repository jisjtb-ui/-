"""Instagram Reel（縦動画）を「予約して手渡す」ための処理。

APIから投稿したReelにはInstagramの音楽ライブラリを使えない。
音は本人がアプリで付けるため、このチャネルは**自動投稿しない**。

  予約時刻になる → 動画を書き出す → reels_ready/ に置く → 手動対応として記録

利用者は出来上がったMP4をスマホへ移し、Instagramアプリで音源を付けて投稿する。
投稿後に `autopost.py reel posted` で紐付けると、以降は通常どおり
反応データ（Analytics）を集められる。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Callable

from ..models import PostBundle
from ..version import APP_ROOT
from .base import AnalyticsResult, ManualRequired, NotSupported, PublishResult, Publisher

LogFn = Callable[[str], None]

PLATFORM = "instagram_reel"


class ReelPublisher(Publisher):
    """Reelを書き出して手渡すところまでを担当する。API投稿は行わない。"""

    name = PLATFORM
    requires_image_url = False      # 公開URLではなくローカルの画像から作る

    def __init__(self, settings, store) -> None:
        self.settings = settings
        self.store = store

    # ------------------------------------------------------------------
    def preflight(self) -> None:
        from night_test.video import ffmpeg_path

        ffmpeg_path()               # 見つからなければ分かる言葉で例外になる

    def account_label(self) -> str:
        return "手動投稿（音源は自分で付ける）"

    # ------------------------------------------------------------------
    def output_dir(self, post_id: str) -> Path:
        base = Path(self.settings.reel_output_dir)
        if not base.is_absolute():
            base = APP_ROOT / base
        return base / (post_id or "post")

    def _source_folder(self, experiment) -> Path:
        folder = Path(experiment.source_folder or "")
        if not folder.is_absolute():
            folder = APP_ROOT / folder
        if not folder.is_dir():
            raise ManualRequired(
                f"元の画像フォルダが見つかりません: {experiment.source_folder}"
            )
        return folder

    # ------------------------------------------------------------------
    def build(self, experiment, log: LogFn = lambda message: None) -> Path:
        """動画とキャプションを書き出し、置いた場所を返す。"""
        from night_test.video import ReelSpec, build_reel

        source = self._source_folder(experiment)
        destination = self.output_dir(experiment.source_post_id)
        destination.mkdir(parents=True, exist_ok=True)

        spec = ReelSpec(
            seconds_question=self.settings.reel_seconds_question,
            seconds_answer=self.settings.reel_seconds_answer,
            lead_in=self.settings.reel_lead_in_seconds,
            tail=self.settings.reel_tail_seconds,
        )
        video = build_reel(source, destination / "reel.mp4", spec, log)

        from .instagram import _compose_caption

        caption = _compose_caption(experiment)
        (destination / "caption.txt").write_text(caption, encoding="utf-8")
        (destination / "meta.json").write_text(
            json.dumps(
                {
                    "experiment_id": experiment.experiment_id,
                    "post_id": experiment.source_post_id,
                    "platform": PLATFORM,
                    "video": video.name,
                    "seconds": round(
                        spec.total_seconds([p.name for p in sorted(source.iterdir())
                                            if p.suffix.lower() in (".png", ".jpg")]), 1
                    ),
                    "audio": "なし（Instagramアプリで付けてください）",
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return destination

    # ------------------------------------------------------------------
    def publish_content(
        self,
        experiment,
        image_url: str = "",
        log: LogFn = lambda message: None,
    ) -> PublishResult:
        """予約時刻に呼ばれる。投稿はせず、手渡せる状態にして止める。"""
        self.preflight()
        destination = self.build(experiment, log)

        raise ManualRequired(
            f"Reelの準備ができました: {destination}\n"
            "  1. reel.mp4 をスマホへ移す\n"
            "  2. Instagramアプリでリールとして開き、音源を付ける\n"
            "  3. caption.txt の文章を貼り付けて投稿\n"
            f"  4. 投稿後: python autopost.py reel posted {experiment.experiment_id} "
            "--url <投稿URL>"
        )

    def publish(self, bundle: PostBundle, image_urls, content, log=lambda m: None):
        raise NotSupported("Reelは自動投稿しません（音源を手で付けるため）")

    # ------------------------------------------------------------------
    def get_post_status(self, external_post_id: str) -> str:
        from .instagram import InstagramPublisher

        return InstagramPublisher(self.settings, self.store).get_post_status(
            external_post_id
        )

    def get_analytics(self, external_post_id: str, log: LogFn = lambda m: None
                      ) -> AnalyticsResult:
        """手動で投稿したReelも、紐付けた後は通常どおり数値を取れる。"""
        from .instagram import InstagramPublisher

        return InstagramPublisher(self.settings, self.store).get_analytics(
            external_post_id, log
        )


def cleanup(post_id: str, settings) -> None:
    """投稿し終えた書き出しを片付ける。"""
    base = Path(settings.reel_output_dir)
    if not base.is_absolute():
        base = APP_ROOT / base
    shutil.rmtree(base / post_id, ignore_errors=True)
