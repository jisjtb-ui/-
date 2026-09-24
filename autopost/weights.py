"""SubCategory別の生成割合を、実績にあわせて少しずつ動かす。

考え方:
  成績のいいSubCategoryを増やし、悪いものを減らす。ただし**ゼロにはしない**。
  SNSの流行や見る人は変わるので、弱いものも少しだけ出し続けて、
  再評価できる状態を保つ（探索枠）。

評価はバズ1本に振り回されないよう、直近N件の**中央値**を使う。
平均だと1本の当たりで評価が跳ね上がってしまう。

媒体ごとに閲覧の規模が違う（Threadsの1,000とInstagramの1,000は重みが違う）。
そのまま混ぜると規模の大きい媒体だけで決まってしまうので、
**まず媒体内での相対値に直してから**SubCategory別にまとめる。

安全のため、1回の更新で動ける幅に上限を置き、サンプルが少ないものは
大きく動かさない。同じ測定データでの二重適用も防ぐ。変更理由は履歴に残す。

内部の受け渡しは必ず ``sub_category_id``。名前は画面表示のときだけ引く。
"""

from __future__ import annotations

import hashlib
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

# viewsが取れない場合に代わりに使う指標（どれも実測値。推測はしない）
VIEW_FALLBACKS = ("impressions", "reach")


# ----------------------------------------------------------------------
@dataclass
class Bounds:
    """実際に使える下限・上限と、設定値から変えた理由。"""

    low: float
    high: float
    configured_low: float
    configured_high: float
    reason: str = ""

    @property
    def adjusted(self) -> bool:
        return bool(self.reason)


def effective_bounds(settings: Settings, count: int) -> Bounds:
    """SubCategoryの数に合わせた、実際に使える下限/上限weight。

    下限を素直に守ると、数が多い時に合計が100を超えてしまう
    （例: 21件 × 5% = 105%）。均等配分を基準に、成立する範囲へ収める。
    理由を持たせて、画面に「なぜ変えたか」を出せるようにする。
    """
    low = high = 0.0
    configured_low, configured_high = settings.weight_min, settings.weight_max
    if count <= 0:
        return Bounds(configured_low, configured_high, configured_low, configured_high)

    equal = 100.0 / count
    low, high = configured_low, configured_high
    reasons: list[str] = []
    if configured_low * count > 100.0:
        low = min(configured_low, equal * 0.5)
        reasons.append(
            f"下限 {configured_low:.1f}% × {count}件 = {configured_low * count:.0f}% は"
            f"100%を超えるため {low:.2f}% に下げました"
        )
    if configured_high < equal:
        high = max(configured_high, equal * 1.5)
        reasons.append(
            f"上限 {configured_high:.1f}% が均等配分 {equal:.2f}% を下回るため"
            f" {high:.2f}% に上げました"
        )
    return Bounds(low, high, configured_low, configured_high, "／".join(reasons))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class SubStat:
    """1つのSubCategoryの成績。"""

    sub_category_id: int
    name: str = ""
    weight: float = 0.0
    locked: bool = False
    posts: int = 0                      # 配信できた件数
    sample_size: int = 0                # 評価に使えた測定の件数
    score: float | None = None          # 媒体内の相対値の中央値。未測定は None
    raw_median: float | None = None     # 生の指標の中央値（画面表示用）
    late_skipped: int = 0               # 遅延測定として除いた件数
    per_platform: dict[str, int] = field(default_factory=dict)

    @property
    def enough_samples(self) -> bool:
        return self.sample_size > 0

    def shortage(self, needed: int) -> bool:
        return self.sample_size < needed


@dataclass
class WeightChange:
    sub_category_id: int
    name: str
    before: float
    after: float
    sample_size: int = 0
    score: float | None = None
    baseline: float | None = None
    reason: str = ""

    @property
    def delta(self) -> float:
        return self.after - self.before


# ----------------------------------------------------------------------
class SubWeightStore:
    """SubCategory別weightの読み書き。"""

    def __init__(self, store: ExperimentStore) -> None:
        self.store = store

    def all(self, platform: str = SHARED) -> dict[int, float]:
        with self.store._connect() as conn:
            rows = conn.execute(
                "SELECT sub_category_id, weight FROM sub_weights WHERE platform=?"
                " ORDER BY weight DESC, sub_category_id",
                (platform,),
            ).fetchall()
        return {int(r["sub_category_id"]): float(r["weight"]) for r in rows}

    def locked(self, platform: str = SHARED) -> set[int]:
        with self.store._connect() as conn:
            rows = conn.execute(
                "SELECT sub_category_id FROM sub_weights WHERE platform=? AND locked=1",
                (platform,),
            ).fetchall()
        return {int(r["sub_category_id"]) for r in rows}

    def set_weight(self, sub_category_id: int, weight: float, platform: str = SHARED,
                   locked: bool | None = None) -> None:
        with self.store._connect() as conn:
            existing = conn.execute(
                "SELECT locked FROM sub_weights WHERE platform=? AND sub_category_id=?",
                (platform, sub_category_id),
            ).fetchone()
            keep = int(existing["locked"]) if existing and locked is None else int(bool(locked))
            conn.execute(
                "INSERT INTO sub_weights (platform, sub_category_id, weight, locked, updated_at)"
                " VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT(platform, sub_category_id) DO UPDATE SET"
                " weight=excluded.weight, locked=excluded.locked, updated_at=excluded.updated_at",
                (platform, int(sub_category_id), float(weight), keep, _now()),
            )

    def set_locked(self, sub_category_id: int, locked: bool, platform: str = SHARED) -> None:
        with self.store._connect() as conn:
            conn.execute(
                "UPDATE sub_weights SET locked=?, updated_at=?"
                " WHERE platform=? AND sub_category_id=?",
                (int(locked), _now(), platform, int(sub_category_id)),
            )

    def ensure(self, sub_ids: list[int], platform: str = SHARED) -> dict[int, float]:
        """未登録のSubCategoryを登録し、合計を100に揃える。

        既存の**比率**は保つ。手で固定したものは、ここでも動かさない。
        """
        current = self.all(platform)
        missing = [s for s in sub_ids if s not in current]
        stale = [s for s in current if s not in sub_ids]
        if not missing and not stale and abs(sum(current.values()) - 100) < 0.01:
            return current

        share = 100.0 / len(sub_ids) if sub_ids else 0.0
        for sub_id in missing:
            current[sub_id] = share

        active = {s: current.get(s, share) for s in sub_ids}
        locked = self.locked(platform)
        fixed = {s: w for s, w in active.items() if s in locked}
        free = {s: w for s, w in active.items() if s not in locked}
        room = 100.0 - sum(fixed.values())

        if free and room > 0:
            total_free = sum(free.values())
            if total_free > 0:
                active.update({s: w * room / total_free for s, w in free.items()})
            else:
                active.update({s: room / len(free) for s in free})
        else:
            total = sum(active.values())
            if total > 0:
                active = {s: w * 100.0 / total for s, w in active.items()}

        for sub_id, weight in active.items():
            self.set_weight(sub_id, round(weight, 2), platform)
        return self.all(platform)

    def reset(self, sub_ids: list[int], platform: str = SHARED) -> dict[int, float]:
        """初期値（均等）へ戻す。固定も解除する。"""
        share = 100.0 / len(sub_ids) if sub_ids else 0.0
        with self.store._connect() as conn:
            conn.execute("DELETE FROM sub_weights WHERE platform=?", (platform,))
        for sub_id in sub_ids:
            self.set_weight(sub_id, share, platform, locked=False)
        return self.all(platform)

    # ------------------------------------------------------------------
    def record(self, changes: list[WeightChange], data_key: str,
               platform: str = SHARED) -> None:
        stamp = _now()
        with self.store._connect() as conn:
            conn.executemany(
                "INSERT INTO sub_weight_history (evaluated_at, platform, sub_category_id,"
                " weight_before, weight_after, sample_size, score, baseline, data_key, reason)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (stamp, platform, c.sub_category_id, c.before, c.after,
                     c.sample_size, c.score, c.baseline, data_key, c.reason)
                    for c in changes
                ],
            )

    def history(self, limit: int = 50, platform: str = SHARED) -> list[dict]:
        with self.store._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM sub_weight_history WHERE platform=?"
                " ORDER BY evaluated_at DESC, id DESC LIMIT ?",
                (platform, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def last_data_key(self, platform: str = SHARED) -> str:
        """前回の更新で使った測定データの指紋。"""
        with self.store._connect() as conn:
            row = conn.execute(
                "SELECT data_key FROM sub_weight_history WHERE platform=?"
                " ORDER BY evaluated_at DESC, id DESC LIMIT 1",
                (platform,),
            ).fetchone()
        return (row["data_key"] or "") if row else ""

    def last_evaluated_at(self, platform: str = SHARED) -> str:
        with self.store._connect() as conn:
            row = conn.execute(
                "SELECT MAX(evaluated_at) AS t FROM sub_weight_history WHERE platform=?",
                (platform,),
            ).fetchone()
        return (row["t"] or "") if row else ""

    def previous_weights(self, platform: str = SHARED) -> dict[int, float]:
        """前回の更新直前のweight。画面の「前回との差」に使う。"""
        with self.store._connect() as conn:
            row = conn.execute(
                "SELECT evaluated_at FROM sub_weight_history WHERE platform=?"
                " ORDER BY evaluated_at DESC LIMIT 1",
                (platform,),
            ).fetchone()
            if row is None:
                return {}
            rows = conn.execute(
                "SELECT sub_category_id, weight_before FROM sub_weight_history"
                " WHERE platform=? AND evaluated_at=?",
                (platform, row["evaluated_at"]),
            ).fetchall()
        return {int(r["sub_category_id"]): float(r["weight_before"]) for r in rows}


# 旧名（1.14まで）。既存の呼び出しを壊さないための別名。
WeightStore = SubWeightStore


# ----------------------------------------------------------------------
def draw_category(weights: dict[int, float], rng: random.Random) -> int:
    """weightに応じてSubCategoryを1つ引く。"""
    items = [(s, w) for s, w in weights.items() if w > 0]
    if not items:
        raise ValueError("weightが設定されていません")
    total = sum(w for _, w in items)
    threshold = rng.random() * total
    cumulative = 0.0
    for sub_id, weight in items:
        cumulative += weight
        if threshold < cumulative:
            return sub_id
    return items[-1][0]


def _score_of(row: dict, metric: str) -> float | None:
    """1投稿の生の点数。取得できていない指標は None のまま（推測しない）。

    Instagram は 2024-07-02 以降の投稿で impressions を返さないため、
    views → impressions → reach の順に、実際に取れたものを使う。
    """
    value = row.get(metric)
    if value is None and metric == "views":
        for name in VIEW_FALLBACKS:
            value = row.get(name)
            if value is not None:
                break
    return float(value) if value is not None else None


def collect_stats(
    settings: Settings,
    store: ExperimentStore,
    weights: SubWeightStore,
    sub_names: dict[int, str],
    platform: str = SHARED,
) -> tuple[dict[int, SubStat], str]:
    """SubCategoryごとの成績と、使った測定データの指紋を返す。

    - 評価に使うのは、設定した計測区分の、**遅延していない**測定だけ
    - 同じ投稿・同じ計測区分は1件として数える（重複計上しない）
    - 媒体内の相対値に直してから中央値をとる（媒体の規模差を持ち込まない）
    """
    snapshot = settings.weight_snapshot
    metric = settings.weight_metric
    window = settings.weight_window
    wanted = set(sub_names)

    with store._connect() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT m.*, p.platform AS pub_platform, p.published_at AS pub_published_at,"
            "       e.sub_category_id AS exp_sub"
            "  FROM experiment_metrics m"
            "  JOIN experiment_publications p"
            "    ON p.experiment_id = m.experiment_id AND p.platform = m.platform"
            "  JOIN experiments e ON e.experiment_id = m.experiment_id"
            " WHERE m.snapshot = ? AND p.status IN ({})"
            " ORDER BY m.collected_at DESC, m.id DESC".format(",".join("?" * len(DELIVERED))),
            (snapshot, *DELIVERED),
        ).fetchall()]
        delivered = [dict(r) for r in conn.execute(
            "SELECT p.platform AS platform, e.sub_category_id AS exp_sub"
            "  FROM experiment_publications p"
            "  JOIN experiments e ON e.experiment_id = p.experiment_id"
            " WHERE p.status IN ({})".format(",".join("?" * len(DELIVERED))),
            tuple(DELIVERED),
        ).fetchall()]

    current = weights.all(platform)
    locked = weights.locked(platform)
    stats = {
        sub_id: SubStat(sub_category_id=sub_id, name=sub_names.get(sub_id, ""),
                        weight=current.get(sub_id, 0.0), locked=sub_id in locked)
        for sub_id in wanted
    }
    for row in delivered:
        sub_id = row["exp_sub"]
        if sub_id in stats:
            stats[sub_id].posts += 1
            key = row["platform"]
            stats[sub_id].per_platform[key] = stats[sub_id].per_platform.get(key, 0) + 1

    # 1. 同じ投稿・同じ計測区分は1件だけ使う（collected_at の新しい方）
    seen: set[tuple[str, str, str]] = set()
    usable: list[dict] = []
    for row in rows:
        sub_id = row["exp_sub"]
        if sub_id not in stats:
            continue
        key = (row["experiment_id"], row["platform"], row["snapshot"])
        if key in seen:
            continue
        seen.add(key)
        if row.get("late"):
            stats[sub_id].late_skipped += 1
            continue                      # 遅れて測ったものは通常評価に混ぜない
        value = _score_of(row, metric)
        if value is None:
            continue
        usable.append({"sub": sub_id, "platform": row["platform"], "value": value,
                       "id": row["id"]})

    # 2. 媒体内の中央値で割って、規模の違いをそろえる
    by_platform: dict[str, list[float]] = {}
    for row in usable:
        by_platform.setdefault(row["platform"], []).append(row["value"])
    platform_median = {
        name: statistics.median(values) for name, values in by_platform.items() if values
    }

    normalized: dict[int, list[float]] = {}
    raw_values: dict[int, list[float]] = {}
    for row in usable:
        median = platform_median.get(row["platform"]) or 0.0
        # 媒体全体が0のときは割れない。0件の実績も評価から落とさないよう、
        # そのまま（1で割る）扱いにする。0とNULLはここでも区別する。
        divisor = median if median > 0 else 1.0
        normalized.setdefault(row["sub"], []).append(row["value"] / divisor)
        raw_values.setdefault(row["sub"], []).append(row["value"])

    for sub_id, values in normalized.items():
        # 直近N件だけを見る（rows は新しい順）
        picked = values[:window]
        stats[sub_id].sample_size = len(picked)
        stats[sub_id].score = statistics.median(picked) if picked else None
    for sub_id, values in raw_values.items():
        picked = values[:window]
        if picked:
            stats[sub_id].raw_median = statistics.median(picked)

    data_key = _fingerprint(row["id"] for row in usable)
    return stats, data_key


def _fingerprint(metric_ids) -> str:
    """評価に使った測定行の指紋。同じデータでの二重適用を見分ける。"""
    joined = ",".join(str(i) for i in sorted(metric_ids))
    if not joined:
        return ""
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]


# ----------------------------------------------------------------------
def plan_update(settings: Settings, stats: dict[int, SubStat]) -> list[WeightChange]:
    """weightの変更案を作る。保存は呼び出し側で行う。"""
    measured = [s for s in stats.values() if s.score is not None]
    if not measured:
        return []
    baseline = statistics.median([s.score for s in measured])
    if baseline <= 0:
        return []

    bounds = effective_bounds(settings, len(stats))
    step = settings.weight_max_step
    needed = settings.weight_min_samples

    changes: list[WeightChange] = []
    for stat in stats.values():
        before = stat.weight
        common = dict(sub_category_id=stat.sub_category_id, name=stat.name,
                      before=before, sample_size=stat.sample_size, baseline=baseline)
        if stat.locked:
            changes.append(WeightChange(after=before, score=stat.score,
                                        reason="手動で固定中のため変更なし", **common))
            continue
        if stat.score is None:
            note = "反応データがまだ無いため変更なし（探索枠として残す）"
            if stat.late_skipped:
                note += f"／遅れて測った {stat.late_skipped}件は評価に使いません"
            changes.append(WeightChange(after=before, score=None, reason=note, **common))
            continue

        ratio = stat.score / baseline
        # サンプルが足りないものは、動かす幅を比例して小さくする
        confidence = min(1.0, stat.sample_size / needed) if needed else 1.0
        delta = (ratio - 1.0) * settings.weight_sensitivity * confidence
        delta = max(-step, min(step, delta))
        after = max(bounds.low, min(bounds.high, before + delta))

        raw = f"（実数の中央値 {stat.raw_median:.0f}）" if stat.raw_median is not None else ""
        if stat.shortage(needed):
            reason = (f"直近{stat.sample_size}件（{needed}件未満のため控えめに調整）"
                      f"／媒体内の相対値 {stat.score:.2f} は全体の {ratio:.2f}倍{raw}")
        else:
            reason = (f"直近{stat.sample_size}件／媒体内の相対値の中央値 {stat.score:.2f} が"
                      f"全体の {ratio:.2f}倍{raw}")
        if after <= bounds.low + 1e-9 and before > bounds.low:
            reason += "／下限で下げ止まり（探索枠）"
        if after >= bounds.high - 1e-9 and before < bounds.high:
            reason += "／上限で頭打ち"
        if stat.late_skipped:
            reason += f"／遅れて測った {stat.late_skipped}件は除外"
        changes.append(WeightChange(after=after, score=stat.score, reason=reason, **common))

    locked = {s.sub_category_id for s in stats.values() if s.locked}
    return _normalize(changes, settings, locked)


def _normalize(changes: list[WeightChange], settings: Settings,
               locked: set[int] | None = None) -> list[WeightChange]:
    """合計100に揃える。

    単純に比率で割ると、上限・下限で頭打ちになった分が消えて合計が
    100から外れる。余り（不足）を、まだ動けるものへ配り直す。
    固定中のものは動かさない。
    """
    locked = locked or set()
    bounds = effective_bounds(settings, len(changes))
    low, high = bounds.low, bounds.high
    values = {c.sub_category_id: c.after for c in changes}
    movable = [c.sub_category_id for c in changes if c.sub_category_id not in locked]
    if not movable:
        return changes

    fixed_total = sum(values[s] for s in values if s in locked)
    if fixed_total + low * len(movable) > 100 or fixed_total + high * len(movable) < 100:
        # 設定同士が矛盾している。範囲に収めるだけにして、合計は諦める。
        for change in changes:
            if change.sub_category_id not in locked:
                change.after = round(max(low, min(high, change.after)), 2)
                change.reason += "／設定が矛盾しているため合計100に揃えられません"
        return changes

    for sub_id in movable:
        values[sub_id] = max(low, min(high, values[sub_id]))

    for _ in range(100):
        diff = 100.0 - sum(values.values())
        if abs(diff) < 0.005:
            break
        headroom = [
            s for s in movable
            if (diff > 0 and values[s] < high - 1e-9) or (diff < 0 and values[s] > low + 1e-9)
        ]
        if not headroom:
            break
        share = diff / len(headroom)
        for sub_id in headroom:
            values[sub_id] = max(low, min(high, values[sub_id] + share))

    for change in changes:
        change.after = round(values[change.sub_category_id], 2)
    return changes


# ----------------------------------------------------------------------
def auto_enabled(settings: Settings, store: ExperimentStore) -> bool:
    """自動最適化のON/OFF。画面での切り替えが .env より優先される。"""
    flag = store.get_flag("weight_auto")
    if flag is None:
        return settings.weight_auto
    return flag == "1"


def set_auto_enabled(store: ExperimentStore, enabled: bool) -> None:
    store.set_flag("weight_auto", "1" if enabled else "0")


def apply_update(
    settings: Settings,
    store: ExperimentStore,
    weights: SubWeightStore,
    sub_names: dict[int, str],
    platform: str = SHARED,
    dry_run: bool = False,
    force: bool = False,
    log: LogFn = print,
) -> list[WeightChange]:
    """集計 → 変更案 → 保存 までを行う。

    同じ測定データで何度呼ばれても、2回目以降は何も変えない。
    """
    if not auto_enabled(settings, store):
        log("自動最適化は無効です（画面のチェック、または .env の WEIGHT_AUTO）")
        return []
    if not sub_names:
        log("SubCategoryがありません")
        return []

    weights.ensure(list(sub_names), platform)
    stats, data_key = collect_stats(settings, store, weights, sub_names, platform)

    if data_key and data_key == weights.last_data_key(platform) and not force:
        log("前回と同じ測定データなので、weightは変えません（二重適用の防止）")
        return []

    changes = plan_update(settings, stats)
    if not changes:
        log("評価できる反応データがまだありません（weightは変更しません）")
        return []

    moved = [c for c in changes if abs(c.delta) >= 0.01]
    if dry_run:
        log(f"[dry-run] {len(moved)}件が変わります")
        return changes

    for change in changes:
        weights.set_weight(change.sub_category_id, change.after, platform)
    weights.record(changes, data_key, platform)
    log(f"weightを更新しました（{len(moved)}件が変化）")
    return changes
