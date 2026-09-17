"""自動投稿システムのCLI。

  python autopost.py status
  python autopost.py connect tiktok
  python autopost.py validate --folder output
  python autopost.py schedule --folder output --start 2026-09-20 --time 21:00 --count 100
  python autopost.py queue
  python autopost.py run
  python autopost.py watch
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, time as dtime
from pathlib import Path

from .config import Settings
from .db import Queue, STATUS_POSTED
from .hosting import get_host
from .loader import LoaderError, load_posts
from .models import PLATFORMS
from .oauth.store import TokenStore
from .scheduler import LOCK_NAME, Runner, SingleInstance, bulk_schedule
from .validate import ERROR, summarize, validate_post

DEFAULT_FOLDER = "output"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="autopost",
        description="生成済みの投稿フォルダをTikTok / Instagramへ予約投稿する",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="接続状況・設定・キューの概況を表示")

    connect = sub.add_parser("connect", help="OAuth認証（ブラウザが開きます）")
    connect.add_argument("platform", choices=PLATFORMS)

    disconnect = sub.add_parser("disconnect", help="保存済みトークンを削除")
    disconnect.add_argument("platform", choices=PLATFORMS)

    validate = sub.add_parser("validate", help="投稿前の機械的検証")
    validate.add_argument("--folder", default=DEFAULT_FOLDER, help="投稿フォルダ（既定: output）")
    validate.add_argument("--platforms", default=",".join(PLATFORMS))
    validate.add_argument("--skip-images", action="store_true", help="画像変換チェックを省略")

    schedule = sub.add_parser("schedule", help="一括予約")
    schedule.add_argument("--folder", default=DEFAULT_FOLDER)
    schedule.add_argument("--start", required=True, help="開始日 例: 2026-09-20")
    schedule.add_argument("--time", default="21:00", help="投稿時刻 例: 21:00")
    schedule.add_argument("--interval", type=float, default=1.0, help="投稿間隔（日）")
    schedule.add_argument("--count", type=int, default=0, help="予約する投稿数（0で全件）")
    schedule.add_argument("--start-index", type=int, default=0, help="何番目のpostから予約するか")
    schedule.add_argument("--platforms", default=",".join(PLATFORMS))
    schedule.add_argument("--replace", action="store_true", help="既存の予約を上書き（投稿済みは除く）")

    queue_cmd = sub.add_parser("queue", help="予約一覧")
    queue_cmd.add_argument("--status", help="状態で絞り込み")
    queue_cmd.add_argument("--post", help="post_id で絞り込み")
    queue_cmd.add_argument("--limit", type=int, default=50)

    run = sub.add_parser("run", help="予約時刻を過ぎたジョブを投稿")
    run.add_argument("--dry-run", action="store_true", help="実際には投稿しない")

    watch = sub.add_parser("watch", help="常駐して定期的に投稿（Ctrl+Cで終了）")
    watch.add_argument("--interval", type=int, default=60, help="チェック間隔（秒）")

    retry = sub.add_parser("retry", help="失敗したジョブを再試行できる状態に戻す")
    retry.add_argument("--post")
    retry.add_argument("--platform", choices=PLATFORMS)

    logs = sub.add_parser("logs", help="最近のログ")
    logs.add_argument("--limit", type=int, default=50)
    return parser


# ----------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = Settings.load()
    queue = Queue(settings.db_path)

    handlers = {
        "status": cmd_status,
        "connect": cmd_connect,
        "disconnect": cmd_disconnect,
        "validate": cmd_validate,
        "schedule": cmd_schedule,
        "queue": cmd_queue,
        "run": cmd_run,
        "watch": cmd_watch,
        "retry": cmd_retry,
        "logs": cmd_logs,
    }
    return handlers[args.command](args, settings, queue)


# ----------------------------------------------------------------------
def cmd_status(args, settings: Settings, queue: Queue) -> int:
    store = TokenStore(settings.token_dir)
    print("=== 接続状況 ===")
    for platform in PLATFORMS:
        missing = settings.missing(platform)
        state = store.status(platform)
        if missing:
            state = f"未設定（.env: {', '.join(missing)}）"
        print(f"  {platform:<10} {state}")

    host = get_host(settings)
    print(f"\n=== 画像ホスティング ===\n  {host.describe()}")

    print(f"\n=== 投稿設定 ===")
    print(f"  タイムゾーン       : {settings.timezone}")
    print(f"  TikTok公開範囲     : {settings.tiktok_privacy_level}")
    print(f"  TikTok音楽自動付与 : {settings.tiktok_auto_add_music}")
    print(f"  Instagram音楽      : APIでは設定不可（not_supported）")
    print(f"  期限切れ時の挙動   : {settings.catchup_policy}（猶予{settings.catchup_grace_minutes}分）")
    print(f"  投稿後のフォルダ移動: {'あり' if settings.move_after_publish else 'なし（SQLiteのみ更新）'}")

    counts = queue.counts()
    print("\n=== キュー ===")
    if not counts:
        print("  （予約なし）")
    for status, count in sorted(counts.items()):
        print(f"  {status:<16} {count}件")
    return 0


def cmd_connect(args, settings: Settings, queue: Queue) -> int:
    store = TokenStore(settings.token_dir)
    try:
        if args.platform == "tiktok":
            from .oauth import tiktok_oauth

            token = tiktok_oauth.connect(settings, store)
        else:
            from .oauth import meta_oauth

            token = meta_oauth.connect(settings, store)
    except Exception as exc:
        print(f"[エラー] 認証に失敗しました: {exc}", file=sys.stderr)
        return 1
    print(f"{args.platform} に接続しました: {token.masked()}")
    return 0


def cmd_disconnect(args, settings: Settings, queue: Queue) -> int:
    TokenStore(settings.token_dir).delete(args.platform)
    print(f"{args.platform} のトークンを削除しました")
    return 0


def cmd_validate(args, settings: Settings, queue: Queue) -> int:
    platforms = _parse_platforms(args.platforms)
    try:
        bundles = load_posts(Path(args.folder))
    except LoaderError as exc:
        print(f"[エラー] {exc}", file=sys.stderr)
        return 1

    total_errors = 0
    for bundle in bundles:
        issues = validate_post(
            bundle, settings, platforms, queue=queue, check_images=not args.skip_images
        )
        errors, warns = summarize(issues)
        total_errors += errors
        mark = "OK" if errors == 0 else "NG"
        print(f"[{mark}] {bundle.post_id}  画像{bundle.image_count}枚  "
              f"タグ{len(bundle.hashtags)}個  エラー{errors} 注意{warns}")
        for issue in issues:
            print("     " + str(issue))
    print(f"\n{len(bundles)}投稿を検証しました / エラー合計: {total_errors}件")
    return 0 if total_errors == 0 else 1


def cmd_schedule(args, settings: Settings, queue: Queue) -> int:
    platforms = _parse_platforms(args.platforms)
    try:
        start = datetime.strptime(args.start, "%Y-%m-%d")
        hour, minute = (int(x) for x in args.time.split(":"))
    except ValueError:
        print("[エラー] --start は 2026-09-20、--time は 21:00 の形式で指定してください", file=sys.stderr)
        return 1

    try:
        results = bulk_schedule(
            queue, settings, Path(args.folder), start, dtime(hour, minute),
            args.interval, args.count, platforms, replace=args.replace,
            start_index=args.start_index,
        )
    except LoaderError as exc:
        print(f"[エラー] {exc}", file=sys.stderr)
        return 1

    created = sum(1 for _, _, action in results if action == "created")
    updated = sum(1 for _, _, action in results if action == "updated")
    kept = sum(1 for _, _, action in results if action == "kept")
    for label, when, action in results[:10]:
        print(f"  {label:<26} {when:%Y-%m-%d %H:%M}  {action}")
    if len(results) > 10:
        print(f"  … 他 {len(results) - 10} 件")
    print(f"\n新規 {created} / 更新 {updated} / 変更なし {kept}")
    if kept:
        print("  ※ 変更なしには「投稿済み」と「既存予約（--replace で上書き可）」が含まれます")
    return 0


def cmd_queue(args, settings: Settings, queue: Queue) -> int:
    jobs = queue.list_jobs(post_id=args.post, status=args.status)
    if not jobs:
        print("予約はありません")
        return 0

    grouped: dict[str, dict] = {}
    for job in jobs:
        entry = grouped.setdefault(job.post_id, {"when": job.scheduled_at, "platforms": {}})
        entry["platforms"][job.platform] = job
        entry["when"] = min(entry["when"], job.scheduled_at)

    for post_id, entry in list(grouped.items())[: args.limit]:
        print(f"{post_id}  {entry['when']:%Y-%m-%d %H:%M}")
        for platform, job in sorted(entry["platforms"].items()):
            extra = ""
            if job.status == STATUS_POSTED and job.platform_post_id:
                extra = f"  ID: {job.platform_post_id}"
            elif job.last_error:
                extra = f"  理由: {job.last_error[:60]}"
            print(f"    {platform:<10} {job.status:<16} 試行{job.attempt_count}回{extra}")
    print(f"\n合計 {len(jobs)} ジョブ / {len(grouped)} 投稿")
    return 0


def cmd_run(args, settings: Settings, queue: Queue) -> int:
    try:
        with SingleInstance(settings.cache_dir / LOCK_NAME):
            runner = Runner(settings, queue)
            report = runner.run_due(dry_run=args.dry_run)
    except RuntimeError as exc:
        print(f"[エラー] {exc}", file=sys.stderr)
        return 1
    print(report.summary())
    for label in report.rescheduled:
        print(f"  再スケジュール: {label}")
    return 0 if not report.failed else 1


def cmd_watch(args, settings: Settings, queue: Queue) -> int:
    print(f"常駐モードを開始しました（{args.interval}秒ごとに確認 / Ctrl+Cで終了）")
    try:
        with SingleInstance(settings.cache_dir / LOCK_NAME):
            runner = Runner(settings, queue)
            while True:
                report = runner.run_due()
                if report.executed or report.failed or report.rescheduled:
                    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {report.summary()}")
                time.sleep(max(10, args.interval))
    except KeyboardInterrupt:
        print("\n終了しました")
        return 0
    except RuntimeError as exc:
        print(f"[エラー] {exc}", file=sys.stderr)
        return 1


def cmd_retry(args, settings: Settings, queue: Queue) -> int:
    count = queue.reset_failed(post_id=args.post, platform=args.platform)
    print(f"{count} 件を再試行対象に戻しました")
    return 0


def cmd_logs(args, settings: Settings, queue: Queue) -> int:
    rows = queue.recent_logs(args.limit)
    for row in rows:
        target = f"{row['post_id']}/{row['platform']}" if row["post_id"] else "-"
        print(f"{row['created_at'][11:19]}  {row['level']:<5} {target:<24} {row['message']}")
    if not rows:
        print("ログはありません")
    return 0


def _parse_platforms(raw: str) -> tuple[str, ...]:
    values = tuple(p.strip() for p in raw.split(",") if p.strip())
    invalid = [p for p in values if p not in PLATFORMS]
    if invalid:
        raise SystemExit(f"[エラー] 不明なプラットフォーム: {', '.join(invalid)}")
    return values or PLATFORMS
