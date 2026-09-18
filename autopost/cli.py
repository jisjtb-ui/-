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
from .engine import ExperimentEngine, create_experiment
from .experiments import ExperimentStore, PUBLISHED
from .hosting import get_host
from .loader import LoaderError, load_post, load_posts
from .models import ALL_PLATFORMS, PLATFORMS
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
    connect.add_argument("platform", choices=ALL_PLATFORMS)
    connect.add_argument(
        "--manual",
        action="store_true",
        help="ローカルサーバを使わず、リダイレクト先URLを貼り付けて認証する（Pinterest向け）",
    )

    disconnect = sub.add_parser("disconnect", help="保存済みトークンを削除")
    disconnect.add_argument("platform", choices=ALL_PLATFORMS)

    pinterest = sub.add_parser("pinterest", help="Pinterestの補助コマンド")
    pin_sub = pinterest.add_subparsers(dest="pinterest_command", required=True)
    pin_sub.add_parser("boards", help="ボード一覧（PINTEREST_BOARD_ID の確認用）")
    test_pin = pin_sub.add_parser("test-pin", help="公開済み画像1枚でPinを作成して疎通確認する")
    test_pin.add_argument("--image-url", required=True, help="公開HTTPS URLの画像")
    test_pin.add_argument("--title", default="テスト投稿", help="Pinのタイトル（100文字まで）")
    test_pin.add_argument("--text", default="", help="Pinの説明（800文字まで）")
    test_pin.add_argument("--link", default="", help="Pinのリンク先")
    test_pin.add_argument("--board-id", default="", help="ボードID（未指定なら .env の値）")
    test_pin.add_argument("--category", default="test", help="実験カテゴリ")
    test_pin.add_argument("--hypothesis", default="疎通確認", help="検証したい仮説")

    tiktok = sub.add_parser("tiktok", help="TikTokの下書き転送")
    tk_sub = tiktok.add_subparsers(dest="tiktok_command", required=True)

    tk_enqueue = tk_sub.add_parser("enqueue", help="投稿フォルダを実験として登録し、画像URLを紐付ける")
    tk_enqueue.add_argument("--folder", default=DEFAULT_FOLDER)
    tk_enqueue.add_argument("--count", type=int, default=0, help="登録する件数（0で全件）")
    tk_enqueue.add_argument(
        "--base-url",
        default="",
        help="公開済みURLの先頭（例: https://honeshinri-media.pages.dev）。"
             "指定するとアップロードせずURLだけ組み立てる",
    )

    tk_drafts = tk_sub.add_parser("drafts", help="1日の上限まで下書きへ送る")
    tk_drafts.add_argument("--max", type=int, default=0, help="今回送る最大件数")
    tk_drafts.add_argument("--dry-run", action="store_true")

    tk_sub.add_parser("queue", help="下書きキューの残件と完了見込み")

    tk_export = tk_sub.add_parser(
        "export-media", help="Cloudflare Pages へ配信する形で画像を書き出す"
    )
    tk_export.add_argument("--dest", default="pages_media", help="書き出し先フォルダ")

    sns = sub.add_parser("sns", help="Threads / Instagram の予約投稿")
    sns_sub = sns.add_subparsers(dest="sns_command", required=True)

    sns_enqueue = sns_sub.add_parser("enqueue", help="投稿フォルダを実験として登録する")
    sns_enqueue.add_argument("--folder", default=DEFAULT_FOLDER)
    sns_enqueue.add_argument("--count", type=int, default=0)
    sns_enqueue.add_argument("--base-url", default="", help="公開済みURLの先頭")
    sns_enqueue.add_argument("--platforms", default="threads,instagram")

    sns_schedule = sns_sub.add_parser("schedule", help="予約時刻を割り当てる")
    sns_schedule.add_argument("--start", default="", help="開始日 例: 2026-09-20（既定は今日）")
    sns_schedule.add_argument("--times", default="21:00", help="投稿時刻をカンマ区切りで（例 09:00,21:00）")
    sns_schedule.add_argument("--per-day", type=int, default=0, help="1日の件数（既定は時刻の数）")
    sns_schedule.add_argument("--count", type=int, default=0)
    sns_schedule.add_argument("--platforms", default="threads,instagram")
    sns_schedule.add_argument(
        "--reschedule",
        action="store_true",
        help="既存の予約も含めて開始日から振り直す（既定は未予約分だけ追加）",
    )

    sns_run = sns_sub.add_parser("run", help="予約時刻を過ぎた分を配信する")
    sns_run.add_argument("--platforms", default="threads,instagram")
    sns_run.add_argument("--dry-run", action="store_true")

    sns_topup = sns_sub.add_parser(
        "topup", help="配信待ちが減ったら新しいコンテンツを作って予約する"
    )
    sns_topup.add_argument("--min", type=int, default=0, help="この件数を下回ったら補充する")
    sns_topup.add_argument("--count", type=int, default=0, help="1回に生成する件数")
    sns_topup.add_argument("--base-url", default="", help="画像の公開URLの先頭")
    sns_topup.add_argument("--times", default="21:00", help="予約時刻をカンマ区切りで")
    sns_topup.add_argument("--platforms", default="threads,instagram")
    sns_topup.add_argument("--force", action="store_true", help="下限に関係なく補充する")

    sns_queue = sns_sub.add_parser("queue", help="予約状況を表示する")
    sns_queue.add_argument("--platforms", default="threads,instagram")

    experiment = sub.add_parser("experiment", help="コンテンツ実験の管理")
    exp_sub = experiment.add_subparsers(dest="experiment_command", required=True)

    exp_new = exp_sub.add_parser("new", help="実験を登録する")
    exp_new.add_argument("--hypothesis", default="", help="検証したい仮説")
    exp_new.add_argument("--category", default="", help="カテゴリ（例: 恋愛/共感）")
    exp_new.add_argument("--hook", default="", help="Hook（1行目・つかみ）")
    exp_new.add_argument("--text", default="", help="本文")
    exp_new.add_argument("--image-url", default="", help="公開済み画像のURL")
    exp_new.add_argument("--image-prompt", default="", help="画像生成に使った指示")
    exp_new.add_argument("--link", default="", help="リンク先URL")
    exp_new.add_argument("--tags", default="", help="カンマ区切りのタグ")
    exp_new.add_argument("--from-post", default="", help="既存の投稿フォルダから文言を取り込む")
    exp_new.add_argument("--board-id", default="", help="Pinterestのボードを個別指定する")
    exp_new.add_argument("--platforms", default="pinterest", help="配信先（カンマ区切り）")

    exp_list = exp_sub.add_parser("list", help="実験一覧")
    exp_list.add_argument("--limit", type=int, default=20)

    exp_show = exp_sub.add_parser("show", help="実験の詳細とログ")
    exp_show.add_argument("experiment_id")

    exp_run = exp_sub.add_parser("run", help="公開待ちの実験を配信する")
    exp_run.add_argument("--platform", choices=ALL_PLATFORMS)
    exp_run.add_argument("--limit", type=int, default=0, help="処理する件数（0で全件）")

    exp_retry = exp_sub.add_parser("retry", help="失敗した配信を再試行できる状態に戻す")
    exp_retry.add_argument("--experiment")
    exp_retry.add_argument("--platform", choices=ALL_PLATFORMS)

    exp_collect = exp_sub.add_parser("collect", help="反応データを取得して保存する")
    exp_collect.add_argument("--experiment")
    exp_collect.add_argument("--platform", choices=ALL_PLATFORMS)
    exp_collect.add_argument(
        "--due",
        action="store_true",
        help="取得時期（1h/6h/24h/72h）が来たものだけを取得する。定期実行向け",
    )
    exp_collect.add_argument("--start-date", default="", help="YYYY-MM-DD")
    exp_collect.add_argument("--end-date", default="", help="YYYY-MM-DD")

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

    sub.add_parser("version", help="バージョンを表示")

    update_parser = sub.add_parser("update", help="ソフト本体の更新")
    update_parser.add_argument(
        "--apply", action="store_true", help="確認だけでなく実際に更新する",
    )
    update_parser.add_argument(
        "--yes", action="store_true", help="確認を求めずに更新する",
    )
    update_parser.add_argument("--branch", default="", help="更新元のブランチ")
    update_parser.add_argument("--repo", default="", help="更新元のリポジトリ")
    update_parser.add_argument(
        "--rollback", metavar="バックアップ名",
        help="1つ前へ戻す（backups/ の名前。list で一覧）",
    )

    sub.add_parser("migrate", help="DBのスキーマ更新（バックアップしてから実行）")

    doctor_parser = sub.add_parser(
        "doctor", help="不具合の切り分け（秘密情報を含まない診断レポートを出す）"
    )
    doctor_parser.add_argument(
        "--offline", action="store_true",
        help="APIへの疎通確認を行わず、設定とキューだけを見る",
    )
    doctor_parser.add_argument(
        "--out", default="診断結果.txt", help="レポートの保存先",
    )
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
        "pinterest": cmd_pinterest,
        "tiktok": cmd_tiktok,
        "sns": cmd_sns,
        "experiment": cmd_experiment,
        "doctor": cmd_doctor,
        "version": cmd_version,
        "update": cmd_update,
        "migrate": cmd_migrate,
    }
    return handlers[args.command](args, settings, queue)


# ----------------------------------------------------------------------
def cmd_version(args, settings: Settings, queue: Queue) -> int:
    from .version import changelog_for, current_version

    version = current_version()
    print(f"本音心理テスト 自動投稿  v{version}")
    notes = changelog_for(version)
    if notes:
        print("\nこのバージョンの変更内容:")
        for note in notes:
            print(f"  ・{note}")
    return 0


# ----------------------------------------------------------------------
def cmd_update(args, settings: Settings, queue: Queue) -> int:
    from . import migrations, updater

    if args.rollback:
        if args.rollback == "list":
            found = updater.backups()
            if not found:
                print("バックアップはありません")
                return 0
            print("戻せるバックアップ:")
            for path in found:
                print(f"  {path.name}")
            return 0
        target = updater.BACKUP_DIR / args.rollback
        try:
            updater.rollback(target)
        except updater.UpdateError as exc:
            print(f"[エラー] {exc}")
            return 1
        print("戻しました。アプリを起動し直してください。")
        return 0

    source = updater.UpdateSource(repo=args.repo, branch=args.branch)
    print(f"更新元: {source.describe()}")
    try:
        info = updater.check(source)
    except updater.UpdateError as exc:
        print(f"[エラー] {exc}")
        return 1

    print(f"現在のバージョン: v{info.current}")
    print(f"配布元の最新版  : v{info.latest}")
    print(info.message)

    if not info.available:
        return 0
    if info.notes:
        print("\n新しいバージョンの変更内容:")
        for note in info.notes:
            print(f"  ・{note}")

    if not args.apply:
        print("\n更新するには --apply を付けてください。")
        return 0

    if not args.yes:
        answer = input("\n更新しますか？ [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            print("中止しました。何も変更していません。")
            return 0

    print("")
    try:
        backup = updater.apply_update(info, source)
    except updater.UpdateError as exc:
        print(f"\n[エラー] {exc}")
        print("現在のバージョンはそのまま使えます。")
        return 1

    try:
        migrations.migrate(settings)
    except migrations.MigrationError as exc:
        print(f"\n[エラー] DBの更新に失敗しました: {exc}")
        print(f"アプリを戻すには: python autopost.py update --rollback {backup.name}")
        return 1

    print(f"\nv{info.latest} へ更新しました。")
    print("アプリを起動し直してください（開いている画面は古いままです）。")
    return 0


# ----------------------------------------------------------------------
def cmd_migrate(args, settings: Settings, queue: Queue) -> int:
    from . import migrations

    try:
        migrations.migrate(settings)
    except migrations.MigrationError as exc:
        print(f"[エラー] {exc}")
        return 1
    return 0


# ----------------------------------------------------------------------
def cmd_doctor(args, settings: Settings, queue: Queue) -> int:
    from . import doctor

    return doctor.run(settings, live=not args.offline, output=Path(args.out))


# ----------------------------------------------------------------------
def cmd_status(args, settings: Settings, queue: Queue) -> int:
    store = TokenStore(settings.token_dir)
    print("=== 接続状況 ===")
    for platform in ALL_PLATFORMS:
        missing = settings.missing(platform)
        state = store.status(platform)
        if missing:
            state = f"未設定（.env: {', '.join(missing)}）"
        print(f"  {platform:<10} {state}")
        token = store.load(platform)
        if token and token.account_id:
            name = f" / {token.account_name}" if token.account_name else ""
            print(f"             ID: {token.account_id}{name}")

    host = get_host(settings)
    print(f"\n=== 画像ホスティング ===\n  {host.describe()}")

    print(f"\n=== 投稿設定 ===")
    print(f"  タイムゾーン       : {settings.timezone}")
    print(f"  TikTok投稿方式     : {settings.tiktok_mode}"
          "（direct_post → upload → queue_only の順にフォールバック）")
    print(f"  TikTok公開範囲     : {settings.tiktok_privacy_level}")
    board = settings.pinterest_board_id or "未設定（pinterest boards で確認）"
    print(f"  Pinterestボード    : {board}"
          + ("  ※サンドボックス" if settings.pinterest_sandbox else ""))
    print(f"  TikTok音楽自動付与 : {settings.tiktok_auto_add_music}")
    print(f"  Instagram音楽      : APIでは設定不可（not_supported）")
    print(f"  期限切れ時の挙動   : {settings.catchup_policy}（猶予{settings.catchup_grace_minutes}分）")
    print(f"  投稿後のフォルダ移動: {'あり' if settings.move_after_publish else 'なし（SQLiteのみ更新）'}")

    experiments = ExperimentStore(settings.experiments_db_path)
    exp_counts = experiments.status_counts()
    print("\n=== 実験キュー ===")
    if not exp_counts:
        print("  （実験なし）")
    for status, count in sorted(exp_counts.items()):
        print(f"  {status:<18} {count}件")

    counts = queue.counts()
    print("\n=== 投稿フォルダのキュー ===")
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
        elif args.platform == "threads":
            from .oauth import threads_oauth

            token = threads_oauth.connect(settings, store, manual=args.manual)
        elif args.platform == "pinterest":
            from .oauth import pinterest_oauth

            token = pinterest_oauth.connect(settings, store, manual=args.manual)
        else:
            from .oauth import meta_oauth

            token = meta_oauth.connect(settings, store)
    except Exception as exc:
        print(f"[エラー] 認証に失敗しました: {exc}", file=sys.stderr)
        return 1
    print(f"{args.platform} に接続しました: {token.masked()}")
    if token.account_name:
        print(f"  アカウント : {token.account_name}")
    if token.account_id:
        label = {"threads": "THREADS_USER_ID", "instagram": "INSTAGRAM_ACCOUNT_ID"}.get(
            args.platform, "アカウントID"
        )
        print(f"  {label} : {token.account_id}")
        print("    ※ .env に書かなくても自動で使われます。固定したい場合だけ記入してください")
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


# ----------------------------------------------------------------------
# Pinterest
# ----------------------------------------------------------------------
def cmd_pinterest(args, settings: Settings, queue: Queue) -> int:
    from .publishers import PublishError, get_publisher

    store = TokenStore(settings.token_dir)
    try:
        publisher = get_publisher("pinterest", settings, store)
    except Exception as exc:
        print(f"[エラー] {exc}", file=sys.stderr)
        return 1

    if args.pinterest_command == "boards":
        try:
            boards = publisher.list_boards()
        except PublishError as exc:
            print(f"[エラー] ボードを取得できません: {exc}", file=sys.stderr)
            return 1
        if not boards:
            print("ボードがありません。Pinterestでボードを1つ作成してください")
            return 1
        print("ボード一覧（PINTEREST_BOARD_ID に設定する値）:")
        for board in boards:
            print(f"  {board['id']:<20} {board['name']}  [{board['privacy']}]")
        return 0

    # test-pin: 公開済み画像1枚で実験を作り、そのまま配信する
    experiments = ExperimentStore(settings.experiments_db_path)
    extra = {"board_id": args.board_id} if args.board_id else {}
    experiment = create_experiment(
        experiments,
        ["pinterest"],
        hypothesis=args.hypothesis,
        content_category=args.category,
        hook=args.title,
        text=args.text,
        image_url=args.image_url,
        link=args.link,
        extra=extra,
    )
    print(f"実験を登録しました: {experiment.experiment_id}")

    engine = ExperimentEngine(settings, experiments, log=print)
    publication = experiments.publication(experiment.experiment_id, "pinterest")
    ok = engine.publish(publication)
    _print_experiment(experiments, experiment.experiment_id)
    return 0 if ok else 1


# ----------------------------------------------------------------------
# 実験
# ----------------------------------------------------------------------
def cmd_experiment(args, settings: Settings, queue: Queue) -> int:
    experiments = ExperimentStore(settings.experiments_db_path)
    command = args.experiment_command

    if command == "new":
        return _experiment_new(args, settings, experiments)

    if command == "list":
        rows = experiments.list_experiments(args.limit)
        if not rows:
            print("実験はまだありません")
            return 0
        for experiment in rows:
            publications = experiments.publications(experiment.experiment_id)
            states = " ".join(f"{p.platform}:{p.status}" for p in publications)
            print(f"{experiment.experiment_id}  {experiment.content_category or '-':<12} {states}")
            print(f"    Hook: {experiment.hook or '（未設定）'}")
        print(f"\n{len(rows)}件")
        return 0

    if command == "show":
        return _print_experiment(experiments, args.experiment_id)

    if command == "run":
        engine = ExperimentEngine(settings, experiments, log=print)
        report = engine.run_pending(args.platform, args.limit or None)
        print(report.summary())
        for label in report.manual:
            print(f"  手動対応: {label}")
        return 0 if not report.failed else 1

    if command == "retry":
        count = experiments.reset_failed(args.experiment, args.platform)
        print(f"{count} 件を再試行できる状態に戻しました")
        return 0

    if command == "collect":
        from .collector import AnalyticsCollector

        collector = AnalyticsCollector(settings, experiments, log=print)
        if args.due:
            report = collector.collect_due(args.platform)
        else:
            report = collector.collect_now(args.experiment, args.platform)
        print(report.summary())
        return 0

    print(f"[エラー] 不明なサブコマンド: {command}", file=sys.stderr)
    return 1


def _experiment_new(args, settings: Settings, experiments: ExperimentStore) -> int:
    platforms = [p.strip() for p in args.platforms.split(",") if p.strip()]
    unknown = [p for p in platforms if p not in ALL_PLATFORMS]
    if unknown:
        print(f"[エラー] 不明な配信先: {', '.join(unknown)}", file=sys.stderr)
        return 1

    hook, text, tags = args.hook, args.text, _split_tags(args.tags)
    source_post_id = source_folder = ""

    if args.from_post:
        try:
            bundle = load_post(Path(args.from_post))
        except LoaderError as exc:
            print(f"[エラー] {exc}", file=sys.stderr)
            return 1
        source_post_id, source_folder = bundle.post_id, str(bundle.folder)
        hook = hook or bundle.title
        text = text or bundle.caption
        tags = tags or bundle.hashtags

    extra = {"board_id": args.board_id} if args.board_id else {}
    experiment = create_experiment(
        experiments,
        platforms,
        hypothesis=args.hypothesis,
        content_category=args.category,
        hook=hook,
        text=text,
        image_prompt=args.image_prompt,
        image_url=args.image_url,
        link=args.link,
        source_post_id=source_post_id,
        source_folder=source_folder,
        tags=tags,
        extra=extra,
    )
    print(f"{experiment.experiment_id} を登録しました（配信先: {', '.join(platforms)}）")
    if not args.image_url:
        print("  画像URLが未設定のため status=generated です。"
              "`experiment new --image-url ...` で指定するか、公開後に再登録してください")
    return 0


def _print_experiment(experiments: ExperimentStore, experiment_id: str) -> int:
    experiment = experiments.get(experiment_id)
    if experiment is None:
        print(f"[エラー] 実験が見つかりません: {experiment_id}", file=sys.stderr)
        return 1

    print(f"=== {experiment.experiment_id} ===")
    print(f"  仮説        : {experiment.hypothesis or '-'}")
    print(f"  カテゴリ    : {experiment.content_category or '-'}")
    print(f"  Hook        : {experiment.hook or '-'}")
    print(f"  本文        : {(experiment.text or '-')[:120]}")
    print(f"  画像URL     : {experiment.image_url or '-'}")
    print(f"  リンク      : {experiment.link or '-'}")
    print(f"  作成        : {experiment.created_at}")

    print("\n  --- 配信状況 ---")
    for publication in experiments.publications(experiment_id):
        print(f"  {publication.platform:<10} {publication.status:<16}"
              f" 試行{publication.retry_count}回")
        if publication.external_post_id:
            print(f"      ID: {publication.external_post_id}  {publication.external_url or ''}")
        if publication.error_message:
            print(f"      理由: {publication.error_message[:160]}")

    metrics = experiments.latest_metrics(experiment_id)
    if metrics:
        print("\n  --- 反応データ ---")
        for item in metrics[:5]:
            print(f"  {item.collected_at[:16]}  {item.platform}: {item.summary()}")

    print("\n  --- ログ ---")
    for event in experiments.events(experiment_id):
        target = f"/{event['platform']}" if event["platform"] else ""
        print(f"  {event['created_at'][11:19]}  [{experiment_id}{target}] {event['message']}")
    return 0


def _split_tags(raw: str) -> list[str]:
    return [t.strip() for t in (raw or "").split(",") if t.strip()]


# ----------------------------------------------------------------------
# TikTok（下書き転送）
# ----------------------------------------------------------------------
def cmd_tiktok(args, settings: Settings, queue: Queue) -> int:
    from .tiktok_drafts import enqueue_folder, queue_overview, send_drafts

    experiments = ExperimentStore(settings.experiments_db_path)

    if args.tiktok_command == "enqueue":
        try:
            report = enqueue_folder(
                settings, experiments, Path(args.folder),
                count=args.count, base_url=args.base_url, log=print,
            )
        except LoaderError as exc:
            print(f"[エラー] {exc}", file=sys.stderr)
            return 1
        for line in report.created[:5]:
            print(f"  {line}")
        if len(report.created) > 5:
            print(f"  … 他 {len(report.created) - 5} 件")
        print(report.summary())
        overview = queue_overview(experiments, settings)
        print(f"\n下書き待ち {overview['queued']}件 /"
              f" 1日{overview['daily_limit']}件ずつで約{overview['days_needed']}日")
        return 0 if not report.failed else 1

    if args.tiktok_command == "queue":
        overview = queue_overview(experiments, settings)
        print(f"下書き待ち     : {overview['queued']}件")
        print(f"転送済み       : {overview['delivered']}件")
        print(f"本日の送信     : {overview['today']}/{overview['daily_limit']}件")
        print(f"完了見込み     : 約{overview['days_needed']}日"
              "（TikTokの制限: 保留中の共有は24時間あたり5件）")
        return 0

    if args.tiktok_command == "export-media":
        from .tiktok_drafts import export_media

        result = export_media(settings, experiments, Path(args.dest), log=print)
        print(f"\nこのフォルダをそのまま Cloudflare Pages へデプロイしてください:")
        print(f"  {result['destination']}")
        print("  例: npx wrangler pages deploy "
              f"{result['destination']} --project-name honeshinri-media")
        return 0

    # drafts
    if settings.tiktok_mode == "queue_only":
        print("[注意] TIKTOK_MODE=queue_only のため送信しません。"
              ".env を upload に変更してください", file=sys.stderr)
        return 1
    result = send_drafts(settings, experiments, args.max, args.dry_run, log=print)
    print(f"\n送信 {result['sent']}件 / 本日の残り枠 {result['remaining_today']}件"
          f" / キュー残り {result['queued']}件")
    return 0


# ----------------------------------------------------------------------
# Threads / Instagram（予約投稿）
# ----------------------------------------------------------------------
def cmd_sns(args, settings: Settings, queue: Queue) -> int:
    from datetime import datetime, time as dtime
    from .queueing import enqueue_folder, overview, publish_due, schedule_publications

    experiments = ExperimentStore(settings.experiments_db_path)
    platforms = _parse_sns_platforms(args.platforms)

    if args.sns_command == "enqueue":
        try:
            report = enqueue_folder(
                settings, experiments, Path(args.folder), platforms,
                count=args.count, base_url=args.base_url, log=print,
            )
        except LoaderError as exc:
            print(f"[エラー] {exc}", file=sys.stderr)
            return 1
        for line in report.created[:5]:
            print(f"  {line}")
        if len(report.created) > 5:
            print(f"  … 他 {len(report.created) - 5} 件")
        print(report.summary())
        return 0 if not report.failed else 1

    if args.sns_command == "schedule":
        try:
            start = (
                datetime.strptime(args.start, "%Y-%m-%d")
                if args.start else datetime.now()
            )
            times = []
            for raw in args.times.split(","):
                hour, _, minute = raw.strip().partition(":")
                times.append(dtime(int(hour), int(minute or 0)))
        except ValueError:
            print("[エラー] --start は 2026-09-20、--times は 09:00,21:00 の形式です",
                  file=sys.stderr)
            return 1
        result = schedule_publications(
            settings, experiments, platforms, start, times,
            per_day=args.per_day, count=args.count,
            reschedule=args.reschedule, log=print,
        )
        print(f"{result['assigned']} 件に予約時刻を割り当てました")
        return 0

    if args.sns_command == "topup":
        from .queueing import topup

        times = []
        for raw in args.times.split(","):
            hour, _, minute = raw.strip().partition(":")
            try:
                times.append(dtime(int(hour), int(minute or 0)))
            except ValueError:
                pass
        result = topup(
            settings, experiments, platforms,
            minimum=args.min, generate_count=args.count,
            base_url=args.base_url, times=times, force=args.force, log=print,
        )
        if result.get("error"):
            return 1
        print(f"\n補充 {result['generated']}件")
        return 0

    if args.sns_command == "run":
        result = publish_due(settings, experiments, platforms, args.dry_run, log=print)
        print()
        for platform, info in result.items():
            state = "未接続" if info.get("error") else f"配信 {info['sent']}件"
            print(f"  {platform:<10} {state}")
        return 0

    # queue
    for platform, info in overview(experiments, settings, platforms).items():
        print(f"[{platform}]")
        print(f"  配信待ち   : {info['queued']}件")
        print(f"  配信済み   : {info['delivered']}件")
        print(f"  本日の配信 : {info['today']}/{info['daily_limit']}件")
        if info["next_at"]:
            print(f"  次の予約   : {info['next_at'][:16].replace('T', ' ')}")
        print(f"  完了見込み : 約{info['days_needed']}日")
    return 0


def _parse_sns_platforms(raw: str) -> list[str]:
    values = [p.strip() for p in raw.split(",") if p.strip()]
    invalid = [p for p in values if p not in ALL_PLATFORMS]
    if invalid:
        raise SystemExit(f"[エラー] 不明なプラットフォーム: {', '.join(invalid)}")
    return values or ["threads", "instagram"]


# `python -m autopost.cli` でも動くようにする（通常の入口は autopost.py）
if __name__ == "__main__":
    raise SystemExit(main())
