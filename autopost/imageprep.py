"""投稿用画像の準備（プラットフォーム要件へ機械的に合わせる）。

Instagram の画像要件（公式ドキュメント）:
  - JPEG のみ / 8MB以下 / アスペクト比 4:5〜1.91:1 / 幅 320〜1440px / sRGB

生成される画像は 1080x1440（3:4 = 0.75）で、**4:5 より縦長**のため
Instagram ではそのままでは要件を満たさない。内容を作り直すのではなく、
左右に白を足して 1152x1440（= 4:5）へ整える。背景が白なので見た目は変わらない。

この 4:5 JPEG は TikTok の写真投稿にもそのまま使えるため、
1投稿につき1セットだけ作ってアップロードも1回で済ませる。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

# Instagram の許容アスペクト比（幅 / 高さ）
MIN_RATIO = 4 / 5          # 0.80
MAX_RATIO = 1.91
MAX_WIDTH = 1440
MIN_WIDTH = 320
MAX_BYTES = 8 * 1024 * 1024
JPEG_QUALITY = 92
WHITE = (255, 255, 255)


@dataclass(frozen=True)
class PreparedImage:
    """アップロード対象の1枚。"""

    index: int          # 1始まり。並び順はここで確定する
    source: Path
    path: Path
    width: int
    height: int
    size_bytes: int

    @property
    def name(self) -> str:
        return self.path.name


def target_size(width: int, height: int) -> tuple[int, int]:
    """要件内に収まる最小限の余白付きサイズを返す（内容は切り取らない）。"""
    ratio = width / height
    if ratio < MIN_RATIO:
        width = int(round(height * MIN_RATIO))
    elif ratio > MAX_RATIO:
        height = int(round(width / MAX_RATIO))
    if width > MAX_WIDTH:
        scale = MAX_WIDTH / width
        width = MAX_WIDTH
        height = int(round(height * scale))
    if width < MIN_WIDTH:
        scale = MIN_WIDTH / width
        width = MIN_WIDTH
        height = int(round(height * scale))
    return width, height


def prepare_image(source: Path, destination: Path) -> PreparedImage:
    """1枚を 4:5 の JPEG に整える（白背景に中央配置）。"""
    destination.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(source) as im:
        im = im.convert("RGB")
        tw, th = target_size(im.width, im.height)
        if (tw, th) != (im.width, im.height):
            canvas = Image.new("RGB", (tw, th), WHITE)
            canvas.paste(im, ((tw - im.width) // 2, (th - im.height) // 2))
            im = canvas
        quality = JPEG_QUALITY
        while True:
            im.save(destination, "JPEG", quality=quality, optimize=True, subsampling=0)
            if destination.stat().st_size <= MAX_BYTES or quality <= 60:
                break
            quality -= 10
        return PreparedImage(
            index=0,
            source=source,
            path=destination,
            width=im.width,
            height=im.height,
            size_bytes=destination.stat().st_size,
        )


def _apply_sequence_times(paths: list[Path]) -> None:
    """並び順どおりの更新日時を振る（アップローダーが日時で並べても崩れないように）。"""
    from night_test.builder import set_sequence_times

    set_sequence_times(paths)


def prepare_post_images(
    post_id: str, images: list[Path], cache_dir: Path, force: bool = False
) -> list[PreparedImage]:
    """1投稿分の画像を順番どおりに変換する（変換結果はキャッシュする）。"""
    out_dir = Path(cache_dir) / post_id
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp_path = out_dir / ".sources.json"
    stamp = {p.name: [p.stat().st_size, int(p.stat().st_mtime)] for p in images}
    cached = {}
    if stamp_path.is_file() and not force:
        try:
            cached = json.loads(stamp_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            cached = {}

    prepared: list[PreparedImage] = []
    for order, source in enumerate(images, start=1):
        destination = out_dir / f"{source.stem}.jpg"
        reuse = (
            destination.is_file()
            and cached.get(source.name) == stamp[source.name]
        )
        if reuse:
            with Image.open(destination) as im:
                item = PreparedImage(
                    index=order,
                    source=source,
                    path=destination,
                    width=im.width,
                    height=im.height,
                    size_bytes=destination.stat().st_size,
                )
        else:
            item = prepare_image(source, destination)
            item = PreparedImage(
                index=order,
                source=source,
                path=item.path,
                width=item.width,
                height=item.height,
                size_bytes=item.size_bytes,
            )
        prepared.append(item)

    stamp_path.write_text(json.dumps(stamp, ensure_ascii=False, indent=2), encoding="utf-8")
    _apply_sequence_times([item.path for item in prepared])
    return prepared


def content_hash(paths: list[Path]) -> str:
    """画像セットの内容ハッシュ（アップロード先のキー名に使う）。"""
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.name.encode("utf-8"))
        digest.update(str(path.stat().st_size).encode("utf-8"))
        with path.open("rb") as handle:
            digest.update(handle.read(65536))
    return digest.hexdigest()[:16]
