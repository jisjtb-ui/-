"""日本語向けのテキスト計測・自動改行・ブロック積み上げ。

- 明示改行(\n)を尊重しつつ、幅に収まらない行を自動で折り返す。
- 簡易的な禁則処理（行頭に句読点・閉じ括弧を置かない／行末に開き括弧を置かない）。
- 英数字は単語単位で折り返す。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from PIL import ImageFont

# 行頭に来てはいけない文字
NO_LINE_START = "、。，．・：；？！ぁぃぅぇぉっゃゅょゎァィゥェォッャュョヮーぐ）］｝〉》」』】〕)]}»”’…ー"
# 行末に来てはいけない文字
NO_LINE_END = "（［｛〈《「『【〔([{«“‘"

_LATIN_TOKEN = re.compile(r"[0-9A-Za-z][0-9A-Za-z'’\-\.]*")


def _tokenize(text: str) -> list[str]:
    """日本語は1文字、英数字は単語単位のトークン列にする。"""
    tokens: list[str] = []
    i = 0
    while i < len(text):
        m = _LATIN_TOKEN.match(text, i)
        if m:
            tokens.append(m.group(0))
            i = m.end()
        else:
            tokens.append(text[i])
            i += 1
    return tokens


def text_width(font: ImageFont.FreeTypeFont, text: str) -> float:
    """1行の描画幅を返す。"""
    if not text:
        return 0.0
    return font.getlength(text)


def wrap(text: str, font: ImageFont.FreeTypeFont, max_width: float) -> list[str]:
    """max_width に収まるように折り返した行リストを返す。"""
    out: list[str] = []
    for paragraph in text.split("\n"):
        paragraph = paragraph.rstrip()
        if not paragraph:
            out.append("")
            continue
        out.extend(_wrap_paragraph(paragraph, font, max_width))
    return out


def _wrap_paragraph(text: str, font: ImageFont.FreeTypeFont, max_width: float) -> list[str]:
    lines: list[str] = []
    current = ""
    for token in _tokenize(text):
        candidate = current + token
        if current and text_width(font, candidate) > max_width:
            # 行頭禁則: 句読点などは前の行に押し込む（ぶら下げ）
            if token and token[0] in NO_LINE_START:
                current = candidate
                continue
            # 行末禁則: 開き括弧で終わるなら、その文字を次行へ送る
            if current[-1] in NO_LINE_END:
                lines.append(current[:-1])
                current = current[-1] + token
                continue
            lines.append(current)
            current = token.lstrip() if token.strip() else ""
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines or [""]


@dataclass
class Block:
    """描画単位。中央のコンテンツボックス内に縦に積む。"""

    lines: list[str]
    font: ImageFont.FreeTypeFont
    line_height: int
    align: str = "center"          # center | left
    x: int | None = None           # align="left" のときの絶対X座標
    space_after: int = 0
    tracking: int = 0              # 字間（px）。小見出しの静かな印象づけに使う
    hanging: str = ""              # ぶら下げインデント用の先頭ラベル（"A" など）
    hanging_gap: int = 0           # ラベルと本文の間隔
    kind: str = "text"             # text | divider | space
    divider_width: int = 0
    thickness: int = 1

    @property
    def height(self) -> int:
        if self.kind == "divider":
            return self.thickness
        if self.kind == "space":
            return 0
        return self.line_height * max(1, len(self.lines))


@dataclass
class Stack:
    """ブロックの集合。総高さを測って安全エリア内で垂直中央に置く。"""

    blocks: list[Block] = field(default_factory=list)

    def add(self, block: Block) -> None:
        self.blocks.append(block)

    @property
    def total_height(self) -> int:
        if not self.blocks:
            return 0
        total = 0
        for i, block in enumerate(self.blocks):
            total += block.height
            if i != len(self.blocks) - 1:
                total += block.space_after
        return total


def wrap_balanced(text: str, font: ImageFont.FreeTypeFont, max_width: float) -> list[str]:
    """中央揃えの文章向けに、行長がなるべく揃うよう折り返す。

    「最後の1行だけ極端に短い」状態（泣き別れ）を避けるため、
    行数を増やさない範囲で折り返し幅を詰めていく。
    """
    out: list[str] = []
    for paragraph in text.split("\n"):
        paragraph = paragraph.rstrip()
        if not paragraph:
            out.append("")
            continue
        lines = _wrap_paragraph(paragraph, font, max_width)
        if len(lines) > 1:
            target = len(lines)
            width = max_width
            step = max_width * 0.04
            while width - step > max_width * 0.45:
                trial = _wrap_paragraph(paragraph, font, width - step)
                if len(trial) != target:
                    break
                lines = trial
                width -= step
        out.extend(lines)
    return out
