"""コンテンツ実験の記録（SQLite）。

このシステムの中心は「投稿」ではなく「実験」。
1つの実験 = 1つの仮説 × 1つのコンテンツで、複数プラットフォームへ配信し、
それぞれの反応を集めて次の仮説の材料にする。

  experiments              実験そのもの（仮説・Hook・本文・画像）
  experiment_publications  プラットフォームごとの配信状況（Queue）
  experiment_metrics       取得した反応データ（共通指標＋プラットフォーム固有指標）
  experiment_events        experiment_id 単位の追跡ログ
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# 配信キューの状態
GENERATED = "generated"                # コンテンツ生成済み
MEDIA_READY = "media_ready"            # 画像生成済み
READY_TO_PUBLISH = "ready_to_publish"  # 公開URLまで用意できた
PUBLISHING = "publishing"              # 送信中
PUBLISHED = "published"                # 公開済み
DRAFT_CREATED = "draft_created"        # 下書きとして転送済み（公開は本人が行う）
FAILED = "failed"                      # 失敗（記録は消さない）
MANUAL_REQUIRED = "manual_required"    # APIでは完結できず手作業が必要

STATUSES = (
    GENERATED, MEDIA_READY, READY_TO_PUBLISH,
    PUBLISHING, PUBLISHED, DRAFT_CREATED, FAILED, MANUAL_REQUIRED,
)
# 送信が完了した状態（1日の上限計算と二重送信防止に使う）
DELIVERED = (PUBLISHED, DRAFT_CREATED)
# 実行対象にできる状態（published は対象外＝二重投稿しない）
CLAIMABLE = (READY_TO_PUBLISH, FAILED)
RUNNABLE = CLAIMABLE + (PUBLISHING,)
# publishing のまま放置された配信を「落ちた」とみなすまでの時間（分）
STALE_MINUTES = 15

# 共通指標。プラットフォームに存在しない指標は NULL のままにする
COMMON_METRICS = (
    "impressions", "views", "likes", "comments", "shares",
    "saves", "clicks", "followers_gained",
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS experiments (
    experiment_id     TEXT PRIMARY KEY,
    hypothesis        TEXT NOT NULL DEFAULT '',
    content_category  TEXT NOT NULL DEFAULT '',
    hook              TEXT NOT NULL DEFAULT '',
    text              TEXT NOT NULL DEFAULT '',
    image_prompt      TEXT NOT NULL DEFAULT '',
    image_url         TEXT NOT NULL DEFAULT '',
    link              TEXT NOT NULL DEFAULT '',
    source_post_id    TEXT NOT NULL DEFAULT '',
    source_folder     TEXT NOT NULL DEFAULT '',
    tags              TEXT NOT NULL DEFAULT '[]',
    extra             TEXT NOT NULL DEFAULT '{}',
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS experiment_publications (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id     TEXT NOT NULL,
    platform          TEXT NOT NULL,
    status            TEXT NOT NULL,
    external_post_id  TEXT,
    external_url      TEXT,
    published_at      TEXT,
    error_message     TEXT,
    retry_count       INTEGER NOT NULL DEFAULT 0,
    last_attempt_at   TEXT,
    extra             TEXT NOT NULL DEFAULT '{}',
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    UNIQUE(experiment_id, platform),
    FOREIGN KEY(experiment_id) REFERENCES experiments(experiment_id)
);
CREATE INDEX IF NOT EXISTS idx_pub_status ON experiment_publications(status);

CREATE TABLE IF NOT EXISTS experiment_metrics (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id     TEXT NOT NULL,
    platform          TEXT NOT NULL,
    collected_at      TEXT NOT NULL,
    snapshot          TEXT NOT NULL DEFAULT '',
    hours_since_post  REAL,
    period_start      TEXT,
    period_end        TEXT,
    impressions       INTEGER,
    views             INTEGER,
    likes             INTEGER,
    comments          INTEGER,
    shares            INTEGER,
    saves             INTEGER,
    clicks            INTEGER,
    followers_gained  INTEGER,
    platform_metrics  TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY(experiment_id) REFERENCES experiments(experiment_id)
);
CREATE INDEX IF NOT EXISTS idx_metrics_exp ON experiment_metrics(experiment_id, platform);

CREATE TABLE IF NOT EXISTS experiment_events (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id  TEXT NOT NULL,
    platform       TEXT NOT NULL DEFAULT '',
    level          TEXT NOT NULL DEFAULT 'info',
    message        TEXT NOT NULL,
    created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_exp ON experiment_events(experiment_id, id);
"""


@dataclass
class Experiment:
    """1つの実験（仮説とコンテンツ）。"""

    experiment_id: str
    hypothesis: str = ""
    content_category: str = ""
    hook: str = ""
    text: str = ""
    image_prompt: str = ""
    image_url: str = ""
    link: str = ""
    source_post_id: str = ""
    source_folder: str = ""
    tags: list[str] = field(default_factory=list)
    extra: dict = field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Experiment":
        data = dict(row)
        data["tags"] = json.loads(data.get("tags") or "[]")
        data["extra"] = json.loads(data.get("extra") or "{}")
        return cls(**data)


@dataclass
class Publication:
    """ある実験の、あるプラットフォームへの配信状況。"""

    id: int
    experiment_id: str
    platform: str
    status: str
    external_post_id: str | None = None
    external_url: str | None = None
    published_at: str | None = None
    error_message: str | None = None
    retry_count: int = 0
    last_attempt_at: str | None = None
    extra: dict = field(default_factory=dict)

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Publication":
        data = {k: row[k] for k in row.keys() if k not in ("created_at", "updated_at")}
        data["extra"] = json.loads(data.get("extra") or "{}")
        return cls(**data)


@dataclass
class Metrics:
    """取得した反応データ。存在しない指標は None のままにする。"""

    experiment_id: str
    platform: str
    collected_at: str = ""
    snapshot: str = ""              # 1h / 6h / 24h / 72h など
    hours_since_post: float | None = None
    period_start: str | None = None
    period_end: str | None = None
    impressions: int | None = None
    views: int | None = None
    likes: int | None = None
    comments: int | None = None
    shares: int | None = None
    saves: int | None = None
    clicks: int | None = None
    followers_gained: int | None = None
    platform_metrics: dict = field(default_factory=dict)

    def summary(self) -> str:
        parts = [
            f"{name}={getattr(self, name)}"
            for name in COMMON_METRICS
            if getattr(self, name) is not None
        ]
        head = f"[{self.snapshot}] " if self.snapshot else ""
        return head + (" ".join(parts) or "（取得できた指標なし）")


class ExperimentStore:
    """実験データの読み書き。"""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(SCHEMA)
            self._migrate(conn)

    @staticmethod
    def _migrate(conn) -> None:
        """既存DBに後から追加した列を足す（データは消さない）。"""
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(experiment_metrics)")}
        for name, ddl in (
            ("snapshot", "ALTER TABLE experiment_metrics ADD COLUMN snapshot TEXT NOT NULL DEFAULT ''"),
            ("hours_since_post", "ALTER TABLE experiment_metrics ADD COLUMN hours_since_post REAL"),
        ):
            if name not in columns:
                conn.execute(ddl)

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
    # 採番と作成
    # ------------------------------------------------------------------
    def next_experiment_id(self, now: datetime | None = None) -> str:
        """EXP-YYYYMMDD-0001 形式で採番する（日付ごとの連番）。"""
        now = now or datetime.now()
        prefix = f"EXP-{now:%Y%m%d}-"
        with self._connect() as conn:
            row = conn.execute(
                "SELECT experiment_id FROM experiments WHERE experiment_id LIKE ?"
                " ORDER BY experiment_id DESC LIMIT 1",
                (prefix + "%",),
            ).fetchone()
        serial = int(row["experiment_id"].rsplit("-", 1)[1]) + 1 if row else 1
        return f"{prefix}{serial:04d}"

    def create(self, experiment: Experiment, platforms: list[str]) -> Experiment:
        """実験を登録し、各プラットフォームの配信レコードを generated で作る。"""
        now = _now()
        experiment.created_at = experiment.created_at or now
        experiment.updated_at = now
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO experiments (experiment_id, hypothesis, content_category, hook,"
                " text, image_prompt, image_url, link, source_post_id, source_folder, tags,"
                " extra, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    experiment.experiment_id, experiment.hypothesis, experiment.content_category,
                    experiment.hook, experiment.text, experiment.image_prompt, experiment.image_url,
                    experiment.link, experiment.source_post_id, experiment.source_folder,
                    json.dumps(experiment.tags, ensure_ascii=False),
                    json.dumps(experiment.extra, ensure_ascii=False),
                    experiment.created_at, experiment.updated_at,
                ),
            )
            for platform in platforms:
                status = READY_TO_PUBLISH if experiment.image_url else GENERATED
                conn.execute(
                    "INSERT INTO experiment_publications (experiment_id, platform, status,"
                    " created_at, updated_at) VALUES (?,?,?,?,?)",
                    (experiment.experiment_id, platform, status, now, now),
                )
        self.log(experiment.experiment_id, "実験を登録しました"
                 f"（カテゴリ: {experiment.content_category or '未設定'} /"
                 f" 配信先: {', '.join(platforms)}）")
        return experiment

    # ------------------------------------------------------------------
    # 参照
    # ------------------------------------------------------------------
    def get(self, experiment_id: str) -> Experiment | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM experiments WHERE experiment_id=?", (experiment_id,)
            ).fetchone()
        return Experiment.from_row(row) if row else None

    def list_experiments(self, limit: int = 50) -> list[Experiment]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM experiments ORDER BY experiment_id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [Experiment.from_row(r) for r in rows]

    def publications(
        self, experiment_id: str | None = None, status: str | None = None,
        platform: str | None = None,
    ) -> list[Publication]:
        sql = "SELECT * FROM experiment_publications"
        clauses, params = [], []
        if experiment_id:
            clauses.append("experiment_id=?")
            params.append(experiment_id)
        if status:
            clauses.append("status=?")
            params.append(status)
        if platform:
            clauses.append("platform=?")
            params.append(platform)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY experiment_id, platform"
        with self._connect() as conn:
            return [Publication.from_row(r) for r in conn.execute(sql, params)]

    def publication(self, experiment_id: str, platform: str) -> Publication | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM experiment_publications WHERE experiment_id=? AND platform=?",
                (experiment_id, platform),
            ).fetchone()
        return Publication.from_row(row) if row else None

    def runnable(self, platform: str | None = None) -> list[Publication]:
        """公開待ちの配信（published は含めない＝二重投稿しない）。"""
        placeholders = ",".join("?" * len(RUNNABLE))
        sql = f"SELECT * FROM experiment_publications WHERE status IN ({placeholders})"
        params: list[object] = list(RUNNABLE)
        if platform:
            sql += " AND platform=?"
            params.append(platform)
        sql += " ORDER BY experiment_id, platform"
        with self._connect() as conn:
            return [Publication.from_row(r) for r in conn.execute(sql, params)]

    # ------------------------------------------------------------------
    # 更新
    # ------------------------------------------------------------------
    def set_image_url(self, experiment_id: str, image_url: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE experiments SET image_url=?, updated_at=? WHERE experiment_id=?",
                (image_url, _now(), experiment_id),
            )
            conn.execute(
                "UPDATE experiment_publications SET status=?, updated_at=?"
                " WHERE experiment_id=? AND status IN (?,?)",
                (READY_TO_PUBLISH, _now(), experiment_id, GENERATED, MEDIA_READY),
            )
        self.log(experiment_id, f"画像を公開しました: {image_url}")

    def claim(self, publication: Publication) -> bool:
        """配信の実行権を取る。取れなければ False。

        ready_to_publish / failed からのみ取得でき、publishing のまま
        STALE_MINUTES を過ぎたもの（プロセス異常終了など）だけ拾い直す。
        これにより同時実行しても同じ配信が二重投稿されない。
        """
        from datetime import timedelta

        stale_before = (
            datetime.now().astimezone() - timedelta(minutes=STALE_MINUTES)
        ).isoformat(timespec="seconds")
        claimable = ",".join("?" * len(CLAIMABLE))
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE experiment_publications SET status=?, retry_count=retry_count+1,"
                " last_attempt_at=?, updated_at=? WHERE id=?"
                f" AND (status IN ({claimable})"
                "      OR (status=? AND (last_attempt_at IS NULL OR last_attempt_at < ?)))",
                (PUBLISHING, _now(), _now(), publication.id, *CLAIMABLE, PUBLISHING, stale_before),
            )
            return cursor.rowcount == 1

    def mark_published(
        self,
        publication_id: int,
        external_post_id: str,
        external_url: str = "",
        extra: dict | None = None,
        status: str = PUBLISHED,
    ) -> None:
        """送信完了を記録する（下書き転送の場合は status=draft_created）。"""
        now = _now()
        with self._connect() as conn:
            conn.execute(
                "UPDATE experiment_publications SET status=?, external_post_id=?, external_url=?,"
                " published_at=?, error_message=NULL, extra=?, updated_at=? WHERE id=?",
                (status, external_post_id, external_url, now,
                 json.dumps(extra or {}, ensure_ascii=False), now, publication_id),
            )

    def mark_failed(self, publication_id: int, message: str, manual: bool = False) -> None:
        """失敗しても記録は消さない。原因を残して再試行できるようにする。"""
        with self._connect() as conn:
            conn.execute(
                "UPDATE experiment_publications SET status=?, error_message=?, updated_at=?"
                " WHERE id=?",
                (MANUAL_REQUIRED if manual else FAILED, message[:1000], _now(), publication_id),
            )

    def reset_failed(self, experiment_id: str | None = None, platform: str | None = None) -> int:
        sql = ("UPDATE experiment_publications SET status=?, error_message=NULL, updated_at=?"
               " WHERE status IN (?,?)")
        params: list[object] = [READY_TO_PUBLISH, _now(), FAILED, MANUAL_REQUIRED]
        if experiment_id:
            sql += " AND experiment_id=?"
            params.append(experiment_id)
        if platform:
            sql += " AND platform=?"
            params.append(platform)
        with self._connect() as conn:
            return conn.execute(sql, params).rowcount

    # ------------------------------------------------------------------
    # 反応データ
    # ------------------------------------------------------------------
    def save_metrics(self, metrics: Metrics) -> None:
        metrics.collected_at = metrics.collected_at or _now()
        data = asdict(metrics)
        data["platform_metrics"] = json.dumps(metrics.platform_metrics, ensure_ascii=False)
        columns = ", ".join(data)
        placeholders = ", ".join("?" * len(data))
        with self._connect() as conn:
            conn.execute(
                f"INSERT INTO experiment_metrics ({columns}) VALUES ({placeholders})",
                list(data.values()),
            )
        self.log(metrics.experiment_id, f"反応データを保存: {metrics.summary()}", metrics.platform)

    def collected_snapshots(self, experiment_id: str, platform: str) -> set[str]:
        """すでに取得済みのスナップショット名。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT snapshot FROM experiment_metrics"
                " WHERE experiment_id=? AND platform=? AND snapshot<>''",
                (experiment_id, platform),
            ).fetchall()
        return {row["snapshot"] for row in rows}

    def latest_metrics(self, experiment_id: str, platform: str | None = None) -> list[Metrics]:
        sql = ("SELECT * FROM experiment_metrics WHERE experiment_id=?")
        params: list[object] = [experiment_id]
        if platform:
            sql += " AND platform=?"
            params.append(platform)
        sql += " ORDER BY collected_at DESC"
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        out = []
        for row in rows:
            data = {k: row[k] for k in row.keys() if k != "id"}
            data["platform_metrics"] = json.loads(data.get("platform_metrics") or "{}")
            out.append(Metrics(**data))
        return out

    # ------------------------------------------------------------------
    # ログ（experiment_id 単位で追跡できる）
    # ------------------------------------------------------------------
    def log(self, experiment_id: str, message: str, platform: str = "", level: str = "info") -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO experiment_events (experiment_id, platform, level, message, created_at)"
                " VALUES (?,?,?,?,?)",
                (experiment_id, platform, level, message, _now()),
            )

    def events(self, experiment_id: str, limit: int = 200) -> list[sqlite3.Row]:
        with self._connect() as conn:
            return list(conn.execute(
                "SELECT * FROM experiment_events WHERE experiment_id=? ORDER BY id LIMIT ?",
                (experiment_id, limit),
            ))

    def published_today(self, platform: str, now: datetime | None = None) -> int:
        """その日に公開（下書き転送を含む）した件数。1日の上限管理に使う。"""
        now = now or datetime.now().astimezone()
        day = now.strftime("%Y-%m-%d")
        placeholders = ",".join("?" * len(DELIVERED))
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) c FROM experiment_publications"
                f" WHERE platform=? AND status IN ({placeholders})"
                " AND substr(published_at,1,10)=?",
                (platform, *DELIVERED, day),
            ).fetchone()
        return int(row["c"])

    def status_counts(self) -> dict[str, int]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) c FROM experiment_publications GROUP BY status"
            )
            return {r["status"]: r["c"] for r in rows}


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def utc_date(days_ago: int = 0) -> str:
    """Pinterest Analytics 等で使う YYYY-MM-DD（UTC基準）。"""
    from datetime import timedelta

    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%d")
