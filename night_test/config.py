"""画像サイズ・安全エリア・文字サイズなどのレイアウト定義。

方針:
  - 背景は白、文字は黒のみ。
  - 画面全体を文字で埋めず、中央のコンテンツボックス内にだけ要素を置く。
  - 文字を置く範囲は画像全体の中央 60〜70% 程度（左右15〜85%, 上下18〜82%）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

# --- 色（白と黒のみ。装飾色は一切使わない） ---
WHITE = (255, 255, 255)
BLACK = (17, 17, 17)          # 純黒より少し落とした黒（静かな紙面のため）
HAIRLINE = (216, 216, 216)    # ごく薄い区切り線のみ許容

# --- 画像サイズ ---
DEFAULT_SIZE = (1080, 1440)   # 標準（3:4）
PRESET_SIZES = {
    "1080x1440": (1080, 1440),
    "1200x1600": (1200, 1600),
    "1350x1800": (1350, 1800),
}

# --- 安全エリア（画像全体に対する比率） ---
SAFE_LEFT = 0.15
SAFE_RIGHT = 0.85
SAFE_TOP = 0.18
SAFE_BOTTOM = 0.82

# 1投稿あたりの問題数（1問 = 問題画像1枚 + 答え画像1枚 → 合計10枚）
TESTS_PER_POST = 5

# 自動縮小の下限（これ以上小さくしない。超える場合は本文を削るべきサイン）
MIN_FIT_SCALE = 0.74
FIT_STEP = 0.02


@dataclass(frozen=True)
class FontSizes:
    """基準サイズ（幅1080pxのときの px 値）。

    メリハリはつけすぎず、静かな紙面として自然に見える差分にとどめる。
    """

    eyebrow: int = 29       # 「夜の心理テスト」「答え」
    number: int = 30        # 問題番号
    title: int = 50         # 短いタイトル
    question: int = 45      # 質問文（最も読みやすく）
    choice: int = 40        # 選択肢 A/B/C/D
    answer: int = 35        # 答えの本文
    closing: int = 33       # 締めの一文
    footer: int = 27        # 「答えは次へ」


@dataclass(frozen=True)
class LineHeights:
    """行送り（フォントサイズに対する倍率）。余白多めの静かな組み。"""

    eyebrow: float = 1.5
    number: float = 1.4
    title: float = 1.45
    question: float = 1.78
    choice: float = 1.72
    answer: float = 1.66
    closing: float = 1.7
    footer: float = 1.4


@dataclass(frozen=True)
class Spacing:
    """ブロック間の空き（幅1080pxのときの px 値）。"""

    after_eyebrow: int = 34
    after_number: int = 26
    after_title: int = 40
    after_divider: int = 44
    after_question: int = 54
    between_choices: int = 16
    after_choices: int = 52
    between_answers: int = 30
    after_answers: int = 52
    divider_width_ratio: float = 0.22   # 区切り線の長さ（安全エリア幅に対する比）


@dataclass
class Layout:
    """1枚の画像を組むためのレイアウト設定。"""

    width: int = DEFAULT_SIZE[0]
    height: int = DEFAULT_SIZE[1]
    paper: bool = False                 # True でごく薄い紙質感を乗せる
    sizes: FontSizes = field(default_factory=FontSizes)
    line_heights: LineHeights = field(default_factory=LineHeights)
    spacing: Spacing = field(default_factory=Spacing)

    # --- 安全エリア（実ピクセル） ---
    @property
    def safe_left(self) -> int:
        return int(self.width * SAFE_LEFT)

    @property
    def safe_right(self) -> int:
        return int(self.width * SAFE_RIGHT)

    @property
    def safe_top(self) -> int:
        return int(self.height * SAFE_TOP)

    @property
    def safe_bottom(self) -> int:
        return int(self.height * SAFE_BOTTOM)

    @property
    def safe_width(self) -> int:
        return self.safe_right - self.safe_left

    @property
    def safe_height(self) -> int:
        return self.safe_bottom - self.safe_top

    @property
    def center_x(self) -> int:
        return self.width // 2

    @property
    def scale(self) -> float:
        """基準幅1080からの拡大率（サイズ変更時に見た目を保つ）。"""
        return self.width / DEFAULT_SIZE[0]

    def px(self, base: float) -> int:
        """基準px値を実サイズに換算する。"""
        return max(1, int(round(base * self.scale)))
