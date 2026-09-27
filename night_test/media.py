"""投稿に使う「中身」の作り分け。

同じ7枚の画像から、投稿先に応じて別の形を作る。

    MediaRenderer
      ├ ImageRenderer  … 画像をそのまま渡す（カルーセル）
      ├ ReelRenderer   … 縦動画へ変換する（Instagram Reels）
      └ 将来 VideoRenderer

投稿側（publishers/）は「どう作るか」を知らない。ここだけが知っている。
動画の作り方そのものは night_test/video.py にあり、ここでは呼ぶだけ。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .video import (
    REELS_MAX_BYTES,
    REELS_MAX_HORIZONTAL,
    REELS_MAX_SECONDS,
    REELS_MIN_FPS,
    REELS_MAX_FPS,
    REELS_MIN_SECONDS,
    ReelSpec,
    VideoError,
    build_reel,
    probe,
    source_images,
)

LogFn = Callable[[str], None]

CAROUSEL = "carousel"
REEL = "reel"


class MediaError(RuntimeError):
    """投稿に使う中身を用意できなかった。"""


@dataclass
class Media:
    """1投稿ぶんの中身。"""

    post_type: str                       # carousel / reel
    images: list[Path] = field(default_factory=list)
    video: Path | None = None
    seconds: float = 0.0
    notes: list[str] = field(default_factory=list)

    @property
    def is_video(self) -> bool:
        return self.post_type == REEL and self.video is not None

    def to_meta(self) -> dict:
        return {
            "post_type": self.post_type,
            "image_count": len(self.images),
            "video": self.video.name if self.video else "",
            "seconds": round(self.seconds, 2) if self.seconds else 0.0,
        }


class MediaRenderer:
    """投稿の中身を作る人の共通の形。"""

    post_type = ""

    def render(self, folder: Path, log: LogFn = lambda m: None) -> Media:
        raise NotImplementedError


class ImageRenderer(MediaRenderer):
    """画像をそのまま使う（カルーセル）。変換はしない。"""

    post_type = CAROUSEL

    def render(self, folder: Path, log: LogFn = lambda m: None) -> Media:
        images = source_images(Path(folder))
        if not images:
            raise MediaError(f"画像が見つかりません: {folder}")
        return Media(post_type=CAROUSEL, images=list(images))


class ReelRenderer(MediaRenderer):
    """画像を縦動画（1080x1920）へ変換する。

    秒数・大きさはすべて ReelSpec で変えられる。BGM・アニメーション・
    字幕などは入れない（今回の範囲外）。
    """

    post_type = REEL

    def __init__(self, spec: ReelSpec | None = None,
                 output_dir: Path | None = None) -> None:
        self.spec = spec or ReelSpec(mode="uniform")
        self.output_dir = output_dir

    def render(self, folder: Path, log: LogFn = lambda m: None) -> Media:
        folder = Path(folder)
        images = source_images(folder)
        if not images:
            raise MediaError(f"画像が見つかりません: {folder}")

        destination = Path(self.output_dir or folder) / "reel.mp4"
        try:
            video = build_reel(folder, destination, self.spec, log=log)
        except VideoError as exc:
            raise MediaError(str(exc)) from exc

        problems = validate_reel(video)
        if problems:
            # 投稿はしない。理由を残して止める
            raise MediaError("動画がInstagramの条件を満たしていません:\n  - "
                             + "\n  - ".join(problems))

        seconds = self.spec.total_seconds([p.name for p in images])
        return Media(post_type=REEL, images=list(images), video=video,
                     seconds=seconds)


# ----------------------------------------------------------------------
def _duration_seconds(text: str) -> float | None:
    """"00:00:11.57" を秒にする。"""
    try:
        hours, minutes, seconds = text.split(":")
        return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    except (ValueError, AttributeError):
        return None


def validate_reel(path: Path) -> list[str]:
    """出来上がった動画がInstagram Reelsの条件に合うか確かめる。

    公式仕様（2026-09時点で確認）:
      MP4/MOV・H264(またはHEVC)・yuv420p・23〜60fps・横は1920まで
      長さ 3秒〜15分・300MBまで・AAC 48kHz・moov atom を先頭へ

    合っていなければ理由を返す。**投稿の前にここで止める。**
    """
    path = Path(path)
    problems: list[str] = []

    if not path.is_file():
        return [f"ファイルがありません: {path}"]
    if path.suffix.lower() not in (".mp4", ".mov"):
        problems.append(f"MP4またはMOVにしてください（いまは {path.suffix}）")

    size = path.stat().st_size
    if size == 0:
        return ["ファイルが空です"]
    if size > REELS_MAX_BYTES:
        problems.append(
            f"ファイルが大きすぎます（{size / 1024 / 1024:.0f}MB > "
            f"{REELS_MAX_BYTES // 1024 // 1024}MB）")

    try:
        info = probe(path)
    except VideoError as exc:
        return [f"動画として読めません: {exc}"]

    seconds = _duration_seconds(info.get("duration", ""))
    if seconds is None:
        problems.append("長さを読み取れません（壊れている可能性があります）")
    elif seconds < REELS_MIN_SECONDS:
        problems.append(f"短すぎます（{seconds:.1f}秒 < {REELS_MIN_SECONDS:.0f}秒）")
    elif seconds > REELS_MAX_SECONDS:
        problems.append(f"長すぎます（{seconds:.0f}秒 > {REELS_MAX_SECONDS:.0f}秒）")

    video = info.get("video", "")
    if not video:
        problems.append("映像トラックがありません")
    else:
        if "h264" not in video and "hevc" not in video:
            problems.append(f"H264かHEVCにしてください: {video[:60]}")
        if "yuv420p" not in video:
            problems.append(f"4:2:0（yuv420p）にしてください: {video[:60]}")
        size_text = _resolution(video)
        if size_text:
            width, height = size_text
            if max(width, height) > REELS_MAX_HORIZONTAL:
                problems.append(f"大きすぎます（{width}x{height}）")
            if width <= 0 or height <= 0:
                problems.append(f"解像度を読み取れません: {video[:60]}")
        fps = _fps(video)
        if fps is not None and not (REELS_MIN_FPS <= fps <= REELS_MAX_FPS):
            problems.append(f"フレームレートが範囲外です（{fps:.0f}fps）")

    audio = info.get("audio", "")
    if audio and "aac" not in audio:
        problems.append(f"音声はAACにしてください: {audio[:40]}")

    return problems


def _resolution(video_line: str) -> tuple[int, int] | None:
    import re

    found = re.search(r"(\d{2,5})x(\d{2,5})", video_line)
    return (int(found.group(1)), int(found.group(2))) if found else None


def _fps(video_line: str) -> float | None:
    import re

    found = re.search(r"([\d.]+)\s*fps", video_line)
    return float(found.group(1)) if found else None


# ----------------------------------------------------------------------
def renderer_for(post_type: str, spec: ReelSpec | None = None,
                 output_dir: Path | None = None) -> MediaRenderer:
    """投稿タイプから、中身を作る人を選ぶ。"""
    if post_type == REEL:
        return ReelRenderer(spec=spec, output_dir=output_dir)
    if post_type == CAROUSEL:
        return ImageRenderer()
    raise MediaError(f"知らない投稿タイプです: {post_type}"
                     f"（使えるのは {CAROUSEL} / {REEL}）")
