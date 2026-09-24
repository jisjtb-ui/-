"""Category / SubCategory / 接続済みアカウントの台帳。

画面では「作成」と「選択」だけで済むようにするための土台。
利用者に名前やIDを打ち直させないため、次の約束を守る。

  - 内部の受け渡しは必ず ``category_id`` / ``sub_category_id``
  - 画面に出す文字列は、ここから引いたものだけ
  - SubCategory は data/tests の ``label`` から自動で作る（入力させない）
  - トークンはこの台帳に入れない（`.tokens/` のまま）

Category  … 運用の単位。媒体アカウントがぶら下がる（例: 心理テスト）
SubCategory … その中身の切り口。成績とweightはこの単位で見る（例: 一人の時間）
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .experiments import ExperimentStore

DISCONNECTED = "disconnected"
CONNECTED = "connected"


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


@dataclass
class Category:
    id: int
    name: str
    enabled: bool = True
    created_at: str = ""


@dataclass
class SubCategory:
    id: int
    category_id: int
    name: str
    source_key: str = ""
    enabled: bool = True
    created_at: str = ""


@dataclass
class SocialAccount:
    """あるCategoryに接続した、ある媒体のアカウント。

    ``account_name`` は画面表示のためだけに持つ。トークンは持たない。
    """

    id: int
    category_id: int
    platform: str
    account_id: str = ""
    account_name: str = ""
    status: str = DISCONNECTED
    connected_at: str = ""

    @property
    def connected(self) -> bool:
        return self.status == CONNECTED

    def label(self) -> str:
        if not self.connected:
            return "未接続"
        return f"接続済み {self.account_name or self.account_id or ''}".strip()


class Catalog:
    """台帳の読み書き。名前で引くのは画面の入口だけ。"""

    def __init__(self, store: ExperimentStore) -> None:
        self.store = store

    # ------------------------------------------------------------------
    # Category
    # ------------------------------------------------------------------
    def categories(self, include_disabled: bool = False) -> list[Category]:
        sql = "SELECT * FROM categories"
        if not include_disabled:
            sql += " WHERE enabled=1"
        sql += " ORDER BY id"
        with self.store._connect() as conn:
            rows = conn.execute(sql).fetchall()
        return [Category(id=r["id"], name=r["name"], enabled=bool(r["enabled"]),
                         created_at=r["created_at"]) for r in rows]

    def category(self, category_id: int) -> Category | None:
        with self.store._connect() as conn:
            row = conn.execute("SELECT * FROM categories WHERE id=?", (category_id,)).fetchone()
        if row is None:
            return None
        return Category(id=row["id"], name=row["name"], enabled=bool(row["enabled"]),
                        created_at=row["created_at"])

    def add_category(self, name: str) -> Category:
        """Categoryを作る。ここが名前を入力する唯一の場所。"""
        name = (name or "").strip()
        if not name:
            raise ValueError("Category名を入れてください")
        with self.store._connect() as conn:
            try:
                cursor = conn.execute(
                    "INSERT INTO categories (name, enabled, created_at) VALUES (?, 1, ?)",
                    (name, _now()),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError(f"「{name}」は既にあります") from exc
            category_id = int(cursor.lastrowid)
        return self.category(category_id)      # type: ignore[return-value]

    def rename_category(self, category_id: int, name: str) -> None:
        name = (name or "").strip()
        if not name:
            raise ValueError("Category名を入れてください")
        with self.store._connect() as conn:
            conn.execute("UPDATE categories SET name=? WHERE id=?", (name, category_id))

    def set_category_enabled(self, category_id: int, enabled: bool) -> None:
        with self.store._connect() as conn:
            conn.execute("UPDATE categories SET enabled=? WHERE id=?",
                         (int(enabled), category_id))

    # ------------------------------------------------------------------
    # SubCategory
    # ------------------------------------------------------------------
    def sub_categories(self, category_id: int,
                       include_disabled: bool = False) -> list[SubCategory]:
        """そのCategoryに属するSubCategoryだけを返す。

        画面のSubCategory選択肢は、必ずこれを使う。
        """
        sql = "SELECT * FROM sub_categories WHERE category_id=?"
        if not include_disabled:
            sql += " AND enabled=1"
        sql += " ORDER BY id"
        with self.store._connect() as conn:
            rows = conn.execute(sql, (category_id,)).fetchall()
        return [self._sub_from_row(r) for r in rows]

    def sub_category(self, sub_category_id: int) -> SubCategory | None:
        with self.store._connect() as conn:
            row = conn.execute("SELECT * FROM sub_categories WHERE id=?",
                               (sub_category_id,)).fetchone()
        return self._sub_from_row(row) if row else None

    @staticmethod
    def _sub_from_row(row: sqlite3.Row) -> SubCategory:
        return SubCategory(id=row["id"], category_id=row["category_id"], name=row["name"],
                           source_key=row["source_key"], enabled=bool(row["enabled"]),
                           created_at=row["created_at"])

    def add_sub_category(self, category_id: int, name: str,
                         source_key: str = "") -> SubCategory:
        name = (name or "").strip()
        if not name:
            raise ValueError("SubCategory名を入れてください")
        if self.category(category_id) is None:
            raise ValueError("先にCategoryを作ってください")
        with self.store._connect() as conn:
            try:
                cursor = conn.execute(
                    "INSERT INTO sub_categories (category_id, name, source_key, enabled, created_at)"
                    " VALUES (?, ?, ?, 1, ?)",
                    (category_id, name, source_key, _now()),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError(f"「{name}」は既にあります") from exc
            return self.sub_category(int(cursor.lastrowid))   # type: ignore[return-value]

    def set_sub_enabled(self, sub_category_id: int, enabled: bool) -> None:
        """使わないSubCategoryを抽選から外す。記録は消さない。"""
        with self.store._connect() as conn:
            conn.execute("UPDATE sub_categories SET enabled=? WHERE id=?",
                         (int(enabled), sub_category_id))

    def sub_by_source_key(self, category_id: int, source_key: str) -> SubCategory | None:
        with self.store._connect() as conn:
            row = conn.execute(
                "SELECT * FROM sub_categories WHERE category_id=? AND source_key=?",
                (category_id, source_key),
            ).fetchone()
        return self._sub_from_row(row) if row else None

    # ------------------------------------------------------------------
    # 接続済みアカウント
    # ------------------------------------------------------------------
    def accounts(self, category_id: int, platforms: list[str] | None = None
                 ) -> list[SocialAccount]:
        """そのCategoryの媒体ごとの接続状況。

        未接続の媒体も「未接続」の行として返すので、画面はそのまま並べられる。
        """
        with self.store._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM social_accounts WHERE category_id=? ORDER BY platform",
                (category_id,),
            ).fetchall()
        found = {r["platform"]: self._account_from_row(r) for r in rows}
        out: list[SocialAccount] = []
        for platform in (platforms or sorted(found)):
            out.append(found.get(platform, SocialAccount(
                id=0, category_id=category_id, platform=platform)))
        return out

    def account(self, category_id: int, platform: str) -> SocialAccount | None:
        with self.store._connect() as conn:
            row = conn.execute(
                "SELECT * FROM social_accounts WHERE category_id=? AND platform=?",
                (category_id, platform),
            ).fetchone()
        return self._account_from_row(row) if row else None

    @staticmethod
    def _account_from_row(row: sqlite3.Row) -> SocialAccount:
        return SocialAccount(
            id=row["id"], category_id=row["category_id"], platform=row["platform"],
            account_id=row["account_id"], account_name=row["account_name"],
            status=row["status"], connected_at=row["connected_at"],
        )

    def link_account(self, category_id: int, platform: str, account_id: str = "",
                     account_name: str = "") -> SocialAccount:
        """OAuthが終わったCategoryへ、そのまま結び付ける。

        接続を始めた画面で category_id は決まっているので、
        終わってから「どのCategoryですか？」と聞き直さない。
        """
        if self.category(category_id) is None:
            raise ValueError("Categoryが見つかりません")
        with self.store._connect() as conn:
            conn.execute(
                "INSERT INTO social_accounts"
                " (category_id, platform, account_id, account_name, status, connected_at)"
                " VALUES (?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(category_id, platform) DO UPDATE SET"
                " account_id=excluded.account_id, account_name=excluded.account_name,"
                " status=excluded.status, connected_at=excluded.connected_at",
                (category_id, platform, account_id, account_name, CONNECTED, _now()),
            )
        return self.account(category_id, platform)      # type: ignore[return-value]

    def unlink_account(self, category_id: int, platform: str) -> None:
        with self.store._connect() as conn:
            conn.execute(
                "UPDATE social_accounts SET status=?, connected_at='' "
                "WHERE category_id=? AND platform=?",
                (DISCONNECTED, category_id, platform),
            )

    def connected_platforms(self, category_id: int) -> list[str]:
        """投稿先を自動で決めるための一覧。利用者に入力させない。"""
        return [a.platform for a in self.accounts(category_id) if a.connected]

    def category_of_account(self, platform: str, account_id: str) -> Category | None:
        with self.store._connect() as conn:
            row = conn.execute(
                "SELECT category_id FROM social_accounts"
                " WHERE platform=? AND account_id=? AND status=?",
                (platform, account_id, CONNECTED),
            ).fetchone()
        return self.category(row["category_id"]) if row else None


# ----------------------------------------------------------------------
def load_labels(tests_dir: Path) -> dict[str, str]:
    """data/tests から「内部キー → 画面に出す名前」を読む。

    SubCategory名を利用者に入力させないための材料。
    """
    labels: dict[str, str] = {}
    for path in sorted(Path(tests_dir).glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        key = data.get("category") or path.stem
        labels[key] = data.get("label") or key
    return labels


def ensure_default(catalog: Catalog, name: str, tests_dir: Path,
                   log=lambda message: None) -> Category:
    """初回起動用。既定Categoryと、中身のSubCategoryを自動で用意する。

    利用者が何も打たなくても、選択式の画面がすぐ使える状態にする。
    """
    existing = {c.name: c for c in catalog.categories(include_disabled=True)}
    category = existing.get(name)
    if category is None:
        category = catalog.add_category(name)
        log(f"  Category「{name}」を作りました")

    labels = load_labels(tests_dir)
    known = {s.source_key for s in catalog.sub_categories(category.id, include_disabled=True)}
    added = 0
    for key, label in labels.items():
        if key in known:
            continue
        try:
            catalog.add_sub_category(category.id, label, source_key=key)
            added += 1
        except ValueError:
            # 同じ表示名が既にある場合は、内部キーを添えて区別する
            try:
                catalog.add_sub_category(category.id, f"{label}（{key}）", source_key=key)
                added += 1
            except ValueError:
                continue
    if added:
        log(f"  SubCategoryを {added}件 自動で登録しました（入力不要）")
    return category


# ----------------------------------------------------------------------
def connect_platform(settings, catalog: Catalog, category_id: int, platform: str,
                     manual: bool = False, log=print) -> SocialAccount:
    """CategoryのままOAuthを済ませ、そのCategoryへ結び付ける。

    始めた画面で ``category_id`` は決まっているので、認証のあとに
    「どのCategoryへ接続しますか？」と聞き直さない。
    """
    from .oauth.store import TokenStore

    category = catalog.category(category_id)
    if category is None:
        raise ValueError("Categoryが見つかりません")

    tokens = TokenStore(settings.token_dir, category_id)
    if tokens.adopt_legacy(platform):
        log(f"  Category分け前の{platform}の接続を「{category.name}」へ引き継ぎました")

    if platform == "tiktok":
        from .oauth import tiktok_oauth as module
        token = module.connect(settings, tokens)
    elif platform == "threads":
        from .oauth import threads_oauth as module
        token = module.connect(settings, tokens, manual=manual)
    elif platform == "pinterest":
        from .oauth import pinterest_oauth as module
        token = module.connect(settings, tokens, manual=manual)
    else:
        from .oauth import meta_oauth as module
        token = module.connect(settings, tokens, manual=manual)

    return catalog.link_account(category_id, platform,
                                account_id=token.account_id,
                                account_name=token.account_name)


def sync_accounts(settings, catalog: Catalog, category_id: int,
                  platforms: list[str]) -> list[SocialAccount]:
    """実際に保存されているトークンを見て、接続状況を台帳へ揃える。

    画面の「接続済み/未接続」を、台帳の思い込みではなく実体に合わせる。
    """
    from .oauth.store import TokenStore

    tokens = TokenStore(settings.token_dir, category_id)
    for platform in platforms:
        tokens.adopt_legacy(platform)
        token = tokens.load(platform)
        if token and token.access_token:
            catalog.link_account(category_id, platform,
                                 account_id=token.account_id,
                                 account_name=token.account_name)
        else:
            existing = catalog.account(category_id, platform)
            if existing and existing.connected:
                catalog.unlink_account(category_id, platform)
    return catalog.accounts(category_id, platforms)
