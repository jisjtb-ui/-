"""1枚目のフック（A/B/C…）の管理。

なぜ分けているか:
  同じ中身でも、1枚目で止まるか進むかが数字を大きく変える。
  そこだけを差し替えて比べられるようにするのがこのモジュール。

決まりごと:
  - 文言は data/hooks.json だけに置く。コードへ書かない
  - 変えるのは**1枚目だけ**。問題・回答・デザイン・投稿時刻は揃える
  - D / E … を足すときは JSON に1件加えるだけでよい
  - 出現割合は当面すべて同じ。サンプルが少ないうちに自動最適化を始めない
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

ROTATE = "rotate"          # 投稿ごとに順番で切り替える（既定）
DEFAULT_PATH_NAME = "hooks.json"


class HookError(RuntimeError):
    """フック設定の不備。"""


@dataclass(frozen=True)
class HookVariant:
    """1種類のフック。"""

    id: str
    label: str = ""
    lines: tuple[str, ...] = ()
    weight: float = 1.0
    enabled: bool = True

    @property
    def headline(self) -> str:
        return self.lines[0] if self.lines else ""

    @property
    def rest(self) -> list[str]:
        return list(self.lines[1:])

    def to_meta(self) -> dict:
        """meta.json に残す形。あとからフック別に集計するための鍵。"""
        return {
            "hook_variant": self.id,
            "hook_label": self.label,
            "hook_lines": list(self.lines),
        }

    def is_empty(self) -> bool:
        return not self.lines


def empty() -> HookVariant:
    """フックを使わない（従来の10枚構成のまま）。"""
    return HookVariant(id="", label="なし")


@dataclass
class HookConfig:
    """data/hooks.json の中身。"""

    active: str = ROTATE
    variants: list[HookVariant] = field(default_factory=list)
    note: str = ""

    @classmethod
    def load(cls, path: Path) -> "HookConfig":
        if not Path(path).is_file():
            return cls(active=ROTATE, variants=[])
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise HookError(f"{path} を読めません: {exc}") from exc

        variants: list[HookVariant] = []
        for raw in data.get("variants") or []:
            identifier = str(raw.get("id") or "").strip()
            lines = tuple(str(line) for line in (raw.get("lines") or []) if str(line).strip())
            if not identifier or not lines:
                continue
            variants.append(HookVariant(
                id=identifier,
                label=str(raw.get("label") or identifier),
                lines=lines,
                weight=float(raw.get("weight", 1.0) or 0.0),
                enabled=bool(raw.get("enabled", True)),
            ))
        return cls(active=str(data.get("active") or ROTATE),
                   variants=variants, note=str(data.get("note") or ""))

    # ------------------------------------------------------------------
    def usable(self) -> list[HookVariant]:
        """抽選に使えるフック（無効にしたものは除く）。"""
        return [v for v in self.variants if v.enabled and v.lines]

    def ids(self) -> list[str]:
        return [v.id for v in self.usable()]

    def choices(self) -> list[tuple[str, str]]:
        """画面の選択肢（値, 表示名）。手入力させないための材料。"""
        options = [(ROTATE, "自動ローテーション")]
        options += [(v.id, f"{v.id} {v.label}") for v in self.usable()]
        return options

    def get(self, identifier: str) -> HookVariant | None:
        for variant in self.usable():
            if variant.id == identifier:
                return variant
        return None

    def select(self, name: str | None, index: int = 0) -> HookVariant:
        """使うフックを1つ決める。

        name が空か "rotate" なら順番で切り替える。
        未知のIDを渡されたときは、黙って別のものにせず例外にする
        （知らないうちに違うフックで検証してしまうのを防ぐ）。
        """
        usable = self.usable()
        if not usable:
            return empty()
        wanted = (name or ROTATE).strip()
        if wanted in ("", ROTATE, "auto"):
            return usable[rotation_index(index, len(usable))]
        found = self.get(wanted)
        if found is None:
            raise HookError(
                f"知らないフックです: {wanted}"
                f"（使えるのは {ROTATE} / {' / '.join(self.ids())}）"
            )
        return found


def rotation_index(position: int, count: int) -> int:
    """順番に切り替えるときの位置。

    ただの `position % count` だと、1日に複数回投稿する運用で
    「Aはいつも1本目」のように**時間帯が固定**されてしまう。
    1周ごとに1つずらして、どのフックもどの時間帯に同じ回数だけ出るようにする。

        A B C / B C A / C A B / …
    """
    if count <= 0:
        return 0
    position = max(0, int(position))
    return (position + position // count) % count
