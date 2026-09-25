"""1枚目フック（A/B/Cテスト）のテスト。

  python tools/test_hooks.py

画像も実際に書き出して、枚数・並び・はみ出しまで確かめる。
"""

from __future__ import annotations

import json
import random
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from autopost.config import Settings
from autopost.engine import create_experiment
from autopost.experiments import ExperimentStore, Metrics
from autopost import report
from night_test.hooks import HookConfig, HookError, empty, rotation_index

HOOKS_PATH = ROOT / "data" / "hooks.json"


def run(check) -> None:
    config = HookConfig.load(HOOKS_PATH)

    # ---------------------------------------------------------------- 1
    # テンプレートは data で管理されている
    check("A/B/Cが用意されている", config.ids() == ["A", "B", "C"], str(config.ids()))
    check("文言がコードではなくJSONにある",
          "本音が出る恋愛心理" not in (ROOT / "night_test" / "hooks.py").read_text(encoding="utf-8"))
    check("表示名がある", all(v.label for v in config.usable()))
    check("1枚目は短く保たれている",
          all(len(line) <= 16 for v in config.usable() for line in v.lines),
          str([line for v in config.usable() for line in v.lines if len(line) > 16]))
    check("画面の選択肢を作れる",
          config.choices()[0] == ("rotate", "自動ローテーション")
          and len(config.choices()) == 4, str(config.choices()))

    # D を足せる構造か（JSONに1件足すだけ）
    raw = json.loads(HOOKS_PATH.read_text(encoding="utf-8"))
    raw["variants"].append({"id": "D", "label": "テスト用", "lines": ["あ", "い"]})
    temporary = Path(tempfile.mkdtemp()) / "hooks.json"
    temporary.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    check("JSONに1件足すだけでDを増やせる",
          HookConfig.load(temporary).ids() == ["A", "B", "C", "D"])
    check("増やしてもローテーションが回る",
          len({HookConfig.load(temporary).select(None, i).id for i in range(8)}) == 4)

    # ---------------------------------------------------------------- 2
    # ローテーション
    picked = [config.select(None, i).id for i in range(30)]
    counts = Counter(picked)
    check("A/B/Cが均等に出る", len(set(counts.values())) == 1, str(dict(counts)))

    slots: dict[str, list[int]] = {}
    for index, hook in enumerate(picked[:9]):
        slots.setdefault(hook, []).append(index % 3)
    check("時間帯（1日の何本目か）にも偏らない",
          all(sorted(v) == [0, 1, 2] for v in slots.values()), str(slots))

    check("固定も選べる", config.select("B", 0).id == "B")
    try:
        config.select("Z", 0)
        rejected = False
    except HookError:
        rejected = True
    check("知らないフックは黙って別のものにしない", rejected)
    check("フックなしにできる", empty().is_empty())
    check("順番の計算が安定している", rotation_index(0, 3) == 0 and rotation_index(3, 3) == 1)

    # ---------------------------------------------------------------- 3
    # 実際に生成して枚数と並びを確かめる
    out = Path(tempfile.mkdtemp())
    for variant in ("A", "B", "C"):
        result = subprocess.run(
            [sys.executable, "generate.py", "--posts", "1", "--tests-per-post", "5",
             "--hook", variant, "--category", "hotel",
             "--output", str(out / variant), "--history", str(out / f"{variant}.json")],
            cwd=ROOT, capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            check(f"{variant} を生成できる", False, (result.stderr or "")[-200:])
            continue
        folder = out / variant / "post_001"
        images = sorted(p.name for p in folder.glob("*.png"))
        meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
        check(f"{variant}: 11枚できる", len(images) == 11, f"{len(images)}枚")
        check(f"{variant}: 1枚目がフック", images[0] == "01_hook.png", images[0])
        check(f"{variant}: 2枚目以降が問題→答えの繰り返し",
              images[1:] == [f"{i:02d}_{'question' if i % 2 == 0 else 'answer'}.png"
                             for i in range(2, 12)], str(images[1:3]))
        check(f"{variant}: hook_variant が残る", meta.get("hook_variant") == variant,
              str(meta.get("hook_variant")))
        check(f"{variant}: 文言も残る", meta.get("hook_lines") == list(config.get(variant).lines))
        check(f"{variant}: 占いの2枚組は入らない",
              not any("choose" in n or "reveal" in n for n in images))

    # 1枚目以外が同じ（比べられる状態か）
    def body(variant: str) -> list[str]:
        folder = out / variant / "post_001"
        meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
        return [t["title"] for t in meta.get("tests", [])]

    check("同じ種（seed）なら問題は揃う仕様である",
          len(body("A")) == len(body("B")) == len(body("C")) == 5,
          f"{len(body('A'))} / {len(body('B'))} / {len(body('C'))}")

    # はみ出し
    layout = subprocess.run([sys.executable, "tools/check_layout.py", str(out)],
                            cwd=ROOT, capture_output=True, text=True, timeout=300)
    check("文字が安全域からはみ出さない", "はみ出しなし" in layout.stdout,
          layout.stdout.strip()[-160:])

    # 4問なら9枚（Instagramにも出せる）
    nine = subprocess.run(
        [sys.executable, "generate.py", "--posts", "1", "--tests-per-post", "4",
         "--hook", "A", "--output", str(out / "nine"), "--history", str(out / "nine.json")],
        cwd=ROOT, capture_output=True, text=True, timeout=300)
    images = sorted((out / "nine" / "post_001").glob("*.png"))
    check("問題4問なら9枚（Instagramの10枚上限に収まる）", len(images) == 9, f"{len(images)}枚")
    check("11枚のときはInstagramの上限を警告する",
          "Instagram" in (subprocess.run(
              [sys.executable, "generate.py", "--posts", "1", "--tests-per-post", "5",
               "--dry-run", "--output", str(out / "dry"),
               "--history", str(out / "dry.json")],
              cwd=ROOT, capture_output=True, text=True, timeout=300).stderr))

    # フックなしなら従来どおり
    legacy = subprocess.run(
        [sys.executable, "generate.py", "--posts", "1", "--tests-per-post", "4",
         "--hook", "none", "--output", str(out / "legacy"),
         "--history", str(out / "legacy.json")],
        cwd=ROOT, capture_output=True, text=True, timeout=300)
    names = sorted(p.name for p in (out / "legacy" / "post_001").glob("*.png"))
    check("フックなしなら従来の10枚構成のまま",
          len(names) == 10 and any("choose" in n for n in names), f"{len(names)}枚 {names[:2]}")

    # ---------------------------------------------------------------- 4
    # DBとレポート
    settings = Settings.load()
    settings.experiments_db_path = Path(tempfile.mkdtemp()) / "hook.db"
    store = ExperimentStore(settings.experiments_db_path)

    profile = {"A": (5000, 0.010), "B": (5200, 0.012), "C": (1800, 0.045)}
    rng = random.Random(3)
    for variant, (views, save_rate) in profile.items():
        for _ in range(12):
            value = int(views * rng.uniform(0.85, 1.15))
            experiment = create_experiment(
                store, ["instagram"], content_category="hotel", category_id=1,
                sub_category_id=7, hook_variant=variant)
            publication = store.publication(experiment.experiment_id, "instagram")
            store.mark_published(publication.id, "x")
            store.save_metrics(Metrics(
                experiment_id=experiment.experiment_id, platform="instagram",
                snapshot="24h", views=value, reach=int(value * 0.8),
                likes=int(value * 0.02), comments=int(value * 0.002),
                saves=int(value * save_rate), shares=int(value * save_rate * 0.4),
                hook_variant=variant))

    rows, note = report.hook_comparison(settings, store)
    by_id = {row.variant: row for row in rows}
    check("フック別に集計できる", set(by_id) == {"A", "B", "C"}, str(set(by_id)))
    check("投稿数を数える", all(row.posts == 12 for row in rows))
    check("率で正規化する（表示が少なくても勝てる）",
          by_id["C"].save_rate > by_id["A"].save_rate
          and by_id["C"].views < by_id["A"].views,
          f"C 保存率{by_id['C'].save_rate:.3f} 表示{by_id['C'].views:.0f} / "
          f"A 保存率{by_id['A'].save_rate:.3f} 表示{by_id['A'].views:.0f}")
    check("共有率・コメント率も出る",
          by_id["C"].share_rate is not None and by_id["C"].comment_rate is not None)
    check("取れない指標は作らない（平均視聴時間は空）",
          all(row.watch_time is None for row in rows))
    check("十分な件数がそろえば比較できる状態になる", note["ready"] is True, str(note))

    small = Settings.load()
    small.experiments_db_path = Path(tempfile.mkdtemp()) / "small.db"
    small_store = ExperimentStore(small.experiments_db_path)
    experiment = create_experiment(small_store, ["instagram"], content_category="hotel",
                                   hook_variant="A")
    publication = small_store.publication(experiment.experiment_id, "instagram")
    small_store.mark_published(publication.id, "x")
    small_store.save_metrics(Metrics(experiment_id=experiment.experiment_id,
                                     platform="instagram", snapshot="24h", views=100,
                                     hook_variant="A"))
    _, small_note = report.hook_comparison(small, small_store)
    check("サンプルが少ないうちは「判断しない」と出す", small_note["ready"] is False)
    check("あと何件必要か出す", bool(small_note["missing"]), str(small_note["missing"]))

    text = report.hook_report(settings, store)
    check("文字のレポートを作れる", "1枚目フックの比較" in text and "保存率" in text)
    check("総再生数だけで決めないよう注意書きがある", "総再生数だけで決めない" in text)

    # 自動最適化はまだ始めない
    weights_source = (ROOT / "autopost" / "weights.py").read_text(encoding="utf-8")
    check("フックの自動最適化はまだ有効になっていない",
          "hook" not in weights_source.lower())


def main() -> int:
    failures: list[str] = []

    def check(name: str, condition, detail: str = "") -> None:
        mark = "OK  " if condition else "NG  "
        print(f"  {mark}{name}" + (f" … {detail}" if detail and not condition else ""))
        if not condition:
            failures.append(f"{name}: {detail}" if detail else name)

    print("1枚目フック（A/B/Cテスト）")
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
