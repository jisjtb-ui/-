"""書き出した画像が安全域からはみ出していないか確かめる。

文字が端に寄ると、TikTok/Instagramのボタンやキャプションに隠れる。
安全域の外に黒い画素が1つでもあれば失敗として報告する。

  python tools/check_layout.py output
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PIL import Image                                  # noqa: E402

from night_test.config import Layout                   # noqa: E402

INK = 200          # これより暗い画素を「文字」とみなす


def violations(path: Path, layout: Layout) -> dict | None:
    with Image.open(path) as image:
        gray = image.convert("L")
        width, height = gray.size
        pixels = gray.load()

        left = right = top = bottom = None
        for y in range(height):
            for x in range(width):
                if pixels[x, y] < INK:
                    left = x if left is None else min(left, x)
                    right = x if right is None else max(right, x)
                    top = y if top is None else min(top, y)
                    bottom = y if bottom is None else max(bottom, y)

    if left is None:
        return None                                    # 真っ白（文字なし）

    out = {}
    if left < layout.safe_left:
        out["左"] = layout.safe_left - left
    if right > layout.safe_right:
        out["右"] = right - layout.safe_right
    if top < layout.safe_top:
        out["上"] = layout.safe_top - top
    if bottom > layout.safe_bottom:
        out["下"] = bottom - layout.safe_bottom
    return out or None


def main() -> int:
    target = Path(sys.argv[1] if len(sys.argv) > 1 else "output")
    layout = Layout()
    images = sorted(target.rglob("*.png"))
    images = [p for p in images if p.name != "preview.jpg"]
    if not images:
        print(f"画像がありません: {target}")
        return 1

    print(f"安全域: 横 {layout.safe_left}〜{layout.safe_right} / "
          f"縦 {layout.safe_top}〜{layout.safe_bottom}（{layout.width}x{layout.height}）")
    print(f"確認する画像: {len(images)}枚\n")

    failed = 0
    for path in images:
        found = violations(path, layout)
        if found:
            failed += 1
            detail = " / ".join(f"{side}へ{px}px" for side, px in found.items())
            print(f"  はみ出し: {path.relative_to(target)}  {detail}")

    print(f"\n{'=' * 46}")
    if failed:
        print(f"はみ出し {failed}枚 / {len(images)}枚")
        return 1
    print(f"はみ出しなし（{len(images)}枚すべて安全域の内側）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
