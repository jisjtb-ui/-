"""CTA（行動喚起）文言の管理。

文言は data/cta.json にまとめ、コード側にハードコードしない。
複数セットを持てるため、将来の A/B テストはセットを切り替えるだけで行える。

配置ルール（広告臭くならないよう最小限にする）:
  01_question.png  → 冒頭CTA（例: 恋人とやってみて🌸）
  02〜09           → CTAなし。心理テスト体験に集中させる
  10_answer.png    → 保存 → 共有 → コメント の順にCTA
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from . import emoji

DEFAULT_SET = "default"
EMPTY_SET = "none"


class CtaError(RuntimeError):
    """CTA設定の不備。"""


@dataclass(frozen=True)
class CtaTexts:
    """1セット分のCTA文言。"""

    name: str = EMPTY_SET
    label: str = ""
    first_page: str = ""
    final_save: str = ""
    final_share: str = ""
    final_comment: str = ""
    # 1枚目に入れるコメント誘導。投稿ごとに1つ選ぶ
    comment_prompts: tuple[str, ...] = ()

    @property
    def final_lines(self) -> list[str]:
        """最終ページのCTA（優先順: 保存 → 共有 → コメント）。"""
        return [t for t in (self.final_save, self.final_share, self.final_comment) if t]

    def to_meta(self) -> dict:
        """meta.json に保存する形（後からCTA別の効果を集計できるようにする）。"""
        return {
            "set": self.name,
            "label": self.label,
            "first_page": self.first_page,
            "final_save": self.final_save,
            "final_share": self.final_share,
            "final_comment": self.final_comment,
            "comment_prompt_count": len(self.comment_prompts),
        }

    def comment_prompt(self, rng, test_count: int = 0) -> str:
        """コメント誘導を1つ選ぶ。{n} は問題数に置き換える。"""
        if not self.comment_prompts:
            return ""
        return rng.choice(self.comment_prompts).replace("{n}", str(test_count or ""))

    def comment_prompt_cycle(self, rng, test_count: int = 0):
        """順番に配る。全部使い切るまで同じ文を出さない。

        毎回ランダムに選ぶと偏って、同じ文が続けて出てしまう。
        """
        if not self.comment_prompts:
            while True:
                yield ""
        pool: list[str] = []
        while True:
            if not pool:
                pool = list(self.comment_prompts)
                rng.shuffle(pool)
            yield pool.pop().replace("{n}", str(test_count or ""))

    def is_empty(self) -> bool:
        return not (self.first_page or self.final_lines)


@dataclass
class CtaConfig:
    """data/cta.json の内容。"""

    active: str = DEFAULT_SET
    sets: dict[str, CtaTexts] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> "CtaConfig":
        raw = json.loads(path.read_text(encoding="utf-8"))
        sets: dict[str, CtaTexts] = {}
        for name, entry in (raw.get("sets") or {}).items():
            sets[name] = CtaTexts(
                name=name,
                label=entry.get("label", ""),
                first_page=entry.get("first_page", "").strip(),
                final_save=entry.get("final_save", "").strip(),
                final_share=entry.get("final_share", "").strip(),
                final_comment=entry.get("final_comment", "").strip(),
                comment_prompts=tuple(
                    t.strip() for t in (entry.get("comment_prompts") or []) if t.strip()
                ),
            )
        if not sets:
            raise CtaError(f"CTAセットが定義されていません: {path}")
        return cls(active=raw.get("active", DEFAULT_SET), sets=sets)

    @property
    def names(self) -> list[str]:
        return sorted(self.sets)

    def select(self, name: str | None = None) -> CtaTexts:
        """使用するCTAセットを返す。"""
        key = name or self.active
        if key not in self.sets:
            raise CtaError(
                f"不明なCTAセット: {key}（使えるのは: {', '.join(self.names)}）"
            )
        return _fit_to_environment(self.sets[key])


def _fit_to_environment(texts: CtaTexts) -> CtaTexts:
    """絵文字を描画できない環境では、豆腐を出さないよう絵文字を外す。"""
    if emoji.available():
        return texts
    return CtaTexts(
        name=texts.name,
        label=texts.label,
        first_page=emoji.strip_emoji(texts.first_page),
        final_save=emoji.strip_emoji(texts.final_save),
        final_share=emoji.strip_emoji(texts.final_share),
        final_comment=emoji.strip_emoji(texts.final_comment),
    )


def empty() -> CtaTexts:
    """CTAを表示しない設定。"""
    return CtaTexts()
