/**
 * 投稿先ごとのAdapterをまとめる。
 *
 * YouTube Shorts や X を足すときは、このファイルに1行加えるだけで済むよう、
 * Adapter は同じ形（name / label / canCollect / advance / collect）にしてある。
 */

import { instagramAdapter } from "./instagram.js";
import { threadsAdapter } from "./threads.js";
import { tiktokAdapter } from "./tiktok.js";

const ADAPTERS = {
  [threadsAdapter.name]: threadsAdapter,
  [instagramAdapter.name]: instagramAdapter,
  [tiktokAdapter.name]: tiktokAdapter,
};

export const platforms = () => Object.keys(ADAPTERS);

export function adapterFor(platform) {
  const adapter = ADAPTERS[platform];
  if (!adapter) throw new Error(`知らない投稿先です: ${platform}`);
  return adapter;
}
