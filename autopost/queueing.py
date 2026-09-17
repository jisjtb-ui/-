"""投稿フォルダの実験登録と、プラットフォーム別の予約・配信。

TikTokの下書き専用だった処理を一般化し、Threads / Instagram でも使えるようにする。
各プラットフォームは1日の上限が異なるため、予約時刻と上限の両方で制御する。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time as dtime, timedelta
from pathlib import Path

from .config import Settings
from .engine import ExperimentEngine, create_experiment
from .experiments import DELIVERED, ExperimentStore, READY_TO_PUBLISH
from .hosting import HostingError, get_host
from .imageprep import content_hash, prepare_post_images
from .loader import load_posts
from .models import strip_hashtags

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
    platforms: list[str],
    count: int = 0,
    base_url: str = "",
    log=print,
) -> EnqueueReport:
    """投稿フォルダを実験として登録し、画像を公開URLに紐付ける。

    すでに登録済みの投稿には、指定されたプラットフォームの配信レコードだけを足す。
    """
    report = EnqueueReport()
    bundles = load_posts(folder)
    if count:
        bundles = bundles[:count]

    known = {e.source_post_id: e for e in store.list_experiments(limit=10000) if e.source_post_id}
    host = None if base_url else get_host(settings)

    for bundle in bundles:
        existing = known.get(bundle.post_id)
        if existing:
            added = _add_platforms(store, existing.experiment_id, platforms)
            (report.created if added else report.skipped).append(
                f"{existing.experiment_id} ← {bundle.post_id}"
                + (f"（{', '.join(added)} を追加）" if added else "")
            )
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
        title = (bundle.title or (strip_hashtags(caption).splitlines() or [""])[0])[:TITLE_LIMIT]
        description = "\n\n".join(
            part for part in (strip_hashtags(caption), " ".join(bundle.hashtags)) if part
        )

        experiment = create_experiment(
            store,
            platforms,
            content_category=bundle.meta.get("category", ""),
            hook=title,
            text=description,
            image_url=urls[0],
            image_prompt=bundle.meta.get("header", ""),
            source_post_id=bundle.post_id,
            source_folder=str(bundle.folder),
            tags=bundle.hashtags,
            extra={
                "image_urls": urls,
                "image_count": len(urls),
                "content_hash": digest,
            },
        )
        store.log(experiment.experiment_id, f"画像{len(urls)}枚を公開URLに紐付けました")
        report.created.append(f"{experiment.experiment_id} ← {bundle.post_id}")

    return report


def _add_platforms(store: ExperimentStore, experiment_id: str, platforms: list[str]) -> list[str]:
    """既存の実験に、まだ無い配信先を足す。"""
    import sqlite3

    current = {p.platform for p in store.publications(experiment_id)}
    missing = [p for p in platforms if p not in current]
    if not missing:
        return []
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    experiment = store.get(experiment_id)
    status = READY_TO_PUBLISH if experiment and experiment.image_url else "generated"
    with store._connect() as conn:
        for platform in missing:
            conn.execute(
                "INSERT OR IGNORE INTO experiment_publications"
                " (experiment_id, platform, status, created_at, updated_at) VALUES (?,?,?,?,?)",
                (experiment_id, platform, status, now, now),
            )
    return missing


# ----------------------------------------------------------------------
def schedule_publications(
    settings: Settings,
    store: ExperimentStore,
    platforms: list[str],
    start: datetime,
    times: list[dtime],
    per_day: int = 0,
    count: int = 0,
    reschedule: bool = False,
    log=print,
) -> dict:
    """未配信の実験に予約時刻を割り当てる。

    既定では**まだ予約が入っていない分だけ**を対象にし、
    すでに埋まっている枠を飛ばして後ろに追加していく。
    そのため2回目以降に実行しても、既存の予定はずれない。

    reschedule=True にすると、未配信のすべてを開始日から振り直す。
    """
    assigned = 0
    details: dict[str, dict] = {}

    for platform in platforms:
        pending = [
            p for p in store.publications(platform=platform)
            if p.status == READY_TO_PUBLISH
        ]
        pending.sort(key=lambda p: p.experiment_id)

        if reschedule:
            targets = pending
            occupied: set[str] = set()
        else:
            targets = [p for p in pending if not p.scheduled_at]
            occupied = {p.scheduled_at for p in pending if p.scheduled_at}

        if count:
            targets = targets[:count]

        slots = times or [dtime(21, 0)]
        limit = per_day or len(slots)
        platform_limit = settings.daily_limit(platform)
        if platform_limit:
            limit = min(limit, platform_limit)

        generator = _slot_generator(start, slots, limit, settings.tz)
        for publication in targets:
            when = next(generator)
            while when.isoformat(timespec="seconds") in occupied:
                when = next(generator)
            store.set_schedule(publication.id, when)
            occupied.add(when.isoformat(timespec="seconds"))
            assigned += 1

        kept = len(pending) - len(targets)
        details[platform] = {"assigned": len(targets), "kept": kept, "per_day": limit}
        message = f"  {platform}: {len(targets)}件を予約（1日{limit}件）"
        if kept:
            message += f" / 既存の予約 {kept}件はそのまま"
        log(message)

    return {"assigned": assigned, "platforms": details}


def _slot_generator(start: datetime, slots: list[dtime], per_day: int, tz):
    """開始日から、1日 per_day 件の割り当て枠を順に返す。"""
    day = 0
    while True:
        date = (start + timedelta(days=day)).date()
        for index in range(per_day):
            slot = slots[index % len(slots)]
            yield datetime.combine(date, slot).replace(tzinfo=tz)
        day += 1


def publish_due(
    settings: Settings,
    store: ExperimentStore,
    platforms: list[str],
    dry_run: bool = False,
    log=print,
) -> dict:
    """予約時刻を過ぎた配信を、1日の上限まで実行する。"""
    engine = ExperimentEngine(settings, store, log=log)
    result: dict[str, dict] = {}

    for platform in platforms:
        limit = settings.daily_limit(platform)
        already = store.published_today(platform)
        remaining = max(0, limit - already) if limit else len(store.due(platform))
        targets = store.due(platform)[:remaining]

        log(f"\n[{platform}] 本日 {already}/{limit}件 / 配信待ち {len(store.due(platform))}件")
        if not targets:
            result[platform] = {"sent": 0, "remaining_today": remaining}
            if remaining == 0:
                log("  本日の上限に達しています")
            continue

        if dry_run:
            for publication in targets:
                log(f"  （dry-run）{publication.experiment_id} を配信予定")
            result[platform] = {"sent": 0, "remaining_today": remaining}
            continue

        try:
            engine.publisher(platform).preflight()
        except Exception as exc:
            log(f"  [中断] {platform}へ接続できていません: {exc}")
            log(f"    python autopost.py connect {platform} --manual を実行してください")
            log("    キューは消費していません")
            result[platform] = {"sent": 0, "error": "not_connected"}
            continue

        sent = sum(1 for publication in targets if engine.publish(publication))
        result[platform] = {"sent": sent, "remaining_today": max(0, remaining - sent)}

    return result


def overview(store: ExperimentStore, settings: Settings, platforms: list[str]) -> dict:
    """予約状況の概況。"""
    out = {}
    for platform in platforms:
        pending = [
            p for p in store.publications(platform=platform) if p.status == READY_TO_PUBLISH
        ]
        delivered = [p for p in store.publications(platform=platform) if p.status in DELIVERED]
        next_at = min(
            (p.scheduled_at for p in pending if p.scheduled_at), default=""
        )
        limit = max(1, settings.daily_limit(platform))
        out[platform] = {
            "queued": len(pending),
            "delivered": len(delivered),
            "today": store.published_today(platform),
            "daily_limit": settings.daily_limit(platform),
            "days_needed": (len(pending) + limit - 1) // limit,
            "next_at": next_at,
        }
    return out


# ----------------------------------------------------------------------
# 自動補充（キューが減ったら作り足す）
# ----------------------------------------------------------------------
def topup(
    settings: Settings,
    store: ExperimentStore,
    platforms: list[str],
    minimum: int = 0,
    generate_count: int = 0,
    base_url: str = "",
    times: list[dtime] | None = None,
    force: bool = False,
    log=print,
) -> dict:
    """配信待ちが少なくなったら、新しいコンテンツを作って予約まで行う。

      生成 → 実験登録 → 画像書き出し → （設定があれば）公開 → 予約

    これを毎日の自動実行に含めることで、実験ループが止まらなくなる。
    """
    import subprocess
    import sys

    minimum = minimum or settings.auto_topup_min
    generate_count = generate_count or settings.auto_topup_count
    base_url = base_url or settings.local_host_base_url or settings.r2_public_base_url

    remaining = {
        platform: len([
            p for p in store.publications(platform=platform) if p.status == READY_TO_PUBLISH
        ])
        for platform in platforms
    }
    lowest = min(remaining.values()) if remaining else 0
    log(f"  配信待ち: " + " / ".join(f"{k} {v}件" for k, v in remaining.items()))

    if not force and lowest >= minimum:
        log(f"  補充は不要です（下限 {minimum}件）")
        return {"generated": 0, "remaining": remaining}

    if not base_url:
        log("  [中断] 画像の公開URLが未設定です（.env の LOCAL_HOST_BASE_URL など）")
        return {"generated": 0, "error": "no_base_url"}

    log(f"  待ちが {lowest}件 まで減ったため、{generate_count}件を生成します")
    root = Path(__file__).resolve().parent.parent
    result = subprocess.run(
        [sys.executable, "generate.py", "--posts", str(generate_count)], cwd=root
    )
    if result.returncode != 0:
        log("  [中断] コンテンツ生成に失敗しました")
        return {"generated": 0, "error": "generate_failed"}

    report = enqueue_folder(settings, store, root / "output", platforms,
                            base_url=base_url, log=log)
    log(f"  {report.summary()}")

    # Cloudflare Pages へ配信する形で書き出す
    subprocess.run(
        [sys.executable, "autopost.py", "tiktok", "export-media", "--dest", "pages_media"],
        cwd=root, capture_output=True,
    )

    if settings.pages_deploy_command:
        log(f"  画像を公開しています: {settings.pages_deploy_command}")
        deploy = subprocess.run(
            settings.pages_deploy_command, cwd=root, shell=True, capture_output=True, text=True
        )
        if deploy.returncode != 0:
            log("  [警告] 画像の公開に失敗しました。手動でデプロイしてください")
            for line in (deploy.stdout + deploy.stderr).splitlines()[-3:]:
                log("    " + line)
    else:
        log("  [注意] 画像は未公開です。次のコマンドで公開してください:")
        log("    npx wrangler pages deploy pages_media --project-name honeshinri-media")
        log("    （.env の PAGES_DEPLOY_COMMAND に設定すると自動化できます）")

    schedule_publications(
        settings, store, platforms, datetime.now(), times or [dtime(21, 0)], log=log
    )
    return {"generated": len(report.created), "remaining": remaining}
