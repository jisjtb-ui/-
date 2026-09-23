"""カテゴリ別の生成割合を、実績にあわせて少しずつ動かす。

考え方:
  成績のいいカテゴリを増やし、悪いカテゴリを減らす。ただし
  **ゼロにはしない**。SNSの流行や見る人は変わるので、弱いカテゴリも
  少しだけ出し続けて、再評価できる状態を保つ（探索枠）。

評価はバズ1本に振り回されないよう、直近N件の**中央値**を使う。
平均だと1本の当たりでカテゴリ全体の評価が跳ね上がってしまう。

安全のため、1回の更新で動ける幅に上限を置き、サンプルが少ない
カテゴリは大きく動かさない。変更の理由はすべて履歴に残す。
"""

from __future__ import annotations

import random
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

from .config import Settings
from .experiments import DELIVERED, ExperimentStore

LogFn = Callable[[str], None]

# 全媒体共通のweightを使う（Phase 1）。媒体別に分けるときはここに媒体名が入る。
SHARED = ""


def effective_bounds(settings: Settings, count: int) -> tuple[float, float]:
    """カテゴリ数に合わせた、実際に使える最低/最大weight。

    最低weightを素直に守ると、カテゴリが多い時に合計が100を超えてしまう
    （例: 21カテゴリ × 5% = 105%）。均等配分を基準に、成立する範囲へ収める。
    """
    if count <= 0:
        return settings.weight_min, settings.weight_max
    equal = 100.0 / count
    low = min(settings.weight_min, equal * 0.5)
    high = max(settings.weight_max, equal * 1.5)
    return low, high


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class CategoryStat:
    """1カテゴリの成績。"""

    category: str
    weight: float
    locked: bool = False
    sample_size: int = 0
    score: float | None = None          # 直近N件の中央値
    posts: int = 0                      # 配信済みの総数
    per_platform: dict = field(default_factory=dict)

    @property
    def enough_samples(self) -> bool:
        return self.score is not None


@dataclass
class WeightChange:
    category: str
    before: float
    after: float
    sample_size: int
    score: float | None
    baseline: float | None
    reason: str

    @property
    def delta(self) -> float:
        return self.after - self.before


class WeightStore:
    """category_weights と weight_history の読み書き。"""

    def __init__(self, store: ExperimentStore) -> None:
        self.store = store

    # ------------------------------------------------------------------
    def all(self, platform: str = SHARED) -> dict[str, float]:
        with self.store._connect() as conn:
            rows = conn.execute(
                "SELECT category, weight FROM category_weights WHERE platform=?",
                (platform,),
            ).fetchall()
        return {row["category"]: float(row["weight"]) for row in rows}

    def locked(self, platform: str = SHARED) -> set[str]:
        with self.store._connect() as conn:
            rows = conn.execute(
                "SELECT category FROM category_weights WHERE platform=? AND locked=1",
                (platform,),
            ).fetchall()
        return {row["category"] for row in rows}

    def set_weight(self, category: str, weight: float, platform: str = SHARED,
                   locked: bool | None = None) -> None:
        with self.store._connect() as conn:
            existing = conn.execute(
                "SELECT locked FROM category_weights WHERE platform=? AND category=?",
                (platform, category),
            ).fetchone()
            keep = int(existing["locked"]) if existing and locked is None else int(bool(locked))
            conn.execute(
                "INSERT INTO category_weights (platform, category, weight, locked, updated_at)"
                " VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT(platform, category) DO UPDATE SET"
                " weight=excluded.weight, locked=excluded.locked, updated_at=excluded.updated_at",
                (platform, category, float(weight), keep, _now()),
            )

    def set_locked(self, category: str, locked: bool, platform: str = SHARED) -> None:
        with self.store._connect() as conn:
            conn.execute(
                "UPDATE category_weights SET locked=?, updated_at=? WHERE platform=? AND category=?",
                (int(locked), _now(), platform, category),
            )

    def ensure(self, categories: list[str], platform: str = SHARED) -> dict[str, float]:
        """未登録のカテゴリを登録し、合計を100に揃える。

        既存カテゴリの**比率**は保つ。新カテゴリを足したときに合計が
        100からずれると、抽選の割合が設定値とかけ離れてしまう。
        手で固定したカテゴリは、ここでも動かさない。
        """
        current = self.all(platform)
        missing = [c for c in categories if c not in current]
        stale = [c for c in current if c not in categories]
        if not missing and not stale and abs(sum(current.values()) - 100) < 0.01:
            return current

        share = 100.0 / len(categories) if categories else 0.0
        for category in missing:
            current[category] = share

        # 対象外になったカテゴリは抽選から外す（記録は残す）
        active = {c: current.get(c, share) for c in categories}
        locked = self.locked(platform)
        fixed = {c: w for c, w in active.items() if c in locked}
        free = {c: w for c, w in active.items() if c not in locked}
        room = 100.0 - sum(fixed.values())

        if free and room > 0:
            # 固定分を差し引いた残りを、固定されていないカテゴリで分け合う
            total_free = sum(free.values())
            if total_free > 0:
                active.update({c: w * room / total_free for c, w in free.items()})
            else:
                active.update({c: room / len(free) for c in free})
        else:
            # 固定分だけで100に達している。比率を保って全体を圧縮する
            total = sum(active.values())
            if total > 0:
                active = {c: w * 100.0 / total for c, w in active.items()}

        for category, weight in active.items():
            self.set_weight(category, round(weight, 2), platform)
        return self.all(platform)

    def reset(self, categories: list[str], platform: str = SHARED) -> dict[str, float]:
        """初期値（均等）へ戻す。固定中のカテゴリも戻す。"""
        share = 100.0 / len(categories) if categories else 0.0
        with self.store._connect() as conn:
            conn.execute("DELETE FROM category_weights WHERE platform=?", (platform,))
        for category in categories:
            self.set_weight(category, share, platform, locked=False)
        return self.all(platform)

    # ------------------------------------------------------------------
    def record(self, changes: list[WeightChange], platform: str = SHARED) -> None:
        stamp = _now()
        with self.store._connect() as conn:
            conn.executemany(
                "INSERT INTO weight_history (evaluated_at, platform, category,"
                " weight_before, weight_after, sample_size, score, baseline, reason)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (stamp, platform, c.category, c.before, c.after,
                     c.sample_size, c.score, c.baseline, c.reason)
                    for c in changes
                ],
            )

    def history(self, limit: int = 50, platform: str = SHARED) -> list[dict]:
        with self.store._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM weight_history WHERE platform=?"
                " ORDER BY evaluated_at DESC, id DESC LIMIT ?",
                (platform, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def last_evaluated_at(self, platform: str = SHARED) -> str:
        with self.store._connect() as conn:
            row = conn.execute(
                "SELECT MAX(evaluated_at) AS t FROM weight_history WHERE platform=?",
                (platform,),
            ).fetchone()
        return (row["t"] or "") if row else ""


# ----------------------------------------------------------------------
def draw_category(weights: dict[str, float], rng: random.Random) -> str:
    """weightに応じてカテゴリを1つ引く。"""
    items = [(c, w) for c, w in weights.items() if w > 0]
    if not items:
        raise ValueError("weightが設定されていません")
    total = sum(w for _, w in items)
    threshold = rng.random() * total
    cumulative = 0.0
    for category, weight in items:
        cumulative += weight
        if threshold < cumulative:
            return category
    return items[-1][0]


# ----------------------------------------------------------------------
# viewsが取れない場合に、代わりに使える指標（どれも実測値。推測はしない）
VIEW_FALLBACKS = ("impressions", "reach")


def _score_of(metrics_row: dict, metric: str) -> float | None:
    """1投稿の点数。取得できていない指標は None のまま（推測しない）。

    Instagram は 2024-07-02 以降の投稿で impressions を返さないため、
    views → impressions → reach の順に、実際に取れたものを使う。
    """
    value = metrics_row.get(metric)
    if value is None and metric == "views":
        for name in VIEW_FALLBACKS:
            value = metrics_row.get(name)
            if value is not None:
                break
    return float(value) if value is not None else None


def collect_stats(
    settings: Settings,
    store: ExperimentStore,
    weights: WeightStore,
    platform: str = SHARED,
) -> dict[str, CategoryStat]:
    """カテゴリごとの成績を集計する。"""
    snapshot = settings.weight_snapshot
    metric = settings.weight_metric
    window = settings.weight_window

    with store._connect() as conn:
        rows = conn.execute(
            "SELECT e.content_category AS category, p.platform AS platform,"
            "       p.published_at AS published_at, m.*"
            "  FROM experiment_publications p"
            "  JOIN experiments e ON e.experiment_id = p.experiment_id"
            "  LEFT JOIN experiment_metrics m"
            "    ON m.experiment_id = p.experiment_id AND m.platform = p.platform"
            "   AND m.snapshot = ?"
            " WHERE p.status IN ({})"
            " ORDER BY p.published_at DESC".format(",".join("?" * len(DELIVERED))),
            (snapshot, *DELIVERED),
        ).fetchall()

    current = weights.all(platform)
    locked = weights.locked(platform)
    stats: dict[str, CategoryStat] = {
        category: CategoryStat(category=category, weight=weight,
                               locked=category in locked)
        for category, weight in current.items()
    }
    scores: dict[str, list[float]] = {c: [] for c in current}

    for row in rows:
        category = row["category"] or ""
        if category not in stats:
            continue
        stat = stats[category]
        stat.posts += 1
        stat.per_platform[row["platform"]] = stat.per_platform.get(row["platform"], 0) + 1
        value = _score_of(dict(row), metric)
        if value is not None and len(scores[category]) < window:
            scores[category].append(value)

    for category, values in scores.items():
        stats[category].sample_size = len(values)
        if values:
            stats[category].score = statistics.median(values)
    return stats


def plan_update(
    settings: Settings,
    stats: dict[str, CategoryStat],
) -> list[WeightChange]:
    """weightの変更案を作る。実際の保存は呼び出し側で行う。"""
    measured = [s for s in stats.values() if s.score is not None]
    if not measured:
        return []
    baseline = statistics.median([s.score for s in measured])
    if baseline <= 0:
        return []

    minimum, maximum = effective_bounds(settings, len(stats))
    step = settings.weight_max_step
    needed = settings.weight_min_samples

    changes: list[WeightChange] = []
    for stat in stats.values():
        before = stat.weight
        if stat.locked:
            changes.append(WeightChange(stat.category, before, before, stat.sample_size,
                                        stat.score, baseline, "手動で固定中のため変更なし"))
            continue
        if stat.score is None:
            changes.append(WeightChange(stat.category, before, before, stat.sample_size,
                                        None, baseline,
                                        "反応データがまだ無いため変更なし（探索枠として残す）"))
            continue

        ratio = stat.score / baseline
        # サンプルが足りないカテゴリは、動かす幅を比例して小さくする
        confidence = min(1.0, stat.sample_size / needed) if needed else 1.0
        delta = (ratio - 1.0) * settings.weight_sensitivity * confidence
        delta = max(-step, min(step, delta))
        after = max(minimum, min(maximum, before + delta))

        if stat.sample_size < needed:
            reason = (f"直近{stat.sample_size}件（{needed}件未満のため控えめに調整）"
                      f"／中央値 {stat.score:.0f} は全体の {ratio:.2f}倍")
        else:
            reason = (f"直近{stat.sample_size}件の中央値 {stat.score:.0f} が"
                      f"全カテゴリ中央値の {ratio:.2f}倍")
        if after == minimum and before > minimum:
            reason += "／最低weightで下げ止まり（探索枠）"
        if after == maximum and before < maximum:
            reason += "／最大weightで頭打ち"
        changes.append(WeightChange(stat.category, before, after, stat.sample_size,
                                    stat.score, baseline, reason))
    locked = {s.category for s in stats.values() if s.locked}
    return _normalize(changes, settings, locked)


def _normalize(changes: list[WeightChange], settings: Settings,
               locked: set[str] | None = None) -> list[WeightChange]:
    """合計100に揃える。

    単純に比率で割ると、上限・下限で頭打ちになった分が消えて合計が
    100から外れる。余り（不足）を、まだ動けるカテゴリへ配り直す。
    固定中のカテゴリは動かさない。
    """
    locked = locked or set()
    low, high = effective_bounds(settings, len(changes))
    values = {c.category: c.after for c in changes}
    movable = [c.category for c in changes if c.category not in locked]
    if not movable:
        return changes

    # 上限・下限の範囲で合計100にできるか
    fixed_total = sum(values[c] for c in values if c in locked)
    room_low = fixed_total + low * len(movable)
    room_high = fixed_total + high * len(movable)
    if room_low > 100 or room_high < 100:
        # 設定同士が矛盾している。範囲に収めるだけにして、合計は諦める。
        for change in changes:
            if change.category not in locked:
                change.after = round(max(low, min(high, change.after)), 2)
        return changes

    for category in movable:
        values[category] = max(low, min(high, values[category]))

    for _ in range(100):
        diff = 100.0 - sum(values.values())
        if abs(diff) < 0.005:
            break
        headroom = [
            c for c in movable
            if (diff > 0 and values[c] < high - 1e-9) or (diff < 0 and values[c] > low + 1e-9)
        ]
        if not headroom:
            break
        share = diff / len(headroom)
        for category in headroom:
            values[category] = max(low, min(high, values[category] + share))

    for change in changes:
        change.after = round(values[change.category], 2)
    return changes


def apply_update(
    settings: Settings,
    store: ExperimentStore,
    weights: WeightStore,
    categories: list[str],
    platform: str = SHARED,
    dry_run: bool = False,
    log: LogFn = print,
) -> list[WeightChange]:
    """集計 → 変更案 → 保存 までを行う。"""
    if not settings.weight_auto:
        log("自動最適化は無効です（.env の WEIGHT_AUTO=true で有効になります）")
        return []

    weights.ensure(categories, platform)
    stats = collect_stats(settings, store, weights, platform)
    changes = plan_update(settings, stats)
    if not changes:
        log("評価できる反応データがまだありません（weightは変更しません）")
        return []

    moved = [c for c in changes if abs(c.delta) >= 0.01]
    if dry_run:
        log(f"[dry-run] {len(moved)}件が変わります")
        return changes

    for change in changes:
        weights.set_weight(change.category, change.after, platform)
    weights.record(changes, platform)
    log(f"weightを更新しました（{len(moved)}件が変化）")
    return changes
