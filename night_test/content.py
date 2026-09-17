"""ネタ元（data/tests/*.json）の読み込みと、1投稿分（5問）の構成。

ネタは JSON を足すだけで増やせる。1件のネタは複数の言い回し
（titles / questions / closings）を持てるため、組み合わせで自然に増殖する。
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass
from pathlib import Path

from .history import (
    History,
    MOTIF_WINDOW,
    RECENT_ITEM_WINDOW,
    RECENT_POST_WINDOW,
    SIMILARITY_THRESHOLD,
)

RANDOM_CATEGORY = "random"
CHOICE_KEYS = ("A", "B", "C", "D")

FORM_LABELS = {
    "scene": "情景選択型",
    "action": "行動選択型",
    "object": "物の選択型",
    "priority": "優先順位型",
    "reaction": "反応型",
    "intuition": "直感選択型",
}


class ContentError(RuntimeError):
    """ネタ元の不備や、構成に失敗した場合に送出する。"""


@dataclass(frozen=True)
class Item:
    """ネタ1件（言い回しのバリエーションを含む）。"""

    id: str
    category: str
    label: str
    form: str
    motif: str
    titles: tuple[str, ...]
    questions: tuple[str, ...]
    choices: dict[str, str]
    answers: dict[str, str]
    closings: tuple[str, ...]

    @property
    def variant_count(self) -> int:
        return len(self.titles) * len(self.questions) * len(self.closings)

    def variant_keys(self) -> list[str]:
        return [
            f"{t}-{q}-{c}"
            for t in range(len(self.titles))
            for q in range(len(self.questions))
            for c in range(len(self.closings))
        ]

    def render(self, variant: str, number: int, header: str = "") -> dict:
        """バリエーションキーから、1問分の辞書を組み立てる。"""
        t, q, c = (int(x) for x in variant.split("-"))
        return {
            "number": number,
            "header": header,
            "id": self.id,
            "category": self.category,
            "category_label": self.label,
            "form": self.form,
            "form_label": FORM_LABELS.get(self.form, self.form),
            "motif": self.motif,
            "title": self.titles[t],
            "question": self.questions[q],
            "choices": dict(self.choices),
            "answers": dict(self.answers),
            "closing": self.closings[c],
            "answer_theme": self.answers.get("A", "")[:18],
            "variant": variant,
        }


# ----------------------------------------------------------------------
# 読み込み
# ----------------------------------------------------------------------
def load_items(tests_dir: Path) -> list[Item]:
    """data/tests/ 以下の JSON をすべて読み込む。"""
    if not tests_dir.is_dir():
        raise ContentError(f"ネタ元ディレクトリが見つかりません: {tests_dir}")

    items: list[Item] = []
    seen: set[str] = set()
    for path in sorted(tests_dir.glob("*.json")):
        raw = json.loads(path.read_text(encoding="utf-8"))
        category = raw.get("category") or path.stem
        label = raw.get("label", category)
        for entry in raw.get("items", []):
            item = _build_item(entry, category, label, path)
            if item.id in seen:
                raise ContentError(f"ネタIDが重複しています: {item.id} ({path.name})")
            seen.add(item.id)
            items.append(item)
    if not items:
        raise ContentError(f"ネタが1件も読み込めませんでした: {tests_dir}")
    return items


def _build_item(entry: dict, category: str, label: str, path: Path) -> Item:
    def _as_tuple(key_plural: str, key_single: str) -> tuple[str, ...]:
        values = entry.get(key_plural)
        if values is None:
            single = entry.get(key_single)
            values = [single] if single else []
        values = [v.strip() for v in values if v and v.strip()]
        if not values:
            raise ContentError(f"{path.name}: {entry.get('id')} に {key_plural} がありません")
        return tuple(values)

    item_id = entry.get("id")
    if not item_id:
        raise ContentError(f"{path.name}: id のないネタがあります")

    choices = entry.get("choices") or {}
    answers = entry.get("answers") or {}
    keys = [k for k in CHOICE_KEYS if k in choices]
    if len(keys) < 3:
        raise ContentError(f"{path.name}: {item_id} の選択肢は3〜4個必要です")
    missing = [k for k in keys if k not in answers]
    if missing:
        raise ContentError(f"{path.name}: {item_id} の答えが不足しています: {missing}")

    return Item(
        id=item_id,
        category=entry.get("category", category),
        label=entry.get("label", label),
        form=entry.get("form", "scene"),
        motif=entry.get("motif", ""),
        titles=_as_tuple("titles", "title"),
        questions=_as_tuple("questions", "question"),
        closings=_as_tuple("closings", "closing"),
        choices={k: choices[k].strip() for k in keys},
        answers={k: answers[k].strip() for k in keys},
    )


def available_categories(items: list[Item]) -> list[str]:
    return sorted({item.category for item in items})


# ----------------------------------------------------------------------
# 1投稿分の構成
# ----------------------------------------------------------------------
@dataclass
class SelectionRules:
    """重複を避けるためのルール。満たせない場合は段階的に緩める。"""

    avoid_recent_ids: bool = True
    avoid_recent_motifs: bool = True
    similarity_threshold: float = SIMILARITY_THRESHOLD
    use_prefix_rule: bool = True
    max_same_form: int = 2
    max_same_category: int = 2

    def relaxed(self, step: int) -> "SelectionRules":
        """緩和段階に応じてルールを弱める。"""
        return SelectionRules(
            avoid_recent_ids=self.avoid_recent_ids and step < 2,
            avoid_recent_motifs=self.avoid_recent_motifs and step < 1,
            similarity_threshold=min(1.01, self.similarity_threshold + 0.12 * step),
            use_prefix_rule=self.use_prefix_rule and step < 2,
            max_same_form=self.max_same_form + (1 if step >= 1 else 0),
            max_same_category=self.max_same_category + (1 if step >= 1 else 0),
        )


def build_post_tests(
    items: list[Item],
    history: History,
    rng: random.Random,
    category: str,
    count: int,
    rules: SelectionRules | None = None,
    header: str = "",
) -> list[dict]:
    """1投稿分（既定5問）を選び、番号付きの辞書リストにして返す。"""
    base_rules = rules or SelectionRules()
    pool = _category_pool(items, category)
    if not pool:
        raise ContentError(f"カテゴリ '{category}' のネタがありません")

    if len(pool) < count:
        raise ContentError(
            f"カテゴリ '{category}' のネタが {len(pool)} 件しかありません（1投稿に {count} 件必要）。"
            " data/tests/ にネタを追加してください。"
        )

    for step in range(4):
        selected = _try_select(pool, history, rng, count, base_rules.relaxed(step), category)
        if selected:
            return apply_header(selected, header)

    # 最終手段: 使用回数がいちばん少ないネタから機械的に選ぶ（言い回しは変える）
    return apply_header(_fallback_select(pool, history, rng, count), header)


def apply_header(tests: list[dict], header: str) -> list[dict]:
    """1投稿内の見出し文言を揃える（10枚すべて同じ見出しにする）。"""
    for test in tests:
        test["header"] = header
    return tests


def _fallback_select(
    pool: list[Item], history: History, rng: random.Random, count: int
) -> list[dict]:
    selected: list[dict] = []
    for item in _ordered_candidates(pool, history, rng):
        if len(selected) >= count:
            break
        variant = _pick_variant(item, history, rng)
        selected.append(item.render(variant, len(selected) + 1))
    return selected


def _category_pool(items: list[Item], category: str) -> list[Item]:
    if category == RANDOM_CATEGORY:
        return list(items)
    return [i for i in items if i.category == category]


def _try_select(
    pool: list[Item],
    history: History,
    rng: random.Random,
    count: int,
    rules: SelectionRules,
    category: str,
) -> list[dict] | None:
    recent_ids = history.recent_item_ids(RECENT_POST_WINDOW) if rules.avoid_recent_ids else set()
    recent_motifs = history.recent_motifs(MOTIF_WINDOW) if rules.avoid_recent_motifs else set()

    candidates = _ordered_candidates(pool, history, rng)
    selected: list[dict] = []
    used_motifs: set[str] = set()
    form_counts: dict[str, int] = {}
    category_counts: dict[str, int] = {}

    for item in candidates:
        if len(selected) >= count:
            break
        if item.id in recent_ids:
            continue
        if item.motif and (item.motif in used_motifs or item.motif in recent_motifs):
            continue
        if form_counts.get(item.form, 0) >= rules.max_same_form:
            continue
        # random 指定時は、1投稿内でテーマを散らす
        if category == RANDOM_CATEGORY and category_counts.get(item.category, 0) >= rules.max_same_category:
            continue

        variant = _pick_variant(item, history, rng)
        test = item.render(variant, len(selected) + 1)

        conflict = history.conflict_score(
            test,
            RECENT_ITEM_WINDOW,
            rules.similarity_threshold,
            rules.use_prefix_rule,
        )
        if conflict > rules.similarity_threshold:
            continue
        if _conflicts_within_post(test, selected, rules.similarity_threshold):
            continue

        selected.append(test)
        used_motifs.add(item.motif)
        form_counts[item.form] = form_counts.get(item.form, 0) + 1
        category_counts[item.category] = category_counts.get(item.category, 0) + 1

    if len(selected) < count:
        return None
    return selected


def _ordered_candidates(pool: list[Item], history: History, rng: random.Random) -> list[Item]:
    """使用回数が少なく、最後に使ってから時間が経ったものを優先する。"""
    shuffled = list(pool)
    rng.shuffle(shuffled)
    return sorted(
        shuffled,
        key=lambda i: (history.use_count(i.id), history.last_used_post(i.id)),
    )


def _pick_variant(item: Item, history: History, rng: random.Random) -> str:
    """未使用の言い回しを優先して選ぶ。"""
    keys = item.variant_keys()
    used = history.used_variants(item.id)
    unused = [k for k in keys if k not in used]
    return rng.choice(unused or keys)


def _conflicts_within_post(test: dict, selected: list[dict], threshold: float) -> bool:
    from .history import COMMON_PREFIX_LIMIT, common_prefix_len, fingerprint, normalize, similarity

    target = fingerprint(test)
    target_q = normalize(test["question"])
    for other in selected:
        if similarity(target, fingerprint(other)) > threshold:
            return True
        if common_prefix_len(target_q, normalize(other["question"])) >= COMMON_PREFIX_LIMIT:
            return True
    return False
