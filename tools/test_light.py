"""恋愛心理の軽量テンプレート（love_psychology_v2_light）のテスト。

  python tools/test_light.py

実際に画像を書き出して、7枚・文字量・結果計算まで確かめる。
"""

from __future__ import annotations

import json
import random
import subprocess
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from autopost.config import Settings
from autopost.engine import create_experiment
from autopost.experiments import ExperimentStore, Metrics
from autopost import report
from night_test.light import (
    CHOICE_LABELS, HOOK_FORMAT_MARK, LEGACY_VERSION, MAX_CHOICE_CHARS,
    MAX_QUESTION_CHARS, NO_LABEL, PAGE_COUNT, QUESTION_COUNT, TEMPLATE_VERSION,
    YES_LABEL, Choice, LightError, Question, load_all, load_set, pick, validate,
)

LIGHT_DIR = ROOT / "data" / "love_light"


def run(check) -> None:
    sets = load_all(LIGHT_DIR)

    # ---------------------------------------------------------------- 1
    # 内容そのもの
    check("テーマが複数ある", len(sets) >= 3, f"{len(sets)}件")
    check("どのテーマも検証に通る", all(not validate(s) for s in sets))
    check("どのテーマも5問", all(len(s.questions) == QUESTION_COUNT for s in sets))
    check("原則2択", all(len(q.choices) == 2 for s in sets for q in s.questions))
    check("質問が短い",
          all(len(q.body) <= MAX_QUESTION_CHARS for s in sets for q in s.questions),
          str([q.body for s in sets for q in s.questions if len(q.body) > MAX_QUESTION_CHARS]))
    check("選択肢が短い",
          all(len(c.label) <= MAX_CHOICE_CHARS
              for s in sets for q in s.questions for c in q.choices))
    check("フックに「占い」を使っていない", all("占い" not in s.hook for s in sets))
    check("フックが結果テーマを名指ししている",
          all(s.hook_title and s.hook_title in s.hook for s in sets),
          str([s.hook for s in sets]))
    check("テーマ部分を大きく出せる", all(s.hook_title for s in sets))

    # ---- 全問 YES / NO（1枚目で答え方が分かる）----
    check(f"1枚目に「{HOOK_FORMAT_MARK}」が出る",
          all(HOOK_FORMAT_MARK in f"{s.hook} {s.hook_title} {s.hook_suffix}"
              for s in sets),
          str([s.hook_suffix for s in sets]))
    off = [f"{s.axis}/{q.id}: {' / '.join(c.label for c in q.choices)}"
           for s in sets for q in s.questions
           if tuple(c.label for c in q.choices) != CHOICE_LABELS]
    check("全問が YES / NO の2択", not off, " ".join(off[:4]))
    check("YESの位置が全問そろっている（毎回同じ場所にある）",
          all(q.choices[0].label == YES_LABEL
              for s in sets for q in s.questions))
    reversed_q = [f"{s.axis}/{q.id}" for s in sets for q in s.questions
                  if {c.label: c.value for c in q.choices}[YES_LABEL]
                  <= {c.label: c.value for c in q.choices}[NO_LABEL]]
    # 結果ページが「YESの数」なので、YESが加点側でないと数が合わない
    check("YESが必ず加点側（利用者が数えたYESの数＝結果）",
          not reversed_q, " ".join(reversed_q[:4]))
    for light in sets:
        yes_count = sum(1 for q in light.questions
                        if {c.label: c.value for c in q.choices}[YES_LABEL] == 1)
        check(f"{light.axis}: YESを全部選ぶと満点になる",
              light.result_for(yes_count) is light.results[-1],
              f"YES{yes_count}個")
    check("補足説明の欄そのものが無い",
          not any(hasattr(q, "note") or hasattr(q, "closing")
                  for s in sets for q in s.questions))

    # 内部の点数（画面には出さない）
    for light in sets:
        values = {c.value for q in light.questions for c in q.choices}
        check(f"{light.axis}: 点数を内部に持つ", values == {0, 1}, str(values))
        covered = [light.result_for(score) for score in range(QUESTION_COUNT + 1)]
        check(f"{light.axis}: 0〜5点すべてに結果がある", all(covered))
    check("結果は3段階", all(len(s.results) == 3 for s in sets))
    check("結果の説明が2行以内",
          all(len([l for l in r.description.split("\n") if l.strip()]) <= 2
              for s in sets for r in s.results))

    # ---------------------------------------------------------------- 2
    # 長すぎるものは弾く
    broken = replace(sets[0], questions=[
        Question(id="x", text="恋人からいつもより返信が遅く、SNSではオンラインになっている場合、"
                              "あなたならどうしますか？",
                 choices=(Choice("気になる", 1), Choice("気にしない", 0)))
    ] + list(sets[0].questions[1:]))
    problems = validate(broken)
    check("長い質問を弾く", any("質問が長すぎ" in p for p in problems), str(problems[:1]))

    four = replace(sets[0], questions=[
        Question(id="x", text="喧嘩したら？", choices=(
            Choice("すぐ話す", 1), Choice("少し離れる", 0),
            Choice("様子を見る", 0), Choice("何もしない", 0)))
    ] + list(sets[0].questions[1:]))
    check("3択・4択を弾く", any("選択肢は2つ" in p for p in validate(four)))

    long_choice = replace(sets[0], questions=[
        Question(id="x", text="喧嘩したら？", choices=(
            Choice("とりあえず自分から先に連絡してみる", 1), Choice("待つ", 0)))
    ] + list(sets[0].questions[1:]))
    check("長い選択肢を弾く", any("選択肢が長すぎ" in p for p in validate(long_choice)))

    same = replace(sets[0], questions=[
        Question(id="x", text="喧嘩したら？",
                 choices=(Choice("すぐ話す", 1), Choice("少し離れる", 1)))
    ] + list(sets[0].questions[1:]))
    check("どちらを選んでも同じ点数なら弾く",
          any("点数が同じ" in p for p in validate(same)))

    other_labels = replace(sets[0], questions=[
        Question(id="x", text="喧嘩したら\nすぐ話す？",
                 choices=(Choice("すぐ話す", 1), Choice("少し離れる", 0)))
    ] + list(sets[0].questions[1:]))
    check("YES / NO 以外の選択肢を弾く",
          any("YES / NO にしてください" in p for p in validate(other_labels)),
          str(validate(other_labels)[:1]))

    flipped = replace(sets[0], questions=[
        Question(id="x", text="一人でも平気？",
                 choices=(Choice(YES_LABEL, 0), Choice(NO_LABEL, 1)))
    ] + list(sets[0].questions[1:]))
    check("YESが減点側なら弾く（YESの数と結果がずれる）",
          any("YESが加点側" in p for p in validate(flipped)),
          str(validate(flipped)[:1]))

    swapped = replace(sets[0], questions=[
        Question(id="x", text="一人でも平気？",
                 choices=(Choice(NO_LABEL, 0), Choice(YES_LABEL, 1)))
    ] + list(sets[0].questions[1:]))
    check("YESとNOの並び順が逆なら弾く",
          any("YES / NO にしてください" in p for p in validate(swapped)))

    no_mark = replace(sets[0], hook="「隠れ嫉妬度」が分かる5問",
                      hook_title="「隠れ嫉妬度」", hook_suffix="が分かる5問")
    check("1枚目に答え方が無ければ弾く",
          any(HOOK_FORMAT_MARK in p for p in validate(no_mark)),
          str(validate(no_mark)[:1]))

    three_q = replace(sets[0], questions=list(sets[0].questions[:3]))
    check("5問でなければ弾く", any("5問" in p for p in validate(three_q)))

    fortune = replace(sets[0], hook_title="「占い」が分かる5問")
    check("「占い」という表現を弾く", any("占い" in p for p in validate(fortune)))

    # 壊れた内容のファイルは読み込み時点で止まる
    bad = Path(tempfile.mkdtemp()) / "bad.json"
    bad.write_text(json.dumps({
        "axis": "bad", "hook": "「重い」が分かる5問", "hook_title": "「重い」",
        "questions": [{"id": "b1", "text": "あ", "choices": [{"label": "YES", "value": 1}]}],
        "results": [],
    }, ensure_ascii=False), encoding="utf-8")
    try:
        load_set(bad)
        stopped = False
    except LightError:
        stopped = True
    check("不備のあるファイルは読み込み時に止まる", stopped)

    # ---------------------------------------------------------------- 3
    # 実際に生成する
    out = Path(tempfile.mkdtemp())
    result = subprocess.run(
        [sys.executable, "generate.py", "--posts", "3", "--template", "v2_light",
         "--output", str(out / "v2"), "--history", str(out / "v2.json")],
        cwd=ROOT, capture_output=True, text=True, timeout=600)
    check("3本生成できる", result.returncode == 0, (result.stderr or "")[-300:])

    for index in (1, 2, 3):
        folder = out / "v2" / f"post_{index:03d}"
        images = sorted(p.name for p in folder.glob("*.png"))
        meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
        check(f"post_{index:03d}: 7枚ちょうど", len(images) == PAGE_COUNT, f"{len(images)}枚")
        check(f"post_{index:03d}: 並びが フック→Q1〜Q5→結果",
              images == ["01_hook.png"] + [f"{i + 1:02d}_q{i}.png" for i in range(1, 6)]
              + ["07_result.png"], str(images))
        check(f"post_{index:03d}: template_version が入る",
              meta.get("template_version") == TEMPLATE_VERSION,
              str(meta.get("template_version")))
        check(f"post_{index:03d}: 点数を内部に持つ",
              all("score_value" in c for q in meta["questions"] for c in q["choices"]))
        check(f"post_{index:03d}: 結果が3段階", len(meta["results"]) == 3)
        check(f"post_{index:03d}: 説明ページや「結果を見る」ページが無い",
              not any(n for n in images
                      if "intro" in n or "next" in n or "choose" in n or "reveal" in n))

    layout = subprocess.run([sys.executable, "tools/check_layout.py", str(out / "v2")],
                            cwd=ROOT, capture_output=True, text=True, timeout=300)
    check("文字が安全域からはみ出さない", "はみ出しなし" in layout.stdout,
          layout.stdout.strip()[-160:])

    # 画像の寸法は従来どおり
    from PIL import Image

    sample = Image.open(out / "v2" / "post_001" / "01_hook.png")
    check("画像サイズは従来のまま（3:4）", sample.size == (1080, 1440), str(sample.size))

    # テーマを指定して作れる
    fixed = subprocess.run(
        [sys.executable, "generate.py", "--posts", "1", "--template", "v2_light",
         "--axis", "jealousy", "--output", str(out / "j"), "--history", str(out / "j.json")],
        cwd=ROOT, capture_output=True, text=True, timeout=300)
    meta = json.loads((out / "j" / "post_001" / "meta.json").read_text(encoding="utf-8"))
    check("テーマを指定できる", meta.get("result_axis") == "jealousy",
          str(meta.get("result_axis")))
    unknown = subprocess.run(
        [sys.executable, "generate.py", "--posts", "1", "--template", "v2_light",
         "--axis", "nope", "--output", str(out / "n"), "--history", str(out / "n.json")],
        cwd=ROOT, capture_output=True, text=True, timeout=300)
    check("知らないテーマは黙って別のものにしない", unknown.returncode != 0)

    # ---------------------------------------------------------------- 3b
    # ふだんの経路（引数なし・GUI・自動補充）でも7枚になる
    plain = subprocess.run(
        [sys.executable, "generate.py", "--posts", "1",
         "--output", str(out / "plain"), "--history", str(out / "plain.json")],
        cwd=ROOT, capture_output=True, text=True, timeout=300)
    images = sorted(p.name for p in (out / "plain" / "post_001").glob("*.png"))
    check("--template を付けなくても7枚になる", len(images) == PAGE_COUNT,
          f"{len(images)}枚（{plain.stderr.strip()[-120:]}）")

    gui_source = (ROOT / "autopost" / "gui.py").read_text(encoding="utf-8")
    check("GUIが --template を渡す", '"--template", self.template_setting' in gui_source)
    check("GUIにテンプレートの選択肢がある", "def template_choices" in gui_source)
    queue_source = (ROOT / "autopost" / "queueing.py").read_text(encoding="utf-8")
    check("自動補充が --template を渡す", '"--template", settings.template' in queue_source)
    catalog_source = (ROOT / "autopost" / "gui_catalog.py").read_text(encoding="utf-8")
    check("画面からテンプレートを選べる", "template_box" in catalog_source)
    check("設定の既定が軽量7枚", Settings.load().template == "v2_light",
          Settings.load().template)

    # ---------------------------------------------------------------- 4
    # 既存のテンプレートに影響していない
    legacy = subprocess.run(
        [sys.executable, "generate.py", "--posts", "1", "--template", "v1",
         "--tests-per-post", "5", "--hook", "A",
         "--output", str(out / "v1"), "--history", str(out / "v1.json")],
        cwd=ROOT, capture_output=True, text=True, timeout=300)
    names = sorted(p.name for p in (out / "v1" / "post_001").glob("*.png"))
    check("従来のフック構成は11枚のまま", len(names) == 11, f"{len(names)}枚")
    old_style = subprocess.run(
        [sys.executable, "generate.py", "--posts", "1", "--template", "v1",
         "--tests-per-post", "4", "--hook", "none",
         "--output", str(out / "v0"), "--history", str(out / "v0.json")],
        cwd=ROOT, capture_output=True, text=True, timeout=300)
    names = sorted(p.name for p in (out / "v0" / "post_001").glob("*.png"))
    check("さらに従来の占い構成も10枚のまま",
          len(names) == 10 and any("choose" in n for n in names), f"{len(names)}枚")

    # ---------------------------------------------------------------- 5
    # 分析
    settings = Settings.load()
    settings.experiments_db_path = Path(tempfile.mkdtemp()) / "light.db"
    store = ExperimentStore(settings.experiments_db_path)
    rng = random.Random(5)
    for version, (views, rate) in {
        LEGACY_VERSION: (4800, 0.011),
        TEMPLATE_VERSION: (4200, 0.028),
    }.items():
        for _ in range(12):
            value = int(views * rng.uniform(0.85, 1.15))
            experiment = create_experiment(store, ["instagram"],
                                           content_category="dependence",
                                           template_version=version)
            publication = store.publication(experiment.experiment_id, "instagram")
            store.mark_published(publication.id, "x")
            store.save_metrics(Metrics(
                experiment_id=experiment.experiment_id, platform="instagram",
                snapshot="24h", views=value, reach=int(value * 0.8),
                likes=int(value * 0.02), comments=int(value * 0.002),
                saves=int(value * rate), shares=int(value * rate * 0.5),
                template_version=version))

    rows, note = report.template_comparison(settings, store)
    by_version = {row.variant: row for row in rows}
    check("版ごとに集計できる",
          set(by_version) == {LEGACY_VERSION, TEMPLATE_VERSION}, str(set(by_version)))
    check("率で比べられる（表示が少なくても保存率で勝てる）",
          by_version[TEMPLATE_VERSION].save_rate > by_version[LEGACY_VERSION].save_rate
          and by_version[TEMPLATE_VERSION].views < by_version[LEGACY_VERSION].views)
    check("取れない指標は作らない", all(row.watch_time is None for row in rows))
    text = report.template_report(settings, store)
    check("文字のレポートを作れる", "テンプレートの比較" in text and "軽量7枚" in text)
    check("フック比較も壊れていない",
          "1枚目フックの比較" in report.hook_report(settings, store))

    # 過去の投稿は書き換えない
    with store._connect() as conn:
        before = conn.execute(
            "SELECT COUNT(*) c FROM experiments WHERE template_version = ?",
            (LEGACY_VERSION,)).fetchone()["c"]
    report.template_comparison(settings, store)
    with store._connect() as conn:
        after = conn.execute(
            "SELECT COUNT(*) c FROM experiments WHERE template_version = ?",
            (LEGACY_VERSION,)).fetchone()["c"]
    check("集計しても過去のデータを書き換えない", before == after == 12)


def main() -> int:
    failures: list[str] = []

    def check(name: str, condition, detail: str = "") -> None:
        mark = "OK  " if condition else "NG  "
        print(f"  {mark}{name}" + (f" … {detail}" if detail and not condition else ""))
        if not condition:
            failures.append(f"{name}: {detail}" if detail else name)

    print("恋愛心理の軽量テンプレート（7枚）")
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
