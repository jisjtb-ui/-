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
    "impressions", "reach", "views", "likes", "comments", "shares",
    "saves", "clicks", "followers_gained", "watch_time_seconds", "completion_rate",
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS experiments (
    experiment_id     TEXT PRIMARY KEY,
    hypothesis        TEXT NOT NULL DEFAULT '',
    content_category  TEXT NOT NULL DEFAULT '',   -- data/tests のキー（表示用ではない）
    category_id       INTEGER,
    sub_category_id   INTEGER,
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
    scheduled_at      TEXT,
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
    reach             INTEGER,
    views             INTEGER,
    likes             INTEGER,
    comments          INTEGER,
    shares            INTEGER,
    saves             INTEGER,
    clicks            INTEGER,
    followers_gained  INTEGER,
    watch_time_seconds REAL,
    completion_rate   REAL,
    external_post_id  TEXT NOT NULL DEFAULT '',
    published_at      TEXT NOT NULL DEFAULT '',
    sub_category_id   INTEGER,
    content_id        TEXT NOT NULL DEFAULT '',   -- 生成物の投稿ID（post_001 など）
    late              INTEGER NOT NULL DEFAULT 0, -- 許容時間を過ぎてから測った
    window_hours      REAL,                       -- その計測区分の予定経過時間
    platform_metrics  TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY(experiment_id) REFERENCES experiments(experiment_id)
);
CREATE INDEX IF NOT EXISTS idx_metrics_exp ON experiment_metrics(experiment_id, platform);

-- カテゴリ別の生成割合。platform='' は全媒体共通（Phase 1）。
-- 将来 platform 別に分けるときは、行を足すだけで済む。
CREATE TABLE IF NOT EXISTS category_weights (
    platform     TEXT NOT NULL DEFAULT '',
    category     TEXT NOT NULL,
    weight       REAL NOT NULL,
    locked       INTEGER NOT NULL DEFAULT 0,
    updated_at   TEXT NOT NULL,
    PRIMARY KEY (platform, category)
);

-- なぜweightが変わったのかを後から説明できるようにする
CREATE TABLE IF NOT EXISTS weight_history (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    evaluated_at  TEXT NOT NULL,
    platform      TEXT NOT NULL DEFAULT '',
    category      TEXT NOT NULL,
    weight_before REAL NOT NULL,
    weight_after  REAL NOT NULL,
    sample_size   INTEGER NOT NULL DEFAULT 0,
    score         REAL,
    baseline      REAL,
    reason        TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_weight_hist ON weight_history(evaluated_at);

-- SubCategory単位の生成割合。platform='' は全媒体共通（Phase 1）。
-- 媒体別に分けるときは platform に媒体名を入れた行を足すだけで済む。
CREATE TABLE IF NOT EXISTS sub_weights (
    platform        TEXT NOT NULL DEFAULT '',
    sub_category_id INTEGER NOT NULL,
    weight          REAL NOT NULL,
    locked          INTEGER NOT NULL DEFAULT 0,
    updated_at      TEXT NOT NULL,
    PRIMARY KEY (platform, sub_category_id)
);

CREATE TABLE IF NOT EXISTS sub_weight_history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    evaluated_at    TEXT NOT NULL,
    platform        TEXT NOT NULL DEFAULT '',
    sub_category_id INTEGER NOT NULL,
    weight_before   REAL NOT NULL,
    weight_after    REAL NOT NULL,
    sample_size     INTEGER NOT NULL DEFAULT 0,
    score           REAL,
    baseline        REAL,
    data_key        TEXT NOT NULL DEFAULT '',   -- 使った測定データの指紋（重複適用の防止）
    reason          TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_subw_hist ON sub_weight_history(evaluated_at);

-- 反応データの取得を試した記録。測定済み／取得失敗／取得不可を区別して数える。
CREATE TABLE IF NOT EXISTS metric_attempts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id   TEXT NOT NULL,
    platform        TEXT NOT NULL,
    snapshot        TEXT NOT NULL DEFAULT '',
    attempted_at    TEXT NOT NULL,
    outcome         TEXT NOT NULL,              -- measured / failed / unavailable / rate_limited
    reason          TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_attempt ON metric_attempts(experiment_id, platform, snapshot);

-- 画面から切り替える設定（.env より優先する）。秘密情報は入れない。
CREATE TABLE IF NOT EXISTS app_flags (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

-- Category / SubCategory / 接続済みアカウント。
-- 画面ではすべて「選択」で扱う。名前を別画面へ打ち直させないための土台。
-- 秘密情報はここに入れない（トークンは .tokens/ のまま）。
CREATE TABLE IF NOT EXISTS categories (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL UNIQUE,
    enabled      INTEGER NOT NULL DEFAULT 1,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sub_categories (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    category_id  INTEGER NOT NULL,
    name         TEXT NOT NULL,                  -- 画面に出す名前
    source_key   TEXT NOT NULL DEFAULT '',       -- data/tests のカテゴリ名（内部の紐付け）
    enabled      INTEGER NOT NULL DEFAULT 1,
    created_at   TEXT NOT NULL,
    UNIQUE(category_id, name),
    FOREIGN KEY(category_id) REFERENCES categories(id)
);
CREATE INDEX IF NOT EXISTS idx_sub_cat ON sub_categories(category_id);

CREATE TABLE IF NOT EXISTS social_accounts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    category_id  INTEGER NOT NULL,
    platform     TEXT NOT NULL,
    account_id   TEXT NOT NULL DEFAULT '',       -- 媒体側のID（秘密情報ではない）
    account_name TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL DEFAULT 'disconnected',
    connected_at TEXT NOT NULL DEFAULT '',
    UNIQUE(category_id, platform),
    FOREIGN KEY(category_id) REFERENCES categories(id)
);

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
    category_id: int | None = None
    sub_category_id: int | None = None
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
    scheduled_at: str | None = None
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
    snapshot: str = ""              # 1h / 6h / 24h / 72h / 7d など
    hours_since_post: float | None = None
    window_hours: float | None = None      # その計測区分の予定経過時間
    late: int = 0                          # 許容時間を過ぎてから測った
    external_post_id: str = ""
    published_at: str = ""
    sub_category_id: int | None = None
    content_id: str = ""                   # 生成物の投稿ID（post_001 など）
    period_start: str | None = None
    period_end: str | None = None
    impressions: int | None = None
    reach: int | None = None
    views: int | None = None
    likes: int | None = None
    comments: int | None = None
    shares: int | None = None
    saves: int | None = None
    clicks: int | None = None
    followers_gained: int | None = None
    watch_time_seconds: float | None = None
    completion_rate: float | None = None
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
            ("reach", "ALTER TABLE experiment_metrics ADD COLUMN reach INTEGER"),
            ("watch_time_seconds",
             "ALTER TABLE experiment_metrics ADD COLUMN watch_time_seconds REAL"),
            ("completion_rate",
             "ALTER TABLE experiment_metrics ADD COLUMN completion_rate REAL"),
            ("external_post_id",
             "ALTER TABLE experiment_metrics ADD COLUMN external_post_id TEXT NOT NULL DEFAULT ''"),
            ("published_at",
             "ALTER TABLE experiment_metrics ADD COLUMN published_at TEXT NOT NULL DEFAULT ''"),
            ("sub_category_id",
             "ALTER TABLE experiment_metrics ADD COLUMN sub_category_id INTEGER"),
            ("content_id",
             "ALTER TABLE experiment_metrics ADD COLUMN content_id TEXT NOT NULL DEFAULT ''"),
            ("late",
             "ALTER TABLE experiment_metrics ADD COLUMN late INTEGER NOT NULL DEFAULT 0"),
            ("window_hours",
             "ALTER TABLE experiment_metrics ADD COLUMN window_hours REAL"),
        ):
            if name not in columns:
                conn.execute(ddl)

        pub_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(experiment_publications)")
        }
        if "scheduled_at" not in pub_columns:
            conn.execute("ALTER TABLE experiment_publications ADD COLUMN scheduled_at TEXT")

        # Category / SubCategory への紐付け。既存行は NULL のまま残し、
        # あとで既定Categoryへ結び付ける（データは消さない）。
        exp_columns = {row["name"] for row in conn.execute("PRAGMA table_info(experiments)")}
        for name, ddl in (
            ("category_id", "ALTER TABLE experiments ADD COLUMN category_id INTEGER"),
            ("sub_category_id", "ALTER TABLE experiments ADD COLUMN sub_category_id INTEGER"),
        ):
            if name not in exp_columns:
                conn.execute(ddl)

        weight_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(category_weights)")
        }
        if weight_columns and "sub_category_id" not in weight_columns:
            conn.execute("ALTER TABLE category_weights ADD COLUMN sub_category_id INTEGER")
        history_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(weight_history)")
        }
        if history_columns and "sub_category_id" not in history_columns:
            conn.execute("ALTER TABLE weight_history ADD COLUMN sub_category_id INTEGER")

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
                "INSERT INTO experiments (experiment_id, hypothesis, content_category,"
                " category_id, sub_category_id, hook,"
                " text, image_prompt, image_url, link, source_post_id, source_folder, tags,"
                " extra, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    experiment.experiment_id, experiment.hypothesis, experiment.content_category,
                    experiment.category_id, experiment.sub_category_id,
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

    def set_schedule(self, publication_id: int, when: datetime) -> None:
        """配信の予約時刻を設定する。"""
        with self._connect() as conn:
            conn.execute(
                "UPDATE experiment_publications SET scheduled_at=?, updated_at=?"
                " WHERE id=? AND status<>?",
                (when.isoformat(timespec="seconds"), _now(), publication_id, PUBLISHED),
            )

    def due(self, platform: str | None = None, now: datetime | None = None) -> list[Publication]:
        """予約時刻を過ぎた配信（時刻未設定のものも対象）。"""
        now = now or datetime.now().astimezone()
        placeholders = ",".join("?" * len(CLAIMABLE))
        sql = (
            f"SELECT * FROM experiment_publications WHERE status IN ({placeholders})"
            " AND (scheduled_at IS NULL OR scheduled_at <= ?)"
        )
        params: list[object] = [*CLAIMABLE, now.isoformat(timespec="seconds")]
        if platform:
            sql += " AND platform=?"
            params.append(platform)
        sql += " ORDER BY COALESCE(scheduled_at, created_at), experiment_id"
        with self._connect() as conn:
            return [Publication.from_row(r) for r in conn.execute(sql, params)]

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

    def discard_pending(self) -> dict:
        """まだ配信していない実験を捨てる。作り直すとき用。

        **配信済み・下書き転送済みのものには触らない。** 記録が消えると
        二重投稿の判断ができなくなるため。
        """
        with self._connect() as conn:
            delivered = ",".join("?" * len(DELIVERED))
            keep = [
                row["experiment_id"]
                for row in conn.execute(
                    f"SELECT DISTINCT experiment_id FROM experiment_publications"
                    f" WHERE status IN ({delivered})", DELIVERED,
                )
            ]
            placeholders = ",".join("?" * len(keep)) if keep else ""
            where = f" WHERE experiment_id NOT IN ({placeholders})" if keep else ""

            removed = conn.execute(
                f"SELECT COUNT(*) AS n FROM experiments{where}", keep
            ).fetchone()["n"]
            for table in ("experiment_metrics", "experiment_events",
                          "experiment_publications", "experiments"):
                conn.execute(f"DELETE FROM {table}{where}", keep)
        return {"removed": removed, "kept": len(keep)}

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
    def save_metrics(self, metrics: Metrics, allow_duplicate: bool = False) -> bool:
        """反応データを1件保存する。保存したら True。

        同じ投稿・同じ媒体・同じ計測区分は1件だけにする。二重に入ると
        評価件数が膨らみ、weightが同じデータで何度も動いてしまう。
        同時に2つ走っても増えないよう、確認と書き込みを1つの
        書き込みトランザクション（BEGIN IMMEDIATE）の中で行う。
        """
        metrics.collected_at = metrics.collected_at or _now()
        data = asdict(metrics)
        data["platform_metrics"] = json.dumps(metrics.platform_metrics, ensure_ascii=False)
        columns = ", ".join(data)
        placeholders = ", ".join("?" * len(data))
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                if not allow_duplicate and metrics.snapshot:
                    existing = conn.execute(
                        "SELECT 1 FROM experiment_metrics"
                        " WHERE experiment_id=? AND platform=? AND snapshot=?",
                        (metrics.experiment_id, metrics.platform, metrics.snapshot),
                    ).fetchone()
                    if existing:
                        conn.execute("ROLLBACK")
                        return False
                conn.execute(
                    f"INSERT INTO experiment_metrics ({columns}) VALUES ({placeholders})",
                    list(data.values()),
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        self.log(metrics.experiment_id, f"反応データを保存: {metrics.summary()}", metrics.platform)
        return True

    # ------------------------------------------------------------------
    # 画面から切り替える設定（.env より優先する）
    # ------------------------------------------------------------------
    def get_flag(self, key: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM app_flags WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def set_flag(self, key: str, value: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO app_flags (key, value, updated_at) VALUES (?, ?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value,"
                " updated_at=excluded.updated_at",
                (key, value, _now()),
            )

    def clear_flag(self, key: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM app_flags WHERE key=?", (key,))

    # ------------------------------------------------------------------
    # 反応データを取りに行った記録（測定済み／失敗／取得不可を数えるため）
    # ------------------------------------------------------------------
    def record_attempt(self, experiment_id: str, platform: str, snapshot: str,
                       outcome: str, reason: str = "") -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO metric_attempts"
                " (experiment_id, platform, snapshot, attempted_at, outcome, reason)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (experiment_id, platform, snapshot, _now(), outcome, reason[:300]),
            )

    def attempt_counts(self) -> dict[str, int]:
        """結果ごとの件数。同じ投稿・区分は最後の結果だけを数える。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT outcome, COUNT(*) AS c FROM ("
                "  SELECT experiment_id, platform, snapshot, outcome,"
                "         ROW_NUMBER() OVER ("
                "           PARTITION BY experiment_id, platform, snapshot"
                "           ORDER BY attempted_at DESC, id DESC) AS rank"
                "    FROM metric_attempts"
                ") WHERE rank = 1 GROUP BY outcome"
            ).fetchall()
        return {row["outcome"]: row["c"] for row in rows}

    def recent_failures(self, limit: int = 10) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM metric_attempts WHERE outcome <> 'measured'"
                " ORDER BY attempted_at DESC, id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def last_collected_at(self) -> str:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT MAX(collected_at) AS t FROM experiment_metrics"
            ).fetchone()
        return (row["t"] or "") if row else ""

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
