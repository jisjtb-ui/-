"""アクション別CTA（プロフィール / いいね / フォロー / 共有）の結果割り当て。

視聴者は「欲しい結果」のアクションを押す。したがって、どのアクションに
どれだけ良い結果を置くかが、そのまま得たい行動の誘導になる。

  共有       … 最も魅力的な結果を中心に
  フォロー   … かなり良い結果を中心に
  いいね     … 中程度の結果を中心に
  プロフィール … 最も弱い結果を中心に（開かれても得るものが小さいため）

ただし毎回同じ序列だとパターンを読まれるので、**確率で配る**。
プロフィールが一番弱いのは7〜8割で、残りでは順位が入れ替わる。

重みも結果の文言も data/cta_actions.json にあり、コードには固定しない。
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_SET = "default"
DEFAULT_TIERS = ("weak", "mid", "good", "best")


class ActionCtaError(RuntimeError):
    """アクションCTA設定の不備。"""


@dataclass(frozen=True)
class Assigned:
    """1アクション分の割り当て結果。"""

    action: str
    label: str
    tier: str
    text: str


@dataclass(frozen=True)
class ActionSet:
    """1セット分の設定。"""

    name: str
    label: str = ""
    headline: str = ""
    footer: str = ""
    separator: str = "…"
    actions: tuple[str, ...] = ()
    action_labels: dict[str, str] = field(default_factory=dict)
    weights: dict[str, dict[str, float]] = field(default_factory=dict)
    results: dict[str, list[str]] = field(default_factory=dict)
    tiers: tuple[str, ...] = DEFAULT_TIERS

    # ------------------------------------------------------------------
    def _pick_tier(self, action: str, rng: random.Random) -> str:
        """重みに従って結果の強さを選ぶ。"""
        weights = self.weights.get(action) or {}
        pairs = [(tier, float(weights.get(tier, 0))) for tier in self.tiers]
        total = sum(weight for _, weight in pairs if weight > 0)
        if total <= 0:
            return self.tiers[0]
        threshold = rng.random() * total
        cumulative = 0.0
        for tier, weight in pairs:
            if weight <= 0:
                continue
            cumulative += weight
            if threshold < cumulative:
                return tier
        return pairs[-1][0]

    def _pick_text(self, tier: str, used: set[str], rng: random.Random) -> tuple[str, str]:
        """その強さの中から未使用の文言を選ぶ。尽きたら近い強さへずらす。"""
        start = self.tiers.index(tier) if tier in self.tiers else 0
        # 選んだ強さ → 近い強さ の順に探す
        order = sorted(range(len(self.tiers)), key=lambda i: (abs(i - start), i))
        for index in order:
            candidate_tier = self.tiers[index]
            available = [t for t in self.results.get(candidate_tier, []) if t not in used]
            if available:
                return candidate_tier, rng.choice(available)
        raise ActionCtaError(
            f"結果の文言が足りません（セット {self.name}）。"
            "data/cta_actions.json の results を増やしてください"
        )

    def assign(self, rng: random.Random) -> list[Assigned]:
        """4アクションへ結果を配る。同じ文言は1投稿に2回出さない。"""
        used: set[str] = set()
        assigned: list[Assigned] = []
        for action in self.actions:
            tier = self._pick_tier(action, rng)
            actual_tier, text = self._pick_text(tier, used, rng)
            used.add(text)
            assigned.append(
                Assigned(action, self.action_labels.get(action, action), actual_tier, text)
            )
        return assigned

    # ------------------------------------------------------------------
    def lines(self, assigned: list[Assigned]) -> list[str]:
        """画像に載せる行。見出し → 各アクション → 締め。"""
        body = [f"{item.label} {self.separator} {item.text}" for item in assigned]
        return [line for line in ([self.headline] + body + [self.footer]) if line]

    def to_meta(self, assigned: list[Assigned]) -> dict:
        """meta.json に残す（後からアクション別の効果を集計するため）。"""
        return {
            "set": self.name,
            "label": self.label,
            "headline": self.headline,
            "assignments": [
                {"action": a.action, "label": a.label, "tier": a.tier, "text": a.text}
                for a in assigned
            ],
        }

    def is_empty(self) -> bool:
        return not self.actions


@dataclass
class ActionConfig:
    """data/cta_actions.json 全体。"""

    active: str = DEFAULT_SET
    tiers: tuple[str, ...] = DEFAULT_TIERS
    sets: dict[str, ActionSet] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> "ActionConfig":
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ActionCtaError(f"設定が見つかりません: {path}") from exc
        except json.JSONDecodeError as exc:
            raise ActionCtaError(f"設定を読めません（{path}）: {exc}") from exc

        tiers = tuple(data.get("tiers") or DEFAULT_TIERS)
        sets: dict[str, ActionSet] = {}
        for name, raw in (data.get("sets") or {}).items():
            sets[name] = ActionSet(
                name=name,
                label=raw.get("label", ""),
                headline=raw.get("headline", ""),
                footer=raw.get("footer", ""),
                separator=raw.get("separator", "…"),
                actions=tuple(raw.get("actions") or ()),
                action_labels=dict(raw.get("action_labels") or {}),
                weights={k: dict(v) for k, v in (raw.get("weights") or {}).items()},
                results={k: list(v) for k, v in (raw.get("results") or {}).items()},
                tiers=tiers,
            )
        return cls(active=data.get("active", DEFAULT_SET), tiers=tiers, sets=sets)

    def get(self, name: str | None = None) -> ActionSet:
        chosen = name or self.active
        if chosen not in self.sets:
            known = ", ".join(sorted(self.sets)) or "（なし）"
            raise ActionCtaError(f"不明なセットです: {chosen}（使えるのは: {known}）")
        return self.sets[chosen]


def empty() -> ActionSet:
    return ActionSet(name="none")
