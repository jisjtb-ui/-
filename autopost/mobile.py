"""スマホから投稿するための一覧ページを書き出す。

画像はすでに Cloudflare Pages へ公開されている（APIが公開URLを要求するため）。
そこに一覧ページを1枚置けば、スマホのブラウザで開いて

  キャプションをコピー → 画像をまとめて写真に保存 → アプリで投稿

までを、PCとスマホをケーブルでつながずに行える。

同じドメインに置くのが必須。画像を取得して共有シートへ渡すため、
別ドメインだとブラウザに止められる。

公開範囲について:
  Pagesは誰でも見られる。URLを知られると予定の投稿が全部見えるので、
  推測しにくいフォルダ名の下に置き、検索避けも入れる。
"""

from __future__ import annotations

import json
import secrets
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from .config import Settings
from .experiments import DELIVERED, ExperimentStore
from .version import APP_ROOT, current_version

LogFn = Callable[[str], None]

# このページかどうかを見分ける印（Pagesの404代替ページと区別するため）
PAGE_MARKER = "data-honeshinri-mobile"

SLUG_FILE = APP_ROOT / ".mobile_slug"
ROBOTS = "User-agent: *\nDisallow: /\n"


def page_slug() -> str:
    """推測しにくいフォルダ名。一度作ったら使い回す。"""
    if SLUG_FILE.is_file():
        saved = SLUG_FILE.read_text(encoding="utf-8").strip()
        if saved:
            return saved
    slug = "m-" + secrets.token_hex(8)
    SLUG_FILE.write_text(slug + "\n", encoding="utf-8")
    return slug


# Workerから投稿できるチャネル（Reelは音源を手で付けるため対象外）
API_PLATFORMS = ("threads", "instagram")


@dataclass
class Card:
    experiment_id: str
    post_id: str
    caption: str
    images: list[str]
    video: str = ""
    scheduled_at: str = ""
    platforms: str = ""
    done: bool = False
    api_platforms: list[str] = None  # type: ignore[assignment]


def _collect(settings: Settings, store: ExperimentStore, destination: Path,
             slug: str, log: LogFn) -> list[Card]:
    cards: list[Card] = []
    reel_base = Path(settings.reel_output_dir)
    if not reel_base.is_absolute():
        reel_base = APP_ROOT / reel_base

    for experiment in store.list_experiments(limit=500):
        extra = experiment.extra or {}
        urls = extra.get("image_urls") or []
        if not urls or not experiment.source_post_id:
            continue

        publications = store.publications(experiment_id=experiment.experiment_id)
        if publications and all(p.status in DELIVERED for p in publications):
            done = True
        else:
            done = False
        scheduled = sorted(p.scheduled_at for p in publications if p.scheduled_at)
        platforms = ", ".join(sorted({p.platform for p in publications}))

        # Reelの動画があれば、スマホから落とせるよう一緒に公開する
        video_url = ""
        source_video = reel_base / experiment.source_post_id / "reel.mp4"
        if source_video.is_file():
            target = destination / experiment.source_post_id / "reel.mp4"
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.is_file() or target.stat().st_size != source_video.stat().st_size:
                shutil.copy2(source_video, target)
                log(f"  動画を公開用にコピー: {experiment.source_post_id}/reel.mp4")
            prefix = urls[0].rsplit("/", 3)[0]
            video_url = f"{prefix}/{experiment.source_post_id}/reel.mp4"

        pending_api = sorted(
            {p.platform for p in publications
             if p.platform in API_PLATFORMS and p.status not in DELIVERED}
        )
        cards.append(
            Card(
                api_platforms=pending_api,
                experiment_id=experiment.experiment_id,
                post_id=experiment.source_post_id,
                caption=experiment.text or "",
                images=list(urls),
                video=video_url,
                scheduled_at=scheduled[0][:16].replace("T", " ") if scheduled else "",
                platforms=platforms,
                done=done,
            )
        )
    return cards


def build(settings: Settings, destination: Path | None = None,
          log: LogFn = print) -> dict:
    """一覧ページを書き出し、開くべきURLを返す。"""
    destination = Path(destination or "pages_media")
    if not destination.is_absolute():
        destination = APP_ROOT / destination
    destination.mkdir(parents=True, exist_ok=True)

    slug = page_slug()
    store = ExperimentStore(settings.experiments_db_path)
    cards = _collect(settings, store, destination, slug, log)

    folder = destination / slug
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "index.html").write_text(
        _render(cards, settings.publish_worker_url), encoding="utf-8"
    )
    (destination / "robots.txt").write_text(ROBOTS, encoding="utf-8")

    base = (settings.local_host_base_url or "").rstrip("/")
    url = f"{base}/{slug}/" if base else f"（{folder}）"
    waiting = sum(1 for c in cards if not c.done)
    log(f"{len(cards)}件を書き出しました（未投稿 {waiting}件）")
    return {"url": url, "slug": slug, "count": len(cards), "folder": str(folder)}


def verify_published(url: str, log: LogFn = print) -> bool:
    """作ったページが本当に公開されているかを確かめる。

    Cloudflare Pages は存在しないパスにも置いてあるHTMLを 200 で返すため、
    「開ける」ことは何の保証にもならない。中身が自分のページかまで見る。
    """
    import requests

    if not url.startswith("http"):
        return False
    try:
        response = requests.get(url, timeout=20)
    except requests.RequestException as exc:
        log(f"  確認できませんでした（{type(exc).__name__}）")
        return False

    if response.status_code != 200:
        log(f"  まだ公開されていません（HTTP {response.status_code}）")
        return False
    if PAGE_MARKER not in response.text:
        log("  このURLには別のページが表示されています（未デプロイ）")
        return False
    log("  公開されています")
    return True


# ----------------------------------------------------------------------
def sync(settings: Settings, log: LogFn = print) -> dict:
    """スマホから投稿されたものをPC側へ取り込む。

    これを行わないと、スマホで投稿したものをPCの予約投稿がもう一度出してしまう。
    """
    import requests

    from .experiments import PUBLISHED

    if not settings.publish_worker_url:
        log("PUBLISH_WORKER_URL が未設定です（スマホからの投稿を使っていなければ不要）")
        return {"marked": 0}
    if not settings.publish_passphrase:
        log("[エラー] PUBLISH_PASSPHRASE が未設定です")
        return {"marked": 0, "error": "no passphrase"}

    url = f"{settings.publish_worker_url}/published"
    try:
        response = requests.get(
            url, headers={"x-passphrase": settings.publish_passphrase}, timeout=30
        )
    except requests.RequestException as exc:
        log(f"[エラー] 取得できませんでした（{type(exc).__name__}）")
        return {"marked": 0, "error": str(exc)}

    if response.status_code == 401:
        log("[エラー] 合言葉が違います（.env の PUBLISH_PASSPHRASE を確認してください）")
        return {"marked": 0, "error": "unauthorized"}
    if response.status_code != 200:
        log(f"[エラー] 取得できませんでした（HTTP {response.status_code}）")
        return {"marked": 0, "error": f"http {response.status_code}"}

    items = (response.json() or {}).get("items") or {}
    store = ExperimentStore(settings.experiments_db_path)
    marked = 0
    for experiment_id, record in items.items():
        for platform, detail in ((record or {}).get("platforms") or {}).items():
            publication = store.publication(experiment_id, platform)
            if publication is None or publication.status in DELIVERED:
                continue
            store.mark_published(
                publication.id, str(detail.get("id", "")), "",
                {"published_from": "mobile"}, PUBLISHED,
            )
            store.log(experiment_id, "スマホから投稿されたため取り込みました", platform)
            log(f"  取り込み: {experiment_id} / {platform}")
            marked += 1

    log(f"スマホからの投稿 {marked}件を取り込みました" if marked
        else "取り込むものはありませんでした")
    return {"marked": marked}


# ----------------------------------------------------------------------
def _render(cards: list[Card], worker_url: str = "") -> str:
    payload = json.dumps(
        [
            {
                "id": c.experiment_id,
                "post": c.post_id,
                "caption": c.caption,
                "images": c.images,
                "video": c.video,
                "scheduled": c.scheduled_at,
                "platforms": c.platforms,
                "done": c.done,
                "api": c.api_platforms or [],
            }
            for c in cards
        ],
        ensure_ascii=False,
    )
    built = datetime.now().strftime("%Y-%m-%d %H:%M")
    return (
        TEMPLATE.replace("__DATA__", payload)
        .replace("__BUILT__", built)
        .replace("__VERSION__", current_version())
        .replace("__WORKER__", json.dumps(worker_url.rstrip("/")))
    )


TEMPLATE = """<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="robots" content="noindex, nofollow">
<title>投稿する</title>
<style>
  :root {
    --bg: #fbfbfb; --card: #ffffff; --ink: #1a1a1a; --sub: #767676;
    --line: #e6e6e6; --accent: #1a1a1a; --ok: #167c4a; --warn: #9a6b00;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #141414; --card: #1e1e1e; --ink: #f2f2f2; --sub: #9a9a9a;
      --line: #333; --accent: #f2f2f2; --ok: #4ec98a; --warn: #e0b24d;
    }
  }
  * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
  body {
    margin: 0; background: var(--bg); color: var(--ink);
    font-family: -apple-system, BlinkMacSystemFont, "Hiragino Sans", "Noto Sans JP", sans-serif;
    padding: 0 16px calc(32px + env(safe-area-inset-bottom));
    line-height: 1.6;
  }
  header { padding: 24px 0 8px; }
  h1 { font-size: 20px; margin: 0 0 4px; letter-spacing: .02em; }
  .meta { color: var(--sub); font-size: 12px; }
  .filters { display: flex; gap: 8px; margin: 16px 0 8px; }
  .filters button {
    flex: 1; padding: 8px; font-size: 13px; border-radius: 8px;
    border: 1px solid var(--line); background: var(--card); color: var(--sub);
  }
  .filters button[aria-pressed="true"] { color: var(--ink); border-color: var(--accent); font-weight: 600; }
  .card {
    background: var(--card); border: 1px solid var(--line); border-radius: 14px;
    padding: 14px; margin: 12px 0;
  }
  .card.done { opacity: .5; }
  .head { display: flex; gap: 12px; align-items: center; }
  .thumb {
    width: 62px; height: 82px; object-fit: cover; border-radius: 8px;
    border: 1px solid var(--line); background: #fff; flex: none;
  }
  .title { font-size: 14px; font-weight: 600; }
  .when { font-size: 12px; color: var(--sub); }
  .tag {
    display: inline-block; font-size: 11px; padding: 1px 7px; border-radius: 999px;
    border: 1px solid var(--line); color: var(--sub); margin-right: 4px;
  }
  .tag.done { color: var(--ok); border-color: var(--ok); }
  .caption {
    white-space: pre-wrap; font-size: 13px; margin: 12px 0 0;
    padding: 10px; background: var(--bg); border-radius: 8px;
    max-height: 5.4em; overflow: hidden; position: relative;
  }
  .caption.open { max-height: none; }
  .actions { display: grid; gap: 8px; margin-top: 12px; }
  button.act {
    width: 100%; padding: 13px; font-size: 14px; font-weight: 600;
    border-radius: 10px; border: 1px solid var(--accent);
    background: var(--accent); color: var(--card);
  }
  button.act.ghost { background: transparent; color: var(--ink); }
  button.act:disabled { opacity: .45; }
  .note { font-size: 11px; color: var(--sub); margin-top: 6px; }
  .grid { display: grid; grid-template-columns: repeat(5, 1fr); gap: 6px; margin-top: 10px; }
  .grid img { width: 100%; border-radius: 4px; border: 1px solid var(--line); background:#fff; }
  .empty { text-align: center; color: var(--sub); padding: 48px 0; font-size: 14px; }
  .toast {
    position: fixed; left: 50%; transform: translateX(-50%);
    bottom: calc(20px + env(safe-area-inset-bottom));
    background: var(--ink); color: var(--bg); padding: 10px 18px;
    border-radius: 999px; font-size: 13px; opacity: 0; pointer-events: none;
    transition: opacity .2s; z-index: 10;
  }
  .toast.show { opacity: 1; }
</style>
</head>
<body data-honeshinri-mobile="1">
<header>
  <h1>投稿する</h1>
  <div class="meta">作成 __BUILT__ ・ v__VERSION__</div>
</header>

<div class="filters">
  <button id="f-todo" aria-pressed="true">未投稿</button>
  <button id="f-all" aria-pressed="false">すべて</button>
</div>

<div id="list"></div>
<div class="toast" id="toast"></div>

<script>
const DATA = __DATA__;
const WORKER = __WORKER__;
const list = document.getElementById("list");
const toastEl = document.getElementById("toast");
let showAll = false;

function toast(message) {
  toastEl.textContent = message;
  toastEl.classList.add("show");
  setTimeout(() => toastEl.classList.remove("show"), 2200);
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    toast("キャプションをコピーしました");
  } catch (e) {
    const area = document.createElement("textarea");
    area.value = text;
    document.body.appendChild(area);
    area.select();
    document.execCommand("copy");
    area.remove();
    toast("キャプションをコピーしました");
  }
}

async function fetchFiles(urls, type) {
  const files = [];
  for (const url of urls) {
    const response = await fetch(url);
    if (!response.ok) throw new Error("取得できません: " + url);
    const blob = await response.blob();
    const name = url.split("/").pop();
    files.push(new File([blob], name, { type: blob.type || type }));
  }
  return files;
}

async function shareFiles(button, urls, label) {
  const original = button.textContent;
  button.disabled = true;
  button.textContent = "準備しています…";
  try {
    const files = await fetchFiles(urls);
    if (navigator.canShare && navigator.canShare({ files })) {
      await navigator.share({ files });
      toast("共有シートから「画像を保存」を選んでください");
    } else {
      toast("この端末では一括保存に対応していません。下の画像を長押しして保存してください");
    }
  } catch (e) {
    if (e && e.name === "AbortError") {
      // 利用者が共有シートを閉じただけ
    } else {
      toast("失敗しました: " + (e && e.message ? e.message : e));
    }
  }
  button.disabled = false;
  button.textContent = original;
}

function passphrase(force) {
  let saved = "";
  try { saved = localStorage.getItem("pass") || ""; } catch (e) { saved = ""; }
  if (saved && !force) return saved;
  const input = prompt("合言葉を入力してください（この端末に保存されます）");
  if (!input) return "";
  try { localStorage.setItem("pass", input); } catch (e) { /* 保存できなくても続行 */ }
  return input;
}

async function publishNow(button, item) {
  if (!WORKER) {
    toast("投稿用のWorkerが設定されていません");
    return;
  }
  const pass = passphrase(false);
  if (!pass) return;
  if (!confirm(`${item.post} を今すぐ投稿します。よろしいですか？\n\n対象: ${item.api.join(", ")}`)) {
    return;
  }

  const original = button.textContent;
  button.disabled = true;
  button.textContent = "投稿しています…";
  try {
    const response = await fetch(WORKER + "/publish", {
      method: "POST",
      headers: { "content-type": "application/json", "x-passphrase": pass },
      body: JSON.stringify({
        experiment_id: item.id,
        platforms: item.api,
        caption: item.caption,
        images: item.images,
      }),
    });
    const data = await response.json();
    if (response.status === 401) {
      passphrase(true);
      toast("合言葉が違います。入れ直してください");
    } else if (data.ok) {
      toast("投稿しました");
      button.textContent = "投稿しました";
      button.closest(".card").classList.add("done");
      return;
    } else {
      const failed = Object.entries(data.results || {})
        .filter(([, r]) => !r.ok)
        .map(([name, r]) => `${name}: ${r.error}`)
        .join(" / ");
      toast(failed || data.error || "失敗しました");
    }
  } catch (e) {
    toast("通信に失敗しました: " + (e && e.message ? e.message : e));
  }
  button.disabled = false;
  button.textContent = original;
}

function card(item) {
  const el = document.createElement("div");
  el.className = "card" + (item.done ? " done" : "");

  const tags = [];
  if (item.done) tags.push('<span class="tag done">投稿済み</span>');
  if (item.platforms) tags.push('<span class="tag">' + item.platforms + "</span>");
  if (item.video) tags.push('<span class="tag">Reel</span>');

  el.innerHTML = `
    <div class="head">
      <img class="thumb" src="${item.images[0]}" loading="lazy" alt="">
      <div>
        <div class="title">${item.post}</div>
        <div class="when">${item.scheduled || "予約なし"}</div>
        <div style="margin-top:4px">${tags.join("")}</div>
      </div>
    </div>
    <div class="caption" data-caption>${item.caption.replace(/[<>&]/g, c => ({ "<": "&lt;", ">": "&gt;", "&": "&amp;" }[c]))}</div>
    <div class="actions"></div>
    <div class="grid"></div>
  `;

  el.querySelector("[data-caption]").addEventListener("click", (event) => {
    event.currentTarget.classList.toggle("open");
  });

  const actions = el.querySelector(".actions");

  if (WORKER && item.api && item.api.length && !item.done) {
    const now = document.createElement("button");
    now.className = "act";
    now.textContent = `今すぐ投稿（${item.api.join(" / ")}）`;
    now.onclick = () => publishNow(now, item);
    actions.appendChild(now);
  }

  const copy = document.createElement("button");
  copy.className = WORKER && item.api && item.api.length && !item.done ? "act ghost" : "act";
  copy.textContent = "キャプションをコピー";
  copy.onclick = () => copyText(item.caption);
  actions.appendChild(copy);

  const save = document.createElement("button");
  save.className = "act ghost";
  save.textContent = `画像${item.images.length}枚をまとめて保存`;
  save.onclick = () => shareFiles(save, item.images);
  actions.appendChild(save);

  if (item.video) {
    const video = document.createElement("button");
    video.className = "act ghost";
    video.textContent = "Reel動画を保存";
    video.onclick = () => shareFiles(video, [item.video]);
    actions.appendChild(video);
  }

  const note = document.createElement("div");
  note.className = "note";
  note.textContent = "うまく保存できないときは、下の画像を長押しして1枚ずつ保存してください。";
  actions.appendChild(note);

  const grid = el.querySelector(".grid");
  item.images.forEach((url) => {
    const img = document.createElement("img");
    img.src = url;
    img.loading = "lazy";
    grid.appendChild(img);
  });

  return el;
}

function render() {
  const items = showAll ? DATA : DATA.filter((d) => !d.done);
  list.innerHTML = "";
  if (!items.length) {
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = showAll ? "まだ何もありません" : "未投稿のものはありません";
    list.appendChild(empty);
    return;
  }
  items.forEach((item) => list.appendChild(card(item)));
}

document.getElementById("f-todo").onclick = () => {
  showAll = false;
  document.getElementById("f-todo").setAttribute("aria-pressed", "true");
  document.getElementById("f-all").setAttribute("aria-pressed", "false");
  render();
};
document.getElementById("f-all").onclick = () => {
  showAll = true;
  document.getElementById("f-all").setAttribute("aria-pressed", "true");
  document.getElementById("f-todo").setAttribute("aria-pressed", "false");
  render();
};

render();
</script>
</body>
</html>
"""
