/**
 * 反応データの定期取得。PCが止まっていてもここで集まる。
 *
 * 決まりごと（PC側と同じにしてある）:
 *   - 計測区分は 24h / 72h / 7d。経過時間に合う区分を選ぶ
 *   - 許容時間（6時間）を過ぎてから測ったものは late=1 にして、
 *     通常の評価には混ぜない。実測していない過去の数字は作らない
 *   - 同じ投稿・同じ区分は1件だけ（UNIQUE制約で守る）
 *   - 取れなかった指標は入れない。0件と取得不可を混同しない
 */

import { adapterFor } from "./adapters/index.js";

// 経過時間（時間）と名前
const WINDOWS = [
  ["24h", 24],
  ["72h", 72],
  ["7d", 168],
];
const GRACE_HOURS = 6;

const now = () => new Date().toISOString();

async function attempt(env, jobId, platform, snapshot, outcome, reason = "") {
  await env.DB.prepare(
    `INSERT INTO metric_attempts (job_id, platform, snapshot, at, outcome, reason)
     VALUES (?1, ?2, ?3, ?4, ?5, ?6)`
  ).bind(jobId, platform, snapshot, now(), outcome, String(reason).slice(0, 300)).run();
}

/** この投稿について、いま測るべき区分。無ければ null。 */
export function dueWindow(postedAt, measured) {
  if (!postedAt) return null;
  const elapsed = (Date.now() - new Date(postedAt).getTime()) / 3600e3;
  const done = new Set(measured);
  const doneHours = WINDOWS.filter(([name]) => done.has(name)).map(([, h]) => h);
  const newest = doneHours.length ? Math.max(...doneHours) : -1;

  const reached = WINDOWS.filter(([name, hours]) =>
    hours <= elapsed && !done.has(name) && hours > newest);
  if (!reached.length) return null;

  const onTime = reached.find(([, hours]) => elapsed <= hours + GRACE_HOURS);
  if (onTime) {
    return { snapshot: onTime[0], windowHours: onTime[1], elapsed, late: false };
  }
  const [name, hours] = reached[reached.length - 1];
  return { snapshot: name, windowHours: hours, elapsed, late: true };
}

/** 投稿済みのうち、測る時期が来たものを少しだけ処理する。 */
export async function collectDue(env, limit = 2) {
  const rows = await env.DB.prepare(
    `SELECT j.*, a.external_id, a.platform AS account_platform
       FROM jobs j JOIN accounts a ON a.id = j.account_id
      WHERE j.status = 'posted' AND j.posted_at IS NOT NULL
        AND j.external_post_id <> ''
      ORDER BY j.posted_at DESC LIMIT 60`).all();

  let collected = 0;
  for (const job of rows.results || []) {
    if (collected >= limit) break;
    const adapter = adapterFor(job.platform);
    if (!adapter.canCollect) continue;

    const measuredRows = await env.DB.prepare(
      "SELECT snapshot FROM metrics WHERE job_id = ?1 AND platform = ?2"
    ).bind(job.id, job.platform).all();
    const due = dueWindow(job.posted_at,
                          (measuredRows.results || []).map((r) => r.snapshot));
    if (!due) continue;

    try {
      const account = { id: job.account_id, platform: job.platform,
                        external_id: job.external_id };
      const { tokenFor } = await import("./tokens.js");
      const token = await tokenFor(env, account);
      const result = await adapter.collect(env, job, account, token);

      const names = ["views", "reach", "impressions", "likes", "comments", "shares", "saves"];
      const got = names.filter((name) => result[name] !== null && result[name] !== undefined);
      if (!got.length) {
        // 空の結果を「測定済み」にしない。あとから入れられなくなる
        await attempt(env, job.id, job.platform, due.snapshot, "failed",
                      "指標が1つも取れませんでした");
        continue;
      }

      await env.DB.prepare(
        `INSERT INTO metrics (job_id, platform, account_id, external_post_id, category,
                              sub_category, snapshot, window_hours, hours_since_post,
                              late, collected_at, published_at, views, reach, impressions,
                              likes, comments, shares, saves, raw)
         VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11,?12,?13,?14,?15,?16,?17,?18,?19,?20)
         ON CONFLICT(job_id, platform, snapshot) DO NOTHING`
      ).bind(job.id, job.platform, job.account_id, job.external_post_id,
             job.category || "", job.sub_category || "", due.snapshot,
             due.windowHours, Math.round(due.elapsed * 100) / 100,
             due.late ? 1 : 0, now(), job.posted_at,
             result.views ?? null, result.reach ?? null, result.impressions ?? null,
             result.likes ?? null, result.comments ?? null, result.shares ?? null,
             result.saves ?? null, JSON.stringify(result.raw || {})).run();
      await attempt(env, job.id, job.platform, due.snapshot, "measured", got.join(", "));
      collected += 1;
    } catch (error) {
      await attempt(env, job.id, job.platform, due.snapshot, "failed",
                    String(error?.message || error));
      collected += 1;     // 失敗も1件ぶんとして数え、1回で粘らない
    }
  }
  return collected;
}

/** PCが取りに来る用。取れた行をそのまま返す。 */
export async function exportMetrics(env, since = "") {
  const rows = await env.DB.prepare(
    `SELECT * FROM metrics WHERE (?1 = '' OR collected_at > ?1)
      ORDER BY collected_at LIMIT 500`).bind(since).all();
  return rows.results || [];
}
