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
    log=print,
) -> dict:
    """未配信の実験に予約時刻を割り当てる。

    1日あたりの件数は times の数（または per_day）で決まる。
    例: times=[09:00, 21:00] なら1日2件、日をまたいで順に埋めていく。
    """
    assigned = 0
    for platform in platforms:
        pending = [
            p for p in store.publications(platform=platform)
            if p.status == READY_TO_PUBLISH
        ]
        pending.sort(key=lambda p: p.experiment_id)
        if count:
            pending = pending[:count]

        slots = times or [dtime(21, 0)]
        limit = per_day or len(slots)
        limit = min(limit, settings.daily_limit(platform) or limit)

        day = 0
        index = 0
        for publication in pending:
            slot = slots[index % len(slots)]
            when = datetime.combine((start + timedelta(days=day)).date(), slot)
            when = when.replace(tzinfo=settings.tz)
            store.set_schedule(publication.id, when)
            assigned += 1
            index += 1
            if index % limit == 0:
                day += 1
                index = 0

        log(f"  {platform}: {len(pending)}件を予約（1日{limit}件）")

    return {"assigned": assigned}


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
