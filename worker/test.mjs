/**
 * Worker の動作確認。Cloudflareへデプロイせず、通信もせずに確かめる。
 *
 *   node worker/test.mjs
 */

import assert from "node:assert";
import worker from "./src/index.js";

const store = new Map();
const KV = {
  async get(k, t) { const v = store.get(k); return v ? (t === "json" ? JSON.parse(v) : v) : null; },
  async put(k, v) { store.set(k, v); },
  async list() { return { keys: [...store.keys()].map((name) => ({ name })) }; },
};

const env = {
  PUBLISH_PASSPHRASE: "correct-horse",
  THREADS_ACCESS_TOKEN: "TOKEN_THREADS", THREADS_USER_ID: "111",
  INSTAGRAM_ACCESS_TOKEN: "TOKEN_INSTAGRAM", INSTAGRAM_ACCOUNT_ID: "222",
  ALLOWED_IMAGE_PREFIX: "https://example.pages.dev/",
  ALLOWED_ORIGIN: "https://example.pages.dev",
  PUBLISHED: KV,
};

let failThreads = false;
let apiCalls = 0;
globalThis.fetch = async (url, init) => {
  const u = String(url);
  apiCalls += 1;
  if (u.includes("threads_publish")) {
    return failThreads
      ? new Response(JSON.stringify({ error_message: "招待を承認していません", error_code: 1349245 }), { status: 400 })
      : new Response(JSON.stringify({ id: "TH_1" }));
  }
  if (u.includes("media_publish")) return new Response(JSON.stringify({ id: "IG_1" }));
  if (init?.method === "POST") return new Response(JSON.stringify({ id: "container" + apiCalls }));
  return new Response(JSON.stringify({ status_code: "FINISHED" }));
};

const images = Array.from({ length: 10 }, (_, i) =>
  `https://example.pages.dev/post_001/abc/${String(i + 1).padStart(2, "0")}.jpg`);

const post = (body, pass = "correct-horse") =>
  worker.fetch(new Request("https://w.dev/publish", {
    method: "POST",
    headers: { "content-type": "application/json", "x-passphrase": pass },
    body: JSON.stringify(body),
  }), env);

const results = [];
function check(name, condition, detail = "") {
  results.push({ name, ok: Boolean(condition), detail });
  console.log(`  ${condition ? "OK  " : "NG  "}${name}${condition ? "" : ` … ${detail}`}`);
}

console.log("\nWorker（スマホからの投稿）\n" + "-".repeat(52));

// プリフライト。ここが壊れるとスマホから一切投稿できない。
const pre = await worker.fetch(
  new Request("https://w.dev/publish", { method: "OPTIONS" }), env);
check("プリフライトが通る", pre.status === 204, `HTTP ${pre.status}`);
check("プリフライトに本文がない", pre.body === null);
check("CORSヘッダがある", Boolean(pre.headers.get("access-control-allow-origin")));

// 合言葉
const denied = await post({ experiment_id: "E1", images, caption: "x" }, "wrong");
check("合言葉が違えば拒否する", denied.status === 401, `HTTP ${denied.status}`);

// 他人のURL
const evil = await post({ experiment_id: "E1", caption: "x", images: ["https://evil.example.com/a.jpg"] });
check("自分のサイト以外の画像は拒否する", evil.status === 400);

// 正常
const ok = await (await post({ experiment_id: "E1", images, caption: "本文" })).json();
check("投稿できる", ok.ok === true, JSON.stringify(ok).slice(0, 120));
check("Threadsの投稿IDが返る", ok.results.threads.id === "TH_1");
check("Instagramの投稿IDが返る", ok.results.instagram.id === "IG_1");

// 二重投稿の防止
apiCalls = 0;
const again = await (await post({ experiment_id: "E1", images, caption: "本文" })).json();
check("同じものを二度投稿しない", apiCalls === 0, `${apiCalls}回呼ばれた`);
check("skippedとして返る", again.results.threads.skipped === true);

// 片方失敗
failThreads = true;
const partial = await (await post({ experiment_id: "E2", images, caption: "本文" })).json();
check("片方が失敗しても、もう片方は投稿する",
      partial.results.threads.ok === false && partial.results.instagram.ok === true);
check("失敗の理由が読める",
      String(partial.results.threads.error).includes("1349245"), partial.results.threads.error);

// PCへの受け渡し
const listed = await (await worker.fetch(
  new Request("https://w.dev/published", { headers: { "x-passphrase": "correct-horse" } }), env)).json();
check("PCが投稿済みを取得できる", Boolean(listed.items.E1 && listed.items.E2));

// トークンが漏れていないか
const text = JSON.stringify(ok) + JSON.stringify(partial) + JSON.stringify(listed);
check("応答にトークンが含まれない",
      !text.includes("TOKEN_THREADS") && !text.includes("TOKEN_INSTAGRAM"));

const failed = results.filter((r) => !r.ok);
console.log("-".repeat(52));
if (failed.length) {
  console.log(`失敗 ${failed.length}件`);
  process.exit(1);
}
console.log(`すべて通りました（${results.length}件）`);
