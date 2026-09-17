"""投稿キュー（SQLite）。

投稿状態の唯一の正本。ファイル移動やPC再起動に影響されないよう、
「どの post を どのプラットフォームへ いつ投稿したか」はすべてここで管理する。

post_id + platform は UNIQUE。posted になった組み合わせは通常実行では二度と投稿しない。
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

STATUS_READY = "ready"
STATUS_SCHEDULED = "scheduled"
STATUS_UPLOADING = "uploading"
STATUS_PUBLISHING = "publishing"
STATUS_POSTED = "posted"
STATUS_FAILED = "failed"
STATUS_MANUAL = "manual_required"
STATUS_SKIPPED = "skipped"

# 実行中とみなす状態（クラッシュ復帰時に拾い直す）
IN_FLIGHT = (STATUS_UPLOADING, STATUS_PUBLISHING)
# 投稿対象にできる状態
CLAIMABLE = (STATUS_READY, STATUS_SCHEDULED, STATUS_FAILED)
RUNNABLE = CLAIMABLE + IN_FLIGHT
# 実行中のまま放置されたジョブを「落ちた」とみなすまでの時間（分）
STALE_MINUTES = 15

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id           TEXT NOT NULL,
    platform          TEXT NOT NULL,
    folder            TEXT NOT NULL,
    scheduled_at      TEXT NOT NULL,
    status            TEXT NOT NULL,
    attempt_count     INTEGER NOT NULL DEFAULT 0,
    platform_post_id  TEXT,
    publish_id        TEXT,
    posted_at         TEXT,
    last_error        TEXT,
    next_attempt_at   TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    UNIQUE(post_id, platform)
);
CREATE INDEX IF NOT EXISTS idx_jobs_due ON jobs(status, scheduled_at);

CREATE TABLE IF NOT EXISTS assets (
    post_id       TEXT PRIMARY KEY,
    content_hash  TEXT NOT NULL,
    urls          TEXT NOT NULL,
    uploaded_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS logs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id    TEXT,
    platform   TEXT,
    level      TEXT NOT NULL,
    message    TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_logs_created ON logs(created_at);
"""


@dataclass
class Job:
    """1件の投稿タスク（post_id × platform）。"""

    id: int
    post_id: str
    platform: str
    folder: str
    scheduled_at: datetime
    status: str
    attempt_count: int
    platform_post_id: str | None
    publish_id: str | None
    posted_at: str | None
    last_error: str | None
    next_attempt_at: datetime | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Job":
        return cls(
            id=row["id"],
            post_id=row["post_id"],
            platform=row["platform"],
            folder=row["folder"],
            scheduled_at=datetime.fromisoformat(row["scheduled_at"]),
            status=row["status"],
            attempt_count=row["attempt_count"],
            platform_post_id=row["platform_post_id"],
            publish_id=row["publish_id"],
            posted_at=row["posted_at"],
            last_error=row["last_error"],
            next_attempt_at=(
                datetime.fromisoformat(row["next_attempt_at"]) if row["next_attempt_at"] else None
            ),
        )


class Queue:
    """投稿キューの読み書き。"""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=30000")
            yield conn
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # 登録
    # ------------------------------------------------------------------
    def enqueue(
        self,
        post_id: str,
        platform: str,
        folder: Path | str,
        scheduled_at: datetime,
        replace: bool = False,
    ) -> str:
        """1件を予約する。戻り値は "created" / "updated" / "kept"。

        すでに posted の組み合わせは、replace 指定でも上書きしない（二重投稿防止）。
        """
        now = _now_iso()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT status FROM jobs WHERE post_id=? AND platform=?", (post_id, platform)
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO jobs (post_id, platform, folder, scheduled_at, status,"
                    " created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                    (post_id, platform, str(folder), scheduled_at.isoformat(),
                     STATUS_SCHEDULED, now, now),
                )
                return "created"
            if row["status"] == STATUS_POSTED:
                return "kept"
            if not replace:
                return "kept"
            conn.execute(
                "UPDATE jobs SET folder=?, scheduled_at=?, status=?, last_error=NULL,"
                " next_attempt_at=NULL, updated_at=? WHERE post_id=? AND platform=?",
                (str(folder), scheduled_at.isoformat(), STATUS_SCHEDULED, now, post_id, platform),
            )
            return "updated"

    # ------------------------------------------------------------------
    # 取得
    # ------------------------------------------------------------------
    def list_jobs(self, post_id: str | None = None, status: str | None = None) -> list[Job]:
        sql = "SELECT * FROM jobs"
        clauses, params = [], []
        if post_id:
            clauses.append("post_id=?")
            params.append(post_id)
        if status:
            clauses.append("status=?")
            params.append(status)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY scheduled_at, post_id, platform"
        with self._connect() as conn:
            return [Job.from_row(r) for r in conn.execute(sql, params)]

    def get(self, post_id: str, platform: str) -> Job | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM jobs WHERE post_id=? AND platform=?", (post_id, platform)
            ).fetchone()
        return Job.from_row(row) if row else None

    def due_jobs(self, now: datetime) -> list[Job]:
        """実行時刻を過ぎていて、まだ投稿できるジョブ（古い順）。"""
        placeholders = ",".join("?" * len(RUNNABLE))
        sql = (
            f"SELECT * FROM jobs WHERE status IN ({placeholders})"
            " AND scheduled_at <= ?"
            " AND (next_attempt_at IS NULL OR next_attempt_at <= ?)"
            " ORDER BY scheduled_at, post_id, platform"
        )
        params = [*RUNNABLE, now.isoformat(), now.isoformat()]
        with self._connect() as conn:
            return [Job.from_row(r) for r in conn.execute(sql, params)]

    def counts(self) -> dict[str, int]:
        with self._connect() as conn:
            rows = conn.execute("SELECT status, COUNT(*) c FROM jobs GROUP BY status")
            return {r["status"]: r["c"] for r in rows}

    # ------------------------------------------------------------------
    # 状態遷移
    # ------------------------------------------------------------------
    def claim(self, job: Job) -> bool:
        """実行権を取る。取れなければ False。

        - 通常は scheduled / ready / failed からのみ取得できる
        - uploading / publishing のまま放置されたジョブ（PCクラッシュ等）は
          STALE_MINUTES を過ぎている場合だけ拾い直す
        この2条件により、同時実行しても同じジョブが二重投稿されない。
        """
        stale_before = (
            datetime.now().astimezone() - timedelta(minutes=STALE_MINUTES)
        ).isoformat(timespec="seconds")
        claimable = ",".join("?" * len(CLAIMABLE))
        in_flight = ",".join("?" * len(IN_FLIGHT))
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE jobs SET status=?, attempt_count=attempt_count+1, updated_at=?"
                f" WHERE id=? AND (status IN ({claimable})"
                f" OR (status IN ({in_flight}) AND updated_at < ?))",
                (STATUS_UPLOADING, _now_iso(), job.id, *CLAIMABLE, *IN_FLIGHT, stale_before),
            )
            return cur.rowcount == 1

    def set_status(
        self,
        job_id: int,
        status: str,
        *,
        platform_post_id: str | None = None,
        publish_id: str | None = None,
        error: str | None = None,
        next_attempt_at: datetime | None = None,
        posted: bool = False,
    ) -> None:
        now = _now_iso()
        fields = ["status=?", "updated_at=?", "last_error=?"]
        params: list[object] = [status, now, error]
        if platform_post_id is not None:
            fields.append("platform_post_id=?")
            params.append(platform_post_id)
        if publish_id is not None:
            fields.append("publish_id=?")
            params.append(publish_id)
        if posted:
            fields.append("posted_at=?")
            params.append(now)
        fields.append("next_attempt_at=?")
        params.append(next_attempt_at.isoformat() if next_attempt_at else None)
        params.append(job_id)
        with self._connect() as conn:
            conn.execute(f"UPDATE jobs SET {', '.join(fields)} WHERE id=?", params)

    def reschedule(self, job_id: int, scheduled_at: datetime) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE jobs SET scheduled_at=?, status=?, next_attempt_at=NULL, updated_at=?"
                " WHERE id=? AND status<>?",
                (scheduled_at.isoformat(), STATUS_SCHEDULED, _now_iso(), job_id, STATUS_POSTED),
            )

    def reset_failed(self, post_id: str | None = None, platform: str | None = None) -> int:
        """失敗ジョブを再試行できる状態に戻す（posted は対象外）。"""
        sql = "UPDATE jobs SET status=?, last_error=NULL, next_attempt_at=NULL, updated_at=?" \
              " WHERE status IN (?,?)"
        params: list[object] = [STATUS_SCHEDULED, _now_iso(), STATUS_FAILED, STATUS_MANUAL]
        if post_id:
            sql += " AND post_id=?"
            params.append(post_id)
        if platform:
            sql += " AND platform=?"
            params.append(platform)
        with self._connect() as conn:
            return conn.execute(sql, params).rowcount

    # ------------------------------------------------------------------
    # 画像URL（二重アップロード防止）
    # ------------------------------------------------------------------
    def get_assets(self, post_id: str, content_hash: str) -> list[str] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT urls FROM assets WHERE post_id=? AND content_hash=?",
                (post_id, content_hash),
            ).fetchone()
        return json.loads(row["urls"]) if row else None

    def save_assets(self, post_id: str, content_hash: str, urls: list[str]) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO assets (post_id, content_hash, urls, uploaded_at) VALUES (?,?,?,?)"
                " ON CONFLICT(post_id) DO UPDATE SET content_hash=excluded.content_hash,"
                " urls=excluded.urls, uploaded_at=excluded.uploaded_at",
                (post_id, content_hash, json.dumps(urls, ensure_ascii=False), _now_iso()),
            )

    # ------------------------------------------------------------------
    # ログ
    # ------------------------------------------------------------------
    def log(self, level: str, message: str, post_id: str = "", platform: str = "") -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO logs (post_id, platform, level, message, created_at) VALUES (?,?,?,?,?)",
                (post_id, platform, level, message, _now_iso()),
            )

    def recent_logs(self, limit: int = 200) -> list[sqlite3.Row]:
        with self._connect() as conn:
            return list(
                conn.execute("SELECT * FROM logs ORDER BY id DESC LIMIT ?", (limit,))
            )[::-1]


def backoff_delay(attempt: int) -> timedelta:
    """一時エラー時の待ち時間（指数バックオフ、上限1時間）。"""
    seconds = min(60 * (2 ** max(0, attempt - 1)), 3600)
    return timedelta(seconds=seconds)


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")
