/**
 * Instagram と Threads の共通部分。
 *
 * どちらも「コンテナを作る → 出来上がるのを待つ → 公開する」の3段。
 * Workers無料枠は **1回の呼び出しで外部リクエスト50件まで** なので、
 * 画像10枚を1回で全部やると上限に触れる。段ごとに分けて、
 * Cronの次の回へ引き継ぐ（stage と state に途中の状態を残す）。
 *
 *   1回目: 子コンテナを作る          → stage=children
 *   2回目: 出来たか確かめ、束を作る    → stage=carousel
 *   3回目: 束が出来たか確かめ、公開     → posted
 */

import { CONTENT, PostError, classifyMeta } from "../errors.js";

export const MAX_CAROUSEL = 10;

async function callApi(url, params, method = "POST") {
  const init = method === "POST"
    ? {
        method: "POST",
        headers: { "content-type": "application/x-www-form-urlencoded" },
        body: new URLSearchParams(params),
      }
    : { method: "GET" };
  const target = method === "POST"
    ? url
    : `${url}?${new URLSearchParams(params)}`;
  const response = await fetch(target, init);
  const data = await response.json().catch(() => ({}));
  raiseForError(data, response.status);
  return data;
}

function raiseForError(data, status) {
  const error = data?.error;
  const message = data?.error_message || error?.error_user_msg || error?.message
    || (status >= 400 ? `HTTP ${status}` : "");
  if (!message) return;
  const code = data?.error_code ?? error?.code;
  const subcode = error?.error_subcode;
  const kind = classifyMeta(code, subcode, message, status);
  throw new PostError(code ? `${message}（code ${code}）` : message, kind, { code, subcode });
}

/** コンテナの状態を1回だけ確かめる（待たない。待つのは次のCronの回）。 */
async function containerStatus(base, containerId, token) {
  const data = await callApi(`${base}/${containerId}`, {
    fields: "status_code,status,error_message",
    access_token: token,
  }, "GET");
  const status = String(data.status_code || data.status || "").toUpperCase();
  return { status, message: data.error_message || "" };
}

/** 全部のコンテナが出来上がったか。 */
async function allFinished(base, ids, token, label) {
  for (const id of ids) {
    const { status, message } = await containerStatus(base, id, token);
    if (status === "ERROR") {
      throw new PostError(`${label}の処理に失敗しました: ${message}`, CONTENT);
    }
    if (status === "EXPIRED") {
      // 期限切れは作り直せば直る
      throw new PostError(`${label}のコンテナが期限切れです`, "retryable");
    }
    if (status && status !== "FINISHED" && status !== "PUBLISHED") return false;
  }
  return true;
}

/**
 * 段を1つ進める。返り値:
 *   { done: false, stage, state }   まだ途中。次のCronで続ける
 *   { done: true, id, url }         公開できた
 */
export async function advanceMeta(config, job, token) {
  const { base, userId, endpoint, publishEndpoint, textField, label } = config;
  const media = JSON.parse(job.media || "[]");
  if (!media.length) throw new PostError("画像URLがありません", CONTENT);
  if (media.length > MAX_CAROUSEL) {
    throw new PostError(`カルーセルは最大${MAX_CAROUSEL}枚です（${media.length}枚）`, CONTENT);
  }
  const state = JSON.parse(job.state || "{}");
  const stage = job.stage || "children";

  // --- 1段目: 子コンテナを作る（画像の枚数ぶん。10枚なら10リクエスト） ---
  if (stage === "children") {
    if (media.length === 1) {
      const single = await callApi(`${base}/${userId}/${endpoint}`, {
        ...(label === "Threads" ? { media_type: "IMAGE" } : {}),
        image_url: media[0],
        [textField]: job.caption || "",
        access_token: token,
      });
      if (!single.id) throw new PostError("コンテナIDを取得できませんでした", "retryable");
      return { done: false, stage: "publish", state: { creationId: single.id } };
    }
    const children = [];
    for (const url of media) {
      const child = await callApi(`${base}/${userId}/${endpoint}`, {
        ...(label === "Threads" ? { media_type: "IMAGE" } : {}),
        image_url: url,
        is_carousel_item: "true",
        access_token: token,
      });
      if (!child.id) throw new PostError("コンテナIDを取得できませんでした", "retryable");
      children.push(child.id);
    }
    return { done: false, stage: "carousel", state: { children } };
  }

  // --- 2段目: 子が出来たら束ねる（確認10 + 作成1 = 11リクエスト） ---
  if (stage === "carousel") {
    const children = state.children || [];
    if (!children.length) return { done: false, stage: "children", state: {} };
    if (!await allFinished(base, children, token, "画像")) {
      return { done: false, stage: "carousel", state, wait: true };
    }
    const carousel = await callApi(`${base}/${userId}/${endpoint}`, {
      media_type: "CAROUSEL",
      children: children.join(","),
      [textField]: job.caption || "",
      access_token: token,
    });
    if (!carousel.id) throw new PostError("束のコンテナを作れませんでした", "retryable");
    return { done: false, stage: "publish", state: { creationId: carousel.id } };
  }

  // --- 3段目: 束が出来たら公開（確認1 + 公開1 = 2リクエスト） ---
  const creationId = state.creationId;
  if (!creationId) return { done: false, stage: "children", state: {} };
  if (!await allFinished(base, [creationId], token, label)) {
    return { done: false, stage: "publish", state, wait: true };
  }
  const published = await callApi(`${base}/${userId}/${publishEndpoint}`, {
    creation_id: creationId,
    access_token: token,
  });
  if (!published.id) throw new PostError("公開結果を取得できませんでした", "retryable");
  return { done: true, id: String(published.id), url: "", raw: published };
}
