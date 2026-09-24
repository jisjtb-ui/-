/**
 * ThreadsAdapter（公式 Threads API）。
 *
 *   POST /{user-id}/threads          コンテナ作成（media_type=IMAGE / CAROUSEL）
 *   GET  /{container-id}             status_code を確認
 *   POST /{user-id}/threads_publish  公開
 *
 * カルーセルは最大10枚（本システムは10枚固定）。
 */

import { advanceMeta } from "./meta.js";

const BASE = "https://graph.threads.net/v1.0";

export const threadsAdapter = {
  name: "threads",
  label: "Threads",
  /** 反応データの取得に対応しているか。 */
  canCollect: true,

  async advance(env, job, account, token) {
    return advanceMeta({
      base: BASE,
      userId: account.external_id,
      endpoint: "threads",
      publishEndpoint: "threads_publish",
      textField: "text",
      label: "Threads",
    }, job, token.accessToken);
  },

  /** 投稿1件の反応データ。取れない指標は入れない。 */
  async collect(env, job, account, token) {
    const metrics = "views,likes,replies,reposts,quotes,shares";
    const url = `${BASE}/${job.external_post_id}/insights`
      + `?metric=${metrics}&access_token=${encodeURIComponent(token.accessToken)}`;
    const response = await fetch(url);
    const data = await response.json().catch(() => ({}));
    if (data?.error) {
      const message = data.error.error_user_msg || data.error.message || "";
      throw new Error(`${message}（code ${data.error.code ?? ""}）`);
    }
    const raw = {};
    for (const item of data.data || []) {
      const value = item?.total_value?.value ?? item?.values?.[0]?.value;
      if (typeof value === "number") raw[item.name] = value;
    }
    const shares = ["reposts", "quotes", "shares"]
      .reduce((total, key) => (key in raw ? total + raw[key] : total), 0);
    return {
      raw,
      views: raw.views ?? null,
      likes: raw.likes ?? null,
      comments: raw.replies ?? null,
      shares: Object.keys(raw).some((k) => ["reposts", "quotes", "shares"].includes(k))
        ? shares : null,
      reach: null,
      impressions: null,
      saves: null,
    };
  },
};
