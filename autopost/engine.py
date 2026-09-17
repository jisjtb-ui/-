"""実験エンジン：キューを処理し、結果を実験DBへ記録する。

  ready_to_publish → publishing → published / failed / manual_required

ログは experiment_id 単位で残るため、
「なぜ投稿されなかったのか」を後から1本の流れとして追える。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from .config import Settings
from .experiments import (
    Experiment,
    ExperimentStore,
    MANUAL_REQUIRED,
    Metrics,
    Publication,
    PUBLISHED,
)
from .oauth.store import TokenStore
from .publishers import (
    ManualRequired,
    NotSupported,
    PermanentError,
    TransientError,
    get_publisher,
)


@dataclass
class EngineReport:
    """1回の実行結果。"""

    published: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    manual: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"公開 {len(self.published)}件 / 失敗 {len(self.failed)}件 /"
            f" 手動対応 {len(self.manual)}件 / スキップ {len(self.skipped)}件"
        )


class ExperimentEngine:
    """配信と反応データ収集をまとめて扱う。"""

    def __init__(
        self,
        settings: Settings,
        store: ExperimentStore,
        log: Callable[[str], None] = print,
    ) -> None:
        self.settings = settings
        self.store = store
        self.log = log
        self.tokens = TokenStore(settings.token_dir)
        self._publishers: dict[str, object] = {}

    # ------------------------------------------------------------------
    def publisher(self, platform: str):
        if platform not in self._publishers:
            self._publishers[platform] = get_publisher(platform, self.settings, self.tokens)
        return self._publishers[platform]

    # ------------------------------------------------------------------
    def publish(self, publication: Publication) -> bool:
        """1件の配信を実行する。成功したら True。"""
        experiment = self.store.get(publication.experiment_id)
        if experiment is None:
            self.store.mark_failed(publication.id, "実験データが見つかりません")
            return False

        label = f"[{experiment.experiment_id}] {publication.platform}"
        if not self.store.claim(publication):
            self.log(f"{label}: 他のプロセスが処理中のためスキップ")
            return False

        def event(message: str, level: str = "info") -> None:
            self.store.log(experiment.experiment_id, message, publication.platform, level)
            self.log(f"  {label}: {message}")

        if not experiment.image_url:
            self.store.mark_failed(publication.id, "画像の公開URLがありません")
            event("画像の公開URLがないため配信できません", "error")
            return False

        try:
            publisher = self.publisher(publication.platform)
        except ValueError as exc:
            self.store.mark_failed(publication.id, str(exc))
            event(str(exc), "error")
            return False

        event("配信を開始します")
        try:
            result = publisher.publish_content(experiment, experiment.image_url, event)
        except ManualRequired as exc:
            self.store.mark_failed(publication.id, str(exc), manual=True)
            event(f"手動対応が必要: {exc}", "warn")
            return False
        except (NotSupported, PermanentError) as exc:
            self.store.mark_failed(publication.id, str(exc))
            event(f"失敗（再試行しません）: {exc}", "error")
            return False
        except TransientError as exc:
            self.store.mark_failed(publication.id, str(exc))
            event(f"一時的な失敗（再試行できます）: {exc}", "warn")
            return False
        except Exception as exc:                       # 想定外でも記録は残す
            self.store.mark_failed(publication.id, f"予期しないエラー: {exc}")
            event(f"予期しないエラー: {exc}", "error")
            return False

        self.store.mark_published(
            publication.id, result.platform_post_id, result.external_url, result.extra
        )
        for note in result.notes:
            event(note)
        event(f"公開完了 ID={result.platform_post_id} {result.external_url}".strip())
        return True

    def run_pending(self, platform: str | None = None, limit: int | None = None) -> EngineReport:
        """公開待ちのキューを処理する。"""
        report = EngineReport()
        pending = self.store.runnable(platform)
        if limit:
            pending = pending[:limit]
        for publication in pending:
            label = f"{publication.experiment_id}/{publication.platform}"
            if self.publish(publication):
                report.published.append(label)
                continue
            current = self.store.publication(publication.experiment_id, publication.platform)
            if current and current.status == MANUAL_REQUIRED:
                report.manual.append(label)
            elif current and current.status == PUBLISHED:
                report.skipped.append(label)
            else:
                report.failed.append(label)
        return report

    # ------------------------------------------------------------------
    def collect_analytics(
        self, experiment_id: str | None = None, platform: str | None = None,
        start_date: str = "", end_date: str = "",
    ) -> list[Metrics]:
        """公開済みの配信から反応データを集める。"""
        collected: list[Metrics] = []
        publications = [
            p for p in self.store.publications(experiment_id, PUBLISHED, platform)
            if p.external_post_id
        ]
        for publication in publications:
            label = f"[{publication.experiment_id}] {publication.platform}"
            try:
                publisher = self.publisher(publication.platform)
                result = publisher.get_analytics(
                    publication.external_post_id, start_date, end_date
                )
            except NotSupported as exc:
                self.store.log(publication.experiment_id, str(exc), publication.platform, "warn")
                self.log(f"  {label}: {exc}")
                continue
            except (PermanentError, TransientError) as exc:
                self.store.log(
                    publication.experiment_id,
                    f"反応データを取得できません: {exc}", publication.platform, "warn",
                )
                self.log(f"  {label}: 取得失敗 {exc}")
                continue

            metrics = Metrics(
                experiment_id=publication.experiment_id,
                platform=publication.platform,
                period_start=result.period_start,
                period_end=result.period_end,
                impressions=result.impressions,
                views=result.views,
                likes=result.likes,
                comments=result.comments,
                shares=result.shares,
                saves=result.saves,
                clicks=result.clicks,
                followers_gained=result.followers_gained,
                platform_metrics=result.platform_metrics,
            )
            self.store.save_metrics(metrics)
            collected.append(metrics)
            self.log(f"  {label}: {metrics.summary()}")
        return collected


# ----------------------------------------------------------------------
def create_experiment(
    store: ExperimentStore,
    platforms: list[str],
    *,
    hypothesis: str = "",
    content_category: str = "",
    hook: str = "",
    text: str = "",
    image_prompt: str = "",
    image_url: str = "",
    link: str = "",
    source_post_id: str = "",
    source_folder: str = "",
    tags: list[str] | None = None,
    extra: dict | None = None,
) -> Experiment:
    """実験を1件登録する（投稿ではなく「仮説の検証」として記録する）。"""
    experiment = Experiment(
        experiment_id=store.next_experiment_id(datetime.now()),
        hypothesis=hypothesis,
        content_category=content_category,
        hook=hook,
        text=text,
        image_prompt=image_prompt,
        image_url=image_url,
        link=link,
        source_post_id=source_post_id,
        source_folder=source_folder,
        tags=tags or [],
        extra=extra or {},
    )
    return store.create(experiment, platforms)
