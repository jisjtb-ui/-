/**
 * 失敗の種類分け。
 *
 * 再試行して直るものと、直らないものを混ぜてはいけない。
 * 権限不足や規約違反を延々と再試行すると、投稿が詰まるうえに
 * APIの制限にも当たる。
 */

export const RETRYABLE = "retryable";   // 通信・一時的な障害・レート制限
export const AUTH = "auth";             // トークン期限切れ・無効
export const PERMISSION = "permission"; // スコープ不足・権限なし
export const CONTENT = "content";       // 画像・本文・規約の問題
export const UNKNOWN = "unknown";

/** 再試行してよい種類か。 */
export const canRetry = (kind) => kind === RETRYABLE || kind === UNKNOWN;

export class PostError extends Error {
  constructor(message, kind = UNKNOWN, detail = {}) {
    super(message);
    this.kind = kind;
    this.detail = detail;
  }
}

/** Meta（Instagram / Threads）のエラーコードを種類へ振り分ける。 */
export function classifyMeta(code, subcode, message, status) {
  const text = String(message || "").toLowerCase();
  if ([4, 17, 32, 613].includes(Number(code))) return RETRYABLE;   // レート制限
  if ([1, 2].includes(Number(code))) return RETRYABLE;             // 一時的な内部エラー
  if (Number(code) === 190) return AUTH;                           // トークン無効・期限切れ
  if ([10, 102, 200, 210, 803].includes(Number(code))) return PERMISSION;
  if (Number(subcode) === 33) return PERMISSION;
  if ([2207026, 2207003, 2207032, 2207020].includes(Number(subcode))) return CONTENT;
  if (text.includes("aspect ratio") || text.includes("media type")
      || text.includes("unsupported") || text.includes("too large")
      || text.includes("caption")) return CONTENT;
  if (status >= 500) return RETRYABLE;
  if (status === 429) return RETRYABLE;
  return UNKNOWN;
}

/** TikTokのエラーコードを種類へ振り分ける。 */
export function classifyTikTok(code, message, status) {
  const value = String(code || "");
  const text = String(message || "").toLowerCase();
  if (value === "rate_limit_exceeded" || status === 429) return RETRYABLE;
  if (value.includes("access_token_invalid") || value.includes("token_expired")
      || value === "invalid_access_token") return AUTH;
  if (value.includes("scope_not_authorized") || value.includes("scope_permission_missed")
      || value === "unaudited_client_can_only_post_to_private_accounts") return PERMISSION;
  if (value.includes("spam_risk") || value.includes("privacy_level")
      || value.includes("invalid_file_upload") || value.includes("url_ownership")
      || value.includes("picture_size") || text.includes("photo")) return CONTENT;
  if (status >= 500) return RETRYABLE;
  return UNKNOWN;
}

/** 待ち時間（秒）。回数を追うごとに広げる。 */
export function backoffSeconds(attempts) {
  const table = [60, 300, 900, 3600];    // 1分 → 5分 → 15分 → 1時間
  return table[Math.min(attempts, table.length - 1)];
}
