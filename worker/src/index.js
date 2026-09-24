/**
 * クラウド側の本体。PCが止まっていても、ここだけで予約投稿が回り続ける。
 *
 *   Cron（毎分） → 予約時刻の来た投稿を1段進める   … runner.js
 *   /api/*        PCからの一括登録、スマホからの操作 … api.js
 *   /            スマホの管理画面                  … dashboard.js
 *   /publish      スマホからの「今すぐ投稿」（従来どおり）
 *
 * トークンはD1へ暗号化して保存し、鍵はSecret（TOKEN_KEY）で持つ。
 * 応答にトークンを出さない。
 *
 * --- 以下は従来からの「今すぐ投稿」の説明 ---
 *
 * スマホから「今すぐ投稿」を受け取るWorker。
 *
 * トークンをスマホにもページにも置かないための中継役。
 * アクセストークンは Cloudflare の Secret として保管し、この Worker の中だけで使う。
 * スマホから送るのは合言葉だけ。
 *
 *   スマホ ──合言葉──> Worker ──トークン──> Threads / Instagram
 *
 * 投稿したものは KV に記録し、PCが取りに来る。
 * これがないとPC側の予約投稿と二重に出てしまう。
 */

import {
  handleAccount, handleAccounts, handleAction, handleEnqueue, handleJob,
  handleMetrics, handleResults, handleStatus,
} from "./api.js";
import { dashboardHtml } from "./dashboard.js";
import { tick } from "./runner.js";

const THREADS_HOST = "https://graph.threads.net/v1.0";
const GRAPH_HOST = "https://graph.instagram.com/v21.0";
const MAX_CAROUSEL = 10;
const CONTAINER_POLL_MS = 3000;
const CONTAINER_MAX_POLLS = 20;

const corsHeaders = (origin) => ({
  "access-control-allow-origin": origin,
  "access-control-allow-headers": "content-type, x-passphrase",
  "access-control-allow-methods": "GET, POST, OPTIONS",
  "access-control-max-age": "86400",
});

const json = (body, status = 200, origin = "*") =>
  new Response(JSON.stringify(body), {
    status,
    headers: {
      "content-type": "application/json; charset=utf-8",
      "cache-control": "no-store",
      ...corsHeaders(origin),
    },
  });

/** プリフライトの応答。204 に本文を付けると Response を作れないので空にする。 */
const preflight = (origin) => new Response(null, { status: 204, headers: corsHeaders(origin) });

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/** 合言葉の照合。長さの違いで中身を推測されないよう、最後まで比べる。 */
function passphraseMatches(given, expected) {
  if (!given || !expected || given.length !== expected.length) return false;
  let diff = 0;
  for (let i = 0; i < given.length; i += 1) {
    diff |= given.charCodeAt(i) ^ expected.charCodeAt(i);
  }
  return diff === 0;
}

/** 画像URLが自分のサイトのものかを確かめる。他人のURLを投稿させないため。 */
function assertOwnUrls(urls, allowedPrefix) {
  if (!Array.isArray(urls) || urls.length === 0) {
    throw new Error("画像URLがありません");
  }
  if (urls.length > MAX_CAROUSEL) {
    throw new Error(`カルーセルは最大${MAX_CAROUSEL}枚です（${urls.length}枚）`);
  }
  if (!allowedPrefix) return;
  for (const url of urls) {
    if (!url.startsWith(allowedPrefix)) {
      throw new Error(`許可されていない画像URLです: ${url}`);
    }
  }
}

async function callApi(url, params) {
  const body = new URLSearchParams(params);
  const response = await fetch(url, {
    method: "POST",
    headers: { "content-type": "application/x-www-form-urlencoded" },
    body,
  });
  const data = await response.json().catch(() => ({}));
  raiseForError(data, response.status);
  return data;
}

async function getApi(url) {
  const response = await fetch(url);
  const data = await response.json().catch(() => ({}));
  raiseForError(data, response.status);
  return data;
}

/** Threads と Instagram でエラーの形が違うので、両方から拾う。 */
function raiseForError(data, status) {
  const message =
    data?.error_message ||
    data?.error?.error_user_msg ||
    data?.error?.message ||
    (status >= 400 ? `HTTP ${status}` : "");
  if (message) {
    const code = data?.error_code || data?.error?.code || "";
    throw new Error(code ? `${message}（code ${code}）` : message);
  }
}

/** コンテナが出来上がるまで待つ。待たずに公開すると失敗する。 */
async function waitContainer(base, containerId, token, label) {
  await sleep(CONTAINER_POLL_MS);
  for (let i = 0; i < CONTAINER_MAX_POLLS; i += 1) {
    const data = await getApi(
      `${base}/${containerId}?fields=status_code,status,error_message&access_token=${encodeURIComponent(token)}`
    );
    const status = (data.status_code || data.status || "").toUpperCase();
    if (status === "FINISHED" || status === "PUBLISHED" || status === "") return;
    if (status === "ERROR") {
      throw new Error(`${label}の処理に失敗しました: ${data.error_message || ""}`);
    }
    if (status === "EXPIRED") throw new Error(`${label}のコンテナが期限切れです`);
    await sleep(CONTAINER_POLL_MS);
  }
  throw new Error(`${label}の処理が終わりませんでした`);
}

// ---------------------------------------------------------------------
async function publishThreads(env, images, text) {
  const token = env.THREADS_ACCESS_TOKEN;
  const userId = env.THREADS_USER_ID;
  if (!token || !userId) throw new Error("ThreadsのSecretが未設定です");

  let creationId;
  if (images.length === 1) {
    const created = await callApi(`${THREADS_HOST}/${userId}/threads`, {
      media_type: "IMAGE",
      image_url: images[0],
      text,
      access_token: token,
    });
    creationId = created.id;
  } else {
    const children = [];
    for (const url of images) {
      const child = await callApi(`${THREADS_HOST}/${userId}/threads`, {
        media_type: "IMAGE",
        image_url: url,
        is_carousel_item: "true",
        access_token: token,
      });
      children.push(child.id);
    }
    const carousel = await callApi(`${THREADS_HOST}/${userId}/threads`, {
      media_type: "CAROUSEL",
      children: children.join(","),
      text,
      access_token: token,
    });
    creationId = carousel.id;
  }
  if (!creationId) throw new Error("コンテナIDを取得できませんでした");

  await waitContainer(THREADS_HOST, creationId, token, "Threads");

  const published = await callApi(`${THREADS_HOST}/${userId}/threads_publish`, {
    creation_id: creationId,
    access_token: token,
  });
  if (!published.id) throw new Error("投稿IDを取得できませんでした");
  return { id: String(published.id), url: "" };
}

async function publishInstagram(env, images, caption) {
  const token = env.INSTAGRAM_ACCESS_TOKEN;
  const igId = env.INSTAGRAM_ACCOUNT_ID;
  if (!token || !igId) throw new Error("InstagramのSecretが未設定です");

  const children = [];
  for (const url of images) {
    const child = await callApi(`${GRAPH_HOST}/${igId}/media`, {
      image_url: url,
      is_carousel_item: "true",
      access_token: token,
    });
    children.push(child.id);
  }
  for (const id of children) {
    await waitContainer(GRAPH_HOST, id, token, "画像");
  }

  const carousel = await callApi(`${GRAPH_HOST}/${igId}/media`, {
    media_type: "CAROUSEL",
    children: children.join(","),
    caption,
    access_token: token,
  });
  await waitContainer(GRAPH_HOST, carousel.id, token, "カルーセル");

  const published = await callApi(`${GRAPH_HOST}/${igId}/media_publish`, {
    creation_id: carousel.id,
    access_token: token,
  });
  if (!published.id) throw new Error("公開結果を取得できませんでした");
  return { id: String(published.id), url: "" };
}

// ---------------------------------------------------------------------
async function handlePublish(request, env, origin) {
  const body = await request.json().catch(() => ({}));
  const { experiment_id: experimentId, platforms, caption, images } = body;

  if (!experimentId) return json({ error: "experiment_id がありません" }, 400, origin);
  const wanted = Array.isArray(platforms) && platforms.length
    ? platforms
    : ["threads", "instagram"];

  try {
    assertOwnUrls(images, env.ALLOWED_IMAGE_PREFIX || "");
  } catch (error) {
    return json({ error: error.message }, 400, origin);
  }

  // すでに投稿したものは二度出さない
  const seen = env.PUBLISHED ? await env.PUBLISHED.get(experimentId, "json") : null;
  const already = seen?.platforms || {};

  const results = {};
  for (const platform of wanted) {
    if (already[platform]) {
      results[platform] = { ok: true, skipped: true, ...already[platform] };
      continue;
    }
    try {
      const outcome =
        platform === "threads"
          ? await publishThreads(env, images, caption || "")
          : platform === "instagram"
          ? await publishInstagram(env, images, caption || "")
          : (() => {
              throw new Error(`このチャネルはWorkerから投稿できません: ${platform}`);
            })();
      results[platform] = { ok: true, ...outcome };
      already[platform] = { id: outcome.id, at: new Date().toISOString() };
    } catch (error) {
      results[platform] = { ok: false, error: String(error.message || error) };
    }
  }

  if (env.PUBLISHED && Object.keys(already).length) {
    await env.PUBLISHED.put(
      experimentId,
      JSON.stringify({ platforms: already, updated: new Date().toISOString() }),
      { expirationTtl: 60 * 60 * 24 * 90 }
    );
  }

  const ok = Object.values(results).every((r) => r.ok);
  return json({ ok, experiment_id: experimentId, results }, ok ? 200 : 207, origin);
}

/** PCが「スマホから投稿されたもの」を取りに来る。 */
async function handlePublished(env, origin) {
  if (!env.PUBLISHED) return json({ items: {} }, 200, origin);
  const listed = await env.PUBLISHED.list({ limit: 1000 });
  const items = {};
  for (const key of listed.keys) {
    items[key.name] = await env.PUBLISHED.get(key.name, "json");
  }
  return json({ items }, 200, origin);
}

/** 管理画面。合言葉は画面の中で入れるので、ここでは通す。 */
const page = (html) =>
  new Response(html, {
    headers: {
      "content-type": "text/html; charset=utf-8",
      "cache-control": "no-store",
      // 画面から外部へ何も送らない
      "content-security-policy":
        "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'",
      "referrer-policy": "no-referrer",
      "x-content-type-options": "nosniff",
    },
  });

/** api.js の戻り値（status を含む）をHTTP応答へ直す。 */
function fromResult(result, origin) {
  const { status = 200, ...body } = result || {};
  return json(body, status, origin);
}

export default {
  /** Cron Trigger。PCの電源とは関係なく、Cloudflare側で毎分動く。 */
  async scheduled(event, env, ctx) {
    ctx.waitUntil(
      (async () => {
        if (!env.DB) return;
        try {
          await tick(env);
        } catch (error) {
          // 1回の失敗で止めない。次の分にまた来る
          console.error("cron", error?.message || error);
        }
      })()
    );
  },

  async fetch(request, env) {
    const url = new URL(request.url);
    const origin = env.ALLOWED_ORIGIN || "*";

    if (request.method === "OPTIONS") {
      return preflight(origin);
    }
    // スマホの管理画面（合言葉は画面の中で入れる）
    if (url.pathname === "/" || url.pathname === "/dashboard") {
      return page(dashboardHtml());
    }
    if (url.pathname === "/health") {
      return json({
        ok: true,
        ready: Boolean(env.PUBLISH_PASSPHRASE),
        cloud_queue: Boolean(env.DB),
        notifications: Boolean(env.NTFY_TOPIC),
      }, 200, origin);
    }

    const given = request.headers.get("x-passphrase") || "";
    if (!passphraseMatches(given, env.PUBLISH_PASSPHRASE || "")) {
      return json({ error: "合言葉が違います" }, 401, origin);
    }

    // --- 従来からの「今すぐ投稿」 ---
    if (url.pathname === "/publish" && request.method === "POST") {
      return handlePublish(request, env, origin);
    }
    if (url.pathname === "/published" && request.method === "GET") {
      return handlePublished(env, origin);
    }

    // --- クラウドQueue（D1が無ければ使えない） ---
    if (url.pathname.startsWith("/api/")) {
      if (!env.DB) {
        return json({ error: "クラウドQueueが未設定です（D1のバインドがありません）" },
                    503, origin);
      }
      const body = request.method === "POST"
        ? await request.json().catch(() => ({}))
        : {};

      if (url.pathname === "/api/enqueue" && request.method === "POST") {
        return fromResult(await handleEnqueue(env, body), origin);
      }
      if (url.pathname === "/api/account" && request.method === "POST") {
        return fromResult(await handleAccount(env, body), origin);
      }
      if (url.pathname === "/api/accounts" && request.method === "GET") {
        return fromResult(await handleAccounts(env), origin);
      }
      if (url.pathname === "/api/status" && request.method === "GET") {
        return fromResult(await handleStatus(env), origin);
      }
      if (url.pathname === "/api/results" && request.method === "GET") {
        return fromResult(await handleResults(env, url), origin);
      }
      if (url.pathname === "/api/metrics" && request.method === "GET") {
        return fromResult(await handleMetrics(env, url), origin);
      }
      if (url.pathname === "/api/action" && request.method === "POST") {
        return fromResult(await handleAction(env, body), origin);
      }
      if (url.pathname.startsWith("/api/job/") && request.method === "GET") {
        const id = decodeURIComponent(url.pathname.slice("/api/job/".length));
        return fromResult(await handleJob(env, id), origin);
      }
      // 手で動かして確かめるための入口（Cronと同じ処理を1回だけ）
      if (url.pathname === "/api/tick" && request.method === "POST") {
        return json({ ok: true, ...(await tick(env)) }, 200, origin);
      }
    }
    return json({ error: "not found" }, 404, origin);
  },
};
