/**
 * スマホへの通知。
 *
 * ntfy.sh を使う。アカウント登録も費用も不要で、iPhoneはアプリを入れて
 * 購読名（トピック）を登録するだけ。トピック名は推測されにくい文字列にする。
 * `NTFY_TOPIC` が未設定なら、通知は黙って行わない（失敗にしない）。
 */

const DEFAULT_HOST = "https://ntfy.sh";

export async function notify(env, { title, message, priority = "default", tags = "" }) {
  const topic = env.NTFY_TOPIC;
  if (!topic) return false;
  const host = (env.NTFY_HOST || DEFAULT_HOST).replace(/\/+$/, "");
  try {
    const headers = {
      "content-type": "text/plain; charset=utf-8",
      title: encodeTitle(title),
      priority,
    };
    if (tags) headers.tags = tags;
    if (env.NTFY_TOKEN) headers.authorization = `Bearer ${env.NTFY_TOKEN}`;
    const response = await fetch(`${host}/${topic}`, {
      method: "POST", headers, body: message,
    });
    return response.ok;
  } catch {
    return false;      // 通知が飛ばなくても投稿処理は続ける
  }
}

/** ntfy のヘッダはASCIIしか通らないので、日本語は本文へ回す。 */
function encodeTitle(title) {
  const text = String(title || "");
  // eslint-disable-next-line no-control-regex
  return /^[\x20-\x7e]*$/.test(text) ? text : "honeshinri";
}

export const notifyFailure = (env, job, error) =>
  notify(env, {
    title: "post failed",
    message: `失敗: ${job.platform} / ${job.category || job.account_id}\n`
      + `${job.id}\n理由: ${String(error?.message || error).slice(0, 200)}`,
    priority: "high",
    tags: "warning",
  });

export const notifySuccess = (env, job, extra = "") =>
  notify(env, {
    title: "posted",
    message: `投稿しました: ${job.platform} / ${job.category || job.account_id}\n`
      + `${job.id}${extra ? `\n${extra}` : ""}`,
    tags: "white_check_mark",
  });

export const notifyManual = (env, job, note) =>
  notify(env, {
    title: "needs your tap",
    message: `${job.platform}: ${note}\n${job.id}`,
    priority: "high",
    tags: "hand",
  });
