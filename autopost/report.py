"""カテゴリ別の成績とweightを、人が読める形で出す。

「なぜこのカテゴリのweightが上がったのか」を後から確認できることを
いちばん重視している。数字だけ出しても、判断の理由が分からないと
自動最適化を信用できない。
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

from .config import Settings
from .experiments import DELIVERED, ExperimentStore
from .models import ALL_PLATFORMS
from .version import current_version
from .weights import SHARED, WeightStore, collect_stats

LINE = "=" * 60
THIN = "-" * 60


def _published_rows(store: ExperimentStore) -> list[dict]:
    with store._connect() as conn:
        rows = conn.execute(
            "SELECT e.content_category AS category, p.platform AS platform,"
            "       p.published_at AS published_at, p.external_url AS url,"
            "       p.experiment_id AS experiment_id"
            "  FROM experiment_publications p"
            "  JOIN experiments e ON e.experiment_id = p.experiment_id"
            " WHERE p.status IN ({})".format(",".join("?" * len(DELIVERED))),
            DELIVERED,
        ).fetchall()
    return [dict(r) for r in rows]


def _top_posts(store: ExperimentStore, settings: Settings, limit: int, best: bool) -> list[dict]:
    order = "DESC" if best else "ASC"
    metric = settings.weight_metric
    with store._connect() as conn:
        rows = conn.execute(
            f"SELECT e.content_category AS category, m.platform AS platform,"
            f"       m.{metric} AS score, m.experiment_id AS experiment_id"
            f"  FROM experiment_metrics m"
            f"  JOIN experiments e ON e.experiment_id = m.experiment_id"
            f" WHERE m.snapshot = ? AND m.{metric} IS NOT NULL"
            f" ORDER BY m.{metric} {order} LIMIT ?",
            (settings.weight_snapshot, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def build(settings: Settings, platform: str = SHARED) -> str:
    store = ExperimentStore(settings.experiments_db_path)
    weights = WeightStore(store)
    out: list[str] = []
    add = out.append

    add(f"{LINE}\n カテゴリ別の成績と生成割合\n{LINE}")
    add(f"バージョン : v{current_version()}")
    add(f"評価の方法 : {settings.weight_snapshot}時点の {settings.weight_metric} / "
        f"直近{settings.weight_window}件の中央値")
    add(f"自動最適化 : {'有効' if settings.weight_auto else '無効（WEIGHT_AUTO=false）'}")
    last = weights.last_evaluated_at()
    add(f"最終更新   : {last[:16].replace('T', ' ') if last else '（まだ一度も更新していません）'}")

    rows = _published_rows(store)
    add(f"\n{THIN}\n 投稿数\n{THIN}")
    add(f"  総投稿数 : {len(rows)}件")
    per_platform = Counter(r["platform"] for r in rows)
    for name in ALL_PLATFORMS:
        if per_platform.get(name):
            add(f"    {name:<16}{per_platform[name]}件")
    if not rows:
        add("  まだ配信された投稿がありません")

    # 最終Analytics取得日時
    with store._connect() as conn:
        row = conn.execute("SELECT MAX(collected_at) AS t FROM experiment_metrics").fetchone()
    stamp = (row["t"] or "") if row else ""
    add(f"  最終データ取得 : {stamp[:16].replace('T', ' ') if stamp else '（未取得）'}")

    # カテゴリ別
    stats = collect_stats(settings, store, weights, platform)
    add(f"\n{THIN}\n カテゴリ別\n{THIN}")
    if not stats:
        add("  カテゴリがまだ登録されていません")
    else:
        add(f"  {'カテゴリ':<14}{'weight':>8}{'中央値':>10}{'件数':>6}{'配信':>6}")
        for stat in sorted(stats.values(), key=lambda s: -s.weight):
            score = f"{stat.score:,.0f}" if stat.score is not None else "－"
            mark = " 固定" if stat.locked else ""
            add(f"  {stat.category:<14}{stat.weight:7.1f}%{score:>10}"
                f"{stat.sample_size:>6}{stat.posts:>6}{mark}")
        add(f"  {'合計':<14}{sum(s.weight for s in stats.values()):7.1f}%")
        add("  ※ 中央値の「－」は、その時点の反応データがまだ無いという意味です")

    # weightが動いた理由
    history = weights.history(limit=40, platform=platform)
    add(f"\n{THIN}\n 直近のweight変更と、その理由\n{THIN}")
    if not history:
        add("  まだ変更されていません")
    else:
        stamp = history[0]["evaluated_at"]
        shown = [h for h in history if h["evaluated_at"] == stamp]
        add(f"  {stamp[:16].replace('T', ' ')} の更新")
        for item in sorted(shown, key=lambda h: h["weight_after"] - h["weight_before"],
                           reverse=True):
            delta = item["weight_after"] - item["weight_before"]
            if abs(delta) < 0.01:
                continue
            add(f"\n  {item['category']}")
            add(f"    {item['weight_before']:.1f}% → {item['weight_after']:.1f}%"
                f"（{delta:+.1f})")
            add(f"    理由: {item['reason']}")
        if all(abs(h["weight_after"] - h["weight_before"]) < 0.01 for h in shown):
            add("    変化はありませんでした")

    # 推移
    add(f"\n{THIN}\n weightの推移（新しい順）\n{THIN}")
    stamps: list[str] = []
    for item in history:
        if item["evaluated_at"] not in stamps:
            stamps.append(item["evaluated_at"])
    if not stamps:
        add("  記録がありません")
    for stamp in stamps[:5]:
        line = ", ".join(
            f"{h['category']} {h['weight_after']:.0f}%"
            for h in history if h["evaluated_at"] == stamp
        )
        add(f"  {stamp[:16].replace('T', ' ')}  {line}")

    # 上位・下位
    add(f"\n{THIN}\n 上位の投稿 / 下位の投稿（{settings.weight_metric}）\n{THIN}")
    best = _top_posts(store, settings, 5, True)
    worst = _top_posts(store, settings, 5, False)
    if not best:
        add("  反応データがまだありません")
    for label, items in (("上位", best), ("下位", worst)):
        if not items:
            continue
        add(f"  [{label}]")
        for item in items:
            add(f"    {item['score']:>8,.0f}  {item['category']:<14}"
                f"{item['platform']:<12}{item['experiment_id']}")

    add(f"\n{LINE}")
    return "\n".join(out)


def run(settings: Settings, output: Path | None = None) -> int:
    text = build(settings)
    print(text)
    target = output or Path("カテゴリ成績.txt")
    target.write_text(text + "\n", encoding="utf-8")
    print(f"\nこの内容を {target} に保存しました。")
    return 0
