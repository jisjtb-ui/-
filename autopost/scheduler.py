"""投稿キューの実行と一括予約。

- 予約時刻を過ぎたジョブを取り出して投稿する
- PCが止まっていた場合の「まとめて一気に投稿」を防ぐ catch-up ポリシー
- 片方のプラットフォームが失敗しても、もう片方には影響させない
- 同じ post_id × platform は二度投稿しない（SQLite側で保証）
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from typing import Callable

from .config import Settings
from .db import (
    Queue,
    STATUS_FAILED,
    STATUS_MANUAL,
    STATUS_POSTED,
    STATUS_PUBLISHING,
    backoff_delay,
)
from .hosting import HostingError, get_host
from .imageprep import content_hash, prepare_post_images
from .loader import LoaderError, load_post, load_posts
from .models import PLATFORMS, PostBundle
from .oauth.store import TokenStore
from .publishers import ManualRequired, PermanentError, TransientError, get_publisher
from .validate import ERROR, validate_post

LOCK_NAME = "autopost.lock"


@dataclass
class RunReport:
    """1回の実行結果。"""

    started_at: datetime
    executed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    rescheduled: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"投稿 {len(self.executed)}件 / 失敗 {len(self.failed)}件 /"
            f" スキップ {len(self.skipped)}件 / 再スケジュール {len(self.rescheduled)}件"
        )


class SingleInstance:
    """同時実行防止（ロックファイル）。"""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.handle = None

    def __enter__(self) -> "SingleInstance":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.handle = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_RDWR)
            os.write(self.handle, str(os.getpid()).encode())
        except FileExistsError as exc:
            raise RuntimeError(
                f"すでに実行中です（ロック: {self.path}）。"
                "終了しているのにこのエラーが出る場合はロックファイルを削除してください"
            ) from exc
        return self

    def __exit__(self, *args) -> None:
        if self.handle is not None:
            os.close(self.handle)
        self.path.unlink(missing_ok=True)


# ----------------------------------------------------------------------
# 一括予約
# ----------------------------------------------------------------------
def bulk_schedule(
    queue: Queue,
    settings: Settings,
    root: Path,
    start_date: datetime,
    post_time: dtime,
    interval_days: float,
    count: int,
    platforms: tuple[str, ...] = PLATFORMS,
    replace: bool = False,
    start_index: int = 0,
) -> list[tuple[str, datetime, str]]:
    """フォルダ内の post_XXX を順に予約する。戻り値は (post_id, 時刻, 結果)。"""
    bundles = load_posts(root)[start_index : start_index + count] if count else load_posts(root)
    tz = settings.tz
    base = datetime.combine(start_date.date(), post_time).replace(tzinfo=tz)
    results: list[tuple[str, datetime, str]] = []

    for order, bundle in enumerate(bundles):
        when = bundle.scheduled_at or (base + timedelta(days=interval_days * order))
        if when.tzinfo is None:
            when = when.replace(tzinfo=tz)
        targets = [p for p in platforms if p in bundle.enabled_platforms]
        for platform in targets:
            action = queue.enqueue(bundle.post_id, platform, bundle.folder, when, replace=replace)
            results.append((f"{bundle.post_id}/{platform}", when, action))
    return results


# ----------------------------------------------------------------------
# 実行
# ----------------------------------------------------------------------
class Runner:
    """予約されたジョブを実際に投稿する。"""

    def __init__(
        self,
        settings: Settings,
        queue: Queue,
        log: Callable[[str], None] = print,
    ) -> None:
        self.settings = settings
        self.queue = queue
        self.log = log
        self.store = TokenStore(settings.token_dir)
        self._publishers: dict[str, object] = {}
        self._host = None

    # ------------------------------------------------------------------
    def run_due(self, now: datetime | None = None, dry_run: bool = False) -> RunReport:
        """実行時刻を過ぎたジョブを処理する。"""
        now = now or datetime.now(self.settings.tz)
        report = RunReport(started_at=now)
        due = self.queue.due_jobs(now)
        if not due:
            return report

        runnable, deferred = self._apply_catchup(due, now)
        for job in deferred:
            new_time = self._next_free_slot(job.scheduled_at, now)
            self.queue.reschedule(job.id, new_time)
            report.rescheduled.append(f"{job.post_id}/{job.platform} → {new_time:%m/%d %H:%M}")
            self._record(job.post_id, job.platform, "info",
                         f"期限切れのため {new_time:%Y-%m-%d %H:%M} へ再スケジュール")

        for job in runnable:
            label = f"{job.post_id}/{job.platform}"
            if dry_run:
                report.skipped.append(f"{label}（dry-run）")
                continue
            try:
                ok = self._run_job(job)
            except Exception as exc:                      # 想定外も1件で止めない
                self._record(job.post_id, job.platform, "error", f"予期しないエラー: {exc}")
                self.queue.set_status(
                    job.id, STATUS_FAILED, error=str(exc)[:500],
                    next_attempt_at=now + backoff_delay(job.attempt_count + 1),
                )
                report.failed.append(label)
                continue
            (report.executed if ok else report.failed).append(label)

        self._finalize_folders(report)
        return report

    # ------------------------------------------------------------------
    def _run_job(self, job) -> bool:
        label = f"{job.post_id}/{job.platform}"
        if not self.queue.claim(job):
            self.log(f"  {label}: 他のプロセスが処理中のためスキップ")
            return False

        self.log(f"[{datetime.now():%H:%M:%S}] {label} 開始")
        try:
            bundle = load_post(Path(job.folder))
        except LoaderError as exc:
            return self._fail(job, PermanentError(str(exc)))

        issues = validate_post(
            bundle, self.settings, (job.platform,), queue=self.queue, check_images=True
        )
        blocking = [i for i in issues if i.level == ERROR]
        if blocking:
            message = " / ".join(i.message for i in blocking[:3])
            return self._fail(job, PermanentError(f"検証エラー: {message}"))

        try:
            image_urls = self._ensure_uploaded(bundle)
        except HostingError as exc:
            return self._fail(job, PermanentError(str(exc)))

        publisher = self._publisher(job.platform)
        content = bundle.content_for(job.platform)
        self.queue.set_status(job.id, STATUS_PUBLISHING)

        def plog(message: str) -> None:
            self.log(f"  [{job.platform}] {message}")
            self._record(job.post_id, job.platform, "info", message)

        try:
            result = publisher.publish(bundle, image_urls, content, plog)
        except TransientError as exc:
            return self._fail(job, exc, transient=True)
        except ManualRequired as exc:
            self.queue.set_status(job.id, STATUS_MANUAL, error=str(exc)[:500])
            self._record(job.post_id, job.platform, "warn", f"手動対応が必要: {exc}")
            self.log(f"  {label}: 手動対応が必要 → {exc}")
            return False
        except PermanentError as exc:
            return self._fail(job, exc)

        self.queue.set_status(
            job.id,
            STATUS_POSTED,
            platform_post_id=result.platform_post_id,
            publish_id=result.publish_id,
            posted=True,
        )
        for note in result.notes:
            self._record(job.post_id, job.platform, "info", note)
        self._record(job.post_id, job.platform, "info",
                     f"投稿完了 ID: {result.platform_post_id or '(未取得)'}")
        self.log(f"  {label}: posted  ID: {result.platform_post_id or '(未取得)'}")
        return True

    def _fail(self, job, exc: Exception, transient: bool = False) -> bool:
        transient = transient or getattr(exc, "transient", False)
        next_attempt = None
        if transient:
            next_attempt = datetime.now(self.settings.tz) + backoff_delay(job.attempt_count)
        self.queue.set_status(
            job.id,
            STATUS_FAILED,
            error=str(exc)[:500],
            next_attempt_at=next_attempt,
        )
        self._record(job.post_id, job.platform, "error", str(exc))
        suffix = f"（{next_attempt:%H:%M} に再試行）" if next_attempt else "（再試行しません）"
        self.log(f"  {job.post_id}/{job.platform}: failed {suffix}\n     理由: {exc}")
        return False

    # ------------------------------------------------------------------
    def _ensure_uploaded(self, bundle: PostBundle) -> list[str]:
        """画像を1回だけアップロードし、TikTok/Instagram で同じURLを使う。"""
        prepared = prepare_post_images(bundle.post_id, bundle.images, self.settings.cache_dir)
        paths = [item.path for item in prepared]
        digest = content_hash(paths)

        cached = self.queue.get_assets(bundle.post_id, digest)
        if cached and len(cached) == len(paths):
            return cached

        host = self._image_host()
        self.log(f"  画像をアップロードしています（{host.name}）")
        urls = host.upload(bundle.post_id, paths, digest)
        self.queue.save_assets(bundle.post_id, digest, urls)
        return urls

    def _image_host(self):
        if self._host is None:
            self._host = get_host(self.settings)
        return self._host

    def _publisher(self, platform: str):
        if platform not in self._publishers:
            self._publishers[platform] = get_publisher(platform, self.settings, self.store)
        return self._publishers[platform]

    def _record(self, post_id: str, platform: str, level: str, message: str) -> None:
        self.queue.log(level, message, post_id=post_id, platform=platform)

    # ------------------------------------------------------------------
    def _apply_catchup(self, due: list, now: datetime) -> tuple[list, list]:
        """PCが止まっていた場合の一気投稿を防ぐ。"""
        grace = timedelta(minutes=self.settings.catchup_grace_minutes)
        overdue = [j for j in due if j.scheduled_at < now - grace]
        on_time = [j for j in due if j.scheduled_at >= now - grace]

        if not overdue:
            return on_time, []
        policy = self.settings.catchup_policy
        if policy == "all":
            return overdue + on_time, []
        if policy == "skip":
            return on_time, overdue

        # single: 期限切れのうち最も古い1投稿分だけ実行し、残りは再スケジュール
        oldest_post = overdue[0].post_id
        keep = [j for j in overdue if j.post_id == oldest_post]
        defer = [j for j in overdue if j.post_id != oldest_post]
        return keep + on_time, defer

    def _next_free_slot(self, original: datetime, now: datetime) -> datetime:
        """同じ時刻のまま、次に空いている日へずらす。"""
        candidate = original
        while candidate < now:
            candidate += timedelta(days=1)
        return candidate

    def _finalize_folders(self, report: RunReport) -> None:
        """両プラットフォーム成功した投稿を posted/ へ移動する（設定が有効な場合のみ）。"""
        if not self.settings.move_after_publish or not report.executed:
            return
        post_ids = {label.split("/")[0] for label in report.executed}
        for post_id in sorted(post_ids):
            jobs = self.queue.list_jobs(post_id=post_id)
            if not jobs or any(j.status != STATUS_POSTED for j in jobs):
                continue
            folder = Path(jobs[0].folder)
            if not folder.is_dir():
                continue
            destination = folder.parent.parent / "posted" / folder.name
            if destination.exists():
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(folder), str(destination))
            self.log(f"  {post_id}: フォルダを posted/ へ移動しました")
            self._record(post_id, "", "info", f"フォルダを {destination} へ移動")
