"""Instagram Reels 対応のテスト。

  python tools/test_reel.py

実際にMP4を書き出して中身を確かめる。Instagram APIは差し替えるので、
**外部へは1件も投稿しない。**
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from autopost.config import Settings
from autopost.engine import create_experiment
from autopost.experiments import Experiment, ExperimentStore, Metrics
from autopost.oauth.store import Token, TokenStore
from autopost.publishers import instagram as ig
from autopost import report
from night_test.media import (
    CAROUSEL, REEL, ImageRenderer, MediaError, ReelRenderer,
    renderer_for, validate_reel,
)
from night_test.video import ReelSpec, ffmpeg_path, probe


def _make_post(out: Path) -> Path:
    """恋愛心理の7枚を実際に作る。"""
    result = subprocess.run(
        [sys.executable, "generate.py", "--posts", "1", "--template", "v2_light",
         "--axis", "dependence", "--output", str(out), "--history", str(out / "h.json")],
        cwd=ROOT, capture_output=True, text=True, timeout=600)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or "")[-300:])
    return out / "post_001"


def run(check) -> None:
    out = Path(tempfile.mkdtemp())
    folder = _make_post(out / "gen")
    images = sorted(folder.glob("*.png"))
    check("7枚の元画像ができる", len(images) == 7, f"{len(images)}枚")

    # ---------------------------------------------------------------- 1
    # MediaRenderer の分かれ方
    check("投稿タイプで作る人が変わる",
          isinstance(renderer_for(CAROUSEL), ImageRenderer)
          and isinstance(renderer_for(REEL), ReelRenderer))
    try:
        renderer_for("story")
        rejected = False
    except MediaError:
        rejected = True
    check("知らない投稿タイプは黙って別のものにしない", rejected)

    carousel = renderer_for(CAROUSEL).render(folder)
    check("カルーセルは画像をそのまま渡す",
          carousel.post_type == CAROUSEL and len(carousel.images) == 7
          and carousel.video is None)

    # ---------------------------------------------------------------- 2
    # Reel の動画
    spec = ReelSpec(mode="uniform", seconds_uniform=1.5, seconds_last=2.5, fps=30)
    media = renderer_for(REEL, spec=spec, output_dir=out).render(folder)
    check("MP4ができる", media.video and media.video.is_file(), str(media.video))
    check("長さが設定どおり（1.5×6 + 2.5 = 11.5秒）",
          abs(media.seconds - 11.5) < 0.01, f"{media.seconds}秒")

    info = probe(media.video)
    video_line = info.get("video", "")
    check("1080x1920（9:16）", "1080x1920" in video_line, video_line[:70])
    check("H264", "h264" in video_line, video_line[:70])
    check("4:2:0（yuv420p）", "yuv420p" in video_line, video_line[:70])
    check("30fps", "30 fps" in video_line, video_line[:70])
    check("音声トラックがある（無音のAAC）", "aac" in info.get("audio", ""),
          info.get("audio", "なし"))
    check("300MB以内", info["size"] < 300 * 1024 * 1024,
          f"{info['size'] / 1024 / 1024:.1f}MB")
    check("3秒以上", media.seconds >= 3.0)

    # 秒数は設定で変えられる
    slow = ReelSpec(mode="uniform", seconds_uniform=3.0, seconds_last=5.0)
    check("秒数を変えられる（ハードコードしていない）",
          abs(slow.total_seconds([f"{i:02d}" for i in range(7)]) - 23.0) < 0.01,
          str(slow.total_seconds([f"{i:02d}" for i in range(7)])))

    # ---------------------------------------------------------------- 3
    # 検証で弾く
    check("正しい動画は合格する", validate_reel(media.video) == [],
          str(validate_reel(media.video)))

    exe = ffmpeg_path()
    bad = out / "bad"
    bad.mkdir(exist_ok=True)
    cases = [
        ("短すぎる", ["-f", "lavfi", "-i", "color=white:s=1080x1920:d=2",
                   "-c:v", "libx264", "-pix_fmt", "yuv420p"], "短すぎ"),
        ("4:2:0でない", ["-f", "lavfi", "-i", "color=white:s=1080x1920:d=5",
                     "-c:v", "libx264", "-pix_fmt", "yuv444p"], "4:2:0"),
        ("大きすぎる", ["-f", "lavfi", "-i", "color=white:s=2160x3840:d=5",
                   "-c:v", "libx264", "-pix_fmt", "yuv420p"], "大きすぎ"),
    ]
    for label, args, expected in cases:
        target = bad / f"{expected}.mp4"
        subprocess.run([exe, "-y", "-loglevel", "error", *args, str(target)],
                       capture_output=True, timeout=120)
        problems = validate_reel(target)
        check(f"{label}動画を弾く", any(expected in p for p in problems), str(problems))

    broken = bad / "broken.mp4"
    broken.write_bytes(b"not a video")
    check("壊れたファイルを弾く", bool(validate_reel(broken)))
    check("無いファイルを弾く", bool(validate_reel(bad / "nope.mp4")))

    # 条件を満たさない動画は「作れなかった」として止まる（投稿しない）
    class BadRenderer(ReelRenderer):
        def render(self, folder, log=lambda m: None):
            short = ReelSpec(mode="uniform", seconds_uniform=0.2, seconds_last=0.2)
            return ReelRenderer(spec=short, output_dir=self.output_dir).render(folder, log)

    try:
        BadRenderer(output_dir=out).render(folder)
        stopped = False
    except MediaError as exc:
        stopped = "短すぎ" in str(exc)
    check("条件を満たさない動画は投稿前に止まる", stopped)

    # ---------------------------------------------------------------- 4
    # Instagram API（差し替え。外部へは投稿しない）
    tokens = TokenStore(out / "tokens")
    tokens.save(Token(platform="instagram", access_token="T", account_id="IG1"))
    calls: list[dict] = []

    def fake_request(method, url, **kwargs):
        payload = kwargs.get("data") or kwargs.get("params") or {}
        calls.append({"url": url, "method": method, **payload})
        if url.endswith("/media"):
            return {"id": "CONTAINER_1"}
        if url.endswith("/media_publish"):
            return {"id": "IG_REEL_123"}
        if "insights" in url:
            names = [m.strip() for m in payload.get("metric", "").split(",")]
            values = {"views": 9000, "reach": 7000, "likes": 180,
                      "ig_reels_avg_watch_time": 6400}
            return {"data": [{"name": n, "total_value": {"value": values[n]}}
                             for n in names if n in values]}
        return {"status_code": "FINISHED", "permalink": "https://instagram.com/reel/x"}

    publisher = ig.InstagramPublisher(Settings.load(), tokens)
    publisher._token = tokens.load("instagram")
    publisher._account_id = "IG1"

    def experiment_for(extra: dict) -> Experiment:
        return Experiment(experiment_id="EXP-1", hook="かくれフック",
                          text="本文", tags=["恋愛心理"], extra=extra)

    reel_experiment = experiment_for({
        "post_type": "reel",
        "video_url": "https://example.pages.dev/p/reel.mp4",
        "image_urls": ["https://example.pages.dev/p/01.jpg"]})

    with patch.object(ig, "request_json", fake_request), \
         patch.object(ig.meta_oauth, "graph_base", lambda s: "https://graph"), \
         patch.object(publisher, "preflight", lambda: None), \
         patch.object(ig.time, "sleep", lambda s: None):
        result = publisher.publish_content(reel_experiment, "", log=lambda m: None)

    container = next((c for c in calls if c["url"].endswith("/media")), {})
    check("media_type=REELS でコンテナを作る", container.get("media_type") == "REELS",
          json.dumps({k: v for k, v in container.items() if k != "access_token"},
                     ensure_ascii=False)[:160])
    check("video_url を渡す", container.get("video_url", "").endswith("reel.mp4"))
    check("caption を渡す", "本文" in container.get("caption", ""))
    check("share_to_feed を渡す", container.get("share_to_feed") == "true")
    check("status_code を確認してから公開する",
          any("fields" in c and "status_code" in str(c.get("fields")) for c in calls))
    check("media_publish で公開する",
          any(c["url"].endswith("/media_publish") for c in calls))
    check("投稿IDが返る", result.platform_post_id == "IG_REEL_123",
          result.platform_post_id)
    check("post_type=reel を結果に残す", result.extra.get("post_type") == "reel")
    check("無音であることを伝える",
          any("無音" in note for note in result.notes), str(result.notes))

    # 動画URLが無ければカルーセルへ落ちる（投稿を止めない）
    no_video = experiment_for({
        "post_type": "reel", "video_url": "",
        "image_urls": [f"https://example.pages.dev/p/{i:02d}.jpg"
                       for i in range(1, 8)]})

    calls.clear()
    with patch.object(ig, "request_json", fake_request), \
         patch.object(ig.meta_oauth, "graph_base", lambda s: "https://graph"), \
         patch.object(publisher, "preflight", lambda: None), \
         patch.object(ig.time, "sleep", lambda s: None):
        publisher.publish_content(no_video, "", log=lambda m: None)
    check("動画URLが無ければカルーセルで投稿する",
          not any(c.get("media_type") == "REELS" for c in calls))

    # ---------------------------------------------------------------- 5
    # Reelの反応データ
    with patch.object(ig, "request_json", fake_request), \
         patch.object(ig.meta_oauth, "graph_base", lambda s: "https://graph"), \
         patch.object(publisher, "preflight", lambda: None):
        reel_metrics = publisher.get_analytics("IG_REEL_123", post_type="reel")
        carousel_metrics = publisher.get_analytics("IG_1")
    check("Reelは平均視聴時間が取れる", reel_metrics.watch_time_seconds == 6.4,
          str(reel_metrics.watch_time_seconds))
    check("カルーセルでは視聴時間を要求しない",
          carousel_metrics.watch_time_seconds is None)

    # ---------------------------------------------------------------- 6
    # 記録と比較
    settings = Settings.load()
    settings.experiments_db_path = out / "reel.db"
    store = ExperimentStore(settings.experiments_db_path)
    for kind, (views, rate) in {"carousel": (4200, 0.021), "reel": (9800, 0.014)}.items():
        for index in range(12):
            experiment = create_experiment(
                store, ["instagram"], content_category="dependence",
                template_version="love_psychology_v2_light")
            publication = store.publication(experiment.experiment_id, "instagram")
            store.mark_published(publication.id, f"x{index}", post_type=kind)
            store.save_metrics(Metrics(
                experiment_id=experiment.experiment_id, platform="instagram",
                snapshot="24h", views=views, reach=int(views * 0.8),
                likes=int(views * 0.02), comments=int(views * 0.002),
                saves=int(views * rate), shares=int(views * rate * 0.5),
                post_type=kind,
                watch_time_seconds=6.4 if kind == "reel" else None))

    saved = store.publication(
        store.list_experiments(limit=1)[0].experiment_id, "instagram")
    check("publications に post_type が残る", saved.post_type in ("carousel", "reel"),
          saved.post_type)

    rows, note = report.post_type_comparison(settings, store)
    by_type = {row.variant: row for row in rows}
    check("カルーセルとReelを分けて集計できる",
          set(by_type) == {"carousel", "reel"}, str(set(by_type)))
    check("Reelの方が表示が多い", by_type["reel"].views > by_type["carousel"].views)
    check("カルーセルの方が保存率が高い（率で比べられる）",
          by_type["carousel"].save_rate > by_type["reel"].save_rate)
    check("Reelの平均視聴時間が出る", by_type["reel"].watch_time == 6.4,
          str(by_type["reel"].watch_time))
    check("カルーセルには視聴時間が入らない", by_type["carousel"].watch_time is None)
    text = report.post_type_report(settings, store)
    check("文字のレポートを作れる", "カルーセル vs Reel" in text)

    # ---------------------------------------------------------------- 7
    # 他の媒体・既存の経路に影響していない
    threads_source = (ROOT / "autopost" / "publishers" / "threads.py").read_text(encoding="utf-8")
    tiktok_source = (ROOT / "autopost" / "publishers" / "tiktok.py").read_text(encoding="utf-8")
    check("Threadsに手を入れていない", "REELS" not in threads_source)
    check("TikTokに手を入れていない", "REELS" not in tiktok_source)
    check("Reelの作り方は投稿処理に書かれていない",
          "libx264" not in (ROOT / "autopost" / "publishers" / "instagram.py")
          .read_text(encoding="utf-8"))
    gui_source = (ROOT / "autopost" / "gui_catalog.py").read_text(encoding="utf-8")
    check("画面から投稿タイプを選べる", "post_type_box" in gui_source)
    check("画面で比較を見られる", "post_type_tree" in gui_source)
    check("設定で既定を決められる", Settings.load().instagram_post_type in ("carousel", "reel"))
    cli = subprocess.run([sys.executable, "autopost.py", "hooks", "posttype"],
                         cwd=ROOT, capture_output=True, text=True, timeout=180)
    check("コマンドからも比較を出せる",
          cli.returncode == 0 and "カルーセル vs Reel" in cli.stdout,
          (cli.stdout + cli.stderr)[-200:])


def main() -> int:
    failures: list[str] = []

    def check(name: str, condition, detail: str = "") -> None:
        mark = "OK  " if condition else "NG  "
        print(f"  {mark}{name}" + (f" … {detail}" if detail and not condition else ""))
        if not condition:
            failures.append(f"{name}: {detail}" if detail else name)

    print("Instagram Reels")
    print("-" * 52)
    run(check)
    print("-" * 52)
    if failures:
        print(f"失敗 {len(failures)}件")
        for item in failures:
            print(f"  - {item}")
        return 1
    print("すべて通りました")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
