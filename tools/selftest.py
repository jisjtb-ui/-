"""リリース前の自己テスト。外部通信もAPIキーも使わない。

  python tools/selftest.py
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    mark = "OK  " if condition else "NG  "
    print(f"  {mark}{name}" + (f" … {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(f"{name}: {detail}" if detail else name)


def section(title: str) -> None:
    print(f"\n{title}")
    print("-" * 52)


def main() -> int:
    section("モジュールの読み込み")
    try:
        import autopost
        import autopost.cli
        import autopost.collector
        import autopost.doctor
        import autopost.engine
        import autopost.migrations
        import autopost.queueing
        import autopost.updater
        import night_test.builder

        check("import", True)
    except Exception as exc:
        check("import", False, str(exc))
        print("\n読み込みに失敗したため以降を中止します")
        return 1

    section("バージョン")
    from autopost.version import bump, changelog_for, current_version, is_newer, parse

    version = current_version()
    check("VERSION が読める", parse(version) != (0, 0, 0), version)
    check("__version__ と一致", autopost.__version__ == version)
    check("比較が正しい", is_newer("1.2.0", "1.1.9") and not is_newer("1.1.0", "1.1.0"))
    check("bump", bump("1.1.0", "minor") == "1.2.0")
    check("CHANGELOGに今のバージョンの記載がある", bool(changelog_for(version)))

    section("更新機能の安全装置")
    from autopost.updater import Manifest, is_protected

    for path in (".env", "autopost.db", ".tokens/threads.json", "history.json",
                 "output/post_001/01_question.png", "autopost.log"):
        check(f"守られる: {path}", is_protected(path))
    for path in ("autopost/cli.py", "data/cta.json", "VERSION", "README.md",
                 ".env.example"):
        check(f"更新対象: {path}", not is_protected(path))

    manifest = Manifest(version="9.9.9", files={"autopost/cli.py": {"sha256": "x"},
                                                ".env": {"sha256": "y"}})
    check("manifestに.envが混ざっても除外する", set(manifest.updatable()) == {"autopost/cli.py"})

    from autopost.updater import UpdateError, _preflight

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "ok.py").write_text("x", encoding="utf-8")
        try:
            _preflight(root, {"ok.py": {}, "new.py": {}}, log=lambda m: None)
            check("置き換え可能なら通る", True)
        except UpdateError as exc:
            check("置き換え可能なら通る", False, str(exc))

        (root / "conflict.py").mkdir()
        try:
            _preflight(root, {"conflict.py": {}}, log=lambda m: None)
            check("同名フォルダがあれば中止する", False, "中止しなかった")
        except UpdateError:
            check("同名フォルダがあれば中止する", True)

    check("v付きでも比較できる", parse("v2.0.1") == (2, 0, 1))

    section("Instagram Reel")
    from autopost.config import Settings as _S
    from autopost.models import ALL_PLATFORMS, MANUAL_PLATFORMS
    from autopost.publishers import get_publisher
    from autopost.oauth.store import TokenStore

    settings0 = _S.load()
    check("チャネルに登録されている", "instagram_reel" in ALL_PLATFORMS)
    check("手動チャネルとして扱われる", "instagram_reel" in MANUAL_PLATFORMS)
    check("認証情報は不要", settings0.missing("instagram_reel") == [])

    reel_pub = get_publisher("instagram_reel", settings0, TokenStore(settings0.token_dir))
    check("公開URLを必要としない", reel_pub.requires_image_url is False)
    try:
        reel_pub.preflight()
        check("ffmpegが使える", True)
    except Exception as exc:
        check("ffmpegが使える", False, str(exc))

    from night_test.video import ReelSpec

    spec = ReelSpec()
    check("問題は答えより長く表示する", spec.seconds_for("01_question.png") > spec.seconds_for("02_answer.png"))
    check("9:16で書き出す", spec.width / spec.height == 1080 / 1920)

    section("アクション別CTAの重み")
    import random
    from collections import Counter
    from night_test.actions import ActionConfig

    action_cfg = ActionConfig.load(ROOT / "data" / "cta_actions.json")
    action_set = action_cfg.get()
    rng = random.Random(12345)
    tier_rank = {t: i for i, t in enumerate(action_set.tiers)}
    counts = {a: Counter() for a in action_set.actions}
    duplicates = 0
    trials = 3000
    for _ in range(trials):
        assigned = action_set.assign(rng)
        if len({x.text for x in assigned}) != len(assigned):
            duplicates += 1
        for item in assigned:
            counts[item.action][item.tier] += 1

    check("1投稿に同じ文言を2回出さない", duplicates == 0, f"{duplicates}件")

    weakest = action_set.tiers[0]

    def mean_rank(action: str) -> float:
        total = sum(counts[action].values())
        return sum(tier_rank[t] * n for t, n in counts[action].items()) / total

    ranks = {a: mean_rank(a) for a in action_set.actions}
    check("4つとも価値のある行動になっている（プロフィールを含まない）",
          "profile" not in action_set.actions, str(action_set.actions))
    check("強さの順が いいね < 保存 < フォロー < 共有",
          ranks["like"] < ranks["save"] < ranks["follow"] < ranks["share"],
          str({k: round(v, 2) for k, v in ranks.items()}))
    for action in action_set.actions:
        check(f"{action_set.action_labels[action]}の結果が固定されていない",
              max(counts[action].values()) < trials * 0.9)
    check("どのアクションにも弱い結果が出うる",
          all(counts[a][weakest] > 0 for a in action_set.actions))

    section("占いの2枚組")
    prompt = action_set.prompt_lines()
    reveal = action_set.reveal_lines(action_set.assign(random.Random(1)))
    joined = " ".join(prompt)
    results = [t for tier in action_set.tiers for t in action_set.results.get(tier, [])]
    check("選ぶページに結果を出さない",
          not any(r in joined for r in results), joined[:80])
    check("選ぶページに4つの選択肢がある",
          all(action_set.action_labels[a] in joined for a in action_set.actions))
    check("答えページに4つの結果が出る", len(reveal) == 5)
    check("差し込み位置が設定で変えられる", isinstance(action_set.insert_after, int))

    section("1枚目のコメント誘導")
    from night_test.cta import CtaConfig

    cta = CtaConfig.load(ROOT / "data" / "cta.json").select()
    check("文言が複数ある", len(cta.comment_prompts) >= 8, str(len(cta.comment_prompts)))
    cycle = cta.comment_prompt_cycle(random.Random(7), 4)
    picked = [next(cycle) for _ in range(len(cta.comment_prompts))]
    check("使い切るまで同じ文を出さない", len(set(picked)) == len(picked))
    check("問題数が文に反映される", all("{n}" not in t for t in picked))
    check("数字1つで答えられる言い回し",
          all(("何番" in t or "番号" in t or "何問目" in t) for t in picked),
          str([t for t in picked if not ("何番" in t or "番号" in t or "何問目" in t)]))
    check("長文を求めていない", all(len(t) <= 28 for t in picked),
          str([t for t in picked if len(t) > 28]))

    section("投稿の順番")
    from night_test.builder import set_sequence_times

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        names = [f"{i:02d}_x.png" for i in range(1, 11)]
        paths = []
        for name in names:
            path = root / name
            path.write_bytes(b"x")
            paths.append(path)
        set_sequence_times(paths)

        by_name = sorted(paths, key=lambda p: p.name)
        by_time = sorted(paths, key=lambda p: p.stat().st_mtime)
        check("名前順と日時順が一致する", by_name == by_time)

        minutes = {int(p.stat().st_mtime // 60) for p in paths}
        check("1枚ずつ別の分になっている", len(minutes) == len(paths),
              f"{len(minutes)}種類")

    section("投稿に含める画像")
    from autopost.loader import IMAGE_RE

    for name in ("01_choose.png", "02_reveal.png", "03_question.png", "10_answer.png"):
        check(f"投稿に含める: {name}", bool(IMAGE_RE.match(name)))
    for name in ("preview.jpg", "meta.json", "caption.txt"):
        check(f"含めない: {name}", not IMAGE_RE.match(name))

    section("Reelの余白")
    from night_test.video import ReelSpec

    rspec = ReelSpec()
    names = [f"{i:02d}_{'question' if i % 2 else 'answer'}.png" for i in range(1, 11)]
    check("冒頭は認知の余白で長い",
          rspec.seconds_for(names[0], 1, 10) > rspec.seconds_for(names[2], 3, 10))
    check("末尾は操作の余白で最も長い",
          rspec.seconds_for(names[9], 10, 10) == max(
              rspec.seconds_for(n, i, 10) for i, n in enumerate(names, 1)))
    check("全体が90秒以内", rspec.total_seconds(names) <= 90,
          f"{rspec.total_seconds(names):.1f}秒")

    section("Worker（スマホからの投稿）")
    node = shutil.which("node")
    if not node:
        print("  --  Node.js が無いため省略（Worker のテストは node worker/test.mjs）")
    else:
        result = subprocess.run([node, "worker/test.mjs"], cwd=ROOT,
                                capture_output=True, text=True, timeout=180)
        for line in result.stdout.splitlines():
            if line.strip().startswith(("OK", "NG")):
                print("  " + line.strip())
        check("Workerの動作", result.returncode == 0,
              (result.stderr or result.stdout).strip()[-300:])

    section("配布物の目録")
    import json as _json

    manifest_path = ROOT / "update_manifest.json"
    if not manifest_path.is_file():
        check("update_manifest.json がある", False, "まだ生成されていません")
    else:
        listed = set(_json.loads(manifest_path.read_text(encoding="utf-8"))["files"])
        # アプリが実際に読み込むPythonファイルは、すべて目録に載っていなければ
        # 利用者のPCへ届かない（v1.14.0で weights.py が届かなかった）
        modules = sorted(
            str(path.relative_to(ROOT)).replace("\\", "/")
            for path in list((ROOT / "autopost").rglob("*.py"))
            + list((ROOT / "night_test").rglob("*.py"))
            + list((ROOT / "tools").glob("*.py"))
            if "__pycache__" not in path.parts
        )
        roots = sorted(
            path.name for path in ROOT.iterdir()
            if path.suffix in (".py", ".bat") and path.is_file()
        )
        buttons = sorted(path.name for path in ROOT.glob("*.bat"))
        missing = [p for p in modules + roots if p not in listed]
        check("Pythonとボタンがすべて目録に載っている",
              not missing, "漏れ: " + ", ".join(missing[:6]))
        check("ボタンが1つ以上載っている",
              all(b in listed for b in buttons),
              "漏れ: " + ", ".join(b for b in buttons if b not in listed))
        data_files = sorted(
            str(path.relative_to(ROOT)).replace("\\", "/")
            for path in (ROOT / "data").rglob("*.json")
        )
        check("data/ のJSONがすべて目録に載っている",
              all(p in listed for p in data_files),
              "漏れ: " + ", ".join(p for p in data_files if p not in listed)[:200])

    result = subprocess.run(["git", "ls-files", "--others", "--exclude-standard"],
                            cwd=ROOT, capture_output=True, text=True)
    leftover = [line for line in result.stdout.splitlines() if line.strip()]
    check("Gitに登録していないファイルが残っていない",
          not leftover, "未登録: " + ", ".join(leftover[:6]))

    section("カテゴリweightの自動最適化")
    import random as _random
    from dataclasses import replace as _replace

    from autopost.config import Settings as _Settings
    from autopost.experiments import Experiment, ExperimentStore, Metrics
    from autopost.weights import (
        WeightStore,
        apply_update,
        collect_stats,
        draw_category,
        effective_bounds,
        plan_update,
    )

    base = _Settings.load()
    many = [f"c{i:02d}" for i in range(21)]
    low, high = effective_bounds(base, len(many))
    check("カテゴリが多いと最低weightが自動で下がる",
          low * len(many) <= 100.0 + 0.01,
          f"最低 {low:.2f}% × {len(many)} = {low * len(many):.1f}%")
    check("カテゴリが少ないときは設定値のまま",
          effective_bounds(base, 4)[0] == base.weight_min)

    rng = _random.Random(11)
    drawn = [draw_category({"a": 90.0, "b": 10.0}, rng) for _ in range(2000)]
    share = drawn.count("a") / len(drawn)
    check("weightの大きいカテゴリがよく出る", 0.85 < share < 0.95, f"{share:.1%}")
    check("weight0のカテゴリは出ない",
          "z" not in {draw_category({"a": 50.0, "z": 0.0}, rng) for _ in range(200)})

    with tempfile.TemporaryDirectory() as tmp:
        settings = _Settings.load()
        settings.experiments_db_path = Path(tmp) / "w.db"
        settings.weight_auto = True
        settings.weight_snapshot = "24h"
        settings.weight_metric = "views"
        settings.weight_min_samples = 3

        store = ExperimentStore(settings.experiments_db_path)
        weights = WeightStore(store)
        categories = ["strong", "weak", "middle", "silent"]
        weights.reset(categories)

        # 投稿 → 反応データ、の1周ぶんを作る
        views = {"strong": 5000, "middle": 1000, "weak": 200}
        for category, value in views.items():
            for i in range(6):
                experiment_id = store.next_experiment_id()
                store.create(Experiment(experiment_id=experiment_id,
                                        content_category=category), ["threads"])
                publication = store.publication(experiment_id, "threads")
                store.mark_published(publication.id, f"x-{experiment_id}")
                store.save_metrics(Metrics(experiment_id=experiment_id, platform="threads",
                                           snapshot="24h", views=value))
        # 1件だけ極端にバズったことにする（中央値なら引きずられない）
        viral = store.next_experiment_id()
        store.create(Experiment(experiment_id=viral, content_category="weak"), ["threads"])
        store.mark_published(store.publication(viral, "threads").id, "x-viral")
        store.save_metrics(Metrics(experiment_id=viral, platform="threads",
                                   snapshot="24h", views=900000))

        stats = collect_stats(settings, store, weights)
        check("投稿数を数えられる", stats["strong"].posts == 6, str(stats["strong"].posts))
        check("中央値が外れ値に引きずられない",
              stats["weak"].score == 200, str(stats["weak"].score))
        check("反応データが無いカテゴリは未測定のまま", stats["silent"].score is None)

        before = weights.all()
        apply_update(settings, store, weights, categories, log=lambda m: None)
        after = weights.all()
        check("成績が良いカテゴリのweightが増える",
              after["strong"] > before["strong"],
              f"{before['strong']:.1f}% → {after['strong']:.1f}%")
        check("成績が悪いカテゴリのweightが減る",
              after["weak"] < before["weak"],
              f"{before['weak']:.1f}% → {after['weak']:.1f}%")
        check("未測定のカテゴリは探索枠として残る", after["silent"] > 0)
        check("1回の変化幅が上限を超えない",
              all(abs(after[c] - before[c]) <= settings.weight_max_step + 0.01
                  for c in categories))
        check("合計が100のまま", abs(sum(after.values()) - 100.0) < 0.5,
              f"{sum(after.values()):.1f}")

        # 何度繰り返しても、合計100と下限は崩れない
        floor = effective_bounds(settings, len(categories))[0]
        for _ in range(30):
            apply_update(settings, store, weights, categories, log=lambda m: None)
        final = weights.all()
        check("繰り返しても合計が100のまま", abs(sum(final.values()) - 100.0) < 0.5,
              f"{sum(final.values()):.1f}")
        check("どのカテゴリも0にならない", min(final.values()) >= floor - 0.01,
              f"最小 {min(final.values()):.2f}% / 下限 {floor:.2f}%")
        check("上限で頭打ちになる", final["strong"] <= settings.weight_max + 0.01,
              f"{final['strong']:.1f}%")

        # 次回の抽選に反映される
        rng = _random.Random(3)
        picks = [draw_category(final, rng) for _ in range(3000)]
        check("更新後のweightが次回の抽選に反映される",
              picks.count("strong") > picks.count("weak"),
              f"strong {picks.count('strong')} / weak {picks.count('weak')}")

        # 手動で固定したカテゴリは動かさない
        weights.set_weight("weak", 20.0)
        weights.set_locked("weak", True)
        locked_before = weights.all()["weak"]
        apply_update(settings, store, weights, categories, log=lambda m: None)
        check("固定したカテゴリは自動更新で動かない",
              abs(weights.all()["weak"] - locked_before) < 0.01,
              f"{locked_before:.1f}% → {weights.all()['weak']:.1f}%")

        check("変更理由が残る", all(h.get("reason") for h in weights.history(limit=10)))

        # 無効にしていれば何も動かさない
        off = _replace(settings, weight_auto=False) if hasattr(settings, "__dataclass_fields__") \
            else settings
        if off is not settings:
            snapshot = weights.all()
            apply_update(off, store, weights, categories, log=lambda m: None)
            check("WEIGHT_AUTO=false なら変更しない", weights.all() == snapshot)

        from autopost import report as _report

        text = _report.build(settings)
        check("成績レポートを作れる",
              "カテゴリ" in text and "strong" in text, text[:120])

    gui_source = (ROOT / "autopost" / "gui.py").read_text(encoding="utf-8")
    check("GUIに成績ボタンがある",
          "成績を見る" in gui_source and "def show_report" in gui_source)
    check("13_成績を見る.bat がある", (ROOT / "13_成績を見る.bat").exists())
    task = (ROOT / "scripts" / "sns_publish_task.cmd").read_bytes()
    check("毎日のタスクにweight更新が入っている", b"weights update" in task)
    check("毎日のタスクは反応データ取得のあとに更新する",
          task.index(b"collect --due") < task.index(b"weights update"))
    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
    check("WEIGHT_* が .env.example に載っている",
          all(f"{k}=" in env_example for k in
              ("WEIGHT_AUTO", "WEIGHT_METRIC", "WEIGHT_SNAPSHOT", "WEIGHT_WINDOW",
               "WEIGHT_MIN_SAMPLES", "WEIGHT_MIN", "WEIGHT_MAX", "WEIGHT_MAX_STEP",
               "WEIGHT_SENSITIVITY")))

    section("CLI")
    for args in (["version"], ["doctor", "--offline", "--out", tempfile.mkstemp(suffix=".txt")[1]],
                 ["update", "--help"], ["reel", "list"]):
        result = subprocess.run([sys.executable, "autopost.py", *args],
                                cwd=ROOT, capture_output=True, text=True, timeout=180)
        check(f"autopost.py {args[0]}", result.returncode == 0,
              (result.stderr or result.stdout).strip()[-200:])

    section("画像生成")
    result = subprocess.run([sys.executable, "generate.py", "--validate"],
                            cwd=ROOT, capture_output=True, text=True, timeout=300)
    check("コンテンツ定義の検証", result.returncode == 0,
          (result.stderr or result.stdout).strip()[-300:])

    section("DBスキーマ")
    from autopost.config import Settings
    from autopost import migrations

    with tempfile.TemporaryDirectory() as tmp:
        settings = Settings.load()
        settings.db_path = Path(tmp) / "a.db"
        settings.experiments_db_path = Path(tmp) / "b.db"
        try:
            migrations.migrate(settings, log=lambda m: None, backup=False)
            migrations.migrate(settings, log=lambda m: None, backup=False)
            check("2回流しても壊れない", True)
        except Exception as exc:
            check("2回流しても壊れない", False, str(exc))

    print("\n" + "=" * 52)
    if FAILURES:
        print(f"失敗 {len(FAILURES)}件")
        for item in FAILURES:
            print(f"  - {item}")
        return 1
    print("すべて通りました")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
