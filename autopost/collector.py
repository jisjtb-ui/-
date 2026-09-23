"""Analytics Collector（投稿処理から分離した反応データ収集）。

投稿直後の数字だけでは「初速」と「伸び」を区別できないため、
投稿からの経過時間ごとにスナップショットを保存する。

  1h → 6h → 24h → 72h

各スナップショットは1回だけ取得し、APIのレート制限に配慮して
「取得すべき時間になったものだけ」を対象にする。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable

from .config import Settings
from .experiments import DELIVERED, ExperimentStore, Metrics, Publication
from .oauth.store import TokenStore
from .publishers import NotSupported, PermanentError, TransientError, get_publisher

# 経過時間（時間）とラベル。取得漏れを防ぐため、少し過ぎていても拾う
SNAPSHOTS: tuple[tuple[str, float], ...] = (
    ("1h", 1),
    ("6h", 6),
    ("24h", 24),
    ("72h", 72),
)
# 予定時刻から何時間までなら「まだ取得してよい」とみなすか
GRACE_HOURS = 6.0


@dataclass
class CollectReport:
    collected: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"取得 {len(self.collected)}件 / 対象外 {len(self.skipped)}件 /"
            f" 失敗 {len(self.failed)}件"
        )


class AnalyticsCollector:
    """プラットフォーム別のCollectorをまとめて扱う。"""

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

    def publisher(self, platform: str):
        if platform not in self._publishers:
            self._publishers[platform] = get_publisher(platform, self.settings, self.tokens)
        return self._publishers[platform]

    # ------------------------------------------------------------------
    def due_snapshots(self, publication: Publication, now: datetime | None = None) -> list[tuple[str, float]]:
        """いま取得すべきスナップショット（未取得かつ時刻を過ぎたもの）。"""
        if not publication.published_at or publication.status not in DELIVERED:
            return []
        now = now or datetime.now().astimezone()
        try:
            published = datetime.fromisoformat(publication.published_at)
        except ValueError:
            return []
        elapsed = (now - published).total_seconds() / 3600
        done = self.store.collected_snapshots(publication.experiment_id, publication.platform)

        due = []
        for label, hours in SNAPSHOTS:
            if label in done:
                continue
            if hours <= elapsed <= hours + GRACE_HOURS:
                due.append((label, elapsed))
            elif elapsed > hours + GRACE_HOURS:
                # 取り逃した区間は、次の区間の取得時にまとめて記録する
                due.append((label, elapsed))
        return due[:1]          # 1回の実行では直近の1区間だけ取る

    def collect_due(
        self, platform: str | None = None, now: datetime | None = None
    ) -> CollectReport:
        """取得時期が来たスナップショットだけを集める。"""
        report = CollectReport()
        for publication in self.store.publications(platform=platform):
            if publication.status not in DELIVERED or not publication.external_post_id:
                continue
            due = self.due_snapshots(publication, now)
            if not due:
                report.skipped.append(f"{publication.experiment_id}/{publication.platform}")
                continue
            label, elapsed = due[0]
            if self._collect_one(publication, label, elapsed):
                report.collected.append(
                    f"{publication.experiment_id}/{publication.platform} [{label}]"
                )
            else:
                report.failed.append(f"{publication.experiment_id}/{publication.platform}")
        return report

    def collect_now(
        self, experiment_id: str | None = None, platform: str | None = None, label: str = "manual"
    ) -> CollectReport:
        """時刻に関係なく、いますぐ取得する。"""
        report = CollectReport()
        for publication in self.store.publications(experiment_id, None, platform):
            if publication.status not in DELIVERED or not publication.external_post_id:
                continue
            if self._collect_one(publication, label, None):
                report.collected.append(f"{publication.experiment_id}/{publication.platform}")
            else:
                report.failed.append(f"{publication.experiment_id}/{publication.platform}")
        return report

    # ------------------------------------------------------------------
    def _collect_one(self, publication: Publication, label: str, elapsed: float | None) -> bool:
        tag = f"[{publication.experiment_id}] {publication.platform}"
        try:
            publisher = self.publisher(publication.platform)
            result = publisher.get_analytics(publication.external_post_id)
        except NotSupported as exc:
            self.store.log(publication.experiment_id, str(exc), publication.platform, "warn")
            self.log(f"  {tag}: {exc}")
            return False
        except (PermanentError, TransientError) as exc:
            self.store.log(
                publication.experiment_id,
                f"反応データを取得できません: {exc}", publication.platform, "warn",
            )
            self.log(f"  {tag}: 取得失敗 {exc}")
            return False

        metrics = Metrics(
            experiment_id=publication.experiment_id,
            platform=publication.platform,
            snapshot=label,
            hours_since_post=round(elapsed, 2) if elapsed is not None else None,
            period_start=result.period_start,
            period_end=result.period_end,
            impressions=result.impressions,
            reach=result.reach,
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
        self.log(f"  {tag}: {metrics.summary()}")
        return True


def next_snapshot_at(published_at: str) -> datetime | None:
    """次に取得すべき時刻（予定表示用）。"""
    try:
        published = datetime.fromisoformat(published_at)
    except (TypeError, ValueError):
        return None
    now = datetime.now().astimezone()
    for _, hours in SNAPSHOTS:
        moment = published + timedelta(hours=hours)
        if moment > now:
            return moment
    return None
