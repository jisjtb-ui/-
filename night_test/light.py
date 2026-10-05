"""恋愛心理カテゴリの軽量テンプレート（love_psychology_v2_light）。

考え方:
  短く要約するのではなく、**無くても成立する情報を作らない**。
  1枚 = 1判断。読んで理解して考えて選ぶ、ではなく、見て直感で選んでスワイプ。

構成（7枚固定）:
  1枚目  フック（何が分かるか＋「YES or NO」と明示する）
  2〜6   Q1〜Q5（**全問 YES / NO の2択**）
  7枚目  結果（YESの数で3段階）

答え方を1枚目で言い切るので、2枚目以降は選択肢を読まなくても答えられる。
置き場所も YES が左（上）で固定。考えるのは問いだけ。

従来の構成（占い2枚＋4問×2枚など）はそのまま残す。
このテンプレートは恋愛心理カテゴリの新規投稿にだけ使う。
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from pathlib import Path

TEMPLATE_VERSION = "love_psychology_v2_light"
LEGACY_VERSION = "love_psychology_v1"
PAGE_COUNT = 7
QUESTION_COUNT = 5

# 文字数の上限。厳密な数より「見た瞬間に選べるか」が目的だが、
# 生成側が長い文を作ってしまったときに気づけるよう、ここで弾く。
MAX_QUESTION_CHARS = 18      # 改行を除いた本文
MAX_CHOICE_CHARS = 8
MAX_CHOICES = 2

# 選択肢は全問これだけ。順番も固定（毎回同じ位置にある＝読まずに選べる）
YES_LABEL = "YES"
NO_LABEL = "NO"
CHOICE_LABELS = (YES_LABEL, NO_LABEL)
# 1枚目に必ず出す。答え方が分かっていないと1問目で止まる
HOOK_FORMAT_MARK = "YES or NO"
MAX_RESULT_NAME_CHARS = 12
MAX_RESULT_LINES = 2
MAX_RESULT_LINE_CHARS = 16
MAX_HOOK_CHARS = 24


class LightError(RuntimeError):
    """軽量テンプレートの内容に不備がある。"""


@dataclass(frozen=True)
class Choice:
    label: str
    value: int = 0            # 内部だけで使う点数。画面には出さない


@dataclass(frozen=True)
class Question:
    id: str
    text: str
    choices: tuple[Choice, ...]

    @property
    def body(self) -> str:
        """改行を除いた本文（文字数の判定に使う）。"""
        return self.text.replace("\n", "")


@dataclass(frozen=True)
class Result:
    min: int
    max: int
    name: str
    description: str = ""

    @property
    def span(self) -> str:
        return f"{self.min}〜{self.max}" if self.min != self.max else str(self.min)


@dataclass
class LightSet:
    """1テーマぶん（フック + 5問 + 結果3段階）。"""

    axis: str
    label: str
    hook: str                 # 表示用の全文（meta に残す）
    hook_title: str = ""      # テーマ。1枚目でいちばん大きく出す
    hook_suffix: str = ""     # 「が分かる5問」。小さく添える
    questions: list[Question] = field(default_factory=list)
    results: list[Result] = field(default_factory=list)
    score_direction: str = ""

    def result_for(self, score: int) -> Result | None:
        for result in self.results:
            if result.min <= score <= result.max:
                return result
        return None

    def tiers(self) -> list[tuple[str, str, str]]:
        """結果ページに並べる（範囲, タイプ名, ひとこと）。"""
        return [(r.span, r.name, r.description) for r in self.results]

    def to_meta(self) -> dict:
        """meta.json に残す形。点数の対応も内部データとして残す。"""
        return {
            "template_version": TEMPLATE_VERSION,
            "result_axis": self.axis,
            "result_label": self.label,
            "hook_text": self.hook,
            "hook_title": self.hook_title,
            "score_direction": self.score_direction,
            "questions": [
                {
                    "id": q.id,
                    "text": q.text,
                    "choices": [
                        {"label": c.label, "score_value": c.value} for c in q.choices
                    ],
                }
                for q in self.questions
            ],
            "results": [
                {"min": r.min, "max": r.max, "name": r.name,
                 "description": r.description}
                for r in self.results
            ],
        }


# ----------------------------------------------------------------------
def _too_long(text: str, limit: int) -> bool:
    return len(text.replace("\n", "")) > limit


def validate(light: LightSet) -> list[str]:
    """長すぎる・多すぎるものを見つける。空なら合格。

    生成のたびに通す。文言を足したときに、うっかり長い説明文を
    入れてしまっても、ここで止まる。
    """
    problems: list[str] = []
    headline = light.hook_title or light.hook
    if _too_long(headline, MAX_HOOK_CHARS):
        problems.append(
            f"フックが長すぎます（{len(headline)}字 > {MAX_HOOK_CHARS}）: {headline}")
    if "占い" in f"{light.hook} {light.hook_title} {light.hook_suffix}":
        problems.append("フックに「占い」は使いません")
    # 1枚目で答え方を言い切る。書き忘れると1問目で考え込ませてしまう
    if HOOK_FORMAT_MARK not in f"{light.hook} {light.hook_title} {light.hook_suffix}":
        problems.append(f"1枚目に「{HOOK_FORMAT_MARK}」を入れてください（答え方が分かりません）")

    if len(light.questions) != QUESTION_COUNT:
        problems.append(f"質問は{QUESTION_COUNT}問にしてください（いまは{len(light.questions)}問）")

    for question in light.questions:
        if _too_long(question.text, MAX_QUESTION_CHARS):
            problems.append(
                f"質問が長すぎます（{len(question.body)}字 > {MAX_QUESTION_CHARS}）: "
                f"{question.body}")
        if len(question.choices) != MAX_CHOICES:
            problems.append(
                f"選択肢は{MAX_CHOICES}つにしてください（{question.id} は"
                f"{len(question.choices)}つ）")
        for choice in question.choices:
            if _too_long(choice.label, MAX_CHOICE_CHARS):
                problems.append(
                    f"選択肢が長すぎます（{len(choice.label)}字 > {MAX_CHOICE_CHARS}）: "
                    f"{choice.label}")
        labels = tuple(c.label for c in question.choices)
        if labels != CHOICE_LABELS:
            problems.append(
                f"{question.id}: 選択肢は全問 {' / '.join(CHOICE_LABELS)} にしてください"
                f"（いまは {' / '.join(labels) or 'なし'}）。"
                "YESで答えられる問いに言い換えてください")
        values = {c.value for c in question.choices}
        if len(values) < 2:
            problems.append(f"{question.id}: どちらを選んでも点数が同じです（結果が動きません）")
        # 結果ページが「YESの数」なので、YESが加点側でないと
        # 利用者が数えたYESの数と結果が合わなくなる
        by_label = {c.label: c.value for c in question.choices}
        if (YES_LABEL in by_label and NO_LABEL in by_label
                and by_label[YES_LABEL] <= by_label[NO_LABEL]):
            problems.append(
                f"{question.id}: YESが加点側になるよう問いを裏返してください"
                "（結果は「YESの数」で出すため）")

    if not light.results:
        problems.append("結果がありません")
    for result in light.results:
        if _too_long(result.name, MAX_RESULT_NAME_CHARS):
            problems.append(f"タイプ名が長すぎます: {result.name}")
        lines = [l for l in result.description.split("\n") if l.strip()]
        if len(lines) > MAX_RESULT_LINES:
            problems.append(
                f"結果の説明は{MAX_RESULT_LINES}行までです（{result.name} は{len(lines)}行）")
        for line in lines:
            if len(line) > MAX_RESULT_LINE_CHARS:
                problems.append(f"結果の説明が長すぎます（{len(line)}字）: {line}")

    # 0〜5点がすべてどれかに当てはまるか（当てはまらない点が出ると結果が出せない）
    highest = sum(max(c.value for c in q.choices) for q in light.questions)
    uncovered = [score for score in range(0, highest + 1)
                 if light.result_for(score) is None]
    if uncovered:
        problems.append(f"点数 {uncovered} に対応する結果がありません")
    return problems


def load_set(path: Path) -> LightSet:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise LightError(f"{path} を読めません: {exc}") from exc

    questions = [
        Question(
            id=str(q.get("id") or ""),
            text=str(q.get("text") or "").strip(),
            choices=tuple(
                Choice(label=str(c.get("label") or "").strip(),
                       value=int(c.get("value", 0)))
                for c in (q.get("choices") or [])
            ),
        )
        for q in (data.get("questions") or [])
    ]
    results = [
        Result(min=int(r.get("min", 0)), max=int(r.get("max", 0)),
               name=str(r.get("name") or "").strip(),
               description=str(r.get("description") or "").strip())
        for r in (data.get("results") or [])
    ]
    light = LightSet(
        axis=str(data.get("axis") or Path(path).stem),
        label=str(data.get("label") or ""),
        hook=str(data.get("hook") or "").strip(),
        hook_title=str(data.get("hook_title") or "").strip(),
        hook_suffix=str(data.get("hook_suffix") or "").strip(),
        questions=questions,
        results=results,
        score_direction=str(data.get("score_direction") or ""),
    )
    problems = validate(light)
    if problems:
        raise LightError(f"{Path(path).name} の内容に問題があります:\n  - "
                         + "\n  - ".join(problems))
    return light


def load_all(directory: Path) -> list[LightSet]:
    """テーマを全部読む。1つでも不備があれば止める。"""
    sets = [load_set(path) for path in sorted(Path(directory).glob("*.json"))]
    if not sets:
        raise LightError(f"{directory} にテーマがありません")
    return sets


def pick(sets: list[LightSet], rng: random.Random, axis: str = "") -> LightSet:
    """使うテーマを1つ選ぶ。"""
    if axis:
        for light in sets:
            if light.axis == axis:
                return light
        raise LightError(
            f"知らないテーマです: {axis}（使えるのは {', '.join(s.axis for s in sets)}）")
    return rng.choice(sets)
