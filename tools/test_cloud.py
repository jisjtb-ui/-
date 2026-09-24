"""PC側 → クラウド の受け渡しテスト。

  python tools/test_cloud.py

本物のクラウドへは繋がない。Workerのふりをする小さなHTTPサーバを
自分のPCの中だけで立てて、そこへ渡す。外部SNSへは1件も投稿しない。
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from autopost import cloud
from autopost.catalog import Catalog, ensure_default
from autopost.config import Settings
from autopost.engine import create_experiment
from autopost.experiments import (
    CLOUD_QUEUED, DELIVERED, READY_TO_PUBLISH, ExperimentStore,
)
from autopost.oauth.store import Token, TokenStore

PASS = "correct-horse"
PREFIX = "https://example.pages.dev/"


class FakeWorker(BaseHTTPRequestHandler):
    """Workerのふり。受け取った内容を記録するだけ。"""

    jobs: dict = {}
    accounts: dict = {}
    seen_passphrases: list = []
    reject_ids: set = set()
    metrics: list = []

    def log_message(self, *args):        # テスト中に余計な出力をしない
        pass

    def _send(self, body, status=200):
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _auth(self) -> bool:
        given = self.headers.get("x-passphrase") or ""
        FakeWorker.seen_passphrases.append(given)
        if given != PASS:
            self._send({"error": "合言葉が違います"}, 401)
            return False
        return True

    def do_GET(self):
        if self.path == "/health":
            self._send({"ok": True, "ready": True, "cloud_queue": True,
                        "notifications": True})
            return
        if not self._auth():
            return
        if self.path == "/api/status":
            counts = {}
            for job in FakeWorker.jobs.values():
                counts[job["status"]] = counts.get(job["status"], 0) + 1
            nxt = sorted((j for j in FakeWorker.jobs.values() if j["status"] == "scheduled"),
                         key=lambda j: j["scheduled_at"])
            self._send({"ok": True, "counts": counts, "total": len(FakeWorker.jobs),
                        "next": nxt[0] if nxt else None, "today": [], "failed": [],
                        "posted": [], "per_platform": [], "per_account": [],
                        "server_time": datetime.now().astimezone().isoformat()})
            return
        if self.path.startswith("/api/results"):
            self._send({"ok": True, "items": [
                {**job, "external_post_id": f"CLOUD_{key}"}
                for key, job in FakeWorker.jobs.items()
                if job["status"] in ("posted", "failed")
            ]})
            return
        if self.path.startswith("/api/metrics"):
            self._send({"ok": True, "items": FakeWorker.metrics})
            return
        if self.path == "/api/accounts":
            self._send({"ok": True, "accounts": [
                {"id": key, "platform": value["platform"], "label": value.get("label", ""),
                 "has_token": True, "token_expires_at": value.get("expires_at", "")}
                for key, value in FakeWorker.accounts.items()
            ]})
            return
        self._send({"error": "not found"}, 404)

    def do_POST(self):
        if not self._auth():
            return
        length = int(self.headers.get("content-length") or 0)
        body = json.loads(self.rfile.read(length) or "{}")

        if self.path == "/api/enqueue":
            added, skipped, rejected = 0, 0, []
            for job in body.get("jobs", []):
                if job["id"] in FakeWorker.reject_ids:
                    rejected.append({"id": job["id"], "reason": "テストのため拒否"})
                    continue
                if job["id"] in FakeWorker.jobs:
                    skipped += 1
                    continue
                FakeWorker.jobs[job["id"]] = {**job, "status": "scheduled"}
                added += 1
            self._send({"ok": not rejected, "added": added, "skipped": skipped,
                        "rejected": rejected, "registered": added + skipped},
                       207 if rejected else 200)
            return
        if self.path == "/api/account":
            FakeWorker.accounts[body["id"]] = body
            self._send({"ok": True, "id": body["id"], "token_saved": bool(body.get("access_token"))})
            return
        if self.path == "/api/action":
            job = FakeWorker.jobs.get(body.get("id"))
            if not job:
                self._send({"error": "見つかりません"}, 404)
                return
            job["status"] = {"cancel": "cancelled", "post_now": "scheduled",
                             "retry": "scheduled"}.get(body.get("action"), job["status"])
            self._send({"ok": True})
            return
        self._send({"error": "not found"}, 404)


def main() -> int:
    failures: list[str] = []

    def check(name: str, condition, detail: str = "") -> None:
        mark = "OK  " if condition else "NG  "
        print(f"  {mark}{name}" + (f" … {detail}" if detail and not condition else ""))
        if not condition:
            failures.append(f"{name}: {detail}" if detail else name)

    server = HTTPServer(("127.0.0.1", 0), FakeWorker)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"

    tmp = Path(tempfile.mkdtemp())
    settings = Settings.load()
    settings.experiments_db_path = tmp / "cloud.db"
    settings.token_dir = tmp / "tokens"
    settings.publish_worker_url = base
    settings.publish_passphrase = PASS

    store = ExperimentStore(settings.experiments_db_path)
    catalog = Catalog(store)
    category = ensure_default(catalog, "心理テスト", ROOT / "data" / "tests",
                              log=lambda m: None)
    subs = catalog.sub_categories(category.id)

    print("PC側 → クラウド の受け渡し")
    print("-" * 52)

    # --- 準備確認 ---
    info = cloud.CloudClient(settings).health()
    check("クラウドの準備を確認できる", info.get("cloud_queue") is True)

    # --- アカウントとトークンを預ける ---
    TokenStore(settings.token_dir, category.id).save(Token(
        platform="instagram", access_token="SECRET-PC-TOKEN", refresh_token="R",
        account_id="999", account_name="@shinri",
        expires_at=(datetime.now().astimezone() + timedelta(days=55)).isoformat()))
    result = cloud.connect(settings, catalog, category.id, "instagram", log=lambda m: None)
    check("接続済みアカウントをクラウドへ預けられる", result.get("token_saved") is True)
    stored = FakeWorker.accounts.get("instagram:1") or {}
    check("CategoryとアカウントIDが対応する",
          stored.get("category_id") == category.id
          and stored.get("id") == f"instagram:{category.id}", json.dumps(stored)[:120])
    check("媒体側のIDも渡る", stored.get("external_id") == "999")

    # --- 大量の予約を作る ---
    now = datetime.now().astimezone()
    for index in range(120):
        experiment = create_experiment(
            store, ["instagram", "threads"],
            content_category=subs[index % len(subs)].source_key,
            category_id=category.id, sub_category_id=subs[index % len(subs)].id,
            text="本文", source_post_id=f"post_{index:03d}",
            extra={"image_urls": [f"{PREFIX}post_{index:03d}/a/{n:02d}.jpg"
                                  for n in range(1, 11)]},
        )
        for platform in ("instagram", "threads"):
            publication = store.publication(experiment.experiment_id, platform)
            store.set_schedule(publication.id, now + timedelta(hours=index))
            with store._connect() as conn:
                conn.execute("UPDATE experiment_publications SET status=? WHERE id=?",
                             (READY_TO_PUBLISH, publication.id))

    # --- dry-run は何も変えない ---
    before = len(FakeWorker.jobs)
    cloud.push(settings, store, dry_run=True, log=lambda m: None)
    check("--dry-run では渡さない", len(FakeWorker.jobs) == before)

    # --- 本番の受け渡し ---
    report = cloud.push(settings, store, log=lambda m: None)
    check("240件を渡せる（100件ずつに分けて送る）", report.added == 240,
          f"{report.added}件 / {report.summary()}")
    check("クラウド側に登録される", len(FakeWorker.jobs) == 240, str(len(FakeWorker.jobs)))
    check("合言葉を付けて送っている", all(p == PASS for p in FakeWorker.seen_passphrases))

    # --- PC側では投稿しない印が付く ---
    handed = [p for p in store.publications() if p.status == CLOUD_QUEUED]
    check("渡した分は「クラウド待ち」になる", len(handed) == 240, str(len(handed)))
    check("PC側の実行対象から外れる（二重投稿しない）", len(store.runnable()) == 0,
          f"{len(store.runnable())}件がまだ実行対象")

    # --- もう一度渡しても増えない ---
    again = cloud.push(settings, store, log=lambda m: None)
    check("二度押しても増えない", again.added == 0 and len(FakeWorker.jobs) == 240,
          f"追加 {again.added}件")

    # --- 画像はPCのパスではなく公開URL ---
    sample = next(iter(FakeWorker.jobs.values()))
    check("画像は公開URLで渡る（PCのパスに依存しない）",
          all(url.startswith("https://") for url in sample["media"]),
          json.dumps(sample["media"][:1]))
    check("画像が10枚そろっている", len(sample["media"]) == 10)
    check("予約時刻が渡る", bool(sample["scheduled_at"]))
    check("Categoryとアカウントが渡る",
          sample["account_id"].startswith("instagram:")
          or sample["account_id"].startswith("threads:"))

    # --- 状況を見る ---
    status = cloud.CloudClient(settings).status()
    check("クラウドの状況を取れる", status["counts"].get("scheduled") == 240,
          json.dumps(status["counts"]))

    # --- クラウドが投稿した結果を取り込む ---
    for key in list(FakeWorker.jobs)[:5]:
        FakeWorker.jobs[key]["status"] = "posted"
    outcome = cloud.sync(settings, store, log=lambda m: None)
    check("クラウドの結果をPCへ取り込める", outcome["applied"] == 5, json.dumps(outcome))
    delivered = [p for p in store.publications() if p.status in DELIVERED]
    check("PC側が配信済みとして記録する", len(delivered) == 5, str(len(delivered)))
    check("取り込んだあとも実行対象にならない", len(store.runnable()) == 0)

    # --- クラウドが測った反応データを取り込む ---
    first_key = list(FakeWorker.jobs)[0]
    FakeWorker.metrics = [{
        "job_id": first_key, "platform": "instagram", "snapshot": "24h",
        "collected_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "hours_since_post": 25.0, "window_hours": 24.0, "late": 0,
        "external_post_id": "CLOUD_1", "published_at": "", "sub_category": str(subs[0].id),
        "views": 1200, "reach": 950, "impressions": None, "likes": 30,
        "comments": 2, "shares": None, "saves": None,
    }]
    pulled = cloud.pull_metrics(settings, store, log=lambda m: None)
    check("クラウドが測った反応データをPCへ取り込める", pulled["metrics_saved"] == 1,
          json.dumps(pulled))
    stored_metrics = store.latest_metrics(first_key.rsplit(":", 1)[0])
    check("実測した数字がPC側に入る",
          stored_metrics and stored_metrics[0].views == 1200
          and stored_metrics[0].reach == 950,
          str(stored_metrics[0].views if stored_metrics else None))
    check("取れていない指標はNULLのまま",
          stored_metrics[0].saves is None and stored_metrics[0].shares is None)
    check("計測区分と遅延の情報も入る",
          stored_metrics[0].snapshot == "24h" and stored_metrics[0].late == 0)
    check("SubCategoryが紐づく", stored_metrics[0].sub_category_id == subs[0].id)
    again_pull = cloud.pull_metrics(settings, store, log=lambda m: None)
    check("同じ反応データを二度入れない", again_pull["metrics_saved"] == 0)

    # --- 失敗も取り込む ---
    key = list(FakeWorker.jobs)[10]
    FakeWorker.jobs[key].update(status="failed", error="権限がありません")
    outcome = cloud.sync(settings, store, log=lambda m: None)
    check("クラウドの失敗も取り込める", outcome["failed"] == 1, json.dumps(outcome))

    # --- スマホと同じ操作をPCからもできる ---
    target = list(FakeWorker.jobs)[20]
    action = cloud.CloudClient(settings).action("cancel", target)
    check("予約を取り消せる", action.get("ok") is True)

    # --- 合言葉が違えば通らない ---
    wrong = Settings.load()
    wrong.publish_worker_url = base
    wrong.publish_passphrase = "wrong"
    try:
        cloud.CloudClient(wrong).status()
        denied = False
    except cloud.CloudError as error:
        denied = "合言葉" in str(error)
    check("合言葉が違えば断られる", denied)

    # --- 受け付けられなかったものは印を付けない ---
    FakeWorker.reject_ids = {"EXP-REJECT:instagram"}
    experiment = create_experiment(
        store, ["instagram"], content_category="hotel", category_id=category.id,
        text="x", source_post_id="post_999",
        extra={"image_urls": [f"{PREFIX}post_999/a/01.jpg"]})
    publication = store.publication(experiment.experiment_id, "instagram")
    store.set_schedule(publication.id, now)
    with store._connect() as conn:
        conn.execute("UPDATE experiment_publications SET status=? WHERE id=?",
                     (READY_TO_PUBLISH, publication.id))
    FakeWorker.reject_ids = {f"{experiment.experiment_id}:instagram"}
    report = cloud.push(settings, store, log=lambda m: None)
    still = store.publication(experiment.experiment_id, "instagram")
    check("受け付けられなかった予約は印を付けない",
          len(report.rejected) == 1 and still.status == READY_TO_PUBLISH,
          f"{still.status} / 拒否 {len(report.rejected)}件")

    # --- トークンの扱い ---
    # PC → Worker へは渡す（そうしないとPCを切ったあと投稿できない）
    check("トークンはクラウドへ預けられている",
          FakeWorker.accounts["instagram:1"].get("access_token") == "SECRET-PC-TOKEN")

    # ただし、Workerが返す応答には出てこない
    listed = cloud.CloudClient(settings).accounts()
    responses = json.dumps({"accounts": listed, "status": status,
                            "results": cloud.CloudClient(settings).results()},
                           ensure_ascii=False)
    check("クラウドの応答にトークンが出てこない", "SECRET-PC-TOKEN" not in responses)
    check("一覧には期限だけが出る",
          any(a.get("has_token") for a in listed.get("accounts", [])))

    # 画面・ログにも出さない
    lines = []
    cloud.connect(settings, catalog, category.id, "instagram", log=lines.append)
    cloud.push(settings, store, log=lines.append)
    check("ログにトークンの値を出さない",
          not any("SECRET-PC-TOKEN" in line for line in lines),
          " / ".join(lines)[:120])

    server.shutdown()
    print("-" * 52)
    if failures:
        print(f"失敗 {len(failures)}件")
        for item in failures:
            print(f"  - {item}")
        return 1
    print("すべて通りました")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
