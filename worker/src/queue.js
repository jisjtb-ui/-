/**
 * 予約投稿のQueue。
 *
 * 大事にしていること:
 *   - 投稿を消さない。失敗しても理由を残して retrying / failed にする
 *   - 二重に出さない。idempotency_key と「取り置き（lease）」で守る
 *   - 直らないエラーを延々と再試行しない（errors.js の種類分けを使う）
 */

import { backoffSeconds, canRetry } from "./errors.js";

export const DRAFT = "draft";
export const SCHEDULED = "scheduled";
export const PROCESSING = "processing";
export const POSTED = "posted";
export const FAILED = "failed";
export const RETRYING = "retrying";
export const CANCELLED = "cancelled";

// 取り置きの長さ。これを過ぎたら、別の回が拾い直してよい
const LEASE_SECONDS = 300;
// 再試行の上限
export const MAX_ATTEMPTS = 4;

const now = () => new Date().toISOString();
const plusSeconds = (seconds) => new Date(Date.now() + seconds * 1000).toISOString();

export async function note(env, jobId, message, level = "info") {
  await env.DB.prepare(
    "INSERT INTO job_events (job_id, at, level, message) VALUES (?1, ?2, ?3, ?4)"
  ).bind(jobId, now(), level, String(message).slice(0, 500)).run();
}

/**
 * 予約を登録する。同じ idempotency_key があれば作り直さない。
 * PC側から100件まとめて送っても、同じものは増えない。
 */
export async function enqueue(env, job) {
  const stamp = now();
  const key = job.idempotency_key || `${job.group_id}:${job.platform}:${job.account_id}`;
  const result = await env.DB.prepare(
    `INSERT INTO jobs (id, group_id, platform, account_id, category, sub_category,
                       caption, media, media_kind, scheduled_at, status, attempts,
                       idempotency_key, created_at, updated_at)
     VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, 0, ?12, ?13, ?13)
     ON CONFLICT(idempotency_key) DO NOTHING`
  ).bind(
    job.id, job.group_id || "", job.platform, job.account_id,
    job.category || "", job.sub_category || "", job.caption || "",
    JSON.stringify(job.media || []), job.media_kind || "image",
    job.scheduled_at, job.status || SCHEDULED, key, stamp,
  ).run();
  const added = (result.meta?.changes ?? 0) > 0;
  if (added) await note(env, job.id, `予約しました（${job.scheduled_at}）`);
  return added;
}

/**
 * いま処理すべき1件を取り置く。
 *
 * D1は書き込みが直列なので、`WHERE` に条件を付けた UPDATE が1件だけ
 * 成功することを使って、同時に2つの回が同じ投稿を掴まないようにする。
 */
export async function claimNext(env, limit = 1) {
  const stamp = now();
  const rows = await env.DB.prepare(
    `SELECT * FROM jobs
      WHERE status IN (?1, ?2, ?3)
        AND scheduled_at <= ?4
        AND (next_attempt_at IS NULL OR next_attempt_at <= ?4)
        AND (lease_until IS NULL OR lease_until <= ?4)
      ORDER BY scheduled_at
      LIMIT ?5`
  ).bind(SCHEDULED, RETRYING, PROCESSING, stamp, limit).all();

  const claimed = [];
  for (const row of rows.results || []) {
    const taken = await env.DB.prepare(
      `UPDATE jobs SET status = ?1, lease_until = ?2, updated_at = ?3
        WHERE id = ?4 AND status IN (?5, ?6, ?7)
          AND (lease_until IS NULL OR lease_until <= ?3)`
    ).bind(PROCESSING, plusSeconds(LEASE_SECONDS), stamp, row.id,
           SCHEDULED, RETRYING, PROCESSING).run();
    if ((taken.meta?.changes ?? 0) > 0) {
      claimed.push({ ...row, status: PROCESSING });
    }
  }
  return claimed;
}

/** 段を1つ進めた状態を書き戻す（まだ公開していない）。 */
export async function saveProgress(env, job, stage, state, { wait = false } = {}) {
  await env.DB.prepare(
    `UPDATE jobs SET stage = ?1, state = ?2, status = ?3,
                     lease_until = NULL, next_attempt_at = ?4, updated_at = ?5
      WHERE id = ?6`
  ).bind(stage, JSON.stringify(state || {}), PROCESSING,
         // 出来上がりを待つ場合だけ、少し間を置いてから次を試す
         wait ? plusSeconds(60) : null, now(), job.id).run();
}

export async function markPosted(env, job, { id, url = "", raw = {}, note: extra = "" }) {
  const stamp = now();
  await env.DB.prepare(
    `UPDATE jobs SET status = ?1, external_post_id = ?2, external_url = ?3,
                     api_response = ?4, posted_at = ?5, lease_until = NULL,
                     stage = '', error = '', error_kind = '', updated_at = ?5
      WHERE id = ?6`
  ).bind(POSTED, String(id || ""), url, JSON.stringify(raw).slice(0, 2000),
         stamp, job.id).run();
  await note(env, job.id, extra || `投稿しました（${id}）`);
}

/**
 * 失敗を記録する。再試行してよいものだけ retrying にする。
 * 回数を使い切ったら failed にして、投稿内容はそのまま残す。
 */
export async function markFailure(env, job, error) {
  const kind = error?.kind || "unknown";
  const attempts = (job.attempts || 0) + 1;
  const retry = canRetry(kind) && attempts < MAX_ATTEMPTS;
  const stamp = now();
  await env.DB.prepare(
    `UPDATE jobs SET status = ?1, attempts = ?2, next_attempt_at = ?3,
                     error = ?4, error_kind = ?5, lease_until = NULL, updated_at = ?6
      WHERE id = ?7`
  ).bind(retry ? RETRYING : FAILED, attempts,
         retry ? plusSeconds(backoffSeconds(job.attempts || 0)) : null,
         String(error?.message || error).slice(0, 500), kind, stamp, job.id).run();
  await note(env, job.id,
             retry
               ? `失敗（${kind}）。${backoffSeconds(job.attempts || 0)}秒後に再試行します: ${error?.message || error}`
               : `失敗（${kind}）。再試行しません: ${error?.message || error}`,
             "error");
  return { retry, attempts, kind };
}

/** 画面からの操作。 */
export async function cancel(env, jobId) {
  const result = await env.DB.prepare(
    `UPDATE jobs SET status = ?1, lease_until = NULL, updated_at = ?2
      WHERE id = ?3 AND status IN (?4, ?5, ?6, ?7)`
  ).bind(CANCELLED, now(), jobId, SCHEDULED, RETRYING, DRAFT, FAILED).run();
  if ((result.meta?.changes ?? 0) > 0) {
    await note(env, jobId, "取り消しました");
    return true;
  }
  return false;
}

export async function reschedule(env, jobId, when) {
  const result = await env.DB.prepare(
    `UPDATE jobs SET scheduled_at = ?1, status = ?2, next_attempt_at = NULL,
                     lease_until = NULL, updated_at = ?3
      WHERE id = ?4 AND status IN (?2, ?5, ?6, ?7)`
  ).bind(when, SCHEDULED, now(), jobId, RETRYING, CANCELLED, FAILED).run();
  if ((result.meta?.changes ?? 0) > 0) {
    await note(env, jobId, `予約時刻を ${when} に変えました`);
    return true;
  }
  return false;
}

/** いますぐ投稿する（予約時刻を今にするだけ。投稿はCronが行う）。 */
export async function postNow(env, jobId) {
  return reschedule(env, jobId, now());
}

/** 失敗した投稿をもう一度試す。回数は0に戻す。 */
export async function retryNow(env, jobId) {
  const result = await env.DB.prepare(
    `UPDATE jobs SET status = ?1, attempts = 0, next_attempt_at = NULL,
                     lease_until = NULL, scheduled_at = ?2, stage = '', state = '{}',
                     error = '', error_kind = '', updated_at = ?2
      WHERE id = ?3 AND status IN (?4, ?5)`
  ).bind(SCHEDULED, now(), jobId, FAILED, CANCELLED).run();
  if ((result.meta?.changes ?? 0) > 0) {
    await note(env, jobId, "もう一度試します");
    return true;
  }
  return false;
}

/** スマホ画面用のまとめ。 */
export async function overview(env) {
  const counts = await env.DB.prepare(
    "SELECT status, COUNT(*) AS n FROM jobs GROUP BY status").all();
  const byStatus = {};
  for (const row of counts.results || []) byStatus[row.status] = row.n;

  const perPlatform = await env.DB.prepare(
    `SELECT platform, status, COUNT(*) AS n FROM jobs GROUP BY platform, status`).all();
  const perAccount = await env.DB.prepare(
    `SELECT j.account_id, a.label, a.platform, j.status, COUNT(*) AS n
       FROM jobs j LEFT JOIN accounts a ON a.id = j.account_id
      GROUP BY j.account_id, j.status`).all();

  const next = await env.DB.prepare(
    `SELECT * FROM jobs WHERE status IN (?1, ?2) ORDER BY scheduled_at LIMIT 1`
  ).bind(SCHEDULED, RETRYING).first();

  const today = new Date().toISOString().slice(0, 10);
  const todays = await env.DB.prepare(
    `SELECT * FROM jobs WHERE substr(scheduled_at, 1, 10) = ?1
      ORDER BY scheduled_at LIMIT 100`).bind(today).all();
  const failures = await env.DB.prepare(
    `SELECT * FROM jobs WHERE status = ?1 ORDER BY updated_at DESC LIMIT 20`
  ).bind(FAILED).all();
  const recent = await env.DB.prepare(
    `SELECT * FROM jobs WHERE status = ?1 ORDER BY posted_at DESC LIMIT 20`
  ).bind(POSTED).all();

  return {
    counts: byStatus,
    total: Object.values(byStatus).reduce((a, b) => a + b, 0),
    per_platform: perPlatform.results || [],
    per_account: perAccount.results || [],
    next: next || null,
    today: todays.results || [],
    failed: failures.results || [],
    posted: recent.results || [],
    server_time: now(),
  };
}
