"""Pillow による画像描画。

設計の骨格:
  1. 先に中央のコンテンツボックス（安全エリア）を定義する。
  2. 要素を Block として縦に積み、総高さを測る。
  3. 安全エリア内で垂直中央に配置してから描画する。
  4. 収まらない場合はフォント倍率を段階的に下げて自動フィットさせる。

画像全体に文字をばらまくことはせず、必ずこのボックスの中だけに置く。
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .config import (
    BLACK,
    FIT_STEP,
    HAIRLINE,
    MIN_FIT_SCALE,
    WHITE,
    Layout,
)
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
    def render_question(self, test: dict, number: int) -> Image.Image:
        """問題画像。答えは一切載せない。"""
        return self._render(lambda s: self._build_question(test, number, s))

    def render_answer(self, test: dict, number: int) -> Image.Image:
        """答え画像。直前の問題に対応する結果のみを載せる。"""
        return self._render(lambda s: self._build_answer(test, number, s))

    # ------------------------------------------------------------------
    # 自動フィット
    # ------------------------------------------------------------------
    def _render(self, build) -> Image.Image:
        lay = self.layout
        scale = 1.0
        stack = build(scale)
        while stack.total_height > lay.safe_height and scale > MIN_FIT_SCALE:
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
                self._draw_text_block(draw, block, y)
            y += block.height
            if index != len(stack.blocks) - 1:
                y += block.space_after

    def _draw_text_block(self, draw: ImageDraw.ImageDraw, block: Block, top: int) -> None:
        lay = self.layout
        font = block.font
        pad = max(0, (block.line_height - font.size) // 2)
        label_width = 0
        if block.hanging:
            label_width = int(text_width(font, block.hanging)) + block.hanging_gap

        for i, line in enumerate(block.lines):
            y = top + i * block.line_height + pad
            if block.align == "center":
                self._draw_line(draw, lay.center_x, y, line, font, block.tracking, "center")
                continue

            x = block.x if block.x is not None else lay.safe_left
            if block.hanging and i == 0:
                self._draw_line(draw, x, y, block.hanging, font, 0, "left")
            self._draw_line(draw, x + label_width, y, line, font, block.tracking, "left")

    def _draw_line(
        self,
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

    def _build_question(self, test: dict, number: int, scale: float) -> Stack:
        lay = self.layout
        sp = lay.spacing
        stack = Stack()

        f_eyebrow = self._font(lay.sizes.eyebrow, scale)
        f_number = self._font(lay.sizes.number, scale)
        f_title = self._font(lay.sizes.title, scale)
        f_question = self._font(lay.sizes.question, scale)
        f_choice = self._font(lay.sizes.choice, scale)
        f_footer = self._font(lay.sizes.footer, scale)

        stack.add(
            Block(
                lines=[test.get("header") or EYEBROW_QUESTION],
                font=f_eyebrow,
                line_height=self._lh(f_eyebrow, lay.line_heights.eyebrow),
                tracking=lay.px(6 * scale),
                space_after=lay.px(sp.after_eyebrow * scale),
            )
        )
        stack.add(
            Block(
                lines=[f"{number:02d}"],
                font=f_number,
                line_height=self._lh(f_number, lay.line_heights.number),
                tracking=lay.px(4 * scale),
                space_after=lay.px(sp.after_number * scale),
            )
        )

        title = (test.get("title") or "").strip()
        if title:
            stack.add(
                Block(
                    lines=wrap_balanced(title, f_title, self._wrap_width(f_title)),
                    font=f_title,
                    line_height=self._lh(f_title, lay.line_heights.title),
                    space_after=lay.px(sp.after_title * scale),
                )
            )

        stack.add(self._divider(lay.px(sp.after_divider * scale)))

        stack.add(
            Block(
                lines=wrap_balanced(test["question"], f_question, self._wrap_width(f_question)),
                font=f_question,
                line_height=self._lh(f_question, lay.line_heights.question),
                space_after=lay.px(sp.after_question * scale),
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
                        (sp.after_choices if last else sp.between_choices) * scale
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

    def _build_answer(self, test: dict, number: int, scale: float) -> Stack:
        lay = self.layout
        sp = lay.spacing
        stack = Stack()

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
                space_after=lay.px(sp.after_eyebrow * scale),
            )
        )
        stack.add(
            Block(
                lines=[f"{number:02d}"],
                font=f_number,
                line_height=self._lh(f_number, lay.line_heights.number),
                tracking=lay.px(4 * scale),
                space_after=lay.px(sp.after_number * scale),
            )
        )

        title = (test.get("title") or "").strip()
        if title:
            stack.add(
                Block(
                    lines=wrap_balanced(title, f_title, self._wrap_width(f_title)),
                    font=f_title,
                    line_height=self._lh(f_title, lay.line_heights.title),
                    space_after=lay.px(sp.after_title * 0.7 * scale),
                )
            )

        stack.add(self._divider(lay.px(sp.after_divider * scale)))

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
                        (sp.after_answers if last else sp.between_answers) * scale
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
