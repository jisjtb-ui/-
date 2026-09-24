"""Analytics と SubCategory weight の独立テスト。

  python tools/test_analytics.py          （単体で実行）
  tools/selftest.py からも呼ばれる

外部通信もAPIキーも使わない。Publisher だけ差し替えて、
それ以外は本番の経路を通す。
"""

from __future__ import annotations

import random
import shutil
import sqlite3
import sys
import tempfile
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from autopost.catalog import Catalog, ensure_default
from autopost.collector import (
    AnalyticsCollector,
    DueSnapshot,
    metrics_from_result,
)
from autopost.config import Settings
from autopost.engine import create_experiment
from autopost.experiments import ExperimentStore, Metrics
from autopost.publishers.base import (
    AnalyticsResult,
    MetricNotSupported,
    NotSupported,
    PermissionDenied,
    RateLimited,
)
from autopost.weights import (
    SubWeightStore,
    apply_update,
    auto_enabled,
    collect_stats,
    draw_category,
    effective_bounds,
    plan_update,
    set_auto_enabled,
)

TESTS_DIR = ROOT / "data" / "tests"


def _fresh(**overrides):
    """使い捨てのDBと台帳を用意する。"""
    tmp = Path(tempfile.mkdtemp())
    settings = Settings.load()
    settings.experiments_db_path = tmp / "t.db"
    settings.weight_snapshot = "24h"
    settings.weight_metric = "views"
    settings.weight_window = 20
    settings.weight_min_samples = 5
    for key, value in overrides.items():
        setattr(settings, key, value)
    store = ExperimentStore(settings.experiments_db_path)
    catalog = Catalog(store)
    category = ensure_default(catalog, "テスト用", TESTS_DIR, log=lambda m: None)
    return settings, store, catalog, category, tmp


def _subs(catalog, category, limit=None):
    subs = catalog.sub_categories(category.id)
    return {s.id: s.name for s in (subs[:limit] if limit else subs)}


def _publish(store, catalog, category, sub_id, platforms, published_at,
             content_id="post_001"):
    sub = catalog.sub_category(sub_id)
    experiment = create_experiment(
        store, list(platforms), content_category=sub.source_key,
        category_id=category.id, sub_category_id=sub_id, source_post_id=content_id,
    )
    for platform in platforms:
        publication = store.publication(experiment.experiment_id, platform)
        store.mark_published(publication.id, f"{experiment.experiment_id}|{sub_id}")
        with store._connect() as conn:
            conn.execute("UPDATE experiment_publications SET published_at=? WHERE id=?",
                         (published_at.isoformat(), publication.id))
    return experiment


def _measure(store, catalog, experiment, platform, snapshot, value, late=0,
             metric="views"):
    publication = store.publication(experiment.experiment_id, platform)
    metrics = Metrics(experiment_id=experiment.experiment_id, platform=platform,
                      snapshot=snapshot, late=late,
                      sub_category_id=experiment.sub_category_id)
    setattr(metrics, metric, value)
    return store.save_metrics(metrics)


# ======================================================================
def run(check) -> None:
    now = datetime.now().astimezone()

    # ---------------------------------------------------------------- 1
    # 外れ値1本で評価が大きく動かない
    settings, store, catalog, category, _ = _fresh()
    subs = _subs(catalog, category, 3)
    ids = list(subs)
    weights = SubWeightStore(store)
    weights.reset(ids)
    for index, sub_id in enumerate(ids):
        for n in range(6):
            experiment = _publish(store, catalog, category, sub_id, ["threads"],
                                  now - timedelta(hours=25))
            _measure(store, catalog, experiment, "threads", "24h", 500 + index * 10)
    viral = _publish(store, catalog, category, ids[0], ["threads"], now - timedelta(hours=25))
    _measure(store, catalog, viral, "threads", "24h", 900_000)
    stats, _ = collect_stats(settings, store, weights, subs)
    check("外れ値1本で中央値が動かない",
          abs(stats[ids[0]].raw_median - 500) < 1,
          f"中央値 {stats[ids[0]].raw_median}")

    # ---------------------------------------------------------------- 2
    # サンプル不足のときは控えめにしか動かさない
    settings, store, catalog, category, _ = _fresh(weight_min_samples=10)
    subs = _subs(catalog, category, 4)
    ids = list(subs)
    weights = SubWeightStore(store)
    weights.reset(ids)
    for n in range(2):                      # 10件に届かない
        experiment = _publish(store, catalog, category, ids[0], ["threads"],
                              now - timedelta(hours=25))
        _measure(store, catalog, experiment, "threads", "24h", 9000)
    for sub_id in ids[1:]:
        for n in range(12):
            experiment = _publish(store, catalog, category, sub_id, ["threads"],
                                  now - timedelta(hours=25))
            _measure(store, catalog, experiment, "threads", "24h", 500)
    stats, _ = collect_stats(settings, store, weights, subs)
    changes = {c.sub_category_id: c for c in plan_update(settings, stats)}
    short = changes[ids[0]]
    full = max((c for c in changes.values() if c.sub_category_id != ids[0]),
               key=lambda c: abs(c.delta))
    check("サンプル不足だと動きが小さい",
          abs(short.delta) < settings.weight_max_step,
          f"{short.delta:+.2f}%（上限 {settings.weight_max_step}）")
    check("サンプル不足の表示が出る",
          "サンプル不足" in str(stats[ids[0]].shortage(settings.weight_min_samples))
          or stats[ids[0]].shortage(settings.weight_min_samples))
    check("サンプル不足の理由が残る", "件未満" in short.reason, short.reason[:60])

    # ---------------------------------------------------------------- 3
    # NULL と 0 を区別する
    empty = AnalyticsResult(platform="threads", external_post_id="1")
    zero = AnalyticsResult(platform="threads", external_post_id="1", views=0)
    check("取得できていない指標は空のまま", empty.is_empty() and empty.obtained() == [])
    check("0件は取得できた数字として扱う",
          not zero.is_empty() and zero.obtained() == ["views"])
    settings, store, catalog, category, _ = _fresh()
    subs = _subs(catalog, category, 2)
    ids = list(subs)
    weights = SubWeightStore(store)
    weights.reset(ids)
    experiment = _publish(store, catalog, category, ids[0], ["threads"],
                          now - timedelta(hours=25))
    _measure(store, catalog, experiment, "threads", "24h", 0)
    other = _publish(store, catalog, category, ids[1], ["threads"], now - timedelta(hours=25))
    with store._connect() as conn:
        conn.execute("INSERT INTO experiment_metrics (experiment_id, platform, collected_at,"
                     " snapshot, views) VALUES (?, ?, ?, ?, NULL)",
                     (other.experiment_id, "threads", "2026-09-23T00:00:00+09:00", "24h"))
    stats, _ = collect_stats(settings, store, weights, subs)
    check("0は評価に入る", stats[ids[0]].sample_size == 1, str(stats[ids[0]].sample_size))
    check("NULLは評価に入らない", stats[ids[1]].sample_size == 0,
          str(stats[ids[1]].sample_size))

    # ---------------------------------------------------------------- 4
    # 合計100 / 下限 / 上限 / 最大変化幅 / 固定
    settings, store, catalog, category, _ = _fresh()
    subs = _subs(catalog, category)
    ids = list(subs)
    weights = SubWeightStore(store)
    weights.reset(ids)
    bounds = effective_bounds(settings, len(ids))
    winners = set(ids[:3])
    for sub_id in ids:
        for n in range(8):
            experiment = _publish(store, catalog, category, sub_id, ["threads"],
                                  now - timedelta(hours=25))
            _measure(store, catalog, experiment, "threads", "24h",
                     5000 if sub_id in winners else 400)
    weights.set_weight(ids[-1], 12.0)
    weights.set_locked(ids[-1], True)
    locked_before = weights.all()[ids[-1]]
    before = weights.all()
    for _ in range(25):
        apply_update(settings, store, weights, subs, force=True, log=lambda m: None)
        step_ok = all(
            abs(weights.all()[s] - before[s]) <= settings.weight_max_step + 0.01
            for s in ids if s != ids[-1]
        )
        if not step_ok:
            break
        before = weights.all()
    final = weights.all()
    check("合計が100のまま", abs(sum(final.values()) - 100) < 0.5, f"{sum(final.values()):.1f}")
    check("下限を割らない（探索枠が残る）", min(final.values()) >= bounds.low - 0.01,
          f"最小 {min(final.values()):.2f}% / 下限 {bounds.low:.2f}%")
    check("上限を超えない", max(final.values()) <= bounds.high + 0.01,
          f"最大 {max(final.values()):.2f}%")
    check("1回の変化幅が上限内", step_ok)
    check("固定したものは動かない", abs(final[ids[-1]] - locked_before) < 0.01,
          f"{locked_before:.1f}% → {final[ids[-1]]:.1f}%")
    check("成績の良いものが増えた", sum(final[s] for s in winners) > 3 * (100 / len(ids)),
          f"{sum(final[s] for s in winners):.1f}%")

    # ---------------------------------------------------------------- 5
    # 実現不能な設定を、理由つきで調整する
    tight = Settings.load()
    tight.weight_min, tight.weight_max = 5.0, 40.0
    many = effective_bounds(tight, 21)
    check("下限×件数が100%を超える設定を調整する",
          many.low * 21 <= 100.01 and many.adjusted,
          f"{many.low:.2f}% × 21 = {many.low * 21:.1f}%")
    check("調整した理由を出せる", "100%を超える" in many.reason, many.reason)
    check("成立する設定はそのまま", not effective_bounds(tight, 4).adjusted)
    narrow = Settings.load()
    narrow.weight_min, narrow.weight_max = 1.0, 2.0
    check("上限が均等配分より小さい設定も調整する",
          effective_bounds(narrow, 21).adjusted)

    # ---------------------------------------------------------------- 6
    # 自動OFF と 初期値復元
    settings, store, catalog, category, _ = _fresh()
    subs = _subs(catalog, category, 4)
    ids = list(subs)
    weights = SubWeightStore(store)
    weights.reset(ids)
    for sub_id in ids:
        for n in range(8):
            experiment = _publish(store, catalog, category, sub_id, ["threads"],
                                  now - timedelta(hours=25))
            _measure(store, catalog, experiment, "threads", "24h",
                     5000 if sub_id == ids[0] else 300)
    apply_update(settings, store, weights, subs, log=lambda m: None)
    skewed = weights.all()
    set_auto_enabled(store, False)
    check("画面のOFFが設定より優先される", not auto_enabled(settings, store))
    apply_update(settings, store, weights, subs, force=True, log=lambda m: None)
    check("OFFなら更新しない", weights.all() == skewed)
    rng = random.Random(1)
    picks = Counter(draw_category(weights.all(), rng) for _ in range(1500))
    check("OFFでも設定済みweightで抽選できる",
          picks[ids[0]] > picks[ids[1]], f"{picks[ids[0]]} vs {picks[ids[1]]}")
    set_auto_enabled(store, True)
    weights.reset(ids)
    restored = weights.all()
    check("初期値へ戻せる",
          all(abs(w - 100 / len(ids)) < 0.01 for w in restored.values()),
          str(restored))
    check("初期値へ戻すと固定も解除される", not weights.locked())

    # ---------------------------------------------------------------- 7
    # 同じAnalyticsで二重に更新しない
    settings, store, catalog, category, _ = _fresh()
    subs = _subs(catalog, category, 4)
    ids = list(subs)
    weights = SubWeightStore(store)
    weights.reset(ids)
    for sub_id in ids:
        for n in range(8):
            experiment = _publish(store, catalog, category, sub_id, ["threads"],
                                  now - timedelta(hours=25))
            _measure(store, catalog, experiment, "threads", "24h",
                     4000 if sub_id == ids[0] else 300)
    apply_update(settings, store, weights, subs, log=lambda m: None)
    once = weights.all()
    apply_update(settings, store, weights, subs, log=lambda m: None)
    check("同じデータでもう一度呼んでも変わらない", weights.all() == once)
    apply_update(settings, store, weights, subs, log=lambda m: None)
    check("何度呼んでも変わらない", weights.all() == once)
    experiment = _publish(store, catalog, category, ids[1], ["threads"],
                          now - timedelta(hours=25))
    _measure(store, catalog, experiment, "threads", "24h", 9000)
    apply_update(settings, store, weights, subs, log=lambda m: None)
    check("新しいデータが来たら更新する", weights.all() != once)

    # 同じ投稿・同じ区分は1件しか保存しない（同時実行対策）
    settings, store, catalog, category, _ = _fresh()
    subs = _subs(catalog, category, 2)
    experiment = _publish(store, catalog, category, list(subs)[0], ["threads"],
                          now - timedelta(hours=25))
    first = _measure(store, catalog, experiment, "threads", "24h", 100)
    second = _measure(store, catalog, experiment, "threads", "24h", 999)
    with store._connect() as conn:
        rows = conn.execute("SELECT views FROM experiment_metrics").fetchall()
    check("同じ投稿・同じ区分は1件だけ", first and not second and len(rows) == 1,
          f"{[r['views'] for r in rows]}")
    check("先に取れた数字を上書きしない", rows[0]["views"] == 100)

    # ---------------------------------------------------------------- 8
    # 失敗・429・権限不足で過去データを壊さない
    settings, store, catalog, category, _ = _fresh()
    subs = _subs(catalog, category, 2)
    sub_id = list(subs)[0]
    experiment = _publish(store, catalog, category, sub_id, ["threads"],
                          now - timedelta(hours=80))
    _measure(store, catalog, experiment, "threads", "24h", 1234)

    class Failing:
        def __init__(self, error): self.error = error
        def get_analytics(self, external_post_id): raise self.error

    for label, error, outcome in (
        ("通信・応答の失敗", RuntimeError("壊れた応答"), "failed"),
        ("レート制限", RateLimited("429", retry_after=5), "rate_limited"),
        ("権限不足", PermissionDenied("権限がありません"), "failed"),
        ("取得手段が無い", NotSupported("下書きのため公開IDが無い"), "unavailable"),
    ):
        collector = AnalyticsCollector(settings, store, log=lambda m: None)
        collector._publishers = {"threads": Failing(error)}
        report = collector.collect_due(now=now)
        with store._connect() as conn:
            kept = conn.execute(
                "SELECT views FROM experiment_metrics WHERE snapshot='24h'").fetchone()
        check(f"{label}でも過去データが残る", kept and kept["views"] == 1234)
        check(f"{label}を結果として記録する",
              outcome in store.attempt_counts() or report.failed,
              str(store.attempt_counts()))

    # 空の結果を測定済みにしない
    class Empty:
        def get_analytics(self, external_post_id):
            return AnalyticsResult(platform="threads", external_post_id=external_post_id)

    other = _publish(store, catalog, category, sub_id, ["threads"], now - timedelta(hours=25))
    collector = AnalyticsCollector(settings, store, log=lambda m: None)
    collector._publishers = {"threads": Empty()}
    collector.collect_due(now=now)
    got = store.collected_snapshots(other.experiment_id, "threads")
    check("空の結果を測定済みにしない", not got, str(got))

    # ---------------------------------------------------------------- 9
    # 取得の失敗が投稿処理を止めない
    settings, store, catalog, category, _ = _fresh()
    subs = _subs(catalog, category, 2)
    sub_id = list(subs)[0]
    good = _publish(store, catalog, category, sub_id, ["threads", "instagram"],
                    now - timedelta(hours=25))

    class HalfBroken:
        def __init__(self, platform, ok): self.platform, self.ok = platform, ok
        def get_analytics(self, external_post_id):
            if not self.ok:
                raise PermissionDenied("権限がありません")
            return AnalyticsResult(platform=self.platform,
                                   external_post_id=external_post_id, views=777)

    collector = AnalyticsCollector(settings, store, log=lambda m: None)
    collector._publishers = {"threads": HalfBroken("threads", False),
                             "instagram": HalfBroken("instagram", True)}
    report = collector.collect_due(now=now)
    check("片方が失敗しても、もう片方は取得できる",
          len(report.collected) == 1 and len(report.failed) == 1,
          f"取得 {report.collected} / 失敗 {report.failed}")
    publication = store.publication(good.experiment_id, "threads")
    check("取得の失敗で配信状態を変えない", publication.status in ("published", "draft_created"),
          publication.status)

    # --------------------------------------------------------------- 10
    # 遅延測定を、過去時点の測定として扱わない
    settings, store, catalog, category, _ = _fresh()
    subs = _subs(catalog, category, 2)
    sub_id = list(subs)[0]
    stale = _publish(store, catalog, category, sub_id, ["threads"],
                     now - timedelta(hours=168))
    collector = AnalyticsCollector(settings, store, log=lambda m: None)
    due = collector.due_snapshots(store.publication(stale.experiment_id, "threads"), now=now)
    check("1週間前の投稿を「1h」として測らない",
          due and due[0].label != "1h", str(due[0].label if due else None))
    check("経過時間に合う区分を選ぶ", due and due[0].label == "7d",
          str(due[0].label if due else None))
    check("実際の経過時間を残す", due and abs(due[0].elapsed - 168) < 1,
          f"{due[0].elapsed:.1f}h")

    # どの区分の許容時間も過ぎている場合は「遅延」にする
    ancient = _publish(store, catalog, category, sub_id, ["threads"],
                       now - timedelta(hours=400))
    late_due = collector.due_snapshots(
        store.publication(ancient.experiment_id, "threads"), now=now)
    check("許容時間を過ぎていれば遅延にする", late_due and late_due[0].late,
          str(late_due[0].label if late_due else None))
    check("遅延でも実際の経過時間を残す",
          late_due and abs(late_due[0].elapsed - 400) < 2,
          f"{late_due[0].elapsed:.1f}h" if late_due else "なし")

    # 後ろの区分を測ったあとに、過ぎた前の区分を後追いしない
    _measure(store, catalog, stale, "threads", "7d", 100)
    after = collector.due_snapshots(
        store.publication(stale.experiment_id, "threads"), now=now)
    check("測り終えたあとに前の区分を後追いしない", not after,
          str([d.label for d in after]))

    fresh = _publish(store, catalog, category, sub_id, ["threads"], now - timedelta(hours=25))
    on_time = collector.due_snapshots(store.publication(fresh.experiment_id, "threads"),
                                      now=now)
    check("時間内なら遅延にしない",
          on_time and on_time[0].label == "24h" and not on_time[0].late,
          str(on_time[0].label if on_time else None))

    weights = SubWeightStore(store)
    weights.reset(list(subs))
    _measure(store, catalog, stale, "threads", "72h", 50_000, late=1)
    _measure(store, catalog, fresh, "threads", "24h", 600)
    stats, _ = collect_stats(settings, store, weights, subs)
    check("遅延測定を24h評価に混ぜない",
          stats[sub_id].raw_median == 600, str(stats[sub_id].raw_median))
    check("除いた遅延の件数を数える", stats[sub_id].late_skipped >= 0)

    # --------------------------------------------------------------- 11
    # 媒体の規模差で偏らない
    settings, store, catalog, category, _ = _fresh()
    subs = _subs(catalog, category, 2)
    small, large = list(subs)
    weights = SubWeightStore(store)
    weights.reset([small, large])
    # small は Threads（規模が小さい）だけ、large は Instagram（規模が大きい）だけ。
    # 媒体内では同じくらいの成績なので、評価はほぼ並ぶべき。
    for n in range(8):
        experiment = _publish(store, catalog, category, small, ["threads"],
                              now - timedelta(hours=25))
        _measure(store, catalog, experiment, "threads", "24h", 1000)
        experiment = _publish(store, catalog, category, large, ["instagram"],
                              now - timedelta(hours=25))
        _measure(store, catalog, experiment, "instagram", "24h", 50_000)
    stats, _ = collect_stats(settings, store, weights, subs)
    check("媒体内の相対値にそろえる",
          abs(stats[small].score - stats[large].score) < 0.1,
          f"{stats[small].score:.2f} vs {stats[large].score:.2f}")
    check("実数の中央値も残す",
          stats[small].raw_median == 1000 and stats[large].raw_median == 50_000)
    changes = {c.sub_category_id: c for c in plan_update(settings, stats)}
    check("規模の大きい媒体だけで決まらない",
          abs(changes[small].delta - changes[large].delta) < 0.5,
          f"{changes[small].delta:+.2f} vs {changes[large].delta:+.2f}")

    # --------------------------------------------------------------- 12
    # 既存DBの移行とデータ保持
    legacy = Path(tempfile.mkdtemp()) / "legacy.db"
    conn = sqlite3.connect(legacy)
    conn.executescript(
        "CREATE TABLE experiments (experiment_id TEXT PRIMARY KEY, hypothesis TEXT,"
        " content_category TEXT, hook TEXT, text TEXT, image_prompt TEXT, image_url TEXT,"
        " link TEXT, source_post_id TEXT, source_folder TEXT, tags TEXT, extra TEXT,"
        " created_at TEXT, updated_at TEXT);"
        "CREATE TABLE experiment_publications (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " experiment_id TEXT, platform TEXT, status TEXT, external_post_id TEXT,"
        " external_url TEXT, published_at TEXT, error_message TEXT, retry_count INTEGER,"
        " last_attempt_at TEXT, extra TEXT, created_at TEXT, updated_at TEXT,"
        " UNIQUE(experiment_id, platform));"
        "CREATE TABLE experiment_metrics (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " experiment_id TEXT, platform TEXT, collected_at TEXT, impressions INTEGER,"
        " views INTEGER, likes INTEGER, comments INTEGER, shares INTEGER, saves INTEGER,"
        " clicks INTEGER, followers_gained INTEGER, period_start TEXT, period_end TEXT,"
        " platform_metrics TEXT);"
        "INSERT INTO experiments VALUES ('old-1','','hotel','','','','','','post_009','',"
        "  '[]','{}','2026-01-01','2026-01-01');"
        "INSERT INTO experiment_publications (experiment_id, platform, status, published_at)"
        "  VALUES ('old-1','threads','published','2026-01-01T21:00:00+09:00');"
        "INSERT INTO experiment_metrics (experiment_id, platform, collected_at, views)"
        "  VALUES ('old-1','threads','2026-01-02T21:00:00+09:00', 4321);"
    )
    conn.commit(); conn.close()
    migrated = ExperimentStore(legacy)
    with migrated._connect() as conn:
        kept = conn.execute("SELECT views FROM experiment_metrics").fetchone()
        experiment = conn.execute("SELECT * FROM experiments").fetchone()
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        tables = {r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    check("古いDBを開いてもデータが残る", kept and kept["views"] == 4321)
    check("移行後も整合性が保たれる", integrity == "ok", integrity)
    check("新しいテーブルが足される",
          {"categories", "sub_categories", "social_accounts", "sub_weights",
           "metric_attempts", "app_flags"} <= tables)
    check("既存行のSubCategoryは推測しない",
          experiment["sub_category_id"] is None)
    ExperimentStore(legacy)              # 2回目
    with migrated._connect() as conn:
        again = conn.execute("SELECT COUNT(*) c FROM experiment_metrics").fetchone()["c"]
    check("2回開いても増えない・壊れない", again == 1, str(again))

    # --------------------------------------------------------------- 13
    # Category / SubCategory：名前を打ち直させない
    settings, store, catalog, category, tmp = _fresh()
    subs = catalog.sub_categories(category.id)
    check("SubCategoryが自動で用意される", len(subs) >= 10, f"{len(subs)}件")
    check("表示名は data/tests の label から来る",
          all(s.name and s.source_key for s in subs))
    check("内部の紐付けはIDで行う", all(isinstance(s.id, int) for s in subs))
    second = catalog.add_category("別のCategory")
    check("Categoryごとに独立している",
          catalog.sub_categories(second.id) == []
          or all(s.category_id == second.id for s in catalog.sub_categories(second.id)))
    catalog.link_account(category.id, "threads", account_id="111", account_name="@a")
    catalog.link_account(second.id, "threads", account_id="222", account_name="@b")
    check("Categoryごとに別のアカウントを持てる",
          catalog.account(category.id, "threads").account_id == "111"
          and catalog.account(second.id, "threads").account_id == "222")
    check("投稿先はCategoryから自動で決まる",
          catalog.connected_platforms(category.id) == ["threads"])
    from autopost.oauth.store import Token, TokenStore

    token_dir = tmp / "tokens"
    TokenStore(token_dir).save(Token(platform="threads", access_token="LEGACY",
                                     account_name="@legacy"))
    scoped = TokenStore(token_dir, category.id)
    check("Category分け前の接続を引き継げる",
          scoped.adopt_legacy("threads") and scoped.load("threads").account_name == "@legacy")
    check("別Categoryのトークンは混ざらない",
          TokenStore(token_dir, second.id).load("threads") is None)

    # --------------------------------------------------------------- 13b
    # 使えない指標が1つ混ざっても、他の指標を取り切る
    from unittest.mock import patch

    from autopost.oauth.store import Token as _Token, TokenStore as _TokenStore
    from autopost.publishers import instagram as _ig

    token_store = _TokenStore(Path(tempfile.mkdtemp()))
    token_store.save(_Token(platform="instagram", access_token="T", account_id="1"))

    def _probe(responder):
        calls = []

        def wrapped(method, url, **kwargs):
            metric = kwargs.get("params", {}).get("metric", "")
            calls.append(metric)
            return responder(metric)

        publisher = _ig.InstagramPublisher(Settings.load(), token_store)
        publisher._token = token_store.load("instagram")
        with patch.object(_ig, "request_json", wrapped), \
             patch.object(_ig.meta_oauth, "graph_base", lambda s: "https://graph"), \
             patch.object(publisher, "preflight", lambda: None):
            return publisher.get_analytics("999"), calls

    def _refuse_views(metric):
        wanted = [m.strip() for m in metric.split(",")]
        if "views" in wanted and len(wanted) > 1:
            return {"error": {"code": 100,
                              "message": "metrics are not valid for this media"
                                         " product type: views"}}
        if wanted == ["views"]:
            return {"error": {"code": 100,
                              "message": "metric must be one of the following values"}}
        return {"data": [{"name": m, "total_value": {"value": 111}}
                         for m in wanted if m != "views"]}

    result, calls = _probe(_refuse_views)
    check("使えない指標が混ざっても他の指標を取り切る",
          "reach" in result.obtained() and "likes" in result.obtained(),
          str(result.obtained()))
    check("使えない指標は0ではなく未取得のまま", result.views is None, str(result.views))

    def _ok_partial(metric):
        wanted = [m.strip() for m in metric.split(",")]
        return {"data": [{"name": m, "total_value": {"value": 5}} for m in wanted
                         if m in ("views", "reach", "likes")]}

    result, calls = _probe(_ok_partial)
    check("断られていなければ余計に問い合わせない", len(calls) == 1, f"{len(calls)}回")

    def _permission_error(metric):
        return {"error": {"code": 10, "message": "権限がありません"}}

    try:
        _probe(_permission_error)
        denied = False
    except PermissionDenied:
        denied = True
    check("権限不足は握りつぶさず呼び出し側へ返す", denied)

    def _nothing(metric):
        return {"data": []}

    try:
        _probe(_nothing)
        raised = False
    except Exception:
        raised = True
    check("1つも取れないときは測定済みにしない（例外にする）", raised)

    # --------------------------------------------------------------- 14
    # 生成 → 模擬投稿 → 取得 → 集計 → 更新 → 次回抽選 の一周
    settings, store, catalog, category, _ = _fresh()
    subs = _subs(catalog, category)
    ids = list(subs)
    weights = SubWeightStore(store)
    weights.reset(ids)
    winners = set(ids[:3])

    class Stub:
        """Threadsは規模が小さく、Instagramは大きい。"""

        def __init__(self, platform, scale):
            self.platform, self.scale = platform, scale

        def get_analytics(self, external_post_id):
            sub_id = int(external_post_id.split("|")[1])
            base = (3000 if sub_id in winners else 500) * self.scale
            views = base + random.Random(external_post_id).randint(-30, 30) * self.scale
            result = AnalyticsResult(platform=self.platform,
                                     external_post_id=external_post_id, views=int(views))
            if self.platform == "instagram":
                result.reach = int(views * 0.8)
            return result

    rng = random.Random(7)
    first_share = last_share = 0.0
    for round_number in range(6):
        table = weights.all()
        drawn = [draw_category(table, rng) for _ in range(40)]
        for index, sub_id in enumerate(drawn):
            _publish(store, catalog, category, sub_id, ["threads", "instagram"],
                     now - timedelta(hours=25), content_id=f"post_{index:03d}")
        collector = AnalyticsCollector(settings, store, log=lambda m: None)
        collector._publishers = {"threads": Stub("threads", 1),
                                 "instagram": Stub("instagram", 15)}
        collector.collect_due(now=now)
        apply_update(settings, store, weights, subs, log=lambda m: None)
        share = sum(1 for s in drawn if s in winners) / len(drawn)
        if round_number == 0:
            first_share = share
        last_share = share

    final = weights.all()
    check("一周まわって成績の良いものが増える", last_share > first_share,
          f"{first_share:.0%} → {last_share:.0%}")
    check("一周後も合計100", abs(sum(final.values()) - 100) < 0.5,
          f"{sum(final.values()):.1f}")
    check("一周後も探索枠が残る",
          min(final.values()) >= effective_bounds(settings, len(ids)).low - 0.01,
          f"最小 {min(final.values()):.2f}%")
    check("実際の抽選に反映される",
          sum(final[s] for s in winners) > 3 * (100 / len(ids)),
          f"{sum(final[s] for s in winners):.1f}%")
    check("取得結果が記録されている", store.attempt_counts().get("measured", 0) > 0,
          str(store.attempt_counts()))
    check("追跡項目が保存されている", _traceable(store))

    # 画面の数字が同じ経路から出る
    from autopost import report as report_module

    data = report_module.overview(settings, store, category_id=category.id)
    check("画面の一覧が作れる", len(data.rows) == len(ids), f"{len(data.rows)}行")
    check("画面に変更理由が出る", any(r.reason for r in data.rows))
    check("画面に前回差が出る", any(r.delta is not None for r in data.rows))
    check("画面に上位・下位の投稿が出る", bool(data.best) and bool(data.worst))
    check("文字の報告も作れる",
          "SubCategory別" in report_module.build(settings, category_id=category.id))


def _traceable(store) -> bool:
    """投稿ID・媒体・公開日時・SubCategory・content_id・経過時間が追える。"""
    with store._connect() as conn:
        row = conn.execute(
            "SELECT experiment_id, platform, published_at, sub_category_id, content_id,"
            "       snapshot, hours_since_post, window_hours, external_post_id"
            "  FROM experiment_metrics WHERE snapshot<>'' LIMIT 1").fetchone()
    if row is None:
        return False
    return all([
        row["experiment_id"], row["platform"], row["published_at"],
        row["sub_category_id"] is not None, row["content_id"], row["snapshot"],
        row["hours_since_post"] is not None, row["window_hours"] is not None,
        row["external_post_id"],
    ])


# ======================================================================
def main() -> int:
    failures: list[str] = []

    def check(name: str, condition, detail: str = "") -> None:
        mark = "OK  " if condition else "NG  "
        print(f"  {mark}{name}" + (f" … {detail}" if detail and not condition else ""))
        if not condition:
            failures.append(f"{name}: {detail}" if detail else name)

    print("Analytics と SubCategory weight")
    print("-" * 52)
    run(check)
    print("-" * 52)
    if failures:
        print(f"失敗 {len(failures)}件")
        for item in failures:
            print(f"  - {item}")
        return 1
    print("すべて通りました")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
