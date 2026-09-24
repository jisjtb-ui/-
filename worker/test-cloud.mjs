/**
 * クラウド予約投稿の動作確認。Cloudflareへデプロイせず、通信もせずに確かめる。
 *
 *   node worker/test-cloud.mjs
 *
 * 外部SNSへは1件も投稿しない。fetch を差し替えて、APIの応答のふりをする。
 */

import worker from "./src/index.js";
import { makeD1 } from "./fake-d1.mjs";
import { tick, advanceOne } from "./src/runner.js";
import { claimNext, enqueue, overview } from "./src/queue.js";
import { saveToken } from "./src/tokens.js";

const PASS = "correct-horse";
const PREFIX = "https://example.pages.dev/";

const results = [];
function check(name, condition, detail = "") {
  results.push({ name, ok: Boolean(condition), detail });
  console.log(`  ${condition ? "OK  " : "NG  "}${name}${condition ? "" : ` … ${detail}`}`);
}

// --- APIのふり -------------------------------------------------------
let calls = [];
let containerReady = true;
let failWith = null;        // { body, status }
let notified = [];

function fakeFetch() {
  globalThis.fetch = async (url, init) => {
    const target = String(url);
    calls.push(target);

    if (target.includes("ntfy")) {
      notified.push({ url: target, body: init?.body, headers: init?.headers });
      return new Response("ok");
    }
    if (failWith) {
      return new Response(JSON.stringify(failWith.body), { status: failWith.status || 400 });
    }
    if (target.includes("refresh_access_token")) {
      return new Response(JSON.stringify({ access_token: "NEW_TOKEN", expires_in: 5184000 }));
    }
    if (target.includes("oauth/token")) {
      return new Response(JSON.stringify({
        access_token: "NEW_TT", refresh_token: "NEW_TT_REFRESH", expires_in: 86400 }));
    }
    if (target.includes("/status/fetch/")) {
      return new Response(JSON.stringify({
        data: { status: containerReady ? "SEND_TO_USER_INBOX" : "PROCESSING_UPLOAD" },
        error: { code: "ok" } }));
    }
    if (target.includes("/content/init/")) {
      return new Response(JSON.stringify({
        data: { publish_id: "v_pub_url~TT1" }, error: { code: "ok" } }));
    }
    if (target.includes("creator_info")) {
      return new Response(JSON.stringify({
        data: { privacy_level_options: ["SELF_ONLY", "PUBLIC_TO_EVERYONE"] },
        error: { code: "ok" } }));
    }
    if (target.includes("threads_publish") || target.includes("media_publish")) {
      return new Response(JSON.stringify({ id: "PUB_1" }));
    }
    if (init?.method === "POST") {
      return new Response(JSON.stringify({ id: "c" + calls.length }));
    }
    // コンテナの状態確認
    return new Response(JSON.stringify(
      containerReady ? { status_code: "FINISHED" } : { status_code: "IN_PROGRESS" }));
  };
}

async function makeEnv({ platform = "instagram", images = 10, tiktokMode } = {}) {
  const env = {
    DB: makeD1(),
    PUBLISH_PASSPHRASE: PASS,
    TOKEN_KEY: "a-long-random-key-for-tests",
    ALLOWED_IMAGE_PREFIX: PREFIX,
    ALLOWED_ORIGIN: PREFIX.replace(/\/$/, ""),
    NTFY_TOPIC: "test-topic",
    TIKTOK_CLIENT_KEY: "ck", TIKTOK_CLIENT_SECRET: "cs",
    TIKTOK_MODE: tiktokMode || "upload",
  };
  await env.DB.prepare(
    `INSERT INTO accounts (id, platform, label, category, category_id, external_id,
                           settings, enabled, created_at, updated_at)
     VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9,?9)`
  ).bind(`${platform}:1`, platform, "@test", "心理テスト", 1, "999",
         JSON.stringify(tiktokMode ? { tiktok_mode: tiktokMode } : {}), 1,
         new Date().toISOString()).run();
  await saveToken(env, `${platform}:1`, {
    accessToken: "SECRET_ACCESS_TOKEN",
    refreshToken: "SECRET_REFRESH_TOKEN",
    expiresAt: new Date(Date.now() + 50 * 24 * 3600e3).toISOString(),
  });
  const media = Array.from({ length: images }, (_, i) =>
    `${PREFIX}post_001/abc/${String(i + 1).padStart(2, "0")}.jpg`);
  return { env, media, accountId: `${platform}:1` };
}

const past = () => new Date(Date.now() - 60000).toISOString();

const request = (path, { method = "GET", body, pass = PASS } = {}) =>
  worker.fetch(new Request("https://w.dev" + path, {
    method,
    headers: { "content-type": "application/json", "x-passphrase": pass },
    body: body ? JSON.stringify(body) : undefined,
  }), CURRENT_ENV);

let CURRENT_ENV = null;

console.log("\nクラウド予約投稿（PCが止まっていても動く部分）\n" + "-".repeat(56));
fakeFetch();

// ===================================================================
// 1) 一括登録
{
  const { env, media, accountId } = await makeEnv();
  CURRENT_ENV = env;
  const jobs = Array.from({ length: 100 }, (_, i) => ({
    id: `EXP-${i}:instagram`,
    group_id: `EXP-${i}`,
    platform: "instagram",
    account_id: accountId,
    category: "心理テスト",
    sub_category: "ホテル・泊まり",
    caption: "本文",
    media,
    scheduled_at: new Date(Date.now() + i * 3600e3).toISOString(),
  }));
  const first = await (await request("/api/enqueue", { method: "POST", body: { jobs } })).json();
  check("PCから100件まとめて登録できる", first.added === 100, JSON.stringify(first).slice(0, 140));
  check("登録件数が返る（PCはこれを見て電源を切れる）", first.registered === 100);

  const again = await (await request("/api/enqueue", { method: "POST", body: { jobs } })).json();
  check("同じものを二度登録しない", again.added === 0 && again.skipped === 100,
        JSON.stringify(again).slice(0, 120));

  const evil = await (await request("/api/enqueue", { method: "POST", body: {
    jobs: [{ ...jobs[0], id: "X:instagram", group_id: "X",
             media: ["https://evil.example.com/a.jpg"] }] } })).json();
  check("自分のサイト以外の画像URLは拒否する",
        evil.rejected.length === 1 && evil.added === 0, JSON.stringify(evil).slice(0, 140));

  const bad = await (await request("/api/enqueue", { method: "POST", body: {
    jobs: [{ id: "Y", platform: "youtube", account_id: accountId,
             scheduled_at: past(), media }] } })).json();
  check("知らない投稿先は拒否する", bad.rejected.length === 1);
}

// ===================================================================
// 2) Instagram 10枚：段を分けて投稿される（無料枠の上限を超えない）
{
  const { env, media, accountId } = await makeEnv();
  CURRENT_ENV = env;
  await enqueue(env, { id: "J1", group_id: "G1", platform: "instagram",
                       account_id: accountId, caption: "本文", media,
                       scheduled_at: past() });

  const perTick = [];
  let job = null;
  for (let round = 0; round < 5; round += 1) {
    calls = [];
    const outcome = await tick(env);
    perTick.push(calls.length);
    job = await env.DB.prepare("SELECT * FROM jobs WHERE id = ?1").bind("J1").first();
    if (job.status === "posted") break;
  }
  check("Instagramのカルーセルを投稿できる", job.status === "posted",
        `${job.status} / ${job.error}`);
  check("投稿IDが記録される", job.external_post_id === "PUB_1", job.external_post_id);
  check("1回のCronで外部リクエストが50件を超えない",
        Math.max(...perTick) <= 50, `最大 ${Math.max(...perTick)}件`);
  check("段に分かれて進む（1回で全部やらない）", perTick.filter((n) => n > 0).length >= 3,
        perTick.join(" / "));
  check("成功をスマホへ通知する", notified.length >= 1, `${notified.length}件`);
  check("通知に本文が含まれ、トークンは含まれない",
        notified.some((n) => String(n.body).includes("instagram"))
        && !notified.some((n) => String(n.body).includes("SECRET_ACCESS_TOKEN")));
}

// ===================================================================
// 3) 出来上がるまで待つ（まだ FINISHED でないとき）
{
  const { env, media, accountId } = await makeEnv();
  CURRENT_ENV = env;
  containerReady = false;
  await enqueue(env, { id: "J2", group_id: "G2", platform: "instagram",
                       account_id: accountId, caption: "x", media, scheduled_at: past() });
  await tick(env);   // 子コンテナを作る
  await tick(env);   // まだ出来ていない
  let job = await env.DB.prepare("SELECT * FROM jobs WHERE id = ?1").bind("J2").first();
  check("出来上がる前に公開しない", job.status === "processing" && !job.external_post_id,
        `${job.status} / ${job.external_post_id}`);
  check("待つあいだは次の試行時刻が入る", Boolean(job.next_attempt_at));
  containerReady = true;
}

// ===================================================================
// 4) 予約時刻より前は動かさない
{
  const { env, media, accountId } = await makeEnv();
  CURRENT_ENV = env;
  await enqueue(env, { id: "J3", group_id: "G3", platform: "instagram",
                       account_id: accountId, caption: "x", media,
                       scheduled_at: new Date(Date.now() + 3600e3).toISOString() });
  calls = [];
  const outcome = await tick(env);
  check("予約時刻より前の投稿は動かさない", outcome.picked === 0 && calls.length === 0);
  const job = await env.DB.prepare("SELECT * FROM jobs WHERE id = ?1").bind("J3").first();
  check("予約のまま残る", job.status === "scheduled");
}

// ===================================================================
// 5) 二重実行の防止（同じ回が2つ走っても1つしか掴めない）
{
  const { env, media, accountId } = await makeEnv();
  CURRENT_ENV = env;
  await enqueue(env, { id: "J4", group_id: "G4", platform: "instagram",
                       account_id: accountId, caption: "x", media, scheduled_at: past() });
  const [a, b] = await Promise.all([claimNext(env, 1), claimNext(env, 1)]);
  check("同時に走っても同じ投稿を2つが掴まない",
        (a.length + b.length) === 1, `${a.length} + ${b.length}`);
}

// ===================================================================
// 6) 失敗の扱い：再試行するものと、しないもの
{
  // 権限不足 → 再試行しない
  const { env, media, accountId } = await makeEnv();
  CURRENT_ENV = env;
  await enqueue(env, { id: "J5", group_id: "G5", platform: "instagram",
                       account_id: accountId, caption: "x", media, scheduled_at: past() });
  failWith = { body: { error: { code: 10, message: "権限がありません" } }, status: 400 };
  notified = [];
  await tick(env);
  let job = await env.DB.prepare("SELECT * FROM jobs WHERE id = ?1").bind("J5").first();
  check("権限不足は再試行しない", job.status === "failed" && job.error_kind === "permission",
        `${job.status} / ${job.error_kind}`);
  check("失敗をスマホへ通知する", notified.length >= 1);
  check("投稿内容は消さずに残る", Boolean(job.caption && job.media));

  // レート制限 → 再試行する
  failWith = { body: { error: { code: 4, message: "レート制限" } }, status: 400 };
  await enqueue(env, { id: "J6", group_id: "G6", platform: "instagram",
                       account_id: accountId, caption: "x", media, scheduled_at: past() });
  await tick(env);
  job = await env.DB.prepare("SELECT * FROM jobs WHERE id = ?1").bind("J6").first();
  check("レート制限は再試行する", job.status === "retrying" && job.error_kind === "retryable",
        `${job.status} / ${job.error_kind}`);
  check("次の試行時刻が入る", Boolean(job.next_attempt_at));

  // 回数を使い切ったら failed
  for (let i = 0; i < 6; i += 1) {
    await env.DB.prepare(
      "UPDATE jobs SET status = ?1, attempts = ?2, next_attempt_at = ?3, error = ?4, error_kind = ?5, lease_until = NULL, updated_at = ?6 WHERE id = ?7"
    ).bind("retrying", i, null, "", "retryable", new Date().toISOString(), "J6").run();
    await tick(env);
  }
  job = await env.DB.prepare("SELECT * FROM jobs WHERE id = ?1").bind("J6").first();
  check("再試行の回数に上限がある（無限に繰り返さない）", job.status === "failed",
        `${job.status} / ${job.attempts}回`);
  failWith = null;
}

// ===================================================================
// 7) トークンの自動更新（PCが止まっていても切れない）
{
  const { env, accountId } = await makeEnv();
  CURRENT_ENV = env;
  await saveToken(env, accountId, {
    accessToken: "OLD_TOKEN", refreshToken: "R",
    expiresAt: new Date(Date.now() + 3 * 24 * 3600e3).toISOString(),
  });
  // 発行から24時間以上経ったことにする
  await env.DB.prepare(
    "INSERT INTO tokens (account_id, access_token, refresh_token, expires_at, refreshed_at, scope, updated_at) VALUES (?1,?2,?3,?4,?5,?6,?7)"
  ).bind(accountId,
         (await env.DB.prepare("SELECT * FROM tokens WHERE account_id = ?1").bind(accountId).first()).access_token,
         "", new Date(Date.now() + 3 * 24 * 3600e3).toISOString(),
         new Date(Date.now() - 48 * 3600e3).toISOString(), "", new Date().toISOString()).run();
  const before = await env.DB.prepare("SELECT * FROM tokens WHERE account_id = ?1")
    .bind(accountId).first();
  const outcome = await tick(env);     // 投稿が無いのでトークンの面倒を見る
  const after = await env.DB.prepare("SELECT * FROM tokens WHERE account_id = ?1")
    .bind(accountId).first();
  check("期限が近いトークンを自動で更新する",
        outcome.refreshed === 1 && after.access_token !== before.access_token,
        `更新 ${outcome.refreshed}件`);
  check("トークンは暗号化して保存される",
        !after.access_token.includes("NEW_TOKEN") && after.access_token.includes("."),
        after.access_token.slice(0, 30));
}

// ===================================================================
// 8) TikTok：下書き転送は「本人の操作が必要」として記録する
{
  const { env, media, accountId } = await makeEnv({ platform: "tiktok" });
  CURRENT_ENV = env;
  notified = [];
  await enqueue(env, { id: "T1", group_id: "GT", platform: "tiktok",
                       account_id: accountId, caption: "x", media, scheduled_at: past() });
  await tick(env);
  await tick(env);
  const job = await env.DB.prepare("SELECT * FROM jobs WHERE id = ?1").bind("T1").first();
  check("TikTokへ下書き転送できる", job.status === "posted", `${job.status} / ${job.error}`);
  const events = await env.DB.prepare(
    "SELECT at, level, message FROM job_events WHERE job_id = ?1 ORDER BY id DESC LIMIT 30"
  ).bind("T1").all();
  check("アプリでの公開が必要だと記録される",
        events.results.some((e) => e.message.includes("アプリで公開")),
        JSON.stringify(events.results).slice(0, 160));
  check("その旨をスマホへ通知する",
        notified.some((n) => String(n.body).includes("アプリで公開")));
}

// ===================================================================
// 9) スマホからの操作
{
  const { env, media, accountId } = await makeEnv();
  CURRENT_ENV = env;
  const later = new Date(Date.now() + 7200e3).toISOString();
  await enqueue(env, { id: "S1", group_id: "GS", platform: "instagram",
                       account_id: accountId, caption: "x", media, scheduled_at: later });

  const status = await (await request("/api/status")).json();
  check("スマホから状況を取れる", status.ok && status.next?.id === "S1",
        JSON.stringify(status.counts));
  check("件数の内訳が出る", status.counts.scheduled === 1);
  check("SNS別の状態が出る", status.per_platform.length >= 1);
  check("アカウント別の状態が出る", status.per_account.length >= 1);

  const when = new Date(Date.now() + 10800e3).toISOString();
  const moved = await (await request("/api/action", { method: "POST",
    body: { action: "reschedule", id: "S1", when } })).json();
  check("予約時刻を変えられる", moved.ok === true, JSON.stringify(moved));

  const cancelled = await (await request("/api/action", { method: "POST",
    body: { action: "cancel", id: "S1" } })).json();
  check("投稿を取り消せる", cancelled.ok === true);
  let job = await env.DB.prepare("SELECT * FROM jobs WHERE id = ?1").bind("S1").first();
  check("取り消しても内容は残る", job.status === "cancelled" && Boolean(job.caption));

  const revived = await (await request("/api/action", { method: "POST",
    body: { action: "retry", id: "S1" } })).json();
  check("取り消したものを戻せる", revived.ok === true);

  await (await request("/api/action", { method: "POST",
    body: { action: "post_now", id: "S1" } })).json();
  job = await env.DB.prepare("SELECT * FROM jobs WHERE id = ?1").bind("S1").first();
  check("いますぐ投稿にできる", job.scheduled_at <= new Date().toISOString());

  const unknown = await request("/api/action", { method: "POST",
    body: { action: "explode", id: "S1" } });
  check("知らない操作は拒否する", unknown.status === 400);
}

// ===================================================================
// 10) 画面と認証
{
  const { env } = await makeEnv();
  CURRENT_ENV = env;
  const page = await worker.fetch(new Request("https://w.dev/"), env);
  const html = await page.text();
  check("スマホの管理画面が返る", page.status === 200 && html.includes("予約投稿の状況"));
  check("スマホ向けの指定がある", html.includes("width=device-width"));
  check("画面にトークンが出てこない",
        !html.includes("SECRET_ACCESS_TOKEN") && !html.includes("TOKEN_KEY"));

  const denied = await request("/api/status", { pass: "wrong" });
  check("合言葉が違えば状況を見せない", denied.status === 401);

  const health = await (await worker.fetch(new Request("https://w.dev/health"), env)).json();
  check("健康確認でQueueの有無が分かる", health.cloud_queue === true);
}

// ===================================================================
// 11) PCが結果を取りに来る（二重投稿の防止）
{
  const { env, media, accountId } = await makeEnv();
  CURRENT_ENV = env;
  await enqueue(env, { id: "R1", group_id: "GR", platform: "instagram",
                       account_id: accountId, caption: "x", media, scheduled_at: past() });
  for (let i = 0; i < 4; i += 1) await tick(env);
  const got = await (await request("/api/results")).json();
  check("PCがクラウドの投稿結果を取れる",
        got.items.some((r) => r.id === "R1" && r.status === "posted"),
        JSON.stringify(got.items).slice(0, 140));
  check("結果にトークンが含まれない", !JSON.stringify(got).includes("SECRET_ACCESS_TOKEN"));

  const accounts = await (await request("/api/accounts")).json();
  check("アカウント一覧に期限は出るが値は出ない",
        accounts.accounts[0].has_token === true
        && !JSON.stringify(accounts).includes("SECRET_ACCESS_TOKEN"));
}

// ===================================================================
// 12) 反応データをクラウド側で定期取得する（PCが止まっていても集まる）
{
  const { env, media, accountId } = await makeEnv();
  CURRENT_ENV = env;
  await enqueue(env, { id: "M1", group_id: "GM", platform: "instagram",
                       account_id: accountId, caption: "x", media, scheduled_at: past(),
                       category: "心理テスト", sub_category: "7" });
  for (let i = 0; i < 4; i += 1) await tick(env);
  let job = await env.DB.prepare("SELECT * FROM jobs WHERE id = ?1").bind("M1").first();
  check("投稿してから測る", job.status === "posted", job.status);

  // まだ24時間経っていないので測らない
  let outcome = await tick(env);
  check("24時間経つ前は測らない", (outcome.collected || 0) === 0);

  // 25時間前に投稿したことにする
  await env.DB.prepare(
    "UPDATE jobs SET status = ?1, external_post_id = ?2, external_url = ?3, api_response = ?4, posted_at = ?5, lease_until = NULL, stage = '', error = '', error_kind = '', updated_at = ?5 WHERE id = ?6"
  ).bind("posted", "PUB_1", "", "{}",
         new Date(Date.now() - 25 * 3600e3).toISOString(), "M1").run();

  globalThis.fetch = async (url) => {
    if (String(url).includes("insights")) {
      return new Response(JSON.stringify({ data: [
        { name: "views", total_value: { value: 1200 } },
        { name: "reach", total_value: { value: 950 } },
        { name: "likes", total_value: { value: 30 } },
      ] }));
    }
    return new Response(JSON.stringify({ ok: true }));
  };
  outcome = await tick(env);
  const metrics = env.DB._tables.metrics;
  check("24時間後の反応データを取る", (outcome.collected || 0) === 1 && metrics.length === 1,
        `取得 ${outcome.collected} / 行 ${metrics.length}`);
  check("実測した数字が入る", metrics[0].views === 1200 && metrics[0].reach === 950,
        JSON.stringify(metrics[0]).slice(0, 120));
  check("取れなかった指標は空のまま", metrics[0].saves === null);
  check("計測区分と経過時間が残る",
        metrics[0].snapshot === "24h" && metrics[0].late === 0
        && metrics[0].hours_since_post >= 24, JSON.stringify(metrics[0]).slice(0, 140));
  check("SubCategoryも残る（カテゴリ別の分析に使う）", metrics[0].sub_category === "7");

  await tick(env);
  check("同じ区分を二度測らない", env.DB._tables.metrics.length === 1);

  // 空の応答は「測定済み」にしない
  const second = await makeEnv();
  CURRENT_ENV = second.env;
  await enqueue(second.env, { id: "M2", group_id: "GM2", platform: "instagram",
                              account_id: second.accountId, caption: "x",
                              media: second.media, scheduled_at: past() });
  fakeFetch();
  for (let i = 0; i < 4; i += 1) await tick(second.env);
  await second.env.DB.prepare(
    "UPDATE jobs SET status = ?1, external_post_id = ?2, external_url = ?3, api_response = ?4, posted_at = ?5, lease_until = NULL, stage = '', error = '', error_kind = '', updated_at = ?5 WHERE id = ?6"
  ).bind("posted", "PUB_1", "", "{}",
         new Date(Date.now() - 25 * 3600e3).toISOString(), "M2").run();
  globalThis.fetch = async () => new Response(JSON.stringify({ data: [] }));
  await tick(second.env);
  check("空の応答を測定済みにしない", second.env.DB._tables.metrics.length === 0);
  check("取得できなかったことは記録する",
        second.env.DB._tables.metric_attempts.some((a) => a.outcome === "failed"));
  fakeFetch();

  // PCが取りに来られる
  CURRENT_ENV = env;
  const exported = await (await request("/api/metrics")).json();
  check("PCが反応データを取りに来られる", exported.items.length === 1,
        JSON.stringify(exported).slice(0, 120));
}

// ===================================================================
console.log("-".repeat(56));
const failed = results.filter((r) => !r.ok);
if (failed.length) {
  console.log(`失敗 ${failed.length}件`);
  for (const item of failed) console.log(`  - ${item.name}: ${item.detail}`);
  process.exit(1);
}
console.log(`すべて通りました（${results.length}件）`);
