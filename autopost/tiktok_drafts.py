"""TikTokへの下書き一括転送。

公式仕様上の制約（2026年9月時点で確認）:
  - 下書き転送は post_mode=MEDIA_UPLOAD。送れるのは title と description のみ
  - 音楽（auto_add_music）は DIRECT_POST 専用。下書きには付けられない
    → 音楽はTikTokアプリの編集画面で本人が選ぶ
  - **保留中の共有は24時間あたり5件まで**（spam_risk_too_many_pending_share）
    → 100件を一度に送ることはできない。1日5件ずつ処理する
  - アクセストークンあたり 6リクエスト/分

そのため「100件をキューに積み、毎日上限まで自動で下書きへ送る」方式にする。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .config import Settings
from .engine import ExperimentEngine, create_experiment
from .experiments import DELIVERED, ExperimentStore, READY_TO_PUBLISH
from .hosting import HostingError, get_host
from .imageprep import content_hash, prepare_post_images
from .loader import load_posts
from .models import extract_hashtags, strip_hashtags

TIKTOK = "tiktok"
TITLE_LIMIT = 90


@dataclass
class EnqueueReport:
    created: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"登録 {len(self.created)}件 / 既存 {len(self.skipped)}件 /"
            f" 失敗 {len(self.failed)}件"
        )


def enqueue_folder(
    settings: Settings,
    store: ExperimentStore,
    folder: Path,
    count: int = 0,
    base_url: str = "",
    upload: bool = True,
    log=print,
) -> EnqueueReport:
    """投稿フォルダを実験として登録し、画像を公開URLに載せる。

    base_url を指定した場合は、すでにその場所へ配信済み／配信予定として
    URLだけを組み立てる（Cloudflare Pages に自分でデプロイする運用）。
    """
    report = EnqueueReport()
    bundles = load_posts(folder)
    if count:
        bundles = bundles[:count]

    existing = {
        e.source_post_id
        for e in store.list_experiments(limit=10000)
        if e.source_post_id
    }
    host = None
    if upload and not base_url:
        host = get_host(settings)

    for bundle in bundles:
        if bundle.post_id in existing:
            report.skipped.append(bundle.post_id)
            continue
        try:
            prepared = prepare_post_images(bundle.post_id, bundle.images, settings.cache_dir)
            paths = [item.path for item in prepared]
            digest = content_hash(paths)
            if base_url:
                urls = [
                    f"{base_url.rstrip('/')}/{bundle.post_id}/{digest}/{p.name}" for p in paths
                ]
            else:
                urls = host.upload(bundle.post_id, paths, digest)
        except (HostingError, OSError) as exc:
            report.failed.append(f"{bundle.post_id}: {exc}")
            log(f"  {bundle.post_id}: 画像を準備できません → {exc}")
            continue

        caption = bundle.caption
        hashtags = bundle.hashtags
        title = (bundle.title or strip_hashtags(caption).splitlines()[0] if caption else "")[:TITLE_LIMIT]
        description = "\n\n".join(
            part for part in (strip_hashtags(caption), " ".join(hashtags)) if part
        )

        experiment = create_experiment(
            store,
            [TIKTOK],
            hypothesis=bundle.meta.get("publish", {}).get("hypothesis", ""),
            content_category=bundle.meta.get("category", ""),
            hook=title,
            text=description,
            image_url=urls[0],
            image_prompt=bundle.meta.get("header", ""),
            source_post_id=bundle.post_id,
            source_folder=str(bundle.folder),
            tags=hashtags,
            extra={
                "image_urls": urls,
                "image_count": len(urls),
                "content_hash": digest,
                "post_mode": "MEDIA_UPLOAD",
                "music": "manual（TikTokの編集画面で選択）",
            },
        )
        store.log(experiment.experiment_id, f"画像{len(urls)}枚を公開URLに紐付けました", TIKTOK)
        report.created.append(f"{experiment.experiment_id} ← {bundle.post_id}")

    return report


def send_drafts(
    settings: Settings,
    store: ExperimentStore,
    max_count: int = 0,
    dry_run: bool = False,
    log=print,
) -> dict:
    """1日の上限まで下書きを送る。中断しても次回は続きから再開できる。"""
    daily_limit = settings.tiktok_daily_draft_limit
    already = store.published_today(TIKTOK)
    remaining = max(0, daily_limit - already)
    if max_count:
        remaining = min(remaining, max_count)

    pending = [p for p in store.runnable(TIKTOK) if p.status == READY_TO_PUBLISH]
    targets = pending[:remaining]

    log(f"本日の送信済み: {already}/{daily_limit} 件 / 待機中: {len(pending)} 件")
    if not targets:
        if pending and remaining == 0:
            log("本日の上限に達しています。明日また実行してください（TikTokの仕様上5件/24時間）")
        return {"sent": 0, "remaining_today": remaining, "queued": len(pending)}

    if dry_run:
        for publication in targets:
            log(f"  （dry-run）{publication.experiment_id} を下書きへ送信予定")
        return {"sent": 0, "remaining_today": remaining, "queued": len(pending)}

    # 先に一度だけ接続を確認する（未接続のままキューを消費しないため）
    engine = ExperimentEngine(settings, store, log=log)
    try:
        engine.publisher(TIKTOK).preflight()
    except Exception as exc:
        log("")
        log("[中断] TikTokへ接続できていないため送信しませんでした。")
        log(f"  理由: {exc}")
        log("")
        log("  次の手順で接続してください:")
        log("    1. .env に TIKTOK_CLIENT_KEY / TIKTOK_CLIENT_SECRET を設定")
        log("    2. TikTok開発者ポータルで video.upload スコープを有効化")
        log("    3. python autopost.py connect tiktok")
        log("")
        log("  キューはそのまま残っています（1件も消費していません）。")
        return {"sent": 0, "remaining_today": remaining, "queued": len(pending),
                "error": "not_connected"}

    sent = 0
    for publication in targets:
        if engine.publish(publication):
            sent += 1
            continue
        current = store.publication(publication.experiment_id, TIKTOK)
        if current and current.error_message and "上限" in current.error_message:
            log("TikTok側の上限に達したため、本日の処理を終了します")
            break

    return {
        "sent": sent,
        "remaining_today": max(0, remaining - sent),
        "queued": len(pending) - sent,
    }


def queue_overview(store: ExperimentStore, settings: Settings) -> dict:
    """キューの概況（残件数と完了見込み日数）。"""
    pending = [p for p in store.runnable(TIKTOK) if p.status == READY_TO_PUBLISH]
    delivered = [
        p for p in store.publications(platform=TIKTOK) if p.status in DELIVERED
    ]
    limit = max(1, settings.tiktok_daily_draft_limit)
    return {
        "queued": len(pending),
        "delivered": len(delivered),
        "today": store.published_today(TIKTOK),
        "daily_limit": settings.tiktok_daily_draft_limit,
        "days_needed": (len(pending) + limit - 1) // limit,
    }


def export_media(
    settings: Settings,
    store: ExperimentStore,
    destination: Path,
    log=print,
) -> dict:
    """Cloudflare Pages へデプロイする形のままメディアを書き出す。

    登録済み実験の image_urls と同じディレクトリ構造で配置するため、
    出力フォルダをそのまま Pages に配信すればURLが一致する。
    """
    import shutil

    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    copied = skipped = 0

    for experiment in store.list_experiments(limit=10000):
        urls = (experiment.extra or {}).get("image_urls") or []
        digest = (experiment.extra or {}).get("content_hash", "")
        if not urls or not experiment.source_post_id:
            continue
        source_dir = settings.cache_dir / experiment.source_post_id
        target_dir = destination / experiment.source_post_id / digest
        target_dir.mkdir(parents=True, exist_ok=True)
        for url in urls:
            name = url.rsplit("/", 1)[-1]
            source = source_dir / name
            target = target_dir / name
            if not source.is_file():
                skipped += 1
                continue
            if target.is_file() and target.stat().st_size == source.stat().st_size:
                continue
            shutil.copy2(source, target)
            copied += 1

    log(f"{destination} へ {copied} ファイルを書き出しました"
        + (f"（未変換のためスキップ: {skipped}）" if skipped else ""))
    return {"copied": copied, "skipped": skipped, "destination": str(destination)}
