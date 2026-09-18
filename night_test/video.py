"""静止画からReel用の縦動画を作る。

既存の10枚（01_question.png … 10_answer.png）をそのまま使い、
9:16（1080x1920）の白背景の中央に置いてつなげる。デザインは変えない。

音声は入れない。Instagramの音楽ライブラリはAPI経由の投稿では使えないため、
**音はアプリ側で自分で付ける前提**にしている。そのため出力は無音のMP4で、
「予約 → 書き出し → 自分で音源を付けて投稿」という流れを想定している。

ffmpeg は imageio-ffmpeg が同梱しているものを使うので、
利用者が別途インストールする必要はない。
"""

from __future__ import annotations

import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from PIL import Image

LogFn = Callable[[str], None]

REEL_WIDTH = 1080
REEL_HEIGHT = 1920
FPS = 30
BACKGROUND = (255, 255, 255)

# 問題は読む時間が必要なので長く、答えは短く
SECONDS_QUESTION = 3.0
SECONDS_ANSWER = 2.5

# 見る人が「認知する」「操作する」ための余白
#   冒頭 : 何の動画かを把握する時間（スクロール直後は内容を読んでいない）
#   末尾 : 最終ページのCTAを読み、実際に押すまでの時間
LEAD_IN_SECONDS = 1.2
TAIL_SECONDS = 6.0


class VideoError(RuntimeError):
    """動画を作れなかった。"""


@dataclass
class ReelSpec:
    width: int = REEL_WIDTH
    height: int = REEL_HEIGHT
    fps: int = FPS
    seconds_question: float = SECONDS_QUESTION
    seconds_answer: float = SECONDS_ANSWER
    lead_in: float = LEAD_IN_SECONDS
    tail: float = TAIL_SECONDS

    def seconds_for(self, name: str, index: int = 1, total: int = 0) -> float:
        """その1枚を映す秒数。

        最初の1枚は認知の余白を足して長く、最後の1枚（CTAのページ）は
        読んで押すまでの時間として、はっきり長く止める。
        """
        if total and index == total:
            return self.tail
        base = self.seconds_answer if "answer" in name else self.seconds_question
        if index == 1:
            return base + self.lead_in
        return base

    def total_seconds(self, names: list[str]) -> float:
        return sum(
            self.seconds_for(name, index, len(names))
            for index, name in enumerate(names, start=1)
        )


def ffmpeg_path() -> str:
    """同梱のffmpegの場所を返す。無ければ分かる言葉で知らせる。"""
    try:
        import imageio_ffmpeg
    except ImportError as exc:
        raise VideoError(
            "動画の作成には imageio-ffmpeg が必要です。"
            "次を実行してください: pip install imageio-ffmpeg"
        ) from exc
    try:
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as exc:
        raise VideoError(f"ffmpegを見つけられませんでした: {exc}") from exc


def source_images(folder: Path) -> list[Path]:
    """投稿フォルダの画像を順番どおりに返す。"""
    images = sorted(
        p for p in folder.iterdir()
        if p.suffix.lower() in (".png", ".jpg", ".jpeg") and p.stem[:2].isdigit()
    )
    if not images:
        raise VideoError(f"画像が見つかりません: {folder}")
    return images


def _fit_to_frame(source: Path, destination: Path, spec: ReelSpec) -> None:
    """9:16の白い枠の中央に置く。拡大はせず、はみ出す場合だけ縮める。"""
    with Image.open(source) as image:
        image = image.convert("RGB")
        scale = min(spec.width / image.width, spec.height / image.height, 1.0)
        if scale < 1.0:
            image = image.resize(
                (round(image.width * scale), round(image.height * scale)),
                Image.LANCZOS,
            )
        frame = Image.new("RGB", (spec.width, spec.height), BACKGROUND)
        frame.paste(image, ((spec.width - image.width) // 2,
                            (spec.height - image.height) // 2))
        frame.save(destination, "JPEG", quality=95)


def build_reel(
    folder: Path,
    output: Path,
    spec: ReelSpec | None = None,
    log: LogFn = lambda message: None,
) -> Path:
    """投稿フォルダ1つ分のReel動画を書き出す。"""
    spec = spec or ReelSpec()
    exe = ffmpeg_path()
    images = source_images(Path(folder))
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="reel_") as tmp:
        workspace = Path(tmp)
        listing: list[str] = []
        last_frame = ""
        for position, image in enumerate(images, start=1):
            frame = workspace / f"{position:03d}.jpg"
            _fit_to_frame(image, frame, spec)
            seconds = spec.seconds_for(image.name, position, len(images))
            listing.append(f"file '{frame.name}'")
            listing.append(f"duration {seconds:.3f}")
            last_frame = frame.name
        # concat demuxer は最後の1枚の表示時間を反映させるため、もう一度書く必要がある
        listing.append(f"file '{last_frame}'")

        script = workspace / "frames.txt"
        script.write_text("\n".join(listing) + "\n", encoding="utf-8")

        total = spec.total_seconds([p.name for p in images])
        log(f"{len(images)}枚 / {total:.1f}秒 の動画を作ります"
            f"（冒頭+{spec.lead_in:.1f}秒・末尾{spec.tail:.1f}秒の余白つき）")

        command = [
            exe, "-y", "-loglevel", "error",
            "-f", "concat", "-safe", "0", "-i", str(script),
            "-vf", f"fps={spec.fps},format=yuv420p",
            "-c:v", "libx264", "-preset", "medium", "-crf", "20",
            "-movflags", "+faststart",
            "-an",                      # 音声なし（自分で付ける前提）
            str(output),
        ]
        result = subprocess.run(command, cwd=workspace, capture_output=True, text=True)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip().splitlines()
            raise VideoError(
                "動画の作成に失敗しました: " + (detail[-1] if detail else "詳細なし")
            )

    if not output.is_file() or output.stat().st_size == 0:
        raise VideoError("動画ファイルが作られませんでした")
    log(f"書き出しました: {output.name}（{output.stat().st_size / 1024 / 1024:.1f} MB）")
    return output


def probe(path: Path) -> dict:
    """出来上がった動画の情報を読む（テストと確認用）。"""
    exe = ffmpeg_path()
    result = subprocess.run(
        [exe, "-i", str(path)], capture_output=True, text=True
    )
    info: dict = {"size": Path(path).stat().st_size}
    for line in result.stderr.splitlines():
        stripped = line.strip()
        if stripped.startswith("Duration:"):
            info["duration"] = stripped.split(",")[0].replace("Duration:", "").strip()
        if "Video:" in stripped:
            info["video"] = stripped.split("Video:", 1)[1].strip()[:110]
        if "Audio:" in stripped:
            info["audio"] = stripped.split("Audio:", 1)[1].strip()[:60]
    return info
