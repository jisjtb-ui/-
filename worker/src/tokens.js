/**
 * アクセストークンの保管と自動更新。
 *
 * D1へ平文で置かない。WorkerのSecret `TOKEN_KEY` から鍵を作り、
 * AES-GCM で暗号化して入れる。DBの中身だけ見えても使えない。
 *
 * 期限の考え方（いずれも公式ドキュメントで確認）:
 *   Threads    長期トークン60日 / 24時間以上経てば更新できる
 *   Instagram  長期トークン60日 / 同じ
 *   TikTok     アクセストークン24時間 / refresh_token は365日・更新時に変わりうる
 *
 * PCが止まっていてもここが動き続けないと、いずれ全部投稿できなくなる。
 */

import { AUTH, PostError } from "./errors.js";

const THREADS_REFRESH = "https://graph.threads.net/refresh_access_token";
const INSTAGRAM_REFRESH = "https://graph.instagram.com/refresh_access_token";
const TIKTOK_TOKEN = "https://open.tiktokapis.com/v2/oauth/token/";

// 期限のこれだけ前になったら更新する
const RENEW_BEFORE_MS = 7 * 24 * 60 * 60 * 1000;   // 60日トークンは残り7日で
const RENEW_BEFORE_SHORT_MS = 30 * 60 * 1000;      // 24時間トークンは残り30分で
// 発行から24時間経たないと更新できない媒体がある
const MIN_AGE_MS = 25 * 60 * 60 * 1000;

const enc = new TextEncoder();
const dec = new TextDecoder();

async function keyFrom(secret) {
  if (!secret) throw new PostError("TOKEN_KEY が未設定です", AUTH);
  const digest = await crypto.subtle.digest("SHA-256", enc.encode(secret));
  return crypto.subtle.importKey("raw", digest, { name: "AES-GCM" }, false,
                                 ["encrypt", "decrypt"]);
}

const toBase64 = (bytes) => btoa(String.fromCharCode(...new Uint8Array(bytes)));
const fromBase64 = (text) =>
  Uint8Array.from(atob(text), (char) => char.charCodeAt(0));

/** 暗号化して "iv.本体" の形にする。 */
export async function seal(secret, plain) {
  if (!plain) return "";
  const key = await keyFrom(secret);
  const iv = crypto.getRandomValues(new Uint8Array(12));
  const sealed = await crypto.subtle.encrypt({ name: "AES-GCM", iv }, key,
                                             enc.encode(plain));
  return `${toBase64(iv)}.${toBase64(sealed)}`;
}

export async function open(secret, stored) {
  if (!stored) return "";
  const [ivPart, bodyPart] = String(stored).split(".");
  if (!bodyPart) throw new PostError("トークンを復号できません", AUTH);
  const key = await keyFrom(secret);
  try {
    const plain = await crypto.subtle.decrypt(
      { name: "AES-GCM", iv: fromBase64(ivPart) }, key, fromBase64(bodyPart));
    return dec.decode(plain);
  } catch {
    throw new PostError("トークンを復号できません（TOKEN_KEY が変わった可能性）", AUTH);
  }
}

// ---------------------------------------------------------------------
export async function saveToken(env, accountId, { accessToken, refreshToken = "",
                                                  expiresAt = "", scope = "" }) {
  const now = new Date().toISOString();
  await env.DB.prepare(
    `INSERT INTO tokens (account_id, access_token, refresh_token, expires_at,
                         refreshed_at, scope, updated_at)
     VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)
     ON CONFLICT(account_id) DO UPDATE SET
       access_token=excluded.access_token, refresh_token=excluded.refresh_token,
       expires_at=excluded.expires_at, refreshed_at=excluded.refreshed_at,
       scope=excluded.scope, updated_at=excluded.updated_at`
  ).bind(accountId, await seal(env.TOKEN_KEY, accessToken),
         await seal(env.TOKEN_KEY, refreshToken), expiresAt, now, scope, now).run();
}

export async function loadToken(env, accountId) {
  const row = await env.DB.prepare("SELECT * FROM tokens WHERE account_id = ?1")
    .bind(accountId).first();
  if (!row) throw new PostError(`トークンがありません: ${accountId}`, AUTH);
  return {
    accountId,
    accessToken: await open(env.TOKEN_KEY, row.access_token),
    refreshToken: await open(env.TOKEN_KEY, row.refresh_token),
    expiresAt: row.expires_at || "",
    refreshedAt: row.refreshed_at || "",
    scope: row.scope || "",
  };
}

const msUntil = (iso) => (iso ? new Date(iso).getTime() - Date.now() : Infinity);
const msSince = (iso) => (iso ? Date.now() - new Date(iso).getTime() : Infinity);

/** 期限が近いか。 */
export function needsRefresh(platform, token) {
  const margin = platform === "tiktok" ? RENEW_BEFORE_SHORT_MS : RENEW_BEFORE_MS;
  return msUntil(token.expiresAt) < margin;
}

/** 更新できる状態か（発行から24時間経っていないと断られる媒体がある）。 */
export function canRefresh(platform, token) {
  if (platform === "tiktok") return Boolean(token.refreshToken);
  if (msUntil(token.expiresAt) <= 0) return false;     // 切れたら更新できない
  return msSince(token.refreshedAt) >= MIN_AGE_MS;
}

async function callJson(url, init) {
  const response = await fetch(url, init);
  const data = await response.json().catch(() => ({}));
  return { data, status: response.status };
}

/**
 * トークンを1つ更新する。更新できたら新しい内容を保存して true。
 * 更新できない事情（まだ24時間経っていない等）は false で、失敗にはしない。
 */
export async function refreshToken(env, account, token) {
  const platform = account.platform;
  if (!canRefresh(platform, token)) return false;

  if (platform === "threads" || platform === "instagram") {
    const base = platform === "threads" ? THREADS_REFRESH : INSTAGRAM_REFRESH;
    const grant = platform === "threads" ? "th_refresh_token" : "ig_refresh_token";
    const url = `${base}?grant_type=${grant}&access_token=${encodeURIComponent(token.accessToken)}`;
    const { data, status } = await callJson(url, { method: "GET" });
    if (!data.access_token) {
      throw new PostError(
        `トークンを更新できません（HTTP ${status}）: ${data?.error?.message || ""}`, AUTH);
    }
    const expiresAt = new Date(Date.now() + Number(data.expires_in || 0) * 1000).toISOString();
    await saveToken(env, account.id, {
      accessToken: data.access_token,
      refreshToken: token.refreshToken,
      expiresAt,
      scope: token.scope,
    });
    return true;
  }

  if (platform === "tiktok") {
    const body = new URLSearchParams({
      client_key: env.TIKTOK_CLIENT_KEY || "",
      client_secret: env.TIKTOK_CLIENT_SECRET || "",
      grant_type: "refresh_token",
      refresh_token: token.refreshToken,
    });
    const { data, status } = await callJson(TIKTOK_TOKEN, {
      method: "POST",
      headers: { "content-type": "application/x-www-form-urlencoded" },
      body,
    });
    if (!data.access_token) {
      throw new PostError(
        `TikTokのトークンを更新できません（HTTP ${status}）: ${data.error_description || data.error || ""}`,
        AUTH);
    }
    await saveToken(env, account.id, {
      accessToken: data.access_token,
      // 返ってきた refresh_token が違っていれば、新しい方を使う（公式の指示）
      refreshToken: data.refresh_token || token.refreshToken,
      expiresAt: new Date(Date.now() + Number(data.expires_in || 0) * 1000).toISOString(),
      scope: data.scope || token.scope,
    });
    return true;
  }
  return false;
}

/** 投稿の直前に呼ぶ。期限が近ければ先に更新してから返す。 */
export async function tokenFor(env, account) {
  let token = await loadToken(env, account.id);
  if (needsRefresh(account.platform, token)) {
    try {
      if (await refreshToken(env, account, token)) {
        token = await loadToken(env, account.id);
      }
    } catch (error) {
      // 期限が残っているならそのまま使う。切れていれば呼び出し側で失敗する
      if (msUntil(token.expiresAt) <= 0) throw error;
    }
  }
  return token;
}
