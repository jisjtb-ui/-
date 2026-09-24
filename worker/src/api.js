/**
 * PC と スマホ から使うAPI。
 *
 * 認証は合言葉1つ（既存の /publish と同じ仕組みを使い回す）。
 * 応答にトークンは絶対に入れない。
 */

import { adapterFor, platforms } from "./adapters/index.js";
import {
  CANCELLED, DRAFT, FAILED, POSTED, PROCESSING, RETRYING, SCHEDULED,
  cancel, enqueue, overview, postNow, reschedule, retryNow,
} from "./queue.js";
import { exportMetrics } from "./metrics.js";
import { saveToken } from "./tokens.js";

const now = () => new Date().toISOString();

/** PC側から予約を一括登録する。100件でも1回で送れる。 */
export async function handleEnqueue(env, body) {
  const items = Array.isArray(body?.jobs) ? body.jobs : [];
  if (!items.length) return { error: "jobs がありません", status: 400 };
  if (items.length > 500) return { error: "1回に送れるのは500件までです", status: 400 };

  const known = new Set(platforms());
  const added = [];
  const skipped = [];
  const rejected = [];
  for (const item of items) {
    if (!item?.id || !item?.platform || !item?.account_id || !item?.scheduled_at) {
      rejected.push({ id: item?.id || "", reason: "id / platform / account_id / scheduled_at が必要です" });
      continue;
    }
    if (!known.has(item.platform)) {
      rejected.push({ id: item.id, reason: `知らない投稿先: ${item.platform}` });
      continue;
    }
    const media = Array.isArray(item.media) ? item.media : [];
    const bad = media.find((url) => !isAllowed(url, env.ALLOWED_IMAGE_PREFIX));
    if (bad) {
      rejected.push({ id: item.id, reason: `許可されていない画像URL: ${bad}` });
      continue;
    }
    try {
      const created = await enqueue(env, item);
      (created ? added : skipped).push(item.id);
    } catch (error) {
      rejected.push({ id: item.id, reason: String(error.message || error) });
    }
  }
  return {
    ok: rejected.length === 0,
    added: added.length, skipped: skipped.length, rejected,
    // PCはこれを見てから電源を切る
    registered: added.length + skipped.length,
    status: rejected.length ? 207 : 200,
  };
}

function isAllowed(url, prefix) {
  if (!prefix) return true;
  return String(url).startsWith(prefix);
}

/** アカウントの登録・更新（PC側の接続後に呼ばれる）。 */
export async function handleAccount(env, body) {
  const { id, platform, label = "", category = "", category_id = null,
          external_id = "", settings = {}, access_token: accessToken,
          refresh_token: refreshToken = "", expires_at: expiresAt = "",
          scope = "", enabled = 1 } = body || {};
  if (!id || !platform) return { error: "id と platform が必要です", status: 400 };
  if (!platforms().includes(platform)) {
    return { error: `知らない投稿先: ${platform}`, status: 400 };
  }
  const stamp = now();
  await env.DB.prepare(
    `INSERT INTO accounts (id, platform, label, category, category_id, external_id,
                           settings, enabled, created_at, updated_at)
     VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?9)
     ON CONFLICT(id) DO UPDATE SET
       platform=excluded.platform, label=excluded.label, category=excluded.category,
       category_id=excluded.category_id, external_id=excluded.external_id,
       settings=excluded.settings, enabled=excluded.enabled, updated_at=excluded.updated_at`
  ).bind(id, platform, label, category, category_id, external_id,
         JSON.stringify(settings || {}), enabled ? 1 : 0, stamp).run();

  if (accessToken) {
    await saveToken(env, id, { accessToken, refreshToken, expiresAt, scope });
  }
  return { ok: true, id, token_saved: Boolean(accessToken), status: 200 };
}

/** 接続状況の確認。トークンの値は返さない。 */
export async function handleAccounts(env) {
  const rows = await env.DB.prepare(
    `SELECT a.id, a.platform, a.label, a.category, a.external_id, a.enabled,
            a.updated_at,
            (SELECT expires_at FROM tokens t WHERE t.account_id = a.id) AS expires_at,
            (SELECT COUNT(*) FROM tokens t WHERE t.account_id = a.id) AS has_token
       FROM accounts a ORDER BY a.platform, a.label`).all();
  return {
    ok: true,
    accounts: (rows.results || []).map((row) => ({
      ...row,
      has_token: Boolean(row.has_token),
      // 期限だけ出す。値は出さない
      token_expires_at: row.expires_at || "",
      expires_at: undefined,
    })),
    status: 200,
  };
}

/** PCが「クラウドで投稿済みのもの」を取りに来る（二重投稿の防止）。 */
export async function handleResults(env, url) {
  const since = url.searchParams.get("since") || "";
  const rows = await env.DB.prepare(
    `SELECT id, group_id, platform, account_id, status, external_post_id,
            external_url, posted_at, error, error_kind, attempts
       FROM jobs
      WHERE status IN (?1, ?2) AND (?3 = '' OR updated_at > ?3)
      ORDER BY updated_at LIMIT 500`
  ).bind(POSTED, FAILED, since).all();
  return { ok: true, items: rows.results || [], server_time: now(), status: 200 };
}

/** PCが反応データを取りに来る（カテゴリ別の分析に使う）。 */
export async function handleMetrics(env, url) {
  const since = url.searchParams.get("since") || "";
  return { ok: true, items: await exportMetrics(env, since), status: 200 };
}


export async function handleStatus(env) {
  return { ok: true, ...(await overview(env)), status: 200 };
}

export async function handleJob(env, jobId) {
  const job = await env.DB.prepare("SELECT * FROM jobs WHERE id = ?1").bind(jobId).first();
  if (!job) return { error: "見つかりません", status: 404 };
  const events = await env.DB.prepare(
    "SELECT at, level, message FROM job_events WHERE job_id = ?1 ORDER BY id DESC LIMIT 30"
  ).bind(jobId).all();
  return { ok: true, job, events: events.results || [], status: 200 };
}

/** スマホからの操作。 */
export async function handleAction(env, body) {
  const { action, id, when } = body || {};
  if (!id) return { error: "id がありません", status: 400 };
  let changed = false;
  if (action === "cancel") changed = await cancel(env, id);
  else if (action === "reschedule") {
    if (!when) return { error: "when がありません", status: 400 };
    changed = await reschedule(env, id, when);
  } else if (action === "post_now") changed = await postNow(env, id);
  else if (action === "retry") changed = await retryNow(env, id);
  else return { error: `知らない操作: ${action}`, status: 400 };

  return changed
    ? { ok: true, action, id, status: 200 }
    : { error: "いまの状態では、その操作はできません", status: 409 };
}

export const STATUSES = [DRAFT, SCHEDULED, PROCESSING, POSTED, FAILED, RETRYING, CANCELLED];
export { adapterFor };
