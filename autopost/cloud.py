"""クラウド（Cloudflare Worker + D1）への受け渡し。

分担:
  PC側   … コンテンツ生成・画像・カテゴリ・アカウント設定・分析画面・大量作成
  クラウド … 予約Queue・予約日時・投稿実行・トークン管理・結果記録・再試行・履歴

PCで作って「クラウドへ渡す」と、**PCの電源を切っても指定時刻に投稿されます。**

二重投稿を防ぐため、渡した配信は状態を `cloud_queued` にします。
この状態はPC側の実行対象（CLAIMABLE）に入っていないので、
`sns run` は自然に手を出しません。投稿結果は `cloud sync` で取り込みます。

秘密情報の扱い:
  - トークンは `cloud connect` で自分のWorkerへHTTPSで送り、D1へ暗号化して入る
  - この画面・ログ・レポートにトークンの値は出さない
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from .config import Settings
from .experiments import (
    CLOUD_QUEUED,
    DELIVERED,
    DRAFT_CREATED,
    PUBLISHED,
    READY_TO_PUBLISH,
    ExperimentStore,
)

LogFn = Callable[[str], None]

# 1回のリクエストで送る件数。Workerの上限（500）より小さくしておく
BATCH = 100
TIMEOUT = 60


class CloudError(RuntimeError):
    """クラウドとのやりとりで失敗した。"""


@dataclass
class PushReport:
    added: int = 0
    skipped: int = 0
    rejected: list[dict] = field(default_factory=list)
    marked: int = 0

    def summary(self) -> str:
        parts = [f"登録 {self.added}件", f"すでに登録済み {self.skipped}件"]
        if self.rejected:
            parts.append(f"受け付けられず {len(self.rejected)}件")
        return " / ".join(parts)


class CloudClient:
    """Workerとの通信。合言葉1つで認証する。"""

    def __init__(self, settings: Settings) -> None:
        self.base = (settings.publish_worker_url or "").rstrip("/")
        self.passphrase = settings.publish_passphrase or ""
        if not self.base:
            raise CloudError(
                "クラウドのURLが未設定です（.env の PUBLISH_WORKER_URL）"
            )
        if not self.passphrase:
            raise CloudError(
                "合言葉が未設定です（.env の PUBLISH_PASSPHRASE）"
            )

    def _call(self, path: str, body: dict | None = None, method: str = "") -> dict:
        url = f"{self.base}{path}"
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        request = urllib.request.Request(
            url, data=data, method=method or ("POST" if data else "GET"),
            headers={
                "content-type": "application/json; charset=utf-8",
                "x-passphrase": self.passphrase,
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                return json.loads(response.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", "replace")[:300]
            try:
                parsed = json.loads(detail)
            except json.JSONDecodeError:
                parsed = {}
            if error.code == 401:
                raise CloudError("合言葉が違います（.env と Worker の Secret を合わせてください）") from error
            if error.code == 503:
                raise CloudError(
                    "クラウドのQueueが未設定です。worker/wrangler.jsonc の d1_databases と"
                    " schema.sql の適用を確認してください"
                ) from error
            raise CloudError(parsed.get("error") or f"HTTP {error.code}: {detail}") from error
        except urllib.error.URLError as error:
            raise CloudError(f"クラウドへ接続できません: {error.reason}") from error

    # ------------------------------------------------------------------
    def health(self) -> dict:
        return self._call("/health")

    def status(self) -> dict:
        return self._call("/api/status")

    def accounts(self) -> dict:
        return self._call("/api/accounts")

    def results(self, since: str = "") -> dict:
        suffix = f"?since={since}" if since else ""
        return self._call(f"/api/results{suffix}")

    def metrics(self, since: str = "") -> dict:
        suffix = f"?since={since}" if since else ""
        return self._call(f"/api/metrics{suffix}")

    def enqueue(self, jobs: list[dict]) -> dict:
        return self._call("/api/enqueue", {"jobs": jobs})

    def register_account(self, payload: dict) -> dict:
        return self._call("/api/account", payload)

    def action(self, action: str, job_id: str, when: str = "") -> dict:
        body = {"action": action, "id": job_id}
        if when:
            body["when"] = when
        return self._call("/api/action", body)


# ----------------------------------------------------------------------
def account_id(platform: str, category_id: int | None) -> str:
    """クラウド側のアカウントID。PC側のCategoryと1対1で対応させる。"""
    return f"{platform}:{category_id or 0}"


def _jobs_for(store: ExperimentStore, platforms: list[str] | None,
              limit: int, log: LogFn) -> list[dict]:
    """クラウドへ渡せる配信を集める。

    条件は「公開URLが用意できている」かつ「まだ配信していない」。
    画像はすでに公開URL（Cloudflare Pages）なので、PCの電源とは無関係に使える。
    """
    jobs: list[dict] = []
    for experiment in store.list_experiments(limit=1000):
        urls = (experiment.extra or {}).get("image_urls") or []
        if not urls:
            continue
        for publication in store.publications(experiment_id=experiment.experiment_id):
            if publication.status != READY_TO_PUBLISH:
                continue
            if platforms and publication.platform not in platforms:
                continue
            if not publication.scheduled_at:
                log(f"  予約時刻が無いので渡しません: {experiment.experiment_id}"
                    f" / {publication.platform}")
                continue
            jobs.append({
                "id": f"{experiment.experiment_id}:{publication.platform}",
                "group_id": experiment.experiment_id,
                "platform": publication.platform,
                "account_id": account_id(publication.platform, experiment.category_id),
                "category": experiment.content_category or "",
                "sub_category": str(experiment.sub_category_id or ""),
                "caption": experiment.text or "",
                "media": list(urls),
                "media_kind": "image",
                "scheduled_at": _iso(publication.scheduled_at),
                "_publication_id": publication.id,
            })
            if len(jobs) >= limit:
                return jobs
    return jobs


def _iso(value: str) -> str:
    """予約時刻をUTCのISO8601へ直す（クラウド側はUTCで比べる）。"""
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return value
    if moment.tzinfo is None:
        moment = moment.astimezone()
    return moment.astimezone().isoformat(timespec="seconds")


def push(settings: Settings, store: ExperimentStore,
         platforms: list[str] | None = None, limit: int = 500,
         dry_run: bool = False, log: LogFn = print) -> PushReport:
    """予約をクラウドへ渡す。終わったらPCの電源を切ってよい。"""
    client = CloudClient(settings)
    report = PushReport()

    jobs = _jobs_for(store, platforms, limit, log)
    if not jobs:
        log("  渡せる予約がありません（公開URLと予約時刻が揃ったものが対象です）")
        return report

    log(f"  {len(jobs)}件を渡します")
    if dry_run:
        for job in jobs[:5]:
            log(f"    {job['scheduled_at'][:16]}  {job['platform']:<10}{job['id']}")
        if len(jobs) > 5:
            log(f"    …ほか{len(jobs) - 5}件")
        return report

    for start in range(0, len(jobs), BATCH):
        chunk = jobs[start:start + BATCH]
        payload = [{k: v for k, v in job.items() if not k.startswith("_")} for job in chunk]
        result = client.enqueue(payload)
        report.added += int(result.get("added") or 0)
        report.skipped += int(result.get("skipped") or 0)
        report.rejected.extend(result.get("rejected") or [])

        # 受け付けられたものはPC側で投稿しない印を付ける
        accepted = {job["id"] for job in chunk}
        for reject in result.get("rejected") or []:
            accepted.discard(reject.get("id"))
        for job in chunk:
            if job["id"] in accepted:
                _mark_handed(store, job["_publication_id"])
                report.marked += 1
        log(f"    {start + len(chunk)}/{len(jobs)} 件目まで完了")

    for reject in report.rejected:
        log(f"  [受付不可] {reject.get('id')}: {reject.get('reason')}")
    return report


def _mark_handed(store: ExperimentStore, publication_id: int) -> None:
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    with store._connect() as conn:
        conn.execute(
            "UPDATE experiment_publications SET status=?, updated_at=? WHERE id=?",
            (CLOUD_QUEUED, now, publication_id),
        )


def sync(settings: Settings, store: ExperimentStore, log: LogFn = print) -> dict:
    """クラウドが投稿した結果をPCへ取り込む。

    これをしないと、PC側の記録が「クラウド待ち」のままになり、
    分析（カテゴリ別の成績）に入ってこない。
    """
    client = CloudClient(settings)
    payload = client.results()
    items = payload.get("items") or []
    applied = 0
    failed = 0

    for item in items:
        group = item.get("group_id") or ""
        platform = item.get("platform") or ""
        if not group or not platform:
            continue
        publication = store.publication(group, platform)
        if publication is None or publication.status in DELIVERED:
            continue
        if item.get("status") == "posted":
            store.mark_published(
                publication.id,
                str(item.get("external_post_id") or ""),
                item.get("external_url") or "",
                extra={"posted_by": "cloud"},
                status=DRAFT_CREATED if platform == "tiktok" else PUBLISHED,
            )
            applied += 1
        elif item.get("status") == "failed":
            store.mark_failed(publication.id,
                              f"クラウドで失敗: {item.get('error') or ''}")
            failed += 1

    log(f"  クラウドの結果を取り込みました: 投稿済み {applied}件 / 失敗 {failed}件")
    metrics = pull_metrics(settings, store, client=client, log=log)
    return {"applied": applied, "failed": failed, "seen": len(items), **metrics}


def pull_metrics(settings: Settings, store: ExperimentStore,
                 client: "CloudClient | None" = None, log: LogFn = print) -> dict:
    """クラウドが集めた反応データをPCへ取り込む。

    カテゴリ別の成績（weight）はPC側のDBを見るので、これを入れないと
    クラウドで測った分が分析に反映されない。
    取れていない指標は入れない（NULLのまま）。
    """
    from .experiments import Metrics

    client = client or CloudClient(settings)
    items = (client.metrics().get("items") or [])
    saved = 0
    for item in items:
        group = (item.get("job_id") or "").rsplit(":", 1)[0]
        platform = item.get("platform") or ""
        if not group or not platform:
            continue
        publication = store.publication(group, platform)
        if publication is None:
            continue
        sub_category_id = None
        raw_sub = item.get("sub_category") or ""
        if str(raw_sub).isdigit():
            sub_category_id = int(raw_sub)
        metrics = Metrics(
            experiment_id=group,
            platform=platform,
            snapshot=item.get("snapshot") or "",
            collected_at=item.get("collected_at") or "",
            hours_since_post=item.get("hours_since_post"),
            window_hours=item.get("window_hours"),
            late=int(item.get("late") or 0),
            external_post_id=item.get("external_post_id") or "",
            published_at=item.get("published_at") or "",
            sub_category_id=sub_category_id,
            views=item.get("views"),
            reach=item.get("reach"),
            impressions=item.get("impressions"),
            likes=item.get("likes"),
            comments=item.get("comments"),
            shares=item.get("shares"),
            saves=item.get("saves"),
            platform_metrics={"source": "cloud"},
        )
        if store.save_metrics(metrics):
            saved += 1
    if items:
        log(f"  クラウドの反応データを取り込みました: {saved}件"
            f"（うち新規 {saved} / 受信 {len(items)}）")
    return {"metrics_saved": saved, "metrics_seen": len(items)}


def connect(settings: Settings, catalog, category_id: int, platform: str,
            log: LogFn = print) -> dict:
    """PCで接続済みのアカウントとトークンをクラウドへ預ける。

    これを済ませておかないと、PCが止まっているあいだクラウドが投稿できない。
    トークンの値はログに出さない。
    """
    from .oauth.store import TokenStore

    category = catalog.category(category_id)
    if category is None:
        raise CloudError("Categoryが見つかりません")
    tokens = TokenStore(settings.token_dir, category_id)
    tokens.adopt_legacy(platform)
    token = tokens.load(platform)
    if token is None or not token.access_token:
        raise CloudError(
            f"{platform} はまだ接続されていません（画面のCategoryタブで接続してください）"
        )

    client = CloudClient(settings)
    result = client.register_account({
        "id": account_id(platform, category_id),
        "platform": platform,
        "label": token.account_name or "",
        "category": category.name,
        "category_id": category_id,
        "external_id": token.account_id or "",
        "settings": {"tiktok_mode": settings.tiktok_mode} if platform == "tiktok" else {},
        "access_token": token.access_token,
        "refresh_token": token.refresh_token or "",
        "expires_at": token.expires_at or "",
        "scope": token.scope or "",
    })
    log(f"  {platform} を「{category.name}」としてクラウドへ登録しました"
        f"（トークンは暗号化して保存されます）")
    return result
