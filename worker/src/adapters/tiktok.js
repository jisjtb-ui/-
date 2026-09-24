/**
 * TikTokAdapter（公式 Content Posting API / 写真投稿）。
 *
 *   POST /v2/post/publish/creator_info/query/   DIRECT_POST の前に必須
 *   POST /v2/post/publish/content/init/         media_type=PHOTO, source=PULL_FROM_URL
 *   POST /v2/post/publish/status/fetch/         publish_id で状態確認
 *
 * 2つのやり方があり、意味が違う:
 *   MEDIA_UPLOAD … 下書き（インボックス）へ送る。**公開は本人がアプリで行う**
 *   DIRECT_POST  … そのまま公開する。video.publish スコープと審査が必要。
 *                  未審査のアプリは非公開アカウントにしか投稿できない
 *
 * 既定は下書き転送（このプロジェクトの方針）。完全自動にしたい場合は
 * アカウント側の設定で direct_post を選ぶ。どちらかを推測で決めない。
 */

import { PERMISSION, PostError, classifyTikTok } from "../errors.js";

const API = "https://open.tiktokapis.com/v2";
const CREATOR_INFO = `${API}/post/publish/creator_info/query/`;
const INIT = `${API}/post/publish/content/init/`;
const STATUS = `${API}/post/publish/status/fetch/`;

async function callApi(url, token, body) {
  const response = await fetch(url, {
    method: "POST",
    headers: {
      authorization: `Bearer ${token}`,
      "content-type": "application/json; charset=UTF-8",
    },
    body: JSON.stringify(body || {}),
  });
  const data = await response.json().catch(() => ({}));
  const error = data?.error;
  const code = error?.code || "";
  if (code && code !== "ok") {
    const message = error?.message || code;
    throw new PostError(`TikTok: ${message}（${code}）`,
                        classifyTikTok(code, message, response.status), { code });
  }
  if (response.status >= 400) {
    throw new PostError(`TikTok: HTTP ${response.status}`,
                        classifyTikTok("", "", response.status));
  }
  return data;
}

export const tiktokAdapter = {
  name: "tiktok",
  label: "TikTok",
  /**
   * 下書き転送では公開された動画IDが手元に残らないため、反応データは取れない。
   * （publish_id は動画IDではない）
   */
  canCollect: false,

  async advance(env, job, account, token) {
    const media = JSON.parse(job.media || "[]");
    if (!media.length) throw new PostError("画像URLがありません", "content");

    const state = JSON.parse(job.state || "{}");
    const settings = JSON.parse(account.settings || "{}");
    const mode = settings.tiktok_mode || env.TIKTOK_MODE || "upload";
    const postMode = mode === "direct_post" ? "DIRECT_POST" : "MEDIA_UPLOAD";

    // --- 1段目: 送信を始める ---
    if (!state.publishId) {
      const info = {
        title: (job.caption || "").slice(0, 90),
        description: job.caption || "",
        disable_comment: false,
        auto_add_music: true,
      };
      if (postMode === "DIRECT_POST") {
        // 公開範囲はクリエイターが選べるものの中から決める（勝手に公開にしない）
        const creator = await callApi(CREATOR_INFO, token.accessToken, {});
        const options = creator?.data?.privacy_level_options || [];
        const wanted = settings.privacy_level || "SELF_ONLY";
        const privacy = options.includes(wanted) ? wanted : options[0];
        if (!privacy) {
          throw new PostError("TikTokの公開範囲を決められません", PERMISSION);
        }
        info.privacy_level = privacy;
      }
      const started = await callApi(INIT, token.accessToken, {
        post_info: info,
        source_info: {
          source: "PULL_FROM_URL",
          photo_cover_index: 0,
          photo_images: media,
        },
        post_mode: postMode,
        media_type: "PHOTO",
      });
      const publishId = started?.data?.publish_id;
      if (!publishId) throw new PostError("publish_id を取得できませんでした", "retryable");
      return { done: false, stage: "publish", state: { publishId, postMode } };
    }

    // --- 2段目: 状態を確かめる ---
    const result = await callApi(STATUS, token.accessToken, { publish_id: state.publishId });
    const status = String(result?.data?.status || "").toUpperCase();
    if (status === "FAILED") {
      const reason = result?.data?.fail_reason || "";
      throw new PostError(`TikTokの送信に失敗しました: ${reason}`,
                          classifyTikTok(reason, reason, 400));
    }
    if (["PUBLISH_COMPLETE", "SEND_TO_USER_INBOX"].includes(status)) {
      const postId = result?.data?.publicaly_available_post_id?.[0]
        || result?.data?.post_id || "";
      const manual = state.postMode !== "DIRECT_POST";
      return {
        done: true,
        id: String(postId || state.publishId),
        url: "",
        raw: result?.data || {},
        // 下書きに入っただけなので、本人がアプリで公開する必要がある
        needsManual: manual,
        note: manual
          ? "TikTokの下書き（インボックス）へ送りました。アプリで公開してください"
          : "",
      };
    }
    return { done: false, stage: "publish", state, wait: true };
  },
};
