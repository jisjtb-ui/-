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
from .publishers import (
    NotSupported,
    PermanentError,
    PermissionDenied,
    RateLimited,
    TransientError,
    get_publisher,
)

# 既定の計測区分。実際に使う一覧は Settings.snapshot_windows() から読む
# （.env の METRIC_SNAPSHOTS / METRIC_GRACE_HOURS で変えられる）。
SNAPSHOTS: tuple[tuple[str, float], ...] = (
    ("1h", 1.0),
    ("6h", 6.0),
    ("24h", 24.0),
    ("72h", 72.0),
    ("7d", 168.0),
)
GRACE_HOURS = 6.0

# 取得を試した結果
MEASURED = "measured"           # 数字が取れた
FAILED = "failed"               # 取れなかった（権限・通信・応答不正など）
UNAVAILABLE = "unavailable"     # APIにそもそも取得手段が無い
RATE_LIMITED = "rate_limited"   # レート制限。次回また試す


@dataclass
class DueSnapshot:
    """いま取りに行くべき1件。"""

    label: str
    window_hours: float
    elapsed: float
    late: bool

    def describe(self) -> str:
        mark = "（遅延）" if self.late else ""
        return f"{self.label}{mark} 実際の経過 {self.elapsed:.1f}h"


def metrics_from_result(
    publication: Publication,
    result,
    due: "DueSnapshot | None" = None,
    sub_category_id: int | None = None,
    content_id: str = "",
) -> Metrics:
    """AnalyticsResult を保存用の Metrics へ移す。

    保存経路をここ1か所にまとめる。engine と collector で別々に組み立てて
    いたため、`reach` や視聴時間の転記漏れが起きていた。
    """
    return Metrics(
        experiment_id=publication.experiment_id,
        platform=publication.platform,
        snapshot=due.label if due else "",
        hours_since_post=round(due.elapsed, 2) if due else None,
        window_hours=due.window_hours if due else None,
        late=1 if (due and due.late) else 0,
        external_post_id=publication.external_post_id or "",
        published_at=publication.published_at or "",
        sub_category_id=sub_category_id,
        content_id=content_id,
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
        watch_time_seconds=result.watch_time_seconds,
        completion_rate=result.completion_rate,
        platform_metrics=result.platform_metrics,
    )


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
    @property
    def windows(self) -> tuple[tuple[str, float], ...]:
        return self.settings.snapshot_windows()

    @property
    def grace(self) -> float:
        return max(0.0, float(self.settings.metric_grace_hours))

    def due_snapshots(self, publication: Publication,
                      now: datetime | None = None) -> list[DueSnapshot]:
        """いま取りに行くべき計測区分（未取得かつ時刻を過ぎたもの）。

        **経過時間に合う区分を選ぶ。** 1週間放置された投稿の現在の数字を
        「1h」として保存すると、24h同士の比較が成り立たなくなる。
        許容時間を過ぎている場合は ``late`` を立て、実際の経過時間も残す。
        """
        if not publication.published_at or publication.status not in DELIVERED:
            return []
        now = now or datetime.now().astimezone()
        try:
            published = datetime.fromisoformat(publication.published_at)
        except ValueError:
            return []
        elapsed = (now - published).total_seconds() / 3600
        done = self.store.collected_snapshots(publication.experiment_id, publication.platform)

        # まだ来ていない区分は対象外。すでに後ろの区分を測っているなら、
        # 過ぎた前の区分を後追いしない（いまの数字を「1h」として残さない）。
        measured_hours = [hours for label, hours in self.windows if label in done]
        newest = max(measured_hours) if measured_hours else -1.0
        reached = [(label, hours) for label, hours in self.windows
                   if hours <= elapsed and label not in done and hours > newest]
        if not reached:
            return []

        on_time = [(label, hours) for label, hours in reached
                   if elapsed <= hours + self.grace]
        if on_time:
            label, hours = on_time[0]
            return [DueSnapshot(label=label, window_hours=hours, elapsed=elapsed, late=False)]

        # すべて許容時間を過ぎている。経過時間にいちばん近い区分として記録する。
        label, hours = reached[-1]
        return [DueSnapshot(label=label, window_hours=hours, elapsed=elapsed, late=True)]

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
            if self._collect_one(publication, due[0]):
                report.collected.append(
                    f"{publication.experiment_id}/{publication.platform} [{due[0].describe()}]"
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
            manual = DueSnapshot(label=label, window_hours=0.0,
                                 elapsed=self._elapsed_hours(publication), late=False)
            if self._collect_one(publication, manual):
                report.collected.append(f"{publication.experiment_id}/{publication.platform}")
            else:
                report.failed.append(f"{publication.experiment_id}/{publication.platform}")
        return report

    def _elapsed_hours(self, publication: Publication) -> float:
        try:
            published = datetime.fromisoformat(publication.published_at or "")
        except ValueError:
            return 0.0
        return (datetime.now().astimezone() - published).total_seconds() / 3600

    # ------------------------------------------------------------------
    def _collect_one(self, publication: Publication, due: DueSnapshot) -> bool:
        """1件ぶん取りに行く。結果は必ず記録する（測定済み／失敗／取得不可）。

        取得できなかったときは**保存しない**。空の行を残すと、その区分は
        「測定済み」になり、あとから正しい数字を入れられなくなる。
        過去に取れている数字は、ここでは決して触らない。
        """
        tag = f"[{publication.experiment_id}] {publication.platform}"
        experiment_id = publication.experiment_id

        def note(outcome: str, reason: str, level: str = "warn") -> bool:
            self.store.record_attempt(experiment_id, publication.platform,
                                      due.label, outcome, reason)
            self.store.log(experiment_id, reason, publication.platform, level)
            return False

        try:
            publisher = self.publisher(publication.platform)
            result = publisher.get_analytics(publication.external_post_id)
        except NotSupported as exc:
            self.log(f"  {tag}: 取得不可 {exc}")
            return note(UNAVAILABLE, f"反応データの取得手段がありません: {exc}", "info")
        except RateLimited as exc:
            self.log(f"  {tag}: レート制限 {exc}（次回また試します）")
            return note(RATE_LIMITED, f"レート制限のため取得を見送りました: {exc}")
        except PermissionDenied as exc:
            self.log(f"  {tag}: 権限不足 {exc}")
            return note(FAILED, f"権限が足りず取得できません: {exc}")
        except (PermanentError, TransientError) as exc:
            self.log(f"  {tag}: 取得失敗 {exc}")
            return note(FAILED, f"反応データを取得できません: {exc}")
        except Exception as exc:                 # 想定外でも投稿処理は止めない
            self.log(f"  {tag}: 取得失敗（想定外） {exc}")
            return note(FAILED, f"反応データの取得で想定外のエラー: {exc}")

        if result.is_empty():
            self.log(f"  {tag}: 指標が1つも取れなかったため保存しません")
            return note(FAILED, "応答に指標が含まれていませんでした（測定済みにしません）")

        experiment = self.store.get(experiment_id)
        metrics = metrics_from_result(
            publication, result, due,
            sub_category_id=getattr(experiment, "sub_category_id", None) if experiment else None,
            content_id=getattr(experiment, "source_post_id", "") if experiment else "",
        )
        if not self.store.save_metrics(metrics):
            self.log(f"  {tag}: {due.label} は既に測定済みのため追加しません")
            return False
        self.store.record_attempt(experiment_id, publication.platform, due.label, MEASURED,
                                  "取得した指標: " + ", ".join(result.obtained()))
        mark = "（遅延）" if due.late else ""
        self.log(f"  {tag}{mark}: {metrics.summary()}")
        return True


def next_snapshot_at(published_at: str,
                     windows: tuple[tuple[str, float], ...] | None = None) -> datetime | None:
    """次に取得すべき時刻（予定表示用）。"""
    try:
        published = datetime.fromisoformat(published_at)
    except (TypeError, ValueError):
        return None
    now = datetime.now().astimezone()
    for _, hours in (windows or SNAPSHOTS):
        moment = published + timedelta(hours=hours)
        if moment > now:
            return moment
    return None
