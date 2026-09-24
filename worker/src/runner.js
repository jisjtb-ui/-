/**
 * Cron（毎分）で動く本体。PCが止まっていてもここだけで投稿が進む。
 *
 * 無料枠の制約に合わせた作り:
 *   - 1回の呼び出しで外部リクエストは50件まで → **1回で1件ずつ**進める
 *   - 段を分けているので、画像10枚のカルーセルは3回（約3分）で公開される
 *   - CPU時間は10msまでだが、待ち時間（通信）はCPUを使わないので足りる
 */

import { adapterFor } from "./adapters/index.js";
import { PostError } from "./errors.js";
import { collectDue } from "./metrics.js";
import { notifyFailure, notifyManual, notifySuccess } from "./notify.js";
import {
  claimNext, markFailure, markPosted, note, saveProgress,
} from "./queue.js";
import { needsRefresh, refreshToken, loadToken, tokenFor } from "./tokens.js";

// 1回のCronで進める件数。増やすと外部リクエストの上限に近づく
const PER_TICK = 2;

async function accountFor(env, accountId) {
  const row = await env.DB.prepare("SELECT * FROM accounts WHERE id = ?1")
    .bind(accountId).first();
  if (!row) throw new PostError(`アカウントの登録がありません: ${accountId}`, "auth");
  if (!row.enabled) throw new PostError(`このアカウントは停止中です: ${accountId}`, "auth");
  return row;
}

/** 1件を1段だけ進める。 */
export async function advanceOne(env, job) {
  try {
    const account = await accountFor(env, job.account_id);
    const adapter = adapterFor(job.platform);
    const token = await tokenFor(env, account);

    const result = await adapter.advance(env, job, account, token);
    if (!result.done) {
      await saveProgress(env, job, result.stage, result.state, { wait: result.wait });
      return { advanced: true, done: false, stage: result.stage };
    }
    await markPosted(env, job, result);
    if (result.needsManual) {
      await notifyManual(env, job, result.note || "アプリで公開してください");
    } else {
      await notifySuccess(env, job, result.note || "");
    }
    return { advanced: true, done: true, id: result.id };
  } catch (error) {
    const outcome = await markFailure(env, job, error);
    if (!outcome.retry) await notifyFailure(env, job, error);
    return { advanced: false, done: false, ...outcome };
  }
}

/** 期限が近いトークンを先に更新しておく（投稿の直前に慌てないため）。 */
export async function refreshExpiring(env, limit = 3) {
  const rows = await env.DB.prepare(
    "SELECT * FROM accounts WHERE enabled = 1").all();
  let refreshed = 0;
  for (const account of (rows.results || []).slice(0, limit)) {
    try {
      const token = await loadToken(env, account.id);
      if (!needsRefresh(account.platform, token)) continue;
      if (await refreshToken(env, account, token)) {
        refreshed += 1;
        await note(env, `account:${account.id}`, "トークンを更新しました");
      }
    } catch (error) {
      await note(env, `account:${account.id}`,
                 `トークンを更新できません: ${error.message}`, "error");
    }
  }
  return refreshed;
}

/** Cronの入口。 */
export async function tick(env) {
  const jobs = await claimNext(env, PER_TICK);
  const done = [];
  for (const job of jobs) {
    done.push({ id: job.id, ...(await advanceOne(env, job)) });
  }
  // 投稿がないときだけ、トークンと反応データの面倒を見る。
  // 同じ回で外部リクエストの上限（50件）を使い切らないため。
  let refreshed = 0;
  let collected = 0;
  if (!jobs.length) {
    refreshed = await refreshExpiring(env);
    if (!refreshed) collected = await collectDue(env);
  }
  return { picked: jobs.length, results: done, refreshed, collected };
}
