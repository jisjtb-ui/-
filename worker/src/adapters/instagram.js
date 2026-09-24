/**
 * InstagramAdapter（Instagram Platform / Graph API）。
 *
 *   POST /{ig-id}/media          is_carousel_item=true で画像ごとに
 *   GET  /{container-id}         status_code=FINISHED を確認
 *   POST /{ig-id}/media          media_type=CAROUSEL, children=[...]
 *   POST /{ig-id}/media_publish  公開
 *
 * 画像は公開HTTPS URL・JPEG・アスペクト比 4:5〜1.91:1 が必要。
 * 本システムは 1152x1440 の白余白つきJPEGを渡す（PC側で用意済み）。
 * Reel（動画）は音源をAPIで付けられないため、ここでは扱わない。
 */

import { advanceMeta } from "./meta.js";

const BASE = "https://graph.instagram.com/v21.0";
// 2024-07-02 以降に作られた投稿では impressions が返らないため views を先に見る
const PRIMARY = ["views", "reach", "likes", "comments", "shares", "saved", "total_interactions"];

export const instagramAdapter = {
  name: "instagram",
  label: "Instagram",
  canCollect: true,

  async advance(env, job, account, token) {
    return advanceMeta({
      base: BASE,
      userId: account.external_id,
      endpoint: "media",
      publishEndpoint: "media_publish",
      textField: "caption",
      label: "Instagram",
    }, job, token.accessToken);
  },

  async collect(env, job, account, token) {
    const ask = async (metrics) => {
      const url = `${BASE}/${job.external_post_id}/insights`
        + `?metric=${metrics.join(",")}&access_token=${encodeURIComponent(token.accessToken)}`;
      const response = await fetch(url);
      const data = await response.json().catch(() => ({}));
      if (data?.error) {
        const message = data.error.error_user_msg || data.error.message || "";
        const err = new Error(`${message}（code ${data.error.code ?? ""}）`);
        err.code = data.error.code;
        throw err;
      }
      const raw = {};
      for (const item of data.data || []) {
        const value = item?.total_value?.value ?? item?.values?.[0]?.value;
        if (typeof value === "number") raw[item.name] = value;
      }
      return raw;
    };

    let raw = {};
    try {
      raw = await ask(PRIMARY);
    } catch (error) {
      // まとめて頼むと、使えない指標が1つ混ざるだけで応答全体が落ちる。
      // 断られたときだけ1つずつ聞き直して、取れるものは取り切る。
      for (const metric of PRIMARY) {
        try {
          Object.assign(raw, await ask([metric]));
        } catch {
          /* この投稿形式では使えない指標。0を作らず、空のままにする */
        }
      }
      if (!Object.keys(raw).length) throw error;
    }
    return {
      raw,
      views: raw.views ?? null,
      reach: raw.reach ?? null,
      impressions: raw.impressions ?? null,
      likes: raw.likes ?? null,
      comments: raw.comments ?? null,
      shares: raw.shares ?? null,
      saves: raw.saved ?? null,
    };
  },
};
