"""設定の読み込み（.env）。秘密情報はここにしか置かない。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"
DEFAULT_DB_PATH = ROOT / "autopost.db"
DEFAULT_EXPERIMENTS_DB = ROOT / "experiments.db"
DEFAULT_CACHE_DIR = ROOT / ".autopost_cache"
DEFAULT_TOKEN_DIR = ROOT / ".tokens"

# TikTok Login Kit (Desktop) のリダイレクトURI。開発者ポータルにも同じ値を登録する
TIKTOK_REDIRECT_URI_DEFAULT = "http://127.0.0.1:3455/callback/"
# Pinterest のリダイレクトURI（登録値と完全一致させる）
PINTEREST_REDIRECT_URI_DEFAULT = "http://localhost:8730/callback/"

# TikTokの投稿方式。direct_post が使えない場合の段階的フォールバック
TIKTOK_MODES = ("direct_post", "upload", "queue_only")

TIKTOK_PRIVACY_LEVELS = (
    "PUBLIC_TO_EVERYONE",
    "MUTUAL_FOLLOW_FRIENDS",
    "FOLLOWER_OF_CREATOR",
    "SELF_ONLY",
)
CATCHUP_POLICIES = ("single", "all", "skip")


def load_env(path: Path = ENV_PATH) -> None:
    """.env を環境変数へ読み込む（python-dotenv が無い場合は簡易パーサを使う）。"""
    if not path.is_file():
        return
    try:
        from dotenv import load_dotenv

        load_dotenv(path, override=False)
        return
    except ImportError:
        pass
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _get(key: str, default: str = "") -> str:
    return (os.environ.get(key) or default).strip()


def _get_bool(key: str, default: bool) -> bool:
    raw = _get(key)
    if not raw:
        return default
    return raw.lower() in ("1", "true", "yes", "on")


def _get_int(key: str, default: int) -> int:
    raw = _get(key)
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass
class Settings:
    """実行時設定。値の中身（特にsecret）はログへ出さないこと。"""

    # TikTok
    tiktok_client_key: str = ""
    tiktok_client_secret: str = ""
    tiktok_redirect_uri: str = TIKTOK_REDIRECT_URI_DEFAULT
    tiktok_privacy_level: str = "SELF_ONLY"
    tiktok_auto_add_music: bool = True

    tiktok_mode: str = "direct_post"      # direct_post | upload | queue_only

    # Pinterest
    pinterest_app_id: str = ""
    pinterest_app_secret: str = ""
    pinterest_redirect_uri: str = PINTEREST_REDIRECT_URI_DEFAULT
    pinterest_board_id: str = ""
    pinterest_sandbox: bool = False
    pinterest_default_link: str = ""

    # Meta / Instagram
    meta_app_id: str = ""
    meta_app_secret: str = ""
    meta_redirect_uri: str = ""
    instagram_account_id: str = ""
    meta_graph_version: str = "v21.0"
    meta_login_mode: str = "instagram"   # instagram | facebook

    # 画像ホスティング
    image_host: str = "none"
    r2_account_id: str = ""
    r2_access_key_id: str = ""
    r2_secret_access_key: str = ""
    r2_bucket: str = ""
    r2_public_base_url: str = ""
    local_host_dir: str = ""
    local_host_base_url: str = ""

    # 投稿動作
    timezone: str = "Asia/Tokyo"
    catchup_policy: str = "single"
    catchup_grace_minutes: int = 120
    move_after_publish: bool = False

    # パス
    db_path: Path = field(default_factory=lambda: DEFAULT_DB_PATH)
    experiments_db_path: Path = field(default_factory=lambda: DEFAULT_EXPERIMENTS_DB)
    cache_dir: Path = field(default_factory=lambda: DEFAULT_CACHE_DIR)
    token_dir: Path = field(default_factory=lambda: DEFAULT_TOKEN_DIR)

    # ------------------------------------------------------------------
    @classmethod
    def load(cls, env_path: Path = ENV_PATH) -> "Settings":
        load_env(env_path)
        privacy = _get("TIKTOK_PRIVACY_LEVEL", "SELF_ONLY").upper()
        if privacy not in TIKTOK_PRIVACY_LEVELS:
            privacy = "SELF_ONLY"
        policy = _get("CATCHUP_POLICY", "single").lower()
        if policy not in CATCHUP_POLICIES:
            policy = "single"
        return cls(
            tiktok_client_key=_get("TIKTOK_CLIENT_KEY"),
            tiktok_client_secret=_get("TIKTOK_CLIENT_SECRET"),
            tiktok_redirect_uri=_get("TIKTOK_REDIRECT_URI", TIKTOK_REDIRECT_URI_DEFAULT),
            tiktok_privacy_level=privacy,
            tiktok_auto_add_music=_get_bool("TIKTOK_AUTO_ADD_MUSIC", True),
            tiktok_mode=(
                _get("TIKTOK_MODE", "direct_post").lower()
                if _get("TIKTOK_MODE", "direct_post").lower() in TIKTOK_MODES
                else "direct_post"
            ),
            pinterest_app_id=_get("PINTEREST_APP_ID"),
            pinterest_app_secret=_get("PINTEREST_APP_SECRET"),
            pinterest_redirect_uri=_get("PINTEREST_REDIRECT_URI", PINTEREST_REDIRECT_URI_DEFAULT),
            pinterest_board_id=_get("PINTEREST_BOARD_ID"),
            pinterest_sandbox=_get_bool("PINTEREST_SANDBOX", False),
            pinterest_default_link=_get("PINTEREST_DEFAULT_LINK"),
            meta_app_id=_get("META_APP_ID"),
            meta_app_secret=_get("META_APP_SECRET"),
            meta_redirect_uri=_get("META_REDIRECT_URI"),
            instagram_account_id=_get("INSTAGRAM_ACCOUNT_ID"),
            meta_graph_version=_get("META_GRAPH_VERSION", "v21.0"),
            meta_login_mode=(
                "facebook" if _get("META_LOGIN_MODE", "instagram").lower() == "facebook"
                else "instagram"
            ),
            image_host=_get("IMAGE_HOST", "none").lower(),
            r2_account_id=_get("R2_ACCOUNT_ID"),
            r2_access_key_id=_get("R2_ACCESS_KEY_ID"),
            r2_secret_access_key=_get("R2_SECRET_ACCESS_KEY"),
            r2_bucket=_get("R2_BUCKET"),
            r2_public_base_url=_get("R2_PUBLIC_BASE_URL").rstrip("/"),
            local_host_dir=_get("LOCAL_HOST_DIR"),
            local_host_base_url=_get("LOCAL_HOST_BASE_URL").rstrip("/"),
            timezone=_get("TIMEZONE", "Asia/Tokyo"),
            catchup_policy=policy,
            catchup_grace_minutes=_get_int("CATCHUP_GRACE_MINUTES", 120),
            move_after_publish=_get_bool("MOVE_AFTER_PUBLISH", False),
        )

    # ------------------------------------------------------------------
    @property
    def tz(self) -> ZoneInfo:
        try:
            return ZoneInfo(self.timezone)
        except Exception:
            return ZoneInfo("Asia/Tokyo")

    def has_tiktok_credentials(self) -> bool:
        return bool(self.tiktok_client_key and self.tiktok_client_secret and self.tiktok_redirect_uri)

    def has_pinterest_credentials(self) -> bool:
        return bool(self.pinterest_app_id and self.pinterest_app_secret)

    def has_meta_credentials(self) -> bool:
        return bool(self.meta_app_id and self.meta_app_secret and self.instagram_account_id)

    def missing(self, platform: str) -> list[str]:
        """不足している .env 項目名を返す（値そのものは返さない）。"""
        if platform == "tiktok":
            keys = {
                "TIKTOK_CLIENT_KEY": self.tiktok_client_key,
                "TIKTOK_CLIENT_SECRET": self.tiktok_client_secret,
                "TIKTOK_REDIRECT_URI": self.tiktok_redirect_uri,
            }
        elif platform == "instagram":
            keys = {
                "META_APP_ID": self.meta_app_id,
                "META_APP_SECRET": self.meta_app_secret,
                "INSTAGRAM_ACCOUNT_ID": self.instagram_account_id,
            }
        elif platform == "pinterest":
            keys = {
                "PINTEREST_APP_ID": self.pinterest_app_id,
                "PINTEREST_APP_SECRET": self.pinterest_app_secret,
                "PINTEREST_REDIRECT_URI": self.pinterest_redirect_uri,
            }
        elif platform == "hosting":
            if self.image_host == "r2":
                keys = {
                    "R2_ACCOUNT_ID": self.r2_account_id,
                    "R2_ACCESS_KEY_ID": self.r2_access_key_id,
                    "R2_SECRET_ACCESS_KEY": self.r2_secret_access_key,
                    "R2_BUCKET": self.r2_bucket,
                    "R2_PUBLIC_BASE_URL": self.r2_public_base_url,
                }
            elif self.image_host == "local":
                keys = {
                    "LOCAL_HOST_DIR": self.local_host_dir,
                    "LOCAL_HOST_BASE_URL": self.local_host_base_url,
                }
            else:
                return ["IMAGE_HOST"]
        else:
            return []
        return [name for name, value in keys.items() if not value]
