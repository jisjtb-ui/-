-- クラウド側（Cloudflare D1）の構造。
--
--   npx wrangler d1 execute honeshinri --remote --file worker/schema.sql
--
-- PCが止まっていても、ここだけで予約投稿が回り続ける。
-- 秘密情報はトークン表にだけ入れ、鍵はWorkerのSecretで持つ（暗号化して保存）。

-- 投稿先アカウント。1つのSNSに複数アカウントを持てる。
CREATE TABLE IF NOT EXISTS accounts (
  id           TEXT PRIMARY KEY,          -- 例 instagram:1（PC側の Category と対応）
  platform     TEXT NOT NULL,             -- instagram / threads / tiktok
  label        TEXT NOT NULL DEFAULT '',  -- 画面に出す名前（@xxxx）
  category     TEXT NOT NULL DEFAULT '',  -- PC側のCategory名（表示用）
  category_id  INTEGER,
  external_id  TEXT NOT NULL DEFAULT '',  -- 媒体側のID（秘密情報ではない）
  settings     TEXT NOT NULL DEFAULT '{}',-- 媒体ごとの細かい設定（TikTokの投稿方法など）
  enabled      INTEGER NOT NULL DEFAULT 1,
  created_at   TEXT NOT NULL,
  updated_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS accounts_platform ON accounts(platform, enabled);

-- アクセストークン。値は AES-GCM で暗号化して入れる（鍵は Secret の TOKEN_KEY）。
CREATE TABLE IF NOT EXISTS tokens (
  account_id    TEXT PRIMARY KEY,
  access_token  TEXT NOT NULL,            -- 暗号化済み
  refresh_token TEXT NOT NULL DEFAULT '', -- 暗号化済み
  expires_at    TEXT NOT NULL DEFAULT '',
  refreshed_at  TEXT NOT NULL DEFAULT '',
  scope         TEXT NOT NULL DEFAULT '',
  updated_at    TEXT NOT NULL
);

-- 予約投稿のQueue。1行 = 1アカウントへの1投稿。
CREATE TABLE IF NOT EXISTS jobs (
  id               TEXT PRIMARY KEY,
  group_id         TEXT NOT NULL DEFAULT '',   -- 同じコンテンツの束（PC側 experiment_id）
  platform         TEXT NOT NULL,
  account_id       TEXT NOT NULL,
  category         TEXT NOT NULL DEFAULT '',
  sub_category     TEXT NOT NULL DEFAULT '',
  caption          TEXT NOT NULL DEFAULT '',
  media            TEXT NOT NULL DEFAULT '[]', -- 公開URLの配列（JSON）
  media_kind       TEXT NOT NULL DEFAULT 'image',
  scheduled_at     TEXT NOT NULL,
  status           TEXT NOT NULL DEFAULT 'scheduled',
  -- draft / scheduled / processing / posted / failed / retrying / cancelled
  stage            TEXT NOT NULL DEFAULT '',   -- children / carousel / publish
  state            TEXT NOT NULL DEFAULT '{}', -- 途中のコンテナIDなど
  attempts         INTEGER NOT NULL DEFAULT 0,
  next_attempt_at  TEXT,
  lease_until      TEXT,                       -- 同時実行の排他
  external_post_id TEXT NOT NULL DEFAULT '',
  external_url     TEXT NOT NULL DEFAULT '',
  api_response     TEXT NOT NULL DEFAULT '',
  error            TEXT NOT NULL DEFAULT '',
  error_kind       TEXT NOT NULL DEFAULT '',   -- retryable / auth / permission / content / unknown
  idempotency_key  TEXT NOT NULL,
  created_at       TEXT NOT NULL,
  posted_at        TEXT,
  updated_at       TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS jobs_idempotency ON jobs(idempotency_key);
CREATE INDEX IF NOT EXISTS jobs_due ON jobs(status, scheduled_at);
CREATE INDEX IF NOT EXISTS jobs_group ON jobs(group_id);
CREATE INDEX IF NOT EXISTS jobs_account ON jobs(account_id, status);

-- 何が起きたかの記録。投稿が消えたように見えないようにする。
CREATE TABLE IF NOT EXISTS job_events (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id     TEXT NOT NULL,
  at         TEXT NOT NULL,
  level      TEXT NOT NULL DEFAULT 'info',
  message    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS job_events_job ON job_events(job_id, id);

-- 反応データ。取れなかった指標は入れない（推測値を保存しない）。
CREATE TABLE IF NOT EXISTS metrics (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id           TEXT NOT NULL,
  platform         TEXT NOT NULL,
  account_id       TEXT NOT NULL DEFAULT '',
  external_post_id TEXT NOT NULL DEFAULT '',
  category         TEXT NOT NULL DEFAULT '',
  sub_category     TEXT NOT NULL DEFAULT '',
  snapshot         TEXT NOT NULL,              -- 24h / 72h / 7d
  window_hours     REAL,
  hours_since_post REAL,
  late             INTEGER NOT NULL DEFAULT 0,
  collected_at     TEXT NOT NULL,
  published_at     TEXT NOT NULL DEFAULT '',
  views            INTEGER,
  reach            INTEGER,
  impressions      INTEGER,
  likes            INTEGER,
  comments         INTEGER,
  shares           INTEGER,
  saves            INTEGER,
  raw              TEXT NOT NULL DEFAULT '{}',
  UNIQUE(job_id, platform, snapshot)
);
CREATE INDEX IF NOT EXISTS metrics_sub ON metrics(sub_category, snapshot);

-- 取りに行った結果（測定済み/失敗/取得不可）。
CREATE TABLE IF NOT EXISTS metric_attempts (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id    TEXT NOT NULL,
  platform  TEXT NOT NULL,
  snapshot  TEXT NOT NULL DEFAULT '',
  at        TEXT NOT NULL,
  outcome   TEXT NOT NULL,
  reason    TEXT NOT NULL DEFAULT ''
);

-- 画面から切り替える設定。秘密情報は入れない。
CREATE TABLE IF NOT EXISTS flags (
  key        TEXT PRIMARY KEY,
  value      TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
