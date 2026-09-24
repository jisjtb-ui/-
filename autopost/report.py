"""成績のまとめ。画面（GUI）と文字の報告で同じ数字を使う。

``overview()`` が数字を作り、``build()`` がそれを文字に組む。
GUIは ``overview()`` をそのまま表として並べる。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .catalog import Catalog
from .config import Settings
from .experiments import DELIVERED, ExperimentStore
from .version import current_version
from .weights import (
    SHARED,
    SubStat,
    SubWeightStore,
    auto_enabled,
    collect_stats,
    effective_bounds,
)

OUTCOME_LABELS = {
    "measured": "測定済み",
    "failed": "取得失敗",
    "unavailable": "取得不可",
    "rate_limited": "レート制限で保留",
}


@dataclass
class SubRow:
    """画面の1行ぶん。"""

    sub_category_id: int
    name: str
    posts: int = 0
    measured: int = 0
    raw_median: float | None = None
    score: float | None = None
    weight: float = 0.0
    previous: float | None = None
    locked: bool = False
    enabled: bool = True
    shortage: bool = False
    reason: str = ""
    late_skipped: int = 0

    @property
    def delta(self) -> float | None:
        return None if self.previous is None else self.weight - self.previous


@dataclass
class Overview:
    category_name: str = ""
    version: str = ""
    auto: bool = False
    metric: str = ""
    snapshot: str = ""
    window: int = 0
    min_samples: int = 0
    bounds_low: float = 0.0
    bounds_high: float = 0.0
    bounds_reason: str = ""
    total_posts: int = 0
    by_platform: dict[str, int] = field(default_factory=dict)
    outcomes: dict[str, int] = field(default_factory=dict)
    last_collected_at: str = ""
    last_evaluated_at: str = ""
    rows: list[SubRow] = field(default_factory=list)
    failures: list[dict] = field(default_factory=list)
    history: list[dict] = field(default_factory=list)
    best: list[dict] = field(default_factory=list)
    worst: list[dict] = field(default_factory=list)


def _platform_counts(store: ExperimentStore) -> tuple[int, dict[str, int]]:
    with store._connect() as conn:
        rows = conn.execute(
            "SELECT platform, COUNT(*) AS c FROM experiment_publications"
            " WHERE status IN ({}) GROUP BY platform".format(",".join("?" * len(DELIVERED))),
            tuple(DELIVERED),
        ).fetchall()
    counts = {row["platform"]: row["c"] for row in rows}
    return sum(counts.values()), counts


def _extreme_posts(store: ExperimentStore, settings: Settings, limit: int,
                   best: bool) -> list[dict]:
    order = "DESC" if best else "ASC"
    metric = settings.weight_metric if settings.weight_metric in (
        "views", "impressions", "reach", "likes", "comments", "shares", "saves") else "views"
    with store._connect() as conn:
        rows = conn.execute(
            f"SELECT m.experiment_id, m.platform, m.{metric} AS score, m.content_id,"
            f"       s.name AS sub_name"
            f"  FROM experiment_metrics m"
            f"  LEFT JOIN sub_categories s ON s.id = m.sub_category_id"
            f" WHERE m.snapshot=? AND m.late=0 AND m.{metric} IS NOT NULL"
            f" ORDER BY m.{metric} {order} LIMIT ?",
            (settings.weight_snapshot, limit),
        ).fetchall()
    return [dict(row) for row in rows]


def overview(settings: Settings, store: ExperimentStore | None = None,
             category_id: int | None = None, platform: str = SHARED) -> Overview:
    """画面と報告で共通に使う数字を作る。"""
    store = store or ExperimentStore(settings.experiments_db_path)
    catalog = Catalog(store)
    weights = SubWeightStore(store)

    categories = catalog.categories()
    category = None
    if category_id is not None:
        category = catalog.category(category_id)
    elif categories:
        category = categories[0]

    subs = catalog.sub_categories(category.id, include_disabled=True) if category else []
    enabled = {s.id: s.enabled for s in subs}
    sub_names = {s.id: s.name for s in subs}
    stats: dict[int, SubStat] = {}
    if sub_names:
        stats, _ = collect_stats(settings, store, weights, sub_names, platform)

    current = weights.all(platform)
    previous = weights.previous_weights(platform)
    locked = weights.locked(platform)
    reasons = {int(h["sub_category_id"]): h["reason"] for h in weights.history(limit=200)}

    rows: list[SubRow] = []
    for sub_id, name in sub_names.items():
        stat = stats.get(sub_id)
        rows.append(SubRow(
            sub_category_id=sub_id, name=name,
            posts=stat.posts if stat else 0,
            measured=stat.sample_size if stat else 0,
            raw_median=stat.raw_median if stat else None,
            score=stat.score if stat else None,
            weight=current.get(sub_id, 0.0),
            previous=previous.get(sub_id),
            locked=sub_id in locked,
            enabled=enabled.get(sub_id, True),
            shortage=bool(stat and stat.shortage(settings.weight_min_samples)),
            reason=reasons.get(sub_id, ""),
            late_skipped=stat.late_skipped if stat else 0,
        ))
    rows.sort(key=lambda r: (-r.weight, r.name))

    total, by_platform = _platform_counts(store)
    bounds = effective_bounds(settings, len(sub_names))
    return Overview(
        category_name=category.name if category else "（Categoryがありません）",
        version=current_version(),
        auto=auto_enabled(settings, store),
        metric=settings.weight_metric,
        snapshot=settings.weight_snapshot,
        window=settings.weight_window,
        min_samples=settings.weight_min_samples,
        bounds_low=bounds.low, bounds_high=bounds.high, bounds_reason=bounds.reason,
        total_posts=total, by_platform=by_platform,
        outcomes=store.attempt_counts(),
        last_collected_at=store.last_collected_at(),
        last_evaluated_at=weights.last_evaluated_at(platform),
        rows=rows,
        failures=store.recent_failures(limit=8),
        history=weights.history(limit=12, platform=platform),
        best=_extreme_posts(store, settings, 5, True),
        worst=_extreme_posts(store, settings, 5, False),
    )


# ----------------------------------------------------------------------
def _stamp(value: str) -> str:
    return value[:16].replace("T", " ") if value else "まだありません"


def build(settings: Settings, category_id: int | None = None,
          platform: str = SHARED) -> str:
    """文字の報告。GUIと同じ ``overview()`` の数字を使う。"""
    data = overview(settings, category_id=category_id, platform=platform)
    lines: list[str] = []
    add = lines.append

    add("=" * 62)
    add(f" SubCategory別の成績と生成割合 — {data.category_name}")
    add("=" * 62)
    add(f"バージョン : v{data.version}")
    add(f"評価の方法 : {data.snapshot}時点の {data.metric} / 直近{data.window}件の中央値")
    add(f"             媒体内の相対値に直してから比べます（媒体の規模差を持ち込まない）")
    add(f"自動最適化 : {'有効' if data.auto else '無効（設定した割合で抽選します）'}")
    add(f"実効の下限・上限 : {data.bounds_low:.2f}% 〜 {data.bounds_high:.1f}%")
    if data.bounds_reason:
        add(f"  ※ {data.bounds_reason}")
    add(f"最終取得   : {_stamp(data.last_collected_at)}")
    add(f"最終更新   : {_stamp(data.last_evaluated_at)}")

    add("")
    add("-" * 62)
    add(" 投稿数と取得状況")
    add("-" * 62)
    add(f"  配信できた投稿 : {data.total_posts}件")
    for name, count in sorted(data.by_platform.items()):
        add(f"    {name:<16}{count}件")
    if data.outcomes:
        add("  反応データ :")
        for key, label in OUTCOME_LABELS.items():
            if key in data.outcomes:
                add(f"    {label:<18}{data.outcomes[key]}件")
    else:
        add("  反応データ : まだ取得していません")

    add("")
    add("-" * 62)
    add(" SubCategory別")
    add("-" * 62)
    add(f"  {'SubCategory':<20}{'割合':>7}{'前回差':>8}{'中央値':>9}{'件数':>6}  状態")
    for row in data.rows:
        delta = f"{row.delta:+.1f}" if row.delta is not None else "—"
        median = f"{row.raw_median:,.0f}" if row.raw_median is not None else "—"
        marks = []
        if not row.enabled:
            marks.append("抽選から除外")
        if row.locked:
            marks.append("固定中")
        if row.shortage and row.measured:
            marks.append(f"サンプル不足({row.measured}/{data.min_samples})")
        elif not row.measured:
            marks.append("未測定")
        if row.late_skipped:
            marks.append(f"遅延{row.late_skipped}件除外")
        add(f"  {row.name:<20}{row.weight:6.1f}%{delta:>8}{median:>9}{row.measured:>6}  "
            + " / ".join(marks))

    changed = [r for r in data.rows if r.reason]
    if changed:
        add("")
        add("-" * 62)
        add(" 割合が変わった理由")
        add("-" * 62)
        for row in changed:
            add(f"  {row.name}")
            add(f"    {row.reason}")

    if data.failures:
        add("")
        add("-" * 62)
        add(" 取得できなかったもの")
        add("-" * 62)
        for item in data.failures:
            label = OUTCOME_LABELS.get(item["outcome"], item["outcome"])
            add(f"  [{label}] {item['experiment_id']} / {item['platform']} "
                f"{item['snapshot']}")
            add(f"    {item['reason']}")

    if data.best:
        add("")
        add("-" * 62)
        add(f" 反応の良かった投稿（{data.metric}）")
        add("-" * 62)
        for item in data.best:
            add(f"  {item['score']:>9,.0f}  {item['sub_name'] or '—':<18}"
                f"{item['content_id'] or item['experiment_id']} / {item['platform']}")
    if data.worst:
        add("")
        add(" 反応が弱かった投稿")
        for item in data.worst:
            add(f"  {item['score']:>9,.0f}  {item['sub_name'] or '—':<18}"
                f"{item['content_id'] or item['experiment_id']} / {item['platform']}")

    add("")
    return "\n".join(lines)


def run(settings: Settings, output: Path | None = None,
        category_id: int | None = None) -> int:
    text = build(settings, category_id=category_id)
    print(text)
    target = output or Path("カテゴリ成績.txt")
    target.write_text(text + "\n", encoding="utf-8")
    print(f"\nこの内容を {target} に保存しました。")
    return 0
