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


# ----------------------------------------------------------------------
# 1枚目フックの比較（A/B/C…）
# ----------------------------------------------------------------------
@dataclass
class HookRow:
    """フック1種類ぶんの成績。取れなかった指標は None のまま。"""

    variant: str
    label: str = ""
    posts: int = 0
    measured: int = 0
    views: float | None = None
    reach: float | None = None
    likes: float | None = None
    comments: float | None = None
    shares: float | None = None
    saves: float | None = None
    watch_time: float | None = None
    completion_rate: float | None = None
    save_rate: float | None = None
    share_rate: float | None = None
    comment_rate: float | None = None
    like_rate: float | None = None
    enough: bool = False


# 率の分母。表示回数で割って、投稿の規模をそろえる
RATE_BASE = ("views", "impressions", "reach")
# これだけ集まるまでは勝敗を決めない
MIN_POSTS_PER_HOOK = 10


def _median(values: list[float]) -> float | None:
    import statistics

    return statistics.median(values) if values else None


def hook_comparison(settings: Settings, store: ExperimentStore | None = None,
                    snapshot: str = "") -> tuple[list[HookRow], dict]:
    """フック別の成績。

    単純な総再生数では比べない:
      - 投稿ごとに率（保存率・共有率・コメント率）を出してから中央値をとる
      - 分母は views。無ければ impressions → reach（実測値だけを使う）
      - 取れない指標は作らない。空欄のまま返す
    """
    store = store or ExperimentStore(settings.experiments_db_path)
    snapshot = snapshot or settings.weight_snapshot

    with store._connect() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT m.*, e.hook_variant AS exp_hook"
            "  FROM experiment_metrics m"
            "  JOIN experiments e ON e.experiment_id = m.experiment_id"
            " WHERE m.snapshot = ? AND m.late = 0"
            " ORDER BY m.collected_at DESC", (snapshot,)).fetchall()]
        posts = [dict(r) for r in conn.execute(
            "SELECT hook_variant, COUNT(*) AS n FROM experiments"
            " WHERE hook_variant <> '' GROUP BY hook_variant").fetchall()]

    labels = _hook_labels()
    counted: dict[str, int] = {r["hook_variant"]: r["n"] for r in posts}
    buckets: dict[str, dict[str, list[float]]] = {}
    seen: set[tuple] = set()

    for row in rows:
        variant = row.get("hook_variant") or row.get("exp_hook") or ""
        if not variant:
            continue
        key = (row["experiment_id"], row["platform"], row["snapshot"])
        if key in seen:
            continue
        seen.add(key)
        bucket = buckets.setdefault(variant, {})
        bucket.setdefault("_n", []).append(1.0)

        for name in ("views", "reach", "likes", "comments", "shares", "saves",
                     "watch_time_seconds", "completion_rate"):
            value = row.get(name)
            if value is not None:
                bucket.setdefault(name, []).append(float(value))

        base = next((row[n] for n in RATE_BASE if row.get(n)), None)
        if base:
            for name, target in (("saves", "save_rate"), ("shares", "share_rate"),
                                 ("comments", "comment_rate"), ("likes", "like_rate")):
                if row.get(name) is not None:
                    bucket.setdefault(target, []).append(float(row[name]) / float(base))

    out: list[HookRow] = []
    for variant in sorted(set(list(counted) + list(buckets))):
        bucket = buckets.get(variant, {})
        out.append(HookRow(
            variant=variant,
            label=labels.get(variant, ""),
            posts=counted.get(variant, 0),
            measured=len(bucket.get("_n", [])),
            views=_median(bucket.get("views", [])),
            reach=_median(bucket.get("reach", [])),
            likes=_median(bucket.get("likes", [])),
            comments=_median(bucket.get("comments", [])),
            shares=_median(bucket.get("shares", [])),
            saves=_median(bucket.get("saves", [])),
            watch_time=_median(bucket.get("watch_time_seconds", [])),
            completion_rate=_median(bucket.get("completion_rate", [])),
            save_rate=_median(bucket.get("save_rate", [])),
            share_rate=_median(bucket.get("share_rate", [])),
            comment_rate=_median(bucket.get("comment_rate", [])),
            like_rate=_median(bucket.get("like_rate", [])),
            enough=counted.get(variant, 0) >= MIN_POSTS_PER_HOOK,
        ))

    note = {
        "snapshot": snapshot,
        "min_posts": MIN_POSTS_PER_HOOK,
        "ready": all(r.enough for r in out) and len(out) >= 2,
        "missing": [f"{r.variant}（あと{MIN_POSTS_PER_HOOK - r.posts}件）"
                    for r in out if not r.enough],
    }
    return out, note


def _hook_labels() -> dict[str, str]:
    """フックの表示名。data/hooks.json から引く（コードに書かない）。"""
    from pathlib import Path as _Path

    try:
        from night_test.hooks import HookConfig

        root = _Path(__file__).resolve().parent.parent
        config = HookConfig.load(root / "data" / "hooks.json")
        return {v.id: v.label for v in config.variants}
    except Exception:
        return {}


def hook_report(settings: Settings, store: ExperimentStore | None = None) -> str:
    """フック別の比較を文字で出す。"""
    rows, note = hook_comparison(settings, store)
    lines: list[str] = []
    add = lines.append
    add("=" * 62)
    add(" 1枚目フックの比較")
    add("=" * 62)
    add(f"評価: {note['snapshot']}時点 / 投稿ごとの率を出してから中央値")
    add(f"分母: views（無ければ impressions → reach）")
    if not rows:
        add("\nまだフック付きの投稿がありません。")
        return "\n".join(lines)

    if not note["ready"]:
        add(f"\n【まだ判断しないでください】各フック{note['min_posts']}件以上でそろえてから比べます。")
        if note["missing"]:
            add("  不足: " + " / ".join(note["missing"]))

    add("")
    add(f"  {'':<4}{'フック':<14}{'投稿':>5}{'測定':>5}{'表示':>9}"
        f"{'保存率':>8}{'共有率':>8}{'コメ率':>8}")
    for row in rows:
        def pct(value):
            return f"{value * 100:.2f}%" if value is not None else "—"
        views = f"{row.views:,.0f}" if row.views is not None else "—"
        add(f"  {row.variant:<4}{row.label:<14}{row.posts:>5}{row.measured:>5}{views:>9}"
            f"{pct(row.save_rate):>8}{pct(row.share_rate):>8}{pct(row.comment_rate):>8}")

    watched = [r for r in rows if r.watch_time is not None or r.completion_rate is not None]
    if watched:
        add("")
        add("  視聴（取得できた媒体のみ）")
        for row in watched:
            watch = f"{row.watch_time:.1f}秒" if row.watch_time is not None else "—"
            done = f"{row.completion_rate * 100:.1f}%" if row.completion_rate is not None else "—"
            add(f"    {row.variant}  平均視聴 {watch} / 完了率 {done}")
    else:
        add("")
        add("  平均視聴時間・完了率: この投稿形式では取得できません（Reel専用の指標）")

    add("")
    add("  ※ 総再生数だけで決めないでください。保存率・共有率は規模の影響を")
    add("     受けにくく、フックの良し悪しが出やすい指標です。")
    return "\n".join(lines)
