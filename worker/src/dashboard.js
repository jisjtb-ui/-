/**
 * スマホから見る管理画面。Worker自身が返すので、PCが止まっていても開ける。
 *
 * 合言葉は画面で1度入れて、端末の中（localStorage）に覚える。
 * 画面のJSにトークンは一切出てこない（投稿はWorkerの中だけで行う）。
 */

export function dashboardHtml() {
  return `<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>予約投稿の状況</title>
<style>
  :root {
    --bg: #fbfaf8; --card: #fff; --line: #e6e2dc; --text: #24211e;
    --muted: #7b736a; --accent: #b4763f;
    --ok: #2f7d4f; --warn: #b3541e; --bad: #a32f2f;
  }
  @media (prefers-color-scheme: dark) {
    :root { --bg:#171513; --card:#201d1a; --line:#332e29; --text:#f0ece7;
            --muted:#a49a8f; --accent:#d69a62; }
  }
  * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
  body { margin:0; background:var(--bg); color:var(--text); font-size:16px;
         font-family:-apple-system,BlinkMacSystemFont,"Hiragino Sans","Noto Sans JP",sans-serif; }
  header { position:sticky; top:0; background:var(--bg); padding:14px 16px 10px;
           border-bottom:1px solid var(--line); z-index:5; }
  h1 { margin:0; font-size:17px; letter-spacing:.02em; }
  .sub { color:var(--muted); font-size:12px; margin-top:3px; }
  main { padding:16px; max-width:720px; margin:0 auto; }
  .tiles { display:grid; grid-template-columns:repeat(auto-fit,minmax(88px,1fr)); gap:8px; }
  .tile { background:var(--card); border:1px solid var(--line); border-radius:12px;
          padding:10px 8px; text-align:center; }
  .tile b { display:block; font-size:22px; font-variant-numeric:tabular-nums; }
  .tile span { font-size:11px; color:var(--muted); }
  section { margin-top:22px; }
  h2 { font-size:13px; color:var(--muted); margin:0 0 8px; font-weight:600; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:12px;
          padding:12px; margin-bottom:8px; }
  .row { display:flex; justify-content:space-between; gap:10px; align-items:baseline; }
  .when { font-variant-numeric:tabular-nums; font-size:14px; }
  .who { font-size:12px; color:var(--muted); margin-top:2px; }
  .pill { font-size:11px; padding:2px 8px; border-radius:999px; border:1px solid var(--line);
          white-space:nowrap; }
  .posted { color:var(--ok); border-color:var(--ok); }
  .failed { color:var(--bad); border-color:var(--bad); }
  .retrying, .processing { color:var(--warn); border-color:var(--warn); }
  .err { color:var(--bad); font-size:12px; margin-top:6px; word-break:break-word; }
  .acts { display:flex; flex-wrap:wrap; gap:6px; margin-top:10px; }
  button { font:inherit; font-size:13px; padding:9px 12px; border-radius:9px;
           border:1px solid var(--line); background:var(--bg); color:var(--text);
           min-height:40px; cursor:pointer; }
  button.main { background:var(--accent); border-color:var(--accent); color:#fff; }
  input { font:inherit; padding:10px; border-radius:9px; border:1px solid var(--line);
          background:var(--card); color:var(--text); width:100%; }
  .gate { max-width:360px; margin:60px auto; padding:0 16px; }
  .note { color:var(--muted); font-size:12px; margin-top:10px; line-height:1.6; }
  .empty { color:var(--muted); font-size:13px; }
  table { width:100%; border-collapse:collapse; font-size:13px; }
  td { padding:5px 0; border-bottom:1px solid var(--line); }
  td:last-child { text-align:right; font-variant-numeric:tabular-nums; }
</style>
</head>
<body>
<div id="gate" class="gate" hidden>
  <h1>予約投稿の状況</h1>
  <p class="note">合言葉を入れてください。この端末にだけ保存されます。</p>
  <input id="pass" type="password" inputmode="text" autocomplete="current-password"
         placeholder="合言葉">
  <div class="acts"><button class="main" id="enter">開く</button></div>
  <p class="note" id="gate-error"></p>
</div>

<div id="app" hidden>
  <header>
    <h1>予約投稿の状況</h1>
    <div class="sub" id="stamp">読み込み中…</div>
  </header>
  <main>
    <div class="tiles" id="tiles"></div>

    <section>
      <h2>次の投稿</h2>
      <div id="next"></div>
    </section>

    <section>
      <h2>本日の予約</h2>
      <div id="today"></div>
    </section>

    <section>
      <h2>失敗（要対応）</h2>
      <div id="failed"></div>
    </section>

    <section>
      <h2>SNS別</h2>
      <div class="card"><table id="per-platform"></table></div>
    </section>

    <section>
      <h2>アカウント別</h2>
      <div class="card"><table id="per-account"></table></div>
    </section>

    <section>
      <h2>投稿済み</h2>
      <div id="posted"></div>
    </section>

    <div class="acts">
      <button id="reload">更新</button>
      <button id="forget">合言葉を消す</button>
    </div>
    <p class="note">PCの電源が入っていなくても、この画面とクラウドの投稿処理は動きます。</p>
  </main>
</div>

<script>
const KEY = "honeshinri-pass";
const $ = (id) => document.getElementById(id);
let pass = localStorage.getItem(KEY) || "";

const LABEL = { draft:"下書き", scheduled:"予約", processing:"処理中", posted:"投稿済み",
                failed:"失敗", retrying:"再試行", cancelled:"取消" };

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { "x-passphrase": pass, "content-type": "application/json", ...(options.headers||{}) },
  });
  if (response.status === 401) { forget("合言葉が違います"); throw new Error("401"); }
  return response.json();
}

function forget(message = "") {
  localStorage.removeItem(KEY); pass = "";
  $("app").hidden = true; $("gate").hidden = false;
  $("gate-error").textContent = message;
}

const fmt = (iso) => {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const today = new Date().toDateString() === d.toDateString();
  const time = d.toLocaleTimeString("ja-JP", { hour:"2-digit", minute:"2-digit" });
  return today ? time : d.toLocaleDateString("ja-JP",{month:"numeric",day:"numeric"}) + " " + time;
};

function jobCard(job, actions) {
  const card = document.createElement("div");
  card.className = "card";
  const who = [job.platform, job.category || job.account_id, job.sub_category]
    .filter(Boolean).join(" / ");
  card.innerHTML =
    '<div class="row"><div><div class="when">' + fmt(job.scheduled_at) + '</div>'
    + '<div class="who"></div></div>'
    + '<span class="pill ' + job.status + '">' + (LABEL[job.status]||job.status)
    + (job.attempts ? " " + job.attempts + "回" : "") + '</span></div>';
  card.querySelector(".who").textContent = who;
  if (job.error) {
    const err = document.createElement("div");
    err.className = "err";
    err.textContent = job.error;
    card.appendChild(err);
  }
  if (actions && actions.length) {
    const box = document.createElement("div");
    box.className = "acts";
    for (const [label, action, main] of actions) {
      const button = document.createElement("button");
      button.textContent = label;
      if (main) button.className = "main";
      button.onclick = () => act(action, job);
      box.appendChild(button);
    }
    card.appendChild(box);
  }
  return card;
}

async function act(action, job) {
  let when;
  if (action === "reschedule") {
    const answer = prompt("新しい予約時刻（例 2026-09-25 21:00）", fmt(job.scheduled_at));
    if (!answer) return;
    const parsed = new Date(answer.replace(" ", "T"));
    if (Number.isNaN(parsed.getTime())) { alert("時刻を読み取れません"); return; }
    when = parsed.toISOString();
  }
  if (action === "cancel" && !confirm("この投稿を取り消しますか？")) return;
  if (action === "post_now" && !confirm("いますぐ投稿しますか？")) return;
  const result = await api("/api/action", {
    method: "POST", body: JSON.stringify({ action, id: job.id, when }),
  });
  if (!result.ok) alert(result.error || "できませんでした");
  load();
}

function fill(target, jobs, actions, emptyText) {
  const box = $(target);
  box.textContent = "";
  if (!jobs.length) {
    const p = document.createElement("p");
    p.className = "empty"; p.textContent = emptyText;
    box.appendChild(p);
    return;
  }
  for (const job of jobs) box.appendChild(jobCard(job, actions));
}

function table(target, rows) {
  const box = $(target);
  box.textContent = "";
  if (!rows.length) { box.innerHTML = '<tr><td class="empty">まだありません</td></tr>'; return; }
  for (const row of rows) {
    const tr = document.createElement("tr");
    const left = document.createElement("td");
    left.textContent = row.left;
    const right = document.createElement("td");
    right.textContent = row.right;
    tr.append(left, right);
    box.appendChild(tr);
  }
}

async function load() {
  const data = await api("/api/status");
  $("app").hidden = false; $("gate").hidden = true;
  $("stamp").textContent = "更新 " + fmt(data.server_time) + "　合計 " + data.total + "件";

  const order = ["scheduled","retrying","processing","posted","failed","cancelled"];
  $("tiles").textContent = "";
  for (const key of order) {
    const tile = document.createElement("div");
    tile.className = "tile";
    tile.innerHTML = "<b></b><span></span>";
    tile.querySelector("b").textContent = data.counts[key] || 0;
    tile.querySelector("span").textContent = LABEL[key];
    $("tiles").appendChild(tile);
  }

  fill("next", data.next ? [data.next] : [],
       [["いますぐ投稿","post_now",true],["時刻変更","reschedule"],["取消","cancel"]],
       "予約はありません");
  fill("today", data.today,
       [["時刻変更","reschedule"],["取消","cancel"]], "本日の予約はありません");
  fill("failed", data.failed, [["もう一度試す","retry",true]], "失敗はありません");
  fill("posted", data.posted, [], "まだありません");

  const platformRows = data.per_platform.map((r) => ({
    left: r.platform + "　" + (LABEL[r.status]||r.status), right: r.n + "件" }));
  table("per-platform", platformRows);
  const accountRows = data.per_account.map((r) => ({
    left: (r.label || r.account_id) + "　" + (LABEL[r.status]||r.status), right: r.n + "件" }));
  table("per-account", accountRows);
}

$("enter").onclick = async () => {
  pass = $("pass").value.trim();
  if (!pass) return;
  localStorage.setItem(KEY, pass);
  try { await load(); } catch { /* forget() が出す */ }
};
$("pass").addEventListener("keydown", (e) => { if (e.key === "Enter") $("enter").click(); });
$("reload").onclick = () => load();
$("forget").onclick = () => forget("消しました");

if (pass) { load().catch(() => {}); } else { $("gate").hidden = false; }
setInterval(() => { if (pass && !document.hidden) load().catch(() => {}); }, 60000);
</script>
</body>
</html>`;
}
