"""Pillow による画像描画。

設計の骨格:
  1. 先に中央のコンテンツボックス（安全エリア）を定義する。
  2. 要素を Block として縦に積み、総高さを測る。
  3. 安全エリア内で垂直中央に配置してから描画する。
  4. 収まらない場合はフォント倍率を段階的に下げて自動フィットさせる。

画像全体に文字をばらまくことはせず、必ずこのボックスの中だけに置く。
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .config import (
    BLACK,
    CTA_GAP_RATIO,
    CTA_GAP_RATIO_QUESTION,
    FIT_STEP,
    HAIRLINE,
    HARD_MIN_FIT_SCALE,
    MIN_FIT_SCALE,
    WHITE,
    Layout,
)
from . import emoji
from .fonts import load_font
from .textlayout import Block, Stack, text_width, wrap, wrap_balanced

CHOICE_KEYS = ("A", "B", "C", "D")
EYEBROW_QUESTION = "ちょっと大人の心理テスト"
EYEBROW_ANSWER = "答え"
FOOTER_QUESTION = "答えは次へ"


class Renderer:
    """1枚ずつ画像を組み立てて返す。"""

    def __init__(self, layout: Layout, font_path: str) -> None:
        self.layout = layout
        self.font_path = font_path

    # ------------------------------------------------------------------
    # 公開API
    # ------------------------------------------------------------------
    def render_question(self, test: dict, number: int, cta_top: str = "") -> Image.Image:
        """問題画像。答えは一切載せない。

        cta_top は1枚目だけに入れる小さなCTA（例: 恋人とやってみて🌸）。
        本文より目立たせないため、見出しより小さいサイズで最上部に置く。
        """
        return self._render(lambda s: self._build_question(test, number, s, cta_top))

    def render_answer(
        self, test: dict, number: int, cta_lines: Sequence[str] = ()
    ) -> Image.Image:
        """答え画像。直前の問題に対応する結果のみを載せる。

        cta_lines は最終ページだけに入れるCTA（保存 → 共有 → コメントの順）。
        """
        return self._render(lambda s: self._build_answer(test, number, s, list(cta_lines)))

    def render_message(
        self, headline: str, items: list[str], footer: str = "",
        emphasis: bool = True, cta_top: str = "",
    ) -> Image.Image:
        """占いの「選ぶ」「答え」ページ。見出し＋並び＋締めの3段だけ。"""
        return self._render(
            lambda s: self._build_message(headline, items, footer, emphasis, s, cta_top)
        )

    # ------------------------------------------------------------------
    # 自動フィット
    # ------------------------------------------------------------------
    def _render(self, build) -> Image.Image:
        lay = self.layout
        scale = 1.0
        stack = build(scale)
        # 推奨下限まで段階的に縮小し、それでも収まらない場合だけ絶対下限まで縮める
        # （文字切れ・画像外へのはみ出しは絶対に避ける）
        for floor in (MIN_FIT_SCALE, HARD_MIN_FIT_SCALE):
            while stack.total_height > lay.safe_height and scale > floor:
                scale = round(scale - FIT_STEP, 3)
                stack = build(scale)
        image = self._background()
        self._draw_stack(image, stack)
        return image

    def _background(self) -> Image.Image:
        lay = self.layout
        image = Image.new("RGB", (lay.width, lay.height), WHITE)
        if lay.paper:
            # 白い紙のようなごく薄い質感（ほぼ視認できないレベル）
            noise = Image.effect_noise((lay.width, lay.height), 10).convert("L")
            noise = Image.merge("RGB", (noise, noise, noise))
            image = Image.blend(image, noise, 0.035)
            image = Image.blend(image, Image.new("RGB", image.size, WHITE), 0.55)
        return image

    def _draw_stack(self, image: Image.Image, stack: Stack) -> None:
        lay = self.layout
        draw = ImageDraw.Draw(image)
        y = lay.safe_top + max(0, (lay.safe_height - stack.total_height) // 2)
        for index, block in enumerate(stack.blocks):
            if block.kind == "divider":
                x0 = lay.center_x - block.divider_width // 2
                draw.rectangle(
                    [x0, y, x0 + block.divider_width, y + block.thickness - 1],
                    fill=HAIRLINE,
                )
            elif block.kind == "text":
                self._draw_text_block(image, draw, block, y)
            y += block.height
            if index != len(stack.blocks) - 1:
                y += block.space_after

    def _draw_text_block(
        self, image: Image.Image, draw: ImageDraw.ImageDraw, block: Block, top: int
    ) -> None:
        lay = self.layout
        font = block.font
        pad = max(0, (block.line_height - font.size) // 2)
        label_width = 0
        if block.hanging:
            label_width = int(text_width(font, block.hanging)) + block.hanging_gap

        for i, line in enumerate(block.lines):
            y = top + i * block.line_height + pad
            if block.align == "center":
                self._draw_line(image, draw, lay.center_x, y, line, font, block.tracking, "center")
                continue

            x = block.x if block.x is not None else lay.safe_left
            if block.hanging and i == 0:
                self._draw_line(image, draw, x, y, block.hanging, font, 0, "left")
            self._draw_line(image, draw, x + label_width, y, line, font, block.tracking, "left")

    def _draw_line(
        self,
        image: Image.Image,
        draw: ImageDraw.ImageDraw,
        x: int,
        y: int,
        text: str,
        font: ImageFont.FreeTypeFont,
        tracking: int,
        align: str,
    ) -> None:
        if not text:
            return
        if emoji.has_emoji(text):
            self._draw_line_with_emoji(image, draw, x, y, text, font, tracking, align)
            return
        if tracking <= 0:
            anchor = "ma" if align == "center" else "la"
            draw.text((x, y), text, font=font, fill=BLACK, anchor=anchor)
            return
        widths = [text_width(font, ch) for ch in text]
        total = sum(widths) + tracking * (len(text) - 1)
        cursor = x - total / 2 if align == "center" else x
        for ch, w in zip(text, widths):
            draw.text((cursor, y), ch, font=font, fill=BLACK, anchor="la")
            cursor += w + tracking

    def _draw_line_with_emoji(
        self,
        image: Image.Image,
        draw: ImageDraw.ImageDraw,
        x: int,
        y: int,
        text: str,
        font: ImageFont.FreeTypeFont,
        tracking: int,
        align: str,
    ) -> None:
        """絵文字を含む行を描く（本文フォントは絵文字を持たないため合成する）。"""
        runs = emoji.split_runs(text)
        glyph_size = int(font.size * 0.96)
        widths = [
            glyph_size if is_emoji else text_width(font, run) for run, is_emoji in runs
        ]
        total = sum(widths) + tracking * max(0, len(runs) - 1)
        cursor = x - total / 2 if align == "center" else x

        for (run, is_emoji), width in zip(runs, widths):
            if is_emoji:
                sprite = emoji.emoji_image(run, glyph_size)
                if sprite is not None:
                    offset_y = y + int((font.size - sprite.height) * 0.45)
                    image.paste(sprite, (int(cursor), offset_y), sprite)
            else:
                draw.text((cursor, y), run, font=font, fill=BLACK, anchor="la")
            cursor += width + tracking

    # ------------------------------------------------------------------
    # ブロック組み立て
    # ------------------------------------------------------------------
    def _font(self, base_size: float, scale: float) -> ImageFont.FreeTypeFont:
        return load_font(self.font_path, max(10, self.layout.px(base_size * scale)))

    def _lh(self, font: ImageFont.FreeTypeFont, ratio: float) -> int:
        return int(round(font.size * ratio))

    def _wrap_width(self, font: ImageFont.FreeTypeFont) -> int:
        """折り返し幅。行頭禁則のぶら下げで安全エリアを越えないよう1文字分引く。"""
        return int(self.layout.safe_width - font.size)

    def _divider(self, space_after: int) -> Block:
        lay = self.layout
        return Block(
            lines=[],
            font=load_font(self.font_path, 10),
            line_height=0,
            kind="divider",
            divider_width=int(lay.safe_width * lay.spacing.divider_width_ratio),
            thickness=max(1, lay.px(1)),
            space_after=space_after,
        )

    def _build_question(
        self, test: dict, number: int, scale: float, cta_top: str = ""
    ) -> Stack:
        lay = self.layout
        sp = lay.spacing
        stack = Stack()
        # CTAを足した分、本文を極端に縮めずに済むよう余白側を詰める
        gap_ratio = CTA_GAP_RATIO_QUESTION if cta_top else 1.0

        f_eyebrow = self._font(lay.sizes.eyebrow, scale)
        f_number = self._font(lay.sizes.number, scale)
        f_title = self._font(lay.sizes.title, scale)
        f_question = self._font(lay.sizes.question, scale)
        f_choice = self._font(lay.sizes.choice, scale)
        f_footer = self._font(lay.sizes.footer, scale)

        # 1枚目だけの冒頭CTA。問題文より目立たないよう、見出しより小さく置く
        if cta_top:
            f_cta = self._font(lay.sizes.cta_top, scale)
            stack.add(
                Block(
                    lines=[cta_top],
                    font=f_cta,
                    line_height=self._lh(f_cta, lay.line_heights.cta),
                    tracking=lay.px(2 * scale),
                    space_after=lay.px(sp.after_cta_top * scale),
                )
            )

        stack.add(
            Block(
                lines=[test.get("header") or EYEBROW_QUESTION],
                font=f_eyebrow,
                line_height=self._lh(f_eyebrow, lay.line_heights.eyebrow),
                tracking=lay.px(6 * scale),
                space_after=lay.px(sp.after_eyebrow * gap_ratio * scale),
            )
        )
        stack.add(
            Block(
                lines=[f"{number:02d}"],
                font=f_number,
                line_height=self._lh(f_number, lay.line_heights.number),
                tracking=lay.px(4 * scale),
                space_after=lay.px(sp.after_number * gap_ratio * scale),
            )
        )

        title = (test.get("title") or "").strip()
        if title:
            stack.add(
                Block(
                    lines=wrap_balanced(title, f_title, self._wrap_width(f_title)),
                    font=f_title,
                    line_height=self._lh(f_title, lay.line_heights.title),
                    space_after=lay.px(sp.after_title * gap_ratio * scale),
                )
            )

        stack.add(self._divider(lay.px(sp.after_divider * gap_ratio * scale)))

        stack.add(
            Block(
                lines=wrap_balanced(test["question"], f_question, self._wrap_width(f_question)),
                font=f_question,
                line_height=self._lh(f_question, lay.line_heights.question),
                space_after=lay.px(sp.after_question * gap_ratio * scale),
            )
        )

        # 選択肢は「左揃えのかたまり」を中央に置く
        choices = test["choices"]
        keys = [k for k in CHOICE_KEYS if k in choices]
        labels = [f"{k}. {choices[k]}" for k in keys]
        group_width = max(text_width(f_choice, t) for t in labels)
        group_x = int(lay.center_x - min(group_width, lay.safe_width) / 2)
        for i, label in enumerate(labels):
            last = i == len(labels) - 1
            stack.add(
                Block(
                    lines=wrap(label, f_choice, self._wrap_width(f_choice)),
                    font=f_choice,
                    line_height=self._lh(f_choice, lay.line_heights.choice),
                    align="left",
                    x=group_x,
                    space_after=lay.px(
                        (sp.after_choices if last else sp.between_choices)
                        * gap_ratio
                        * scale
                    ),
                )
            )

        stack.add(
            Block(
                lines=[FOOTER_QUESTION],
                font=f_footer,
                line_height=self._lh(f_footer, lay.line_heights.footer),
                tracking=lay.px(3 * scale),
            )
        )
        return stack

    def _build_answer(
        self, test: dict, number: int, scale: float, cta_lines: list[str] | None = None
    ) -> Stack:
        cta_lines = [line for line in (cta_lines or []) if line]
        lay = self.layout
        sp = lay.spacing
        stack = Stack()
        gap_ratio = CTA_GAP_RATIO if cta_lines else 1.0

        f_eyebrow = self._font(lay.sizes.eyebrow, scale)
        f_number = self._font(lay.sizes.number, scale)
        f_title = self._font(lay.sizes.title * 0.78, scale)
        f_answer = self._font(lay.sizes.answer, scale)
        f_closing = self._font(lay.sizes.closing, scale)

        stack.add(
            Block(
                lines=[EYEBROW_ANSWER],
                font=f_eyebrow,
                line_height=self._lh(f_eyebrow, lay.line_heights.eyebrow),
                tracking=lay.px(8 * scale),
                space_after=lay.px(sp.after_eyebrow * gap_ratio * scale),
            )
        )
        stack.add(
            Block(
                lines=[f"{number:02d}"],
                font=f_number,
                line_height=self._lh(f_number, lay.line_heights.number),
                tracking=lay.px(4 * scale),
                space_after=lay.px(sp.after_number * gap_ratio * scale),
            )
        )

        title = (test.get("title") or "").strip()
        if title:
            stack.add(
                Block(
                    lines=wrap_balanced(title, f_title, self._wrap_width(f_title)),
                    font=f_title,
                    line_height=self._lh(f_title, lay.line_heights.title),
                    space_after=lay.px(sp.after_title * 0.7 * gap_ratio * scale),
                )
            )

        stack.add(self._divider(lay.px(sp.after_divider * gap_ratio * scale)))

        answers = test["answers"]
        keys = [k for k in CHOICE_KEYS if k in answers]
        gap = lay.px(14 * scale)
        inset = lay.px(6)
        label_w = int(text_width(f_answer, "A")) + gap
        # 行頭禁則のぶら下げ（。や」）で右端がはみ出さないよう1文字分の余裕を引く
        body_width = lay.safe_width - label_w - inset - f_answer.size
        for i, key in enumerate(keys):
            last = i == len(keys) - 1
            stack.add(
                Block(
                    lines=wrap(answers[key], f_answer, body_width),
                    font=f_answer,
                    line_height=self._lh(f_answer, lay.line_heights.answer),
                    align="left",
                    x=lay.safe_left + inset,
                    hanging=key,
                    hanging_gap=gap,
                    space_after=lay.px(
                        (sp.after_answers if last else sp.between_answers)
                        * gap_ratio
                        * scale
                    ),
                )
            )

        closing = (test.get("closing") or "").strip()
        if closing:
            stack.add(
                Block(
                    lines=wrap_balanced(closing, f_closing, self._wrap_width(f_closing)),
                    font=f_closing,
                    line_height=self._lh(f_closing, lay.line_heights.closing),
                    space_after=lay.px(sp.before_cta * scale) if cta_lines else 0,
                )
            )

        # 最終ページのCTA。優先順位は 回答結果 > 保存 > 共有・コメント
        for index, line in enumerate(cta_lines):
            primary = index == 0
            f_cta = self._font(
                lay.sizes.cta_primary if primary else lay.sizes.cta_secondary, scale
            )
            if index == 0:
                space_after = lay.px(sp.between_cta_primary * scale)
            else:
                space_after = lay.px(sp.between_cta * scale)
            stack.add(
                Block(
                    lines=wrap_balanced(line, f_cta, self._wrap_width(f_cta)),
                    font=f_cta,
                    line_height=self._lh(f_cta, lay.line_heights.cta),
                    tracking=lay.px((2 if primary else 1) * scale),
                    space_after=space_after if index < len(cta_lines) - 1 else 0,
                )
            )
        return stack


    def _build_message(
        self, headline: str, items: list[str], footer: str, emphasis: bool,
        scale: float, cta_top: str = "",
    ) -> Stack:
        """テストのページより余白を多くとり、行動に集中させる。"""
        lay = self.layout
        sp = lay.spacing
        stack = Stack()

        if cta_top:
            f_top = self._font(lay.sizes.cta_secondary, scale)
            stack.add(
                Block(
                    lines=wrap_balanced(cta_top, f_top, self._wrap_width(f_top)),
                    font=f_top,
                    line_height=self._lh(f_top, lay.line_heights.cta),
                    tracking=lay.px(1 * scale),
                    space_after=lay.px(sp.after_eyebrow * CTA_GAP_RATIO_QUESTION * scale),
                )
            )

        f_head = self._font(lay.sizes.title * 0.72, scale)
        f_item = self._font(lay.sizes.question * (1.0 if emphasis else 0.86), scale)
        f_foot = self._font(lay.sizes.cta_secondary, scale)

        if headline:
            stack.add(
                Block(
                    lines=wrap_balanced(headline, f_head, self._wrap_width(f_head)),
                    font=f_head,
                    line_height=self._lh(f_head, lay.line_heights.title),
                    tracking=lay.px(2 * scale),
                    space_after=lay.px(sp.after_title * scale),
                )
            )
            stack.add(self._divider(lay.px(sp.after_divider * 1.2 * scale)))

        for index, item in enumerate(items):
            stack.add(
                Block(
                    lines=wrap_balanced(item, f_item, self._wrap_width(f_item)),
                    font=f_item,
                    line_height=self._lh(f_item, lay.line_heights.question),
                    tracking=lay.px(1 * scale),
                    space_after=lay.px(
                        (sp.between_choices * (1.5 if emphasis else 1.1)) * scale
                    ) if index < len(items) - 1 else lay.px(sp.before_cta * scale),
                )
            )

        for line in [l for l in footer.split("\n") if l.strip()]:
            stack.add(
                Block(
                    lines=wrap_balanced(line, f_foot, self._wrap_width(f_foot)),
                    font=f_foot,
                    line_height=self._lh(f_foot, lay.line_heights.cta),
                    tracking=lay.px(1 * scale),
                    space_after=lay.px(sp.between_cta * scale),
                )
            )
        return stack


def build_contact_sheet(
    image_paths: list[Path],
    columns: int = 5,
    thumb_width: int = 300,
    margin: int = 24,
    gap: int = 16,
) -> Image.Image:
    """投稿前確認用の簡易コンタクトシート（10枚一覧）を作る。"""
    thumbs: list[Image.Image] = []
    for path in image_paths:
        with Image.open(path) as im:
            ratio = thumb_width / im.width
            thumbs.append(im.convert("RGB").resize(
                (thumb_width, int(im.height * ratio)), Image.LANCZOS
            ))
    if not thumbs:
        raise ValueError("コンタクトシート用の画像がありません")

    thumb_height = max(t.height for t in thumbs)
    rows = (len(thumbs) + columns - 1) // columns
    sheet_w = margin * 2 + columns * thumb_width + (columns - 1) * gap
    sheet_h = margin * 2 + rows * thumb_height + (rows - 1) * gap
    sheet = Image.new("RGB", (sheet_w, sheet_h), WHITE)
    draw = ImageDraw.Draw(sheet)

    for index, thumb in enumerate(thumbs):
        row, col = divmod(index, columns)
        x = margin + col * (thumb_width + gap)
        y = margin + row * (thumb_height + gap)
        sheet.paste(thumb, (x, y))
        draw.rectangle([x, y, x + thumb_width - 1, y + thumb_height - 1], outline=HAIRLINE)
    return sheet
